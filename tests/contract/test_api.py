from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from hongying_ai.api import create_app
from hongying_ai.config import Settings
from hongying_ai.container import Container
from hongying_ai.infrastructure.memory import MemoryCoordinationStore
from hongying_ai.infrastructure.repository import MemoryRunRepository


class Healthy:
    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class FakeStore(Healthy):
    pass


def app_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        app_work_dir=tmp_path,
        internal_service_allowlist="video-task-service",
    )
    healthy = Healthy()
    container = Container(
        settings=settings,
        store=FakeStore(),
        coordination=MemoryCoordinationStore(),
        repository=MemoryRunRepository(),
        runner=healthy,
        bus=healthy,
        model=healthy,  # type: ignore[arg-type]
        media=None,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        quality=None,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings, container))


def headers() -> dict[str, str]:
    return {
        "X-Service-Name": "video-task-service",
        "X-Tenant-Id": "10001",
        "X-Trace-Id": "trace-test",
    }


def test_liveness_does_not_require_internal_headers(tmp_path: Path) -> None:
    with app_client(tmp_path) as client:
        response = client.get("/internal/health/liveness")
    assert response.status_code == 200
    assert response.json()["status"] == "UP"


def test_timeline_validation_contract(tmp_path: Path, timeline, manifest) -> None:
    payload: dict[str, Any] = {
        "schemaVersion": "1.0",
        "taskId": 90001,
        "timeline": timeline.model_dump(by_alias=True, mode="json"),
        "inputManifest": manifest.model_dump(by_alias=True, mode="json"),
    }
    with app_client(tmp_path) as client:
        response = client.post("/internal/v1/timelines/validate", headers=headers(), json=payload)
    assert response.status_code == 200
    assert response.json()["code"] == "SUCCESS"
    assert response.json()["data"]["valid"] is True


def test_unknown_service_is_rejected(tmp_path: Path, timeline) -> None:
    payload = {
        "schemaVersion": "1.0",
        "taskId": 90001,
        "timeline": timeline.model_dump(by_alias=True, mode="json"),
    }
    denied = headers() | {"X-Service-Name": "unknown-service"}
    with app_client(tmp_path) as client:
        response = client.post("/internal/v1/timelines/validate", headers=denied, json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "AI_INVALID_COMMAND"

