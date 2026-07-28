from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from hongying_ai.application.planner import PlannerService
from hongying_ai.application.quality import QualityService
from hongying_ai.application.render import RenderService
from hongying_ai.application.studio import StudioWorkflowService
from hongying_ai.config import Settings
from hongying_ai.contracts.studio import (
    StudioAutofillRequest,
    StudioGenerateRequest,
    StudioGenerationOptions,
    StudioPublishRequest,
    StudioScriptRequest,
)
from hongying_ai.domain.models import AssetManifestEntry, RenderRun, RunStage
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
    create_source_duration(path, duration=2)


def create_source_duration(path: Path, *, duration: int) -> None:
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
            f"testsrc2=s=360x640:r=30:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=520:duration={duration}",
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


class FakeVideoGenerator:
    def __init__(self) -> None:
        self.prompts: tuple[str, ...] = ()
        self.reference_hashes: tuple[str | None, ...] = ()

    async def generate_videos(
        self,
        prompts: tuple[str, ...],
        output_dir: Path,
        *,
        ratio: str,
        duration_seconds: int,
        reference_images: tuple[Path | None, ...] = (),
        on_progress=None,
    ) -> list[Path]:
        self.prompts = prompts
        self.reference_hashes = tuple(
            hashlib.sha256(path.read_bytes()).hexdigest() if path else None for path in reference_images
        )
        paths = []
        for index, _prompt in enumerate(prompts, start=1):
            path = output_dir / f"fake-ai-video-{index:02d}.mp4"
            create_source_duration(path, duration=duration_seconds)
            paths.append(path)
            if on_progress:
                await on_progress(index, len(prompts))
        return paths


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
    generator = FakeVideoGenerator()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render,
        repository,
        store,
        bus,
        video_generator=generator,
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
        materialTerms=("菜品", "火锅"),
        options=StudioGenerationOptions(
            videoAspect="1:1",
            durationSeconds=20,
            clipDurationSeconds=3,
            transitionMode="cut",
            renderCount=1,
        ),
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
    assert abs(float(probe["format"]["duration"]) - 20) <= 0.25
    assert completed.metadata["billingTargetDurationMs"] == 20_000
    assert abs(completed.metadata["durationDeltaMs"]) <= 250
    plan = await store.get_json(
        f"prod/10001/task/{completed.task_id}/plan/food-promo-vertical-v1/timeline.json"
    )
    assert plan["generatedVideoAssetIds"]
    assert len(generator.prompts) >= 5
    video_track = next(track for track in plan["timeline"]["tracks"] if track["type"] == "video")
    source_ranges = [
        (clip["assetId"], clip["sourceInMs"], clip["sourceOutMs"])
        for clip in video_track["clips"]
    ]
    assert len(source_ranges) == len(set(source_ranges))
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    assert video["width"] == 1080
    assert video["height"] == 1080
    assert "ai.plan.generated" in bus.events
    assert "video.render.completed" in bus.events


@pytest.mark.asyncio
async def test_one_click_workflow_generates_default_visuals_without_user_assets(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = LocalStore(tmp_path / "objects")
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
        activityTitle="新品引流活动",
        activityType="新品上市",
        userGoal="给宏映火锅做一条没有实拍素材也能发布的新品短视频",
        topic="新品锅底上新",
        script="新品锅底上新，适合朋友聚餐。到店可以看看门店环境和招牌菜品。",
        templateId="campaign-flash-v1",
        useAi=False,
        sellingPoints=("新品锅底", "朋友聚餐", "到店体验"),
        options=StudioGenerationOptions(
            videoAspect="9:16",
            durationSeconds=20,
            clipDurationSeconds=4,
            transitionMode="cut",
            renderCount=1,
        ),
        assets=(),
    )

    waiting = await studio.start(request, tenant_id=10001, trace_id="trace-no-assets")
    await asyncio.gather(*tuple(studio.tasks))
    completed = await repository.get(waiting.run_id, 10001)

    assert completed is not None
    assert completed.stage == RunStage.COMPLETED, completed.error_summary
    assert completed.output_object_key
    assert store.path(completed.output_object_key).exists()
    plan = await store.get_json(f"prod/10001/task/{completed.task_id}/plan/campaign-flash-v1/timeline.json")
    assert len(plan["generatedImageAssetIds"]) >= 5
    assert plan["workflowEngine"] == "langgraph"
    assert plan["effectiveBgmAssetId"].startswith("asset_audio_bed_")
    probe = await runner.probe(store.path(completed.output_object_key))
    assert float(probe["format"]["duration"]) >= 20


