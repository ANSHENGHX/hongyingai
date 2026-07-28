from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hongying_ai.application.compiler import compile_timeline
from hongying_ai.application.media import sha256_file
from hongying_ai.application.quality import QualityService
from hongying_ai.config import Settings
from hongying_ai.contracts.events import EventEnvelope, RenderCommand
from hongying_ai.domain.errors import ErrorCode, PlatformError, TimelineInvalid
from hongying_ai.domain.models import QualityReport, RenderRun, RunStage
from hongying_ai.domain.ports import (
    CoordinationStore,
    MediaRunner,
    MessageBus,
    ObjectStore,
    RunRepository,
)
from hongying_ai.domain.timeline import assert_safe_object_key, validate_timeline

MIN_OUTPUT_DURATION_MS = 15_000


class RenderService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ObjectStore,
        coordination: CoordinationStore,
        repository: RunRepository,
        runner: MediaRunner,
        quality: QualityService,
        bus: MessageBus | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.coordination = coordination
        self.repository = repository
        self.runner = runner
        self.quality = quality
        self.bus = bus

    async def execute(self, command: RenderCommand) -> tuple[RenderRun, QualityReport]:
        started_at = time.perf_counter()
        idempotency = f"{command.command_type}:{command.task_id}:{command.run_no}"
        if command.timeline.duration_ms < MIN_OUTPUT_DURATION_MS:
            raise PlatformError(
                ErrorCode.INVALID_COMMAND,
                "成片时长必须不少于 15 秒",
            )
        issues = validate_timeline(command.timeline, command.input_manifest)
        if issues:
            raise TimelineInvalid("Timeline 校验失败", [item.to_dict() for item in issues])
        if command.deadline and command.deadline <= datetime.now(UTC):
            raise PlatformError(ErrorCode.TIMEOUT, "命令已超过 deadline")
        input_bytes = sum(item.size_bytes for item in command.input_manifest.assets)
        if input_bytes > self.settings.max_work_bytes:
            raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "输入素材超过工作目录预算", True)
        free_bytes = shutil.disk_usage(self.settings.app_work_dir.resolve()).free
        if input_bytes * 2 > free_bytes:
            raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "Worker 可用磁盘不足", True)
        if not await self.coordination.claim_command(idempotency):
            existing = await self.repository.get(command.run_id, command.tenant_id)
            if existing and existing.stage == RunStage.COMPLETED:
                quality = QualityReport.model_validate(existing.metadata["qualityReport"])
                return existing, quality
            raise PlatformError(ErrorCode.INVALID_COMMAND, "重复的渲染命令正在执行")

        worker_id = f"{socket.gethostname()}:{self.settings.worker_kind}"
        if not await self.coordination.acquire_lease(
            command.run_id, worker_id, self.settings.lease_seconds
        ):
            await self.coordination.release_command(idempotency)
            raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "Run 已被其他 Worker 领取", True)

        existing_run = await self.repository.get(command.run_id, command.tenant_id)
        run = RenderRun(
            runId=command.run_id,
            taskId=command.task_id,
            tenantId=command.tenant_id,
            runNo=command.run_no,
            stage=RunStage.CREATED,
            workerId=worker_id,
            leaseUntil=datetime.now(UTC) + timedelta(seconds=self.settings.lease_seconds),
            sequence=existing_run.sequence if existing_run else 0,
            metadata=existing_run.metadata if existing_run else {},
            createdAt=existing_run.created_at if existing_run else datetime.now(UTC),
        )
        await self.repository.upsert(run)
        work_dir = (
            self.settings.app_work_dir.resolve()
            / str(command.tenant_id)
            / str(command.task_id)
            / command.run_id
        )
        input_dir = work_dir / "input" / "assets"
        plan_dir = work_dir / "plan"
        output_dir = work_dir / "output"
        for directory in (input_dir, plan_dir, output_dir):
            directory.mkdir(parents=True, exist_ok=True)

        heartbeat = asyncio.create_task(self._heartbeat(command.run_id, worker_id))
        try:
            run = await self._advance(run, RunStage.DOWNLOADING, 0.05, command)
            local_assets = await self._download_assets(command, input_dir)
            (input_dir.parent / "manifest.json").write_text(
                command.input_manifest.model_dump_json(by_alias=True, indent=2),
                encoding="utf-8",
            )
            (plan_dir / "timeline.json").write_text(
                command.timeline.model_dump_json(by_alias=True, indent=2),
                encoding="utf-8",
            )
            run = await self._advance(run, RunStage.COMPILING, 0.15, command)
            output_path = output_dir / "candidate.mp4"
            compiled = compile_timeline(
                command.timeline,
                command.input_manifest,
                local_assets,
                plan_dir,
                output_path,
            )
            (plan_dir / "compiled-filter.txt").write_text(compiled.filter_graph, encoding="utf-8")

            run = await self._advance(run, RunStage.RENDERING, 0.2, command)

            async def progress(out_time_microseconds: float) -> None:
                nonlocal run
                if await self.coordination.is_cancelled(command.run_id, command.tenant_id):
                    raise PlatformError(ErrorCode.CANCELLED, "渲染已被取消")
                render_ratio = min(1, out_time_microseconds / (command.timeline.duration_ms * 1000))
                overall = 0.2 + render_ratio * 0.55
                if overall - run.progress >= 0.01:
                    run = await self._advance(run, RunStage.RENDERING, overall, command)

            timeout = self.settings.render_timeout_seconds
            if command.deadline:
                timeout = min(timeout, max(1, int((command.deadline - datetime.now(UTC)).total_seconds())))
            await self.runner.render(list(compiled.args), timeout_seconds=timeout, on_progress=progress)

            run = await self._advance(run, RunStage.QUALITY, 0.8, command)
            report = await self.quality.inspect(
                output_path,
                command.output_profile,
                expected_duration_ms=command.timeline.duration_ms,
            )
            cover_path = output_dir / "cover.jpg"
            preview_path = output_dir / "preview.mp4"
            await self.runner.create_thumbnail(
                output_path,
                cover_path,
                min(1.0, command.timeline.duration_ms / 2000),
            )
            await self.runner.create_proxy(output_path, preview_path)
            quality_path = output_dir / "quality.json"
            quality_path.write_text(report.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
            if report.decision == "REJECT":
                raise PlatformError(
                    ErrorCode.QUALITY_REJECTED,
                    "候选视频未通过技术质量门禁",
                    details=[
                        item.model_dump(by_alias=True, mode="json")
                        for group in (report.technical, report.visual, report.audio)
                        for item in group
                        if not item.passed
                    ],
                )

            run = await self._advance(run, RunStage.UPLOADING, 0.9, command)
            prefix = (
                f"{self.settings.environment_object_prefix}/{command.tenant_id}/task/"
                f"{command.task_id}/run/{command.run_id}"
            )
            temp_key = f"{prefix}/.tmp-{command.command_id}-candidate.mp4"
            final_key = f"{prefix}/candidate.mp4"
            await self.store.upload(output_path, temp_key, "video/mp4")
            await self.store.stat(temp_key)
            await self.store.promote(temp_key, final_key)
            cover_key = f"{prefix}/cover.jpg"
            preview_key = f"{prefix}/preview.mp4"
            await self.store.upload(cover_path, cover_key, "image/jpeg")
            await self.store.upload(preview_path, preview_key, "video/mp4")
            quality_key = f"{prefix}/quality.json"
            await self.store.put_json(report.model_dump(by_alias=True, mode="json"), quality_key)
            manifest = {
                "schemaVersion": "1.0",
                "outputObjectKey": final_key,
                "coverObjectKey": cover_key,
                "previewObjectKey": preview_key,
                "qualityObjectKey": quality_key,
                "sha256": sha256_file(output_path),
                "sizeBytes": output_path.stat().st_size,
                "ffmpegPresetVersion": self.settings.ffmpeg_preset_version,
                "timelineDigest": sha256_file(plan_dir / "timeline.json"),
            }
            manifest_key = f"{prefix}/media-manifest.json"
            await self.store.put_json(manifest, manifest_key)
            await self.repository.record_cost(
                command.tenant_id,
                command.task_id,
                command.run_id,
                {
                    "cpuSeconds": round(time.perf_counter() - started_at, 3),
                    "inputMediaSeconds": round(
                        sum(item.duration_ms for item in command.input_manifest.assets) / 1000,
                        3,
                    ),
                    "outputMediaSeconds": command.timeline.duration_ms / 1000,
                    "storageBytes": (
                        output_path.stat().st_size
                        + preview_path.stat().st_size
                        + cover_path.stat().st_size
                    ),
                    "transferBytes": sum(
                        item.size_bytes for item in command.input_manifest.assets
                    ),
                },
            )
            run = run.model_copy(
                update={
                    "stage": RunStage.COMPLETED,
                    "progress": 1.0,
                    "sequence": run.sequence + 1,
                    "output_object_key": final_key,
                    "metadata": {
                        **run.metadata,
                        "qualityReport": report.model_dump(by_alias=True, mode="json"),
                        "manifestObjectKey": manifest_key,
                        "coverObjectKey": cover_key,
                        "previewObjectKey": preview_key,
                        "qualityObjectKey": quality_key,
                    },
                    "updated_at": datetime.now(UTC),
                }
            )
            await self.repository.upsert(run)
            await self._publish(
                "video.render.completed",
                command,
                {
                    "outputObjectKey": final_key,
                    "coverObjectKey": cover_key,
                    "previewObjectKey": preview_key,
                    "qualityObjectKey": quality_key,
                    "manifestObjectKey": manifest_key,
                },
            )
            return run, report
        except Exception as raw_error:
            exc = (
                raw_error
                if isinstance(raw_error, PlatformError)
                else PlatformError(
                    ErrorCode.INTERNAL_ERROR,
                    f"渲染链路内部错误: {type(raw_error).__name__}",
                    retryable=True,
                )
            )
            if exc.retryable:
                await self.coordination.release_command(idempotency)
            stage = RunStage.CANCELLED if exc.code == ErrorCode.CANCELLED else (
                RunStage.TIMEOUT if exc.code == ErrorCode.TIMEOUT else RunStage.FAILED
            )
            failed = run.model_copy(
                update={
                    "stage": stage,
                    "sequence": run.sequence + 1,
                    "error_code": exc.code.value,
                    "error_summary": exc.message[:1000],
                    "updated_at": datetime.now(UTC),
                }
            )
            await self.repository.upsert(failed)
            diagnostic = {
                "runId": command.run_id,
                "stage": run.stage.value,
                "errorCode": exc.code.value,
                "message": exc.message,
                "details": exc.details,
            }
            (work_dir / "diagnostic.json").write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            event = "video.render.cancelled" if stage == RunStage.CANCELLED else "video.render.failed"
            await self._publish(event, command, diagnostic)
            if exc is raw_error:
                raise
            raise exc from raw_error
        finally:
            heartbeat.cancel()
            await self.coordination.release_lease(command.run_id, worker_id)
            if run.stage == RunStage.COMPLETED:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def _download_assets(
        self, command: RenderCommand, input_dir: Path
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for asset in command.input_manifest.assets:
            assert_safe_object_key(
                asset.object_key,
                command.tenant_id,
                self.settings.environment_object_prefix,
            )
            suffix = Path(asset.object_key).suffix[:12]
            destination = input_dir / f"{asset.asset_id}{suffix}"
            await self.store.download(asset.object_key, destination)
            if destination.stat().st_size != asset.size_bytes:
                raise PlatformError(ErrorCode.INVALID_COMMAND, f"素材大小校验失败: {asset.asset_id}")
            if sha256_file(destination).lower() != asset.sha256.lower():
                raise PlatformError(ErrorCode.INVALID_COMMAND, f"素材摘要校验失败: {asset.asset_id}")
            result[asset.asset_id] = destination
        return result

    async def _advance(
        self,
        run: RenderRun,
        stage: RunStage,
        progress: float,
        command: RenderCommand,
    ) -> RenderRun:
        if await self.coordination.is_cancelled(command.run_id, command.tenant_id):
            raise PlatformError(ErrorCode.CANCELLED, "Run 已收到取消请求")
        if command.deadline and command.deadline <= datetime.now(UTC):
            raise PlatformError(ErrorCode.TIMEOUT, "Run 已超过 deadline")
        updated = run.model_copy(
            update={
                "stage": stage,
                "progress": round(max(run.progress, progress), 2),
                "sequence": run.sequence + 1,
                "lease_until": datetime.now(UTC) + timedelta(seconds=self.settings.lease_seconds),
                "updated_at": datetime.now(UTC),
            }
        )
        await self.repository.upsert(updated)
        await self._publish(
            "video.render.progress",
            command,
            {
                "sequence": updated.sequence,
                "stage": stage.value,
                "stageProgress": round(progress, 2),
                "overallProgress": updated.progress,
            },
        )
        return updated

    async def _publish(
        self, event_type: str, command: RenderCommand, payload: dict[str, Any]
    ) -> None:
        if not self.bus:
            return
        event = EventEnvelope(
            eventType=event_type.upper().replace(".", "_"),
            tenantId=command.tenant_id,
            traceId=command.trace_id,
            taskId=command.task_id,
            runId=command.run_id,
            payload=payload,
        )
        await self.bus.publish(
            event_type,
            event.model_dump(by_alias=True, mode="json"),
            "hongying.video.exchange",
        )

    async def _heartbeat(self, run_id: str, worker_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            renewed = await self.coordination.renew_lease(
                run_id, worker_id, self.settings.lease_seconds
            )
            if not renewed:
                return
