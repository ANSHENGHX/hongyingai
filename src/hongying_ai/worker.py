from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from pydantic import ValidationError

from hongying_ai.application.render import RenderService
from hongying_ai.config import Settings, get_settings
from hongying_ai.container import Container
from hongying_ai.contracts.events import (
    AssetAnalyzeCommand,
    CancelCommand,
    EventEnvelope,
    PlanGenerateCommand,
    QualityCommand,
    RenderCommand,
)
from hongying_ai.domain.errors import ErrorCode, PlatformError
from hongying_ai.domain.models import AnalysisProfile
from hongying_ai.domain.timeline import assert_safe_object_key

LOGGER = logging.getLogger("hongying.worker")


class WorkerApplication:
    def __init__(self, settings: Settings, container: Container) -> None:
        self.settings = settings
        self.container = container
        self.render = RenderService(
            settings=settings,
            store=container.store,
            coordination=container.coordination,
            repository=container.repository,
            runner=container.runner,
            quality=container.quality,
            bus=container.bus,
        )

    async def parser(self, raw: dict[str, Any]) -> None:
        command = AssetAnalyzeCommand.model_validate(raw)
        key = f"{command.command_type}:{command.material_version}:{command.sha256}"
        if not await self.container.coordination.claim_command(key):
            return
        try:
            result = await self.container.media.analyze(
                tenant_id=command.tenant_id,
                asset_id=command.asset_id,
                object_key=command.object_key,
                expected_sha256=command.sha256,
                expected_size=command.size_bytes,
                profile=AnalysisProfile(command.analysis_profile),
            )
            output_key = (
                f"{self.settings.environment_object_prefix}/{command.tenant_id}/material/"
                f"{command.asset_id}/{command.material_version}/analysis/manifest.json"
            )
            await self.container.store.put_json(result, output_key)
            await self._event(
                "ai.asset.analyzed",
                command.tenant_id,
                command.trace_id,
                command.task_id,
                None,
                {"assetId": command.asset_id, "analysisObjectKey": output_key},
                exchange="hongying.ai.exchange",
            )
        except Exception as exc:
            if isinstance(exc, PlatformError) and exc.retryable:
                await self.container.coordination.release_command(key)
            await self._failure("ai.asset.analysis.failed", command, exc)
            raise

    async def planner(self, raw: dict[str, Any]) -> None:
        command = PlanGenerateCommand.model_validate(raw)
        key = f"{command.command_type}:{command.task_snapshot.task_id}:1"
        if not await self.container.coordination.claim_command(key):
            return
        try:
            brief, storyboard, timeline, model_meta = await self.container.planner.generate(
                snapshot=command.task_snapshot,
                user_goal=command.user_goal,
                industry=command.industry,
                brand_knowledge=command.brand_knowledge,
                assets=command.candidate_assets,
            )
            await self.container.repository.record_model_call(
                command.tenant_id,
                command.task_snapshot.task_id,
                command.trace_id,
                model_meta,
            )
            prefix = (
                f"{self.settings.environment_object_prefix}/{command.tenant_id}/task/"
                f"{command.task_snapshot.task_id}/plan/v1"
            )
            plan = {
                "schemaVersion": "1.0",
                "creativeBrief": brief.model_dump(by_alias=True, mode="json"),
                "storyboard": storyboard.model_dump(by_alias=True, mode="json"),
                "timeline": timeline.model_dump(by_alias=True, mode="json"),
                "modelCall": model_meta,
            }
            key_name = f"{prefix}/timeline.json"
            await self.container.store.put_json(plan, key_name)
            await self._event(
                "ai.plan.generated",
                command.tenant_id,
                command.trace_id,
                command.task_snapshot.task_id,
                None,
                {"planObjectKey": key_name, **plan},
                exchange="hongying.ai.exchange",
            )
        except Exception as exc:
            if isinstance(exc, PlatformError) and exc.retryable:
                await self.container.coordination.release_command(key)
            await self._failure("ai.plan.generation.failed", command, exc)
            raise

    async def composer(self, raw: dict[str, Any]) -> None:
        command = RenderCommand.model_validate(raw)
        await self._with_retry(lambda: self.render.execute(command))

    async def quality(self, raw: dict[str, Any]) -> None:
        command = QualityCommand.model_validate(raw)
        key = f"{command.command_type}:{command.task_id}:{command.run_id}"
        if not await self.container.coordination.claim_command(key):
            return
        work_dir = Path(
            mkdtemp(prefix=f"quality-{command.run_id}-", dir=self.settings.app_work_dir.resolve())
        )
        try:
            assert_safe_object_key(
                command.output_object_key,
                command.tenant_id,
                self.settings.environment_object_prefix,
            )
            output = work_dir / "candidate.mp4"
            await self.container.store.download(command.output_object_key, output)
            report = await self.container.quality.inspect(
                output,
                command.expected_profile,
                policy_version=command.policy_version,
            )
            key_name = (
                f"{self.settings.environment_object_prefix}/{command.tenant_id}/task/"
                f"{command.task_id}/run/{command.run_id}/quality.json"
            )
            await self.container.store.put_json(
                report.model_dump(by_alias=True, mode="json"), key_name
            )
            await self._event(
                "video.quality.completed",
                command.tenant_id,
                command.trace_id,
                command.task_id,
                command.run_id,
                {
                    "decision": report.decision,
                    "qualityObjectKey": key_name,
                    "report": report.model_dump(by_alias=True, mode="json"),
                },
                exchange="hongying.video.exchange",
            )
        except Exception as exc:
            if isinstance(exc, PlatformError) and exc.retryable:
                await self.container.coordination.release_command(key)
            await self._failure("video.quality.failed", command, exc)
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def cancel(self, raw: dict[str, Any]) -> None:
        command = CancelCommand.model_validate(raw)
        created = await self.container.coordination.request_cancel(
            command.run_id,
            command.tenant_id,
            command.reason,
        )
        await self._event(
            "video.render.cancel.acknowledged",
            command.tenant_id,
            command.trace_id,
            command.task_id,
            command.run_id,
            {
                "cancelRequested": True,
                "alreadyRequested": not created,
                "requestedBy": command.requested_by,
            },
            exchange="hongying.video.exchange",
        )

    async def _with_retry(self, operation: Any) -> None:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_attempts):
            try:
                await operation()
                return
            except PlatformError as exc:
                last_error = exc
                if not exc.retryable or attempt + 1 >= self.settings.max_attempts:
                    raise
                await asyncio.sleep(min(30, 2**attempt))
        if last_error:
            raise last_error

    async def _failure(self, routing_key: str, command: Any, exc: Exception) -> None:
        if isinstance(exc, PlatformError):
            code = exc.code.value
            retryable = exc.retryable
            message = exc.message
        elif isinstance(exc, ValidationError):
            code = ErrorCode.INVALID_COMMAND.value
            retryable = False
            message = "命令 Schema 校验失败"
        else:
            code = ErrorCode.INTERNAL_ERROR.value
            retryable = False
            message = type(exc).__name__
        tenant_id = int(command.tenant_id)
        trace_id = str(command.trace_id)
        task_id = getattr(command, "task_id", None)
        if task_id is None and hasattr(command, "task_snapshot"):
            task_id = command.task_snapshot.task_id
        await self._event(
            routing_key,
            tenant_id,
            trace_id,
            task_id,
            getattr(command, "run_id", None),
            {"code": code, "message": message, "retryable": retryable},
            exchange="hongying.ai.exchange" if routing_key.startswith("ai.") else "hongying.video.exchange",
        )

    async def _event(
        self,
        routing_key: str,
        tenant_id: int,
        trace_id: str,
        task_id: int | None,
        run_id: str | None,
        payload: dict[str, Any],
        *,
        exchange: str,
    ) -> None:
        event = EventEnvelope(
            eventType=routing_key.upper().replace(".", "_"),
            tenantId=tenant_id,
            traceId=trace_id,
            taskId=task_id,
            runId=run_id,
            payload=payload,
        )
        await self.container.bus.publish(
            routing_key, event.model_dump(by_alias=True, mode="json"), exchange
        )