@pytest.mark.asyncio
async def test_one_click_workflow_uses_ai_video_generator_without_user_assets(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = LocalStore(tmp_path / "objects")
    settings = Settings(
        _env_file=None,
        app_work_dir=work,
        environment_object_prefix="prod",
        render_timeout_seconds=120,
        ai_video_clip_duration_seconds=8,
        ai_video_min_clip_count=3,
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
    generator = FakeVideoGenerator()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render,
        repository,
        store,
        bus,
        video_generator=generator,
    )
    request = StudioGenerateRequest(
        merchantId="M1001",
        merchantName="宏映火锅",
        activityId="A1001",
        activityTitle="新品引流活动",
        activityType="新品上市",
        userGoal="给宏映火锅做一条 AI 生成素材的新品短视频",
        topic="新品锅底上新",
        script="新品锅底上新，适合朋友聚餐。到店可以看看门店环境和招牌菜品。",
        templateId="campaign-flash-v1",
        useAi=False,
        sellingPoints=("新品锅底", "朋友聚餐", "到店体验"),
        options=StudioGenerationOptions(
            videoAspect="9:16",
            durationSeconds=20,
            clipDurationSeconds=4,
            transitionMode="cut",
            renderCount=1,
        ),
        assets=(),
    )

    waiting = await studio.start(request, tenant_id=10001, trace_id="trace-ai-media")
    await asyncio.gather(*tuple(studio.tasks))
    completed = await repository.get(waiting.run_id, 10001)

    assert completed is not None
    assert completed.stage == RunStage.COMPLETED, completed.error_summary
    assert len(generator.prompts) == 5
    plan = await store.get_json(f"prod/10001/task/{completed.task_id}/plan/campaign-flash-v1/timeline.json")
    assert len(plan["generatedVideoAssetIds"]) == 5
    assert plan["mediaGenerationWarning"] is None
    assert plan["workflowEngine"] == "langgraph"
    assert all(asset_id.startswith("asset_ai_video_") for asset_id in plan["generatedVideoAssetIds"])


@pytest.mark.asyncio
async def test_avatar_product_pitch_requires_selected_portrait(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, app_work_dir=tmp_path / "work")
    repository = MemoryRunRepository()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render=None,  # type: ignore[arg-type]
        repository=repository,
        store=LocalStore(tmp_path / "objects"),
        bus=EventBus(),
    )
    request = StudioGenerateRequest(
        merchantId="M1001",
        merchantName="宏映火锅",
        activityId="A-avatar-missing",
        activityTitle="人物口播",
        userGoal="上传人物照片后介绍产品",
        templateId="food-promo-vertical-v1",
        useAi=False,
        options=StudioGenerationOptions(
            generationDirection="avatar_product_pitch",
            durationSeconds=15,
        ),
    )

    waiting = await studio.start(request, tenant_id=10001, trace_id="trace-avatar-missing")
    await asyncio.gather(*tuple(studio.tasks))
    failed = await repository.get(waiting.run_id, 10001)

    assert failed is not None
    assert failed.stage == RunStage.FAILED
    assert "必须上传并选择 1 张人物照片" in (failed.error_summary or "")


