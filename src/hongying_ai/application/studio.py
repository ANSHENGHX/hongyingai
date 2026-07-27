from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from hongying_ai.application.render import RenderService
from hongying_ai.application.templates import apply_template, get_template
from hongying_ai.config import Settings
from hongying_ai.contracts.events import EventEnvelope, RenderCommand
from hongying_ai.contracts.studio import StudioGenerateRequest
from hongying_ai.domain.errors import ErrorCode, PlatformError, TimelineInvalid
from hongying_ai.domain.models import (
    AssetManifestEntry,
    InputManifest,
    RenderRun,
    RunStage,
    TaskConstraints,
    TaskSnapshot,
)
from hongying_ai.domain.ports import MessageBus, ObjectStore, RunRepository
from hongying_ai.domain.timeline import validate_timeline


class StudioWorkflowService:
    def __init__(
        self,
        settings: Settings,
        planner: Any,
        render: RenderService,
        repository: RunRepository,
        store: ObjectStore,
        bus: MessageBus,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.render = render
        self.repository = repository
        self.store = store
        self.bus = bus
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        trace_id: str,
    ) -> RenderRun:
        task_id = int(datetime.now(UTC).timestamp() * 1000)
        run_id = f"run_{uuid4().hex}"
        run = RenderRun(
            runId=run_id,
            taskId=task_id,
            tenantId=tenant_id,
            runNo=1,
            stage=RunStage.WAITING,
            metadata={
                "merchantId": request.merchant_id,
                "merchantName": request.merchant_name,
                "activityId": request.activity_id,
                "activityTitle": request.activity_title,
                "templateId": request.template_id,
            },
        )
        await self.repository.upsert(run)
        task = asyncio.create_task(
            self._execute(request, tenant_id=tenant_id, trace_id=trace_id, run=run)
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run

    async def _execute(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        trace_id: str,
        run: RenderRun,
    ) -> None:
        try:
            template = get_template(request.template_id)
            await self.repository.upsert(
                run.model_copy(
                    update={
                        "stage": RunStage.PLANNING,
                        "progress": 0.02,
                        "sequence": run.sequence + 1,
                        "updated_at": datetime.now(UTC),
                    }
                )
            )
            manifest = InputManifest(tenantId=tenant_id, assets=request.assets)
            asset_ids = set(manifest.by_id())
            for role, asset_id in (
                ("logo", request.logo_asset_id),
                ("bgm", request.bgm_asset_id),
            ):
                if asset_id and asset_id not in asset_ids:
                    raise PlatformError(
                        ErrorCode.INVALID_COMMAND,
                        f"{role} 素材不在本次选择的素材清单中",
                    )
            if request.logo_asset_id and manifest.by_id()[request.logo_asset_id].media_type != "image":
                raise PlatformError(ErrorCode.INVALID_COMMAND, "Logo 必须选择图片素材")
            if request.bgm_asset_id and manifest.by_id()[request.bgm_asset_id].media_type != "audio":
                raise PlatformError(ErrorCode.INVALID_COMMAND, "BGM 必须选择音频素材")
            visual_assets = tuple(
                asset
                for asset in manifest.assets
                if asset.media_type in {"video", "image"}
                and asset.asset_id != request.logo_asset_id
            )
            if not visual_assets:
                raise PlatformError(
                    ErrorCode.INVALID_COMMAND,
                    "至少需要选择一个用于主画面的图片或视频素材",
                )
            planning_manifest = InputManifest(tenantId=tenant_id, assets=visual_assets)
            snapshot = TaskSnapshot(
                taskId=run.task_id,
                tenantId=tenant_id,
                templateVersion=template.id,
                materialVersions=tuple(asset.asset_id for asset in manifest.assets),
                constraints=TaskConstraints(
                    durationMs=template.duration_ms,
                    width=template.width,
                    height=template.height,
                    maxModelCalls=2 if request.use_ai else 0,
                    maxTokens=6000 if request.use_ai else 0,
                ),
            )
            brand = {
                "merchantId": request.merchant_id,
                "merchantName": request.merchant_name,
                "activityId": request.activity_id,
                "activityTitle": request.activity_title,
                "activityType": request.activity_type,
                "sellingPoints": list(request.selling_points),
                "forbiddenWords": list(request.forbidden_words),
                "cta": f"立即参与{request.activity_title}",
                "sourceIds": [asset.asset_id for asset in manifest.assets],
            }
            brief, storyboard, base_timeline, model_meta = await self.planner.generate(
                snapshot=snapshot,
                user_goal=request.user_goal,
                industry=request.activity_type,
                brand_knowledge=brand,
                assets=planning_manifest,
            )
            timeline = apply_template(
                base_timeline,
                storyboard,
                manifest,
                template,
                logo_asset_id=request.logo_asset_id,
                bgm_asset_id=request.bgm_asset_id,
            )
            issues = validate_timeline(timeline, manifest)
            if issues:
                raise TimelineInvalid("模板生成的 Timeline 校验失败", [item.to_dict() for item in issues])

            await self.repository.record_model_call(
                tenant_id,
                run.task_id,
                trace_id,
                model_meta,
            )
            plan_key = (
                f"{self.settings.environment_object_prefix}/{tenant_id}/task/{run.task_id}/"
                f"plan/{template.id}/timeline.json"
            )
            plan = {
                "schemaVersion": "1.0",
                "merchant": {
                    "id": request.merchant_id,
                    "name": request.merchant_name,
                },
                "activity": {
                    "id": request.activity_id,
                    "title": request.activity_title,
                    "type": request.activity_type,
                },
                "template": template.to_dict(),
                "creativeBrief": brief.model_dump(by_alias=True, mode="json"),
                "storyboard": storyboard.model_dump(by_alias=True, mode="json"),
                "timeline": timeline.model_dump(by_alias=True, mode="json"),
                "inputManifest": manifest.model_dump(by_alias=True, mode="json"),
            }
            await self.store.put_json(plan, plan_key)
            await self.bus.publish(
                "ai.plan.generated",
                EventEnvelope(
                    eventType="AI_PLAN_GENERATED",
                    tenantId=tenant_id,
                    traceId=trace_id,
                    taskId=run.task_id,
                    runId=run.run_id,
                    payload={"planObjectKey": plan_key},
                ).model_dump(by_alias=True, mode="json"),
                "hongying.ai.exchange",
            )
            await self.render.execute(
                RenderCommand(
                    commandId=f"cmd_{uuid4().hex}",
                    tenantId=tenant_id,
                    traceId=trace_id,
                    taskId=run.task_id,
                    runId=run.run_id,
                    runNo=1,
                    timeline=timeline,
                    inputManifest=manifest,
                    outputProfile=timeline.output,
                )
            )
        except Exception as exc:
            current = await self.repository.get(run.run_id, tenant_id)
            if current and current.stage not in {
                RunStage.COMPLETED,
                RunStage.FAILED,
                RunStage.CANCELLED,
                RunStage.TIMEOUT,
            }:
                platform = (
                    exc
                    if isinstance(exc, PlatformError)
                    else PlatformError(
                        ErrorCode.INTERNAL_ERROR,
                        f"一键生成失败: {type(exc).__name__}",
                    )
                )
                await self.repository.upsert(
                    current.model_copy(
                        update={
                            "stage": RunStage.FAILED,
                            "sequence": current.sequence + 1,
                            "error_code": platform.code.value,
                            "error_summary": platform.message[:1000],
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )


def asset_from_analysis(
    *,
    tenant_id: int,
    asset_id: str,
    object_key: str,
    sha256: str,
    size_bytes: int,
    analysis: dict[str, Any],
) -> AssetManifestEntry:
    profile = analysis["mediaProfile"]
    media_type = profile.get("mediaType", "video")
    labels = tuple(
        str(item["name"])
        for item in analysis.get("visual", {}).get("labels") or ()
        if isinstance(item, dict) and item.get("name")
    )
    quality_score = analysis.get("visual", {}).get("quality", {}).get("qualityScore")
    duration_ms = int(profile.get("durationMs") or 0)
    if media_type == "image":
        duration_ms = 90_000
    return AssetManifestEntry(
        assetId=asset_id,
        objectKey=object_key,
        sha256=sha256,
        durationMs=max(1, duration_ms),
        sizeBytes=size_bytes,
        licenseId=f"studio-upload-{tenant_id}",
        mediaType=media_type,
        hasAudio=bool(profile.get("hasAudio", False)),
        labels=labels,
        qualityScore=quality_score,
    )