async def run_worker(settings: Settings | None = None, container: Container | None = None) -> None:
    settings = settings or get_settings()
    settings.app_work_dir.mkdir(parents=True, exist_ok=True)
    container = container or Container.build(settings)
    worker = WorkerApplication(settings, container)
    topology = {
        "parser": [
            ("ai.parser.v1", "ai.asset.analyze.requested", "hongying.ai.exchange", worker.parser)
        ],
        "planner": [
            ("ai.planner.v1", "ai.plan.generate.requested", "hongying.ai.exchange", worker.planner)
        ],
        "composer": [
            ("video.render.high.v1", "video.render.high", "hongying.video.exchange", worker.composer),
            (
                "video.render.normal.v1",
                "video.render.normal",
                "hongying.video.exchange",
                worker.composer,
            ),
            (
                "video.run.cancel.v1",
                "video.run.cancel.requested",
                "hongying.video.exchange",
                worker.cancel,
            ),
        ],
        "quality": [
            ("video.quality.v1", "video.quality.requested", "hongying.video.exchange", worker.quality)
        ],
    }

    async def consume(entry: tuple[str, str, str, Any]) -> None:
        queue, routing_key, exchange, handler = entry
        async def retrying_handler(raw: dict[str, Any]) -> None:
            if handler.__name__ == "composer":
                await handler(raw)
            else:
                await worker._with_retry(lambda: handler(raw))

        async for _ in container.bus.consume(queue, routing_key, exchange, retrying_handler):
            pass

    try:
        await asyncio.gather(*(consume(entry) for entry in topology[settings.worker_kind]))
    finally:
        await container.close()


def run() -> None:
    logging.basicConfig(
        level=get_settings().app_log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker())