@pytest.mark.asyncio
async def test_avatar_product_pitch_uses_one_portrait_for_every_generated_clip(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = LocalStore(tmp_path / "objects")
    avatar_key = "prod/10001/material/avatar/v1/original.jpg"
    avatar_path = store.path(avatar_key)
    avatar_path.parent.mkdir(parents=True)
    Image.new("RGB", (720, 1080), (198, 146, 118)).save(avatar_path, "JPEG")
    avatar_sha = hashlib.sha256(avatar_path.read_bytes()).hexdigest()
    avatar = AssetManifestEntry(
        assetId="avatar-person",
        objectKey=avatar_key,
        sha256=avatar_sha,
        durationMs=90_000,
        sizeBytes=avatar_path.stat().st_size,
        licenseId="portrait-commercial-license",
        mediaType="image",
        hasAudio=False,
        labels=("人物", "正面照", "商用授权"),
        qualityScore=90,
    )
    settings = Settings(
        _env_file=None,
        app_work_dir=work,
        environment_object_prefix="prod",
        render_timeout_seconds=120,
        ai_video_clip_duration_seconds=5,
        ai_video_min_clip_count=3,
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
    generator = FakeVideoGenerator()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render,
        repository,
        store,
        bus,
        video_generator=generator,
    )
    request = StudioGenerateRequest(
        merchantId="M1001",
        merchantName="宏映火锅",
        activityId="A-avatar",
        activityTitle="人物口播新品介绍",
        activityType="新品上市",
        userGoal="让人物出镜介绍现切鲜牛肉双人套餐并引导到店",
        topic="现切鲜牛肉双人套餐",
        script="今天直接给你看宏映火锅现切鲜牛肉双人套餐。牛肉现切，锅底现炒，喜欢就到店体验。",
        templateId="food-promo-vertical-v1",
        avatarAssetId=avatar.asset_id,
        avatarCommercialConsent=True,
        useAi=False,
        sellingPoints=("牛肉现切", "锅底现炒", "双人套餐"),
        options=StudioGenerationOptions(
            generationDirection="avatar_product_pitch",
            videoAspect="9:16",
            durationSeconds=15,
            clipDurationSeconds=5,
            transitionMode="cut",
            renderCount=1,
        ),
        assets=(avatar,),
    )

    waiting = await studio.start(request, tenant_id=10001, trace_id="trace-avatar-pitch")
    await asyncio.gather(*tuple(studio.tasks))
    completed = await repository.get(waiting.run_id, 10001)

    assert completed is not None
    assert completed.stage == RunStage.COMPLETED, completed.error_summary
    assert len(generator.prompts) == 3
    assert all(value == avatar_sha for value in generator.reference_hashes)
    assert all("同一人物" in prompt and "嘴唇持续" in prompt for prompt in generator.prompts)
    assert len(set(generator.prompts)) == len(generator.prompts)
    plan = await store.get_json(
        f"prod/10001/task/{completed.task_id}/plan/food-promo-vertical-v1/timeline.json"
    )
    assert plan["avatarAgent"]["agent"] == "avatar-spokesperson-v1"
    assert plan["avatarAgent"]["avatarAssetId"] == avatar.asset_id
    assert "avatar_spokesperson_agent" in plan["workflowPath"]
    assert len(plan["generatedVideoAssetIds"]) == 3


@pytest.mark.asyncio
async def test_script_draft_falls_back_without_model(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, app_work_dir=tmp_path / "work")
    repository = MemoryRunRepository()
    bus = EventBus()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render=None,  # type: ignore[arg-type]
        repository=repository,
        store=LocalStore(tmp_path / "objects"),
        bus=bus,
    )
    draft = await studio.draft_script(
        StudioScriptRequest(
            merchantId="M1001",
            merchantName="宏映火锅",
            activityId="A1001",
            activityTitle="双人餐活动",
            activityType="餐饮促销",
            topic="招牌锅底探店",
            targetPlatform="douyin",
            sellingPoints=("现切鲜肉", "手工锅底"),
            useAi=False,
        ),
        tenant_id=10001,
        trace_id="trace-script",
    )

    assert draft.title.startswith("招牌锅底探店")
    assert "招牌锅底探店" in draft.narration
    assert "宏映火锅" in draft.narration
    assert "招牌锅底探店" in draft.material_terms
    assert draft.model_meta["fallback"] is True


@pytest.mark.asyncio
async def test_autofill_creates_editable_generation_fields(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, app_work_dir=tmp_path / "work")
    repository = MemoryRunRepository()
    bus = EventBus()
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render=None,  # type: ignore[arg-type]
        repository=repository,
        store=LocalStore(tmp_path / "objects"),
        bus=bus,
    )

    result = await studio.autofill(
        StudioAutofillRequest(
            creationGoal="给蜀香里火锅做一条抖音短视频，主推双人套餐，突出手工锅底和现切鲜肉",
            merchantId="M-REGISTERED",
            merchantName="注册火锅店",
            useAi=False,
        ),
        tenant_id=10001,
        trace_id="trace-autofill",
    )

    assert result.user_goal.startswith("给蜀香里火锅")
    assert result.merchant_id == "M-REGISTERED"
    assert result.merchant_name == "注册火锅店"
    assert result.options.duration_seconds == 15
    assert result.template_id
    assert result.script_text
    assert result.material_terms
    assert result.model_meta["fallback"] is True


@pytest.mark.asyncio
async def test_publish_completed_video_creates_publication_manifest(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        app_work_dir=tmp_path / "work",
        environment_object_prefix="prod",
    )
    store = LocalStore(tmp_path / "objects")
    repository = MemoryRunRepository()
    bus = EventBus()
    run = RenderRun(
        runId="run_publish",
        taskId=90001,
        tenantId=10001,
        runNo=1,
        stage=RunStage.COMPLETED,
        progress=1,
        outputObjectKey="prod/10001/task/90001/output/final.mp4",
    )
    await repository.upsert(run)
    studio = StudioWorkflowService(
        settings,
        PlannerService(NoModel(), repository),
        render=None,  # type: ignore[arg-type]
        repository=repository,
        store=store,
        bus=bus,
    )

    result = await studio.publish(
        StudioPublishRequest(
            runId="run_publish",
            platforms=("douyin", "kuaishou", "wechat_channels"),
            title="双人餐活动",
            description="欢迎到店体验",
            hashtags=("#火锅", "#同城探店"),
        ),
        tenant_id=10001,
        trace_id="trace-publish",
    )

    assert result.publish_object_key.endswith(".json")
    assert store.path(result.publish_object_key).exists()
    assert {item.platform for item in result.platforms} == {
        "douyin",
        "kuaishou",
        "wechat_channels",
    }
    assert "video.publish.requested" in bus.events
