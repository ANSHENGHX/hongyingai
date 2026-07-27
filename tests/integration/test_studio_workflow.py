from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hongying_ai.application.planner import PlannerService
from hongying_ai.application.quality import QualityService
from hongying_ai.application.render import RenderService
from hongying_ai.application.studio import StudioWorkflowService
from hongying_ai.config import Settings
from hongying_ai.contracts.studio import StudioGenerateRequest
from hongying_ai.domain.models import AssetManifestEntry, RunStage
from hongying_ai.infrastructure.ffmpeg import FfmpegRunner
from hongying_ai.infrastructure.memory import MemoryCoordinationStore
from hongying_ai.infrastructure.repository import MemoryRunRepository

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / key

    async def download(self, object_key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path(object_key), destination)

    async def upload(
        self, source: Path, object_key: str, content_type: str = "application/octet-stream"
    ) -> str:
        destination = self.path(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return hashlib.md5(destination.read_bytes()).hexdigest()  # noqa: S324

    async def get_json(self, object_key: str) -> dict[str, Any]:
        return json.loads(self.path(object_key).read_text(encoding="utf-8"))

    async def put_json(self, value: dict[str, Any], object_key: str) -> str:
        destination = self.path(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return "json-etag"

    async def promote(self, temporary_key: str, final_key: str) -> str:
        destination = self.path(final_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.path(temporary_key).replace(destination)
        return "promoted"

    async def stat(self, object_key: str) -> dict[str, Any]:
        return {"size": self.path(object_key).stat().st_size}

    async def list(self, prefix: str) -> list[dict[str, Any]]:
        return []

    async def presigned_get(self, object_key: str, expires_seconds: int = 3600) -> str:
        return self.path(object_key).as_uri()

    async def health(self) -> bool:
        return True


class NoModel:
    async def structured_output(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        raise AssertionError("规则模式不应调用模型")


class EventBus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, routing_key: str, body: dict[str, Any], exchange: str) -> None:
        self.events.append(routing_key)

    async def health(self) -> bool:
        return True


def create_source(path: Path) -> None:
    assert FFMPEG
    subprocess.run(  # noqa: S603
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=360x640:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=520:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


@pytest.mark.asyncio
async def test_one_click_template_workflow_produces_quality_checked_video(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = LocalStore(tmp_path / "objects")
    source_key = "prod/10001/material/food/v1/original.mp4"
    source = store.path(source_key)
    source.parent.mkdir(parents=True)
    create_source(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    settings = Settings(
        _env_file=None,
        app_work_dir=work,
        environment_object_prefix="prod",
        render_timeout_seconds=120,
    )
    runner = FfmpegRunner()
    repository = MemoryRunRepository()
    coordination = MemoryCoordinationStore()
    bus = EventBus()
    render = RenderService(
        settings=settings,
        store=store,
        coordination=coordination,
        repository=repository,
        runner=runner,
        quality=QualityService(runner),
        bus=bus,
    )
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render,
        repository,
        store,
        bus,
    )
    request = StudioGenerateRequest(
        merchantId="M1001",
        merchantName="宏映火锅",
        activityId="A1001",
        activityTitle="双人餐活动",
        activityType="餐饮促销",
        userGoal="展示招牌菜并邀请顾客到店",
        templateId="food-promo-vertical-v1",
        useAi=False,
        assets=(
            AssetManifestEntry(
                assetId="food",
                objectKey=source_key,
                sha256=digest,
                durationMs=2000,
                sizeBytes=source.stat().st_size,
                licenseId="license-food",
                hasAudio=True,
                labels=("菜品", "含原声"),
                qualityScore=90,
            ),
        ),
    )

    waiting = await studio.start(request, tenant_id=10001, trace_id="trace-studio")
    await asyncio.gather(*tuple(studio.tasks))
    completed = await repository.get(waiting.run_id, 10001)

    assert completed is not None
    assert completed.stage == RunStage.COMPLETED, completed.error_summary
    assert completed.output_object_key
    assert store.path(completed.output_object_key).exists()
    probe = await runner.probe(store.path(completed.output_object_key))
    assert float(probe["format"]["duration"]) >= 10
    assert "ai.plan.generated" in bus.events
    assert "video.render.completed" in bus.events
