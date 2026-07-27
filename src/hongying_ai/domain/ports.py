from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from .models import RenderRun


class ObjectStore(Protocol):
    async def download(self, object_key: str, destination: Path) -> None: ...

    async def upload(
        self, source: Path, object_key: str, content_type: str = "application/octet-stream"
    ) -> str: ...

    async def get_json(self, object_key: str) -> dict[str, Any]: ...

    async def put_json(self, value: dict[str, Any], object_key: str) -> str: ...

    async def promote(self, temporary_key: str, final_key: str) -> str: ...

    async def stat(self, object_key: str) -> dict[str, Any]: ...

    async def health(self) -> bool: ...


class RunRepository(Protocol):
    async def get(self, run_id: str, tenant_id: int | None = None) -> RenderRun | None: ...

    async def upsert(self, run: RenderRun) -> None: ...

    async def record_model_call(
        self,
        tenant_id: int,
        task_id: int,
        trace_id: str,
        value: dict[str, Any],
    ) -> None: ...

    async def record_cost(
        self,
        tenant_id: int,
        task_id: int,
        run_id: str,
        value: dict[str, Any],
    ) -> None: ...

    async def search_knowledge(
        self, tenant_id: int, query: str, limit: int = 8
    ) -> list[dict[str, Any]]: ...

    async def health(self) -> bool: ...


class CoordinationStore(Protocol):
    async def acquire_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool: ...

    async def renew_lease(self, run_id: str, worker_id: str, ttl_seconds: int) -> bool: ...

    async def release_lease(self, run_id: str, worker_id: str) -> None: ...

    async def request_cancel(self, run_id: str, tenant_id: int, reason: str) -> bool: ...

    async def is_cancelled(self, run_id: str, tenant_id: int) -> bool: ...

    async def claim_command(self, idempotency_key: str, ttl_seconds: int = 604800) -> bool: ...

    async def release_command(self, idempotency_key: str) -> None: ...

    async def health(self) -> bool: ...


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MessageBus(Protocol):
    async def publish(self, routing_key: str, body: dict[str, Any], exchange: str) -> None: ...

    def consume(
        self, queue: str, routing_key: str, exchange: str, handler: MessageHandler
    ) -> AsyncIterator[None]: ...

    async def health(self) -> bool: ...


class ModelClient(Protocol):
    async def structured_output(
        self,
        *,
        prompt_id: str,
        prompt_version: str,
        system_prompt: str,
        user_data: dict[str, Any],
        json_schema: dict[str, Any],
        max_tokens: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


class MediaRunner(Protocol):
    async def probe(self, path: Path) -> dict[str, Any]: ...

    async def scan_quality(self, path: Path) -> dict[str, Any]: ...

    async def create_thumbnail(self, source: Path, destination: Path, at_seconds: float) -> None: ...

    async def create_proxy(self, source: Path, destination: Path) -> None: ...

    async def render(
        self,
        args: list[str],
        *,
        timeout_seconds: int,
        on_progress: Callable[[float], Awaitable[None]] | None = None,
    ) -> None: ...
