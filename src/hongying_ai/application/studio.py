from __future__ import annotations

import asyncio
import mimetypes
import re
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from PIL import Image, ImageDraw, ImageFont

from hongying_ai.application.media import sha256_file
from hongying_ai.application.render import RenderService
from hongying_ai.application.studio_graph import StudioGraphState, build_studio_graph
from hongying_ai.application.templates import TEMPLATES, VideoTemplate, apply_template, get_template
from hongying_ai.config import Settings
from hongying_ai.contracts.events import EventEnvelope, RenderCommand
from hongying_ai.contracts.studio import (
    StudioAutofillRequest,
    StudioAutofillResult,
    StudioGenerateRequest,
    StudioGenerationOptions,
    StudioPublishPlatformResult,
    StudioPublishRequest,
    StudioPublishResult,
    StudioScriptRequest,
    StudioScriptResult,
)
from hongying_ai.domain.errors import ErrorCode, PlatformError, TimelineInvalid
from hongying_ai.domain.models import (
    AssetManifestEntry,
    InputManifest,
    RenderRun,
    RunStage,
    TaskConstraints,
    TaskSnapshot,
)
from hongying_ai.domain.ports import (
    ImageGenerationClient,
    MessageBus,
    ObjectStore,
    RunRepository,
    TextToSpeechClient,
    VideoGenerationClient,
)
from hongying_ai.domain.timeline import validate_timeline

MIN_STUDIO_VIDEO_DURATION_MS = 15_000
MAX_STUDIO_VIDEO_ASSETS = 9
MAX_STUDIO_IMAGE_ASSETS = 100
TTS_VOICE_PRESETS: dict[str, dict[str, Any]] = {
    "baidu_hot_female": {
        "label": "热门女声·度小雯",
        "description": "清亮、有亲和力，适合探店、种草和口播。",
        "voice": 4100,
        "speed": 6,
        "pitch": 6,
        "volume": 8,
    },
    "baidu_hot_male": {
        "label": "磁性男声",
        "description": "稳重、有信任感，适合品牌介绍和知识讲解。",
        "voice": 5003,
        "speed": 5,
        "pitch": 5,
        "volume": 8,
    },
    "baidu_energy_female": {
        "label": "元气女声·度小乔",
        "description": "节奏轻快，适合活动快闪和年轻化内容。",
        "voice": 4117,
        "speed": 7,
        "pitch": 6,
        "volume": 8,
    },
    "baidu_story_male": {
        "label": "故事男声",
        "description": "叙事感更强，适合短剧、故事和科普。",
        "voice": 4106,
        "speed": 5,
        "pitch": 5,
        "volume": 8,
    },
    "baidu_child": {
        "label": "童趣声音",
        "description": "更活泼，适合绘本、儿童故事和治愈内容。",
        "voice": 4105,
        "speed": 5,
        "pitch": 6,
        "volume": 8,
    },
}
GENERATION_DIRECTIONS: dict[str, dict[str, str]] = {
    "merchant_promo": {
        "name": "商户爆款推广",
        "recipe": "本地生活推广+真实场景+卖点证明+语音字幕",
        "prompt": (
            "真实本地生活商户推广片，只呈现与制作目标相关的产品、环境、"
            "服务或活动证据；镜头干净、高级、节奏快，有明确转化感。"
        ),
        "negative": "不要无关品牌、不要随机城市空镜、不要测试画面、不要杂乱拼贴。",
    },
    "avatar_product_pitch": {
        "name": "人物口播带货",
        "recipe": "单人物参考图+产品口播+自然表情动作+语音字幕",
        "prompt": (
            "严格保持参考照片中的同一人物身份、五官、发型和服装稳定；人物正面面对镜头，"
            "自然眨眼、轻微点头和手势，像真实主播介绍产品；画面干净专业，产品卖点清楚，"
            "适合抖音、快手、视频号商业口播。"
        ),
        "negative": (
            "禁止更换人物、禁止多人、禁止脸部变形、禁止身份漂移、禁止夸张肢体、"
            "禁止嘴部撕裂、禁止无关产品、禁止文字水印和测试画面。"
        ),
    },
    "knowledge_stickman": {
        "name": "火柴人知识讲解",
        "recipe": "知识+静态火柴人+语音字幕",
        "prompt": (
            "纯白或浅灰背景、黑白火柴人、简单图标和箭头、信息图式构图，"
            "画面像可商用知识讲解动画；只用轻微推拉或手绘线条动效。"
        ),
        "negative": (
            "禁止真人实拍、禁止彩色杂乱背景、禁止无关食物/门店、禁止3D怪异人物、禁止混乱拼贴、禁止测试彩条。"
        ),
    },
    "knowledge_pencil": {
        "name": "铅笔画知识讲解",
        "recipe": "知识+静态铅笔画+语音字幕",
        "prompt": (
            "白纸质感、灰黑铅笔线稿、干净手绘示意图、标题区留白，"
            "适合教程、原理、清单型内容；商业可用、画面统一。"
        ),
        "negative": "禁止照片质感、禁止彩色爆炸背景、禁止无关人物和物品、禁止乱线、禁止测试彩条。",
    },
    "miniature_world": {
        "name": "微缩景观小人国",
        "recipe": "故事+动态小人国+背景音乐",
        "prompt": "微缩景观、小人国、童话质感、浅景深、镜头运动细腻，围绕同一个故事世界连续展开。",
        "negative": "禁止随机拼贴、禁止恐怖怪异、禁止无关商业广告元素、禁止测试彩条。",
    },
    "orange_cat_daily": {
        "name": "橘猫的日常",
        "recipe": "故事+动态橘猫+背景音乐",
        "prompt": "同一只可爱橘猫、温暖生活化、轻喜剧镜头，动作连贯，适合治愈日常短剧。",
        "negative": "禁止多只猫随机切换、禁止恐怖怪异、禁止杂乱拼贴、禁止测试彩条。",
    },
    "anime_drama": {
        "name": "动漫短剧",
        "recipe": "故事+动态漫画+语音字幕",
        "prompt": "商业动漫短剧风格，人物一致、强情绪、强冲突、分镜清楚，适合剧情反转和人物对白。",
        "negative": "禁止风格频繁漂移、禁止脸部崩坏、禁止无关镜头、禁止测试彩条。",
    },
    "children_picture_book": {
        "name": "儿童绘本故事",
        "recipe": "故事+动态绘本+语音字幕",
        "prompt": "儿童绘本故事风格，柔和、可爱、安全、明亮，主角一致，画面像高质量绘本插画轻动画。",
        "negative": "禁止恐怖阴暗、禁止暴力、禁止写实成人营销、禁止测试彩条。",
    },
}
VIRAL_COPY_RULES = (
    "爆款文案硬性规则：标题必须像短视频平台真实热门标题，有具体利益点或好奇点；"
    "开头 3 秒必须直接命中用户制作目标里的痛点、反差、利益或知识钩子；"
    "正文必须按“钩子-证明-场景-行动”推进，每句都能配画面，禁止流水账；"
    "素材关键词必须能直接驱动画面生成或素材匹配；"
    "不要套话，不要空泛夸奖，不要生成联系方式、二维码、网址或无法核验的价格/疗效承诺。"
)


class StudioWorkflowService:
    def __init__(
        self,
        settings: Settings,
        planner: Any,
        render: RenderService,
        repository: RunRepository,
        store: ObjectStore,
        bus: MessageBus,
        tts: TextToSpeechClient | None = None,
        video_generator: VideoGenerationClient | None = None,
        image_generator: ImageGenerationClient | None = None,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.render = render
        self.repository = repository
        self.store = store
        self.bus = bus
        self.tts = tts
        self.video_generator = video_generator
        self.image_generator = image_generator
        self.tasks: set[asyncio.Task[None]] = set()
        self.workflow_graph = build_studio_graph(
            prepare=self._graph_prepare,
            route_materials=self._graph_route_materials,
            match_uploaded=self._graph_match_uploaded,
            prepare_avatar_pitch=self._graph_prepare_avatar_pitch,
            generate_images=self._graph_generate_images,
            use_static_scenes=self._graph_use_static_scenes,
            generate_dynamic_scenes=self._graph_generate_dynamic_scenes,
            generate_voiceover=self._graph_generate_voiceover,
            plan=self._graph_plan,
            build_timeline=self._graph_build_timeline,
            validate_timeline=self._graph_validate_timeline,
            persist_plan=self._graph_persist_plan,
            compose_and_quality=self._graph_compose_and_quality,
        )

    async def autofill(
        self,
        request: StudioAutofillRequest,
        *,
        tenant_id: int,
        trace_id: str,
    ) -> StudioAutofillResult:
        if not request.use_ai:
            return _fallback_autofill(request, tenant_id, {"fallback": True, "reason": "MODEL_DISABLED"})
        model = getattr(self.planner, "model", None)
        structured_output = getattr(model, "structured_output", None)
        if not structured_output:
            return _fallback_autofill(request, tenant_id, {"fallback": True, "reason": "MODEL_UNAVAILABLE"})

        system = (
            "你是宏映AI短视频工作台的爆款短视频策划。只输出严格 JSON，不要输出解释。"
            "用户的制作目标是最高优先级，所有活动主题、标题、文案、卖点、关键词、分镜都必须紧扣该目标。"
            "如果 selectedGenerationDirection 不为空，必须使用该生成方向；为空时再根据制作目标自动判断。"
            "商户名称和商户编号只能使用 merchantContext 中的注册信息；"
            "禁止从制作目标里猜测、改写或创造商户名称。"
            "如果 merchantContext 缺失，只能使用“当前注册商户”作为占位。"
            f"{VIRAL_COPY_RULES}"
            "内容用于抖音、快手、视频号，必须真实、合规、可执行。"
            "成片时长必须不少于 15 秒，默认优先竖屏 9:16。"
        )
        user_data = {
            "tenantId": tenant_id,
            "traceId": trace_id,
            "creationGoal": request.creation_goal,
            "merchantContext": {
                "merchantId": request.merchant_id or f"M{tenant_id}",
                "merchantName": request.merchant_name or "当前注册商户",
                "source": "registered-profile",
                "locked": True,
            },
            "selectedGenerationDirection": request.generation_direction,
            "generationDirections": GENERATION_DIRECTIONS,
            "targetPlatforms": list(request.target_platforms),
            "availableTemplates": [template.to_dict() for template in TEMPLATES],
        }
        try:
            value, model_meta = await structured_output(
                prompt_id="studio-autofill",
                prompt_version="1.1.0",
                system_prompt=system,
                user_data=user_data,
                json_schema=StudioAutofillResult.model_json_schema(by_alias=True),
                max_tokens=2400,
            )
            result = StudioAutofillResult.model_validate(value)
            options_update: dict[str, Any] = {}
            if result.options.duration_seconds and result.options.duration_seconds < 15:
                options_update["duration_seconds"] = 15
            if request.generation_direction:
                options_update["generation_direction"] = request.generation_direction
            options = result.options.model_copy(update=options_update) if options_update else result.options
            valid_template_ids = {template.id for template in TEMPLATES}
            template_id = (
                result.template_id
                if result.template_id in valid_template_ids
                else _infer_template_id(request.creation_goal, result.activity_type)
            )
            return result.model_copy(
                update={
                    "merchant_id": request.merchant_id or f"M{tenant_id}",
                    "merchant_name": request.merchant_name or "当前注册商户",
                    "user_goal": request.creation_goal,
                    "target_platforms": request.target_platforms,
                    "template_id": template_id,
                    "options": options,
                    "model_meta": model_meta,
                }
            )
        except Exception as exc:
            return _fallback_autofill(
                request,
                tenant_id,
                {"fallback": True, "reason": type(exc).__name__},
            )

    async def draft_script(
        self,
        request: StudioScriptRequest,
        *,
        tenant_id: int,
        trace_id: str,
    ) -> StudioScriptResult:
        if not request.use_ai:
            return _fallback_script(request, {"fallback": True, "reason": "MODEL_DISABLED"})
        model = getattr(self.planner, "model", None)
        structured_output = getattr(model, "structured_output", None)
        if not structured_output:
            return _fallback_script(request, {"fallback": True, "reason": "MODEL_UNAVAILABLE"})

        system = (
            "你是本地生活爆款短视频文案策划。只输出严格 JSON。"
            "必须先理解用户的视频主题和制作目标，再生成标题、开场、口播、CTA、话题和素材关键词。"
            f"{VIRAL_COPY_RULES}"
            "文案结构必须是：强钩子开场 → 目标相关卖点或知识点 → 真实场景/使用理由 → 明确行动。"
            "每一句都要服务于用户目标、generationDirection 和 sellingPoints，"
            "禁止泛泛而谈，禁止把商户名改成别的名字。"
            "文案用于抖音、快手、视频号发布，必须口语化、有传播感、真实合规、避免夸大承诺。"
            "不要生成链接、联系方式、二维码、价格承诺或无法核验的信息。"
        )
        user_data = {
            "tenantId": tenant_id,
            "traceId": trace_id,
            "merchant": {"id": request.merchant_id, "name": request.merchant_name},
            "activity": {
                "id": request.activity_id,
                "title": request.activity_title,
                "type": request.activity_type,
            },
            "topic": request.topic,
            "targetPlatform": request.target_platform,
            "generationDirection": {
                "id": request.generation_direction,
                **_generation_direction(request.generation_direction),
            },
            "sellingPoints": list(request.selling_points),
            "tone": request.tone,
            "durationSeconds": request.duration_seconds,
        }
        try:
            value, model_meta = await structured_output(
                prompt_id="studio-script",
                prompt_version="1.1.0",
                system_prompt=system,
                user_data=user_data,
                json_schema=StudioScriptResult.model_json_schema(by_alias=True),
                max_tokens=1800,
            )
            return StudioScriptResult.model_validate(value).model_copy(update={"model_meta": model_meta})
        except Exception as exc:
            return _fallback_script(
                request,
                {"fallback": True, "reason": type(exc).__name__},
            )

    async def start_many(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        trace_id: str,
    ) -> tuple[RenderRun, ...]:
        runs = []
        for index in range(request.options.render_count):
            run = await self.start(
                request,
                tenant_id=tenant_id,
                trace_id=f"{trace_id}_variant_{index + 1}",
                variant_no=index + 1,
            )
            runs.append(run)
        return tuple(runs)

    async def start(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        trace_id: str,
        variant_no: int = 1,
    ) -> RenderRun:
        task_id = int(datetime.now(UTC).timestamp() * 1000) * 10 + variant_no
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
                "generationDirection": request.options.generation_direction,
                "avatarAssetId": request.avatar_asset_id,
                "avatarCommercialConsent": request.avatar_commercial_consent,
                "ttsVoice": request.options.tts_voice,
                "topic": request.topic,
                "script": request.script,
                "targetPlatforms": list(request.target_platforms),
                "materialTerms": list(request.material_terms),
                "generationOptions": request.options.model_dump(by_alias=True, mode="json"),
                "variantNo": variant_no,
            },
        )
        await self.repository.upsert(run)
        task = asyncio.create_task(self._execute(request, tenant_id=tenant_id, trace_id=trace_id, run=run))
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
            await self.workflow_graph.ainvoke(
                {
                    "request": request,
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "run": run,
                },
                config={"configurable": {"thread_id": run.run_id}},
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
                        f"一键生成失败: {type(exc).__name__}: {str(exc)[:500]}",
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

    async def _graph_prepare(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        tenant_id = state["tenant_id"]
        run = state["run"]
        await self._workflow_progress(run, 0.02, "validate_input", "校验输入与模板")
        template = _template_with_options(get_template(request.template_id), request)
        asset_list = list(request.assets)
        _validate_material_limits(asset_list)
        manifest = InputManifest(tenantId=tenant_id, assets=tuple(asset_list))
        asset_ids = set(manifest.by_id())
        for role, asset_id in (
            ("人物照片", request.avatar_asset_id),
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
        if request.options.generation_direction == "avatar_product_pitch":
            if not request.avatar_asset_id:
                raise PlatformError(
                    ErrorCode.INVALID_COMMAND,
                    "人物口播带货必须上传并选择 1 张人物照片",
                )
            avatar = manifest.by_id()[request.avatar_asset_id]
            if avatar.media_type != "image":
                raise PlatformError(
                    ErrorCode.INVALID_COMMAND,
                    "人物口播参考素材必须是图片",
                )
            if not request.avatar_commercial_consent:
                raise PlatformError(
                    ErrorCode.INVALID_COMMAND,
                    "请确认已获得人物肖像与商用授权",
                )
        return {
            "template": template,
            "asset_list": asset_list,
            "manifest": manifest,
            "visual_assets": _visual_assets(manifest.assets, request.logo_asset_id),
            "generated_image_asset_ids": (),
            "generated_video_asset_ids": (),
            "media_generation_warning": None,
        }

    async def _graph_route_materials(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        route = "uploaded" if state["visual_assets"] else "generate"
        scene_route = (
            "static"
            if request.options.generation_direction in {"knowledge_stickman", "knowledge_pencil"}
            else "dynamic"
        )
        return {"material_route": route, "scene_route": scene_route}

    async def _graph_match_uploaded(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        await self._workflow_progress(
            state["run"],
            0.05,
            "match_uploaded_materials",
            "按文案、标签、质量和授权匹配用户素材",
        )
        return {}

    async def _graph_prepare_avatar_pitch(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        avatar = state["manifest"].by_id()[request.avatar_asset_id]
        await self._workflow_progress(
            state["run"],
            0.07,
            "avatar_spokesperson_agent",
            "人物口播智能体锁定人物形象并拆解产品讲解镜头",
        )
        return {
            "avatar_agent": {
                "agent": "avatar-spokesperson-v1",
                "avatarAssetId": avatar.asset_id,
                "identityConsistency": "strict",
                "commercialConsent": request.avatar_commercial_consent,
                "productGoal": request.user_goal,
                "scriptSource": "confirmed-script",
            }
        }

    async def _graph_generate_images(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        run = state["run"]
        await self._workflow_progress(
            run,
            0.05,
            "generate_scene_images",
            "生成统一风格的分镜图片",
        )
        generated = ()
        warning: str | None = None
        if self.image_generator:
            try:
                generated = await self._create_provider_image_assets(
                    request,
                    tenant_id=state["tenant_id"],
                    run=run,
                    template=state["template"],
                )
            except Exception as exc:
                warning = f"{type(exc).__name__}: AI 生图失败，已使用本地商业视觉降级"
        if not generated:
            generated = await self._create_generated_visual_assets(
                request,
                tenant_id=state["tenant_id"],
                run=run,
                template=state["template"],
            )
        asset_list = [*state["asset_list"], *generated]
        _validate_material_limits(asset_list)
        manifest = InputManifest(tenantId=state["tenant_id"], assets=tuple(asset_list))
        return {
            "asset_list": asset_list,
            "manifest": manifest,
            "visual_assets": tuple(generated),
            "generated_image_asset_ids": tuple(asset.asset_id for asset in generated),
            "media_generation_warning": warning,
        }

    async def _graph_use_static_scenes(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        await self._workflow_progress(
            state["run"],
            0.08,
            "use_static_scene_sequence",
            "按火柴人/铅笔画分镜组织静态知识画面",
        )
        return {}

    async def _graph_generate_dynamic_scenes(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        await self._workflow_progress(
            state["run"],
            0.08,
            "generate_dynamic_scene_videos",
            "以统一角色分镜执行图生视频",
        )
        if not self.video_generator:
            if self.settings.ai_video_require_motion:
                raise PlatformError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "当前生成方向要求真实动态视频，但未配置视频生成模型",
                )
            return {}
        try:
            generated = await self._create_ai_video_assets(
                state["request"],
                tenant_id=state["tenant_id"],
                run=state["run"],
                template=state["template"],
                reference_assets=state["visual_assets"],
            )
        except Exception as exc:
            if self.settings.ai_video_require_motion:
                raise PlatformError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "真实动态视频生成失败，已阻止静态分镜冒充动态成片",
                    retryable=True,
                ) from exc
            previous = state.get("media_generation_warning")
            warning = f"{type(exc).__name__}: AI 视频生成失败，已使用分镜图片动效降级"
            return {"media_generation_warning": f"{previous}; {warning}" if previous else warning}
        asset_list = [*state["asset_list"], *generated]
        _validate_material_limits(asset_list)
        return {
            "asset_list": asset_list,
            "manifest": InputManifest(
                tenantId=state["tenant_id"],
                assets=tuple(asset_list),
            ),
            "visual_assets": tuple(generated),
            "generated_video_asset_ids": tuple(asset.asset_id for asset in generated),
        }

    async def _graph_generate_voiceover(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        run = state["run"]
        template = state["template"]
        await self._workflow_progress(
            run,
            0.11,
            "generate_voiceover",
            "生成配音并准备背景声音",
        )
        asset_list = list(state["asset_list"])
        manifest = state["manifest"]
        narration_asset_id: str | None = None
        effective_bgm_asset_id = request.bgm_asset_id
        voiceover_warning: str | None = None
        narration_text = _voiceover_text(request)
        if narration_text:
            try:
                narration = await self._create_narration_asset(
                    narration_text,
                    tenant_id=state["tenant_id"],
                    task_id=run.task_id,
                    run_id=run.run_id,
                    tts_voice=request.options.tts_voice,
                )
                asset_list.append(narration)
                narration_asset_id = narration.asset_id
                manifest = InputManifest(
                    tenantId=state["tenant_id"],
                    assets=tuple(asset_list),
                )
            except Exception as exc:
                detail = str(exc).strip()
                voiceover_warning = (
                    f"{type(exc).__name__}: {detail[:300] or '配音生成失败'}；已继续生成无口播视频"
                )
        if not _manifest_has_sound(manifest, request.logo_asset_id):
            fallback_audio = await self._create_fallback_audio_asset(
                tenant_id=state["tenant_id"],
                task_id=run.task_id,
                run_id=run.run_id,
                duration_ms=template.duration_ms,
            )
            asset_list.append(fallback_audio)
            effective_bgm_asset_id = fallback_audio.asset_id
            manifest = InputManifest(
                tenantId=state["tenant_id"],
                assets=tuple(asset_list),
            )
        return {
            "asset_list": asset_list,
            "manifest": manifest,
            "narration_asset_id": narration_asset_id,
            "effective_bgm_asset_id": effective_bgm_asset_id,
            "voiceover_warning": voiceover_warning,
        }

    async def _graph_plan(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        run = state["run"]
        template = state["template"]
        manifest = state["manifest"]
        await self._workflow_progress(
            run,
            0.14,
            "planner_brief_storyboard",
            "生成 Brief、爆款文案分镜与素材查询词",
        )
        snapshot = TaskSnapshot(
            taskId=run.task_id,
            tenantId=state["tenant_id"],
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
            "topic": request.topic,
            "script": request.script,
            "targetPlatforms": list(request.target_platforms),
            "materialTerms": list(request.material_terms),
            "matchMaterialsToScript": request.options.match_materials_to_script,
            "generationDirection": {
                "id": request.options.generation_direction,
                **_generation_direction(request.options.generation_direction),
            },
            "ttsVoice": {
                "id": request.options.tts_voice,
                **_tts_voice_preset(request.options.tts_voice),
            },
            "sellingPoints": list(request.selling_points),
            "forbiddenWords": list(request.forbidden_words),
            "cta": f"立即参与{request.activity_title}",
            "sourceIds": [asset.asset_id for asset in manifest.assets],
        }
        brief, storyboard, base_timeline, model_meta = await self.planner.generate(
            snapshot=snapshot,
            user_goal=_compose_generation_goal(request),
            industry=request.activity_type,
            brand_knowledge=brand,
            assets=InputManifest(
                tenantId=state["tenant_id"],
                assets=state["visual_assets"],
            ),
        )
        return {
            "snapshot": snapshot,
            "brief": brief,
            "storyboard": storyboard,
            "base_timeline": base_timeline,
            "model_meta": model_meta,
        }

    async def _graph_build_timeline(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        template = state["template"]
        await self._workflow_progress(
            state["run"],
            0.16,
            "build_timeline",
            "应用模板并生成可执行 Timeline",
        )
        narration_asset_id = state.get("narration_asset_id")
        # 用户选择的时长是计费目标，也是 Timeline 的硬约束。配音过长时由
        # 模板在目标时长处截断，不能再反向把成片撑长。
        target_duration_ms = template.duration_ms
        effective_template = template
        storyboard = _apply_clip_duration_limit(
            state["storyboard"],
            state["manifest"],
            request.options.clip_duration_seconds * 1000,
            target_duration_ms,
            0 if effective_template.transition == "cut" else effective_template.transition_ms,
        )
        timeline = apply_template(
            state["base_timeline"],
            storyboard,
            state["manifest"],
            effective_template,
            logo_asset_id=request.logo_asset_id,
            bgm_asset_id=state.get("effective_bgm_asset_id"),
            narration_asset_id=narration_asset_id,
            narration_text=_voiceover_text(request) if narration_asset_id else None,
        )
        return {
            "storyboard": storyboard,
            "timeline": timeline,
            "template": effective_template,
        }

    async def _graph_validate_timeline(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        await self._workflow_progress(
            state["run"],
            0.17,
            "validate_timeline_schema",
            "执行 Timeline Schema 与素材引用校验",
        )
        issues = validate_timeline(state["timeline"], state["manifest"])
        if issues:
            raise TimelineInvalid(
                "模板生成的 Timeline 校验失败",
                [item.to_dict() for item in issues],
            )
        return {}

    async def _graph_persist_plan(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        request = state["request"]
        run = state["run"]
        template = state["template"]
        await self._workflow_progress(
            run,
            0.18,
            "persist_plan",
            "保存策划、分镜和 Timeline",
        )
        await self.repository.record_model_call(
            state["tenant_id"],
            run.task_id,
            state["trace_id"],
            state["model_meta"],
        )
        plan_key = (
            f"{self.settings.environment_object_prefix}/{state['tenant_id']}/task/"
            f"{run.task_id}/plan/{template.id}/timeline.json"
        )
        narration_asset_id = state.get("narration_asset_id")
        narration_asset = state["manifest"].by_id().get(narration_asset_id) if narration_asset_id else None
        plan = {
            "schemaVersion": "1.0",
            "workflowEngine": "langgraph",
            "workflowPath": [
                "validate_input",
                "route_materials",
                (
                    "match_uploaded_materials"
                    if state["material_route"] == "uploaded"
                    else "generate_scene_images"
                ),
                *(
                    ["avatar_spokesperson_agent"]
                    if request.options.generation_direction == "avatar_product_pitch"
                    else []
                ),
                (
                    "generate_dynamic_scene_videos"
                    if state["scene_route"] == "dynamic" and state.get("generated_video_asset_ids")
                    else "use_static_scene_sequence"
                ),
                "generate_voiceover",
                "planner_brief_storyboard",
                "build_timeline",
                "validate_timeline_schema",
                "composer_ffmpeg_quality",
            ],
            "merchant": {"id": request.merchant_id, "name": request.merchant_name},
            "avatarAgent": state.get("avatar_agent"),
            "activity": {
                "id": request.activity_id,
                "title": request.activity_title,
                "type": request.activity_type,
            },
            "template": template.to_dict(),
            "creativeBrief": state["brief"].model_dump(by_alias=True, mode="json"),
            "storyboard": state["storyboard"].model_dump(by_alias=True, mode="json"),
            "timeline": state["timeline"].model_dump(by_alias=True, mode="json"),
            "inputManifest": state["manifest"].model_dump(by_alias=True, mode="json"),
            "generatedImageAssetIds": list(state.get("generated_image_asset_ids", ())),
            "generatedVideoAssetIds": list(state.get("generated_video_asset_ids", ())),
            "mediaGenerationWarning": state.get("media_generation_warning"),
            "narrationAssetId": state.get("narration_asset_id"),
            "effectiveBgmAssetId": state.get("effective_bgm_asset_id"),
            "voiceoverWarning": state.get("voiceover_warning"),
            "ttsVoice": request.options.tts_voice,
            "subtitleSync": {
                "mode": ("narration-duration-weighted" if narration_asset else "storyboard-shot-timing"),
                "narrationDurationMs": narration_asset.duration_ms if narration_asset else None,
                "cueCount": len(state["timeline"].subtitles),
            },
        }
        await self.store.put_json(plan, plan_key)
        await self.bus.publish(
            "ai.plan.generated",
            EventEnvelope(
                eventType="AI_PLAN_GENERATED",
                tenantId=state["tenant_id"],
                traceId=state["trace_id"],
                taskId=run.task_id,
                runId=run.run_id,
                payload={"planObjectKey": plan_key, "workflowEngine": "langgraph"},
            ).model_dump(by_alias=True, mode="json"),
            "hongying.ai.exchange",
        )
        return {"plan_key": plan_key}

    async def _graph_compose_and_quality(
        self,
        state: StudioGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        run = state["run"]
        await self._workflow_progress(
            run,
            0.19,
            "composer_ffmpeg_quality",
            "执行 FFmpeg 合成与质量门禁",
        )
        await self.render.execute(
            RenderCommand(
                commandId=f"cmd_{uuid4().hex}",
                tenantId=state["tenant_id"],
                traceId=state["trace_id"],
                taskId=run.task_id,
                runId=run.run_id,
                runNo=1,
                timeline=state["timeline"],
                inputManifest=state["manifest"],
                outputProfile=state["timeline"].output,
            )
        )
        return {}

    async def _workflow_progress(
        self,
        run: RenderRun,
        progress: float,
        node: str,
        label: str,
    ) -> None:
        current = await self.repository.get(run.run_id, run.tenant_id) or run
        history = list(current.metadata.get("aiWorkflowHistory", []))
        if not history or history[-1].get("node") != node:
            history.append(
                {
                    "node": node,
                    "label": label,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
        await self.repository.upsert(
            current.model_copy(
                update={
                    "stage": RunStage.PLANNING,
                    "progress": round(max(current.progress, progress), 2),
                    "sequence": current.sequence + 1,
                    "metadata": {
                        **current.metadata,
                        "workflowEngine": "langgraph",
                        "aiWorkflowNode": node,
                        "aiWorkflowLabel": label,
                        "aiWorkflowHistory": history[-20:],
                    },
                    "updated_at": datetime.now(UTC),
                }
            )
        )

    async def _create_ai_video_assets(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        run: RenderRun,
        template: VideoTemplate,
        reference_assets: tuple[AssetManifestEntry, ...] = (),
    ) -> tuple[AssetManifestEntry, ...]:
        if not self.video_generator:
            raise PlatformError(ErrorCode.MODEL_UNAVAILABLE, "未配置 AI 视频生成客户端")
        self.settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(mkdtemp(prefix=f"studio-ai-video-{tenant_id}-", dir=self.settings.app_work_dir))
        try:
            clip_duration = max(4, min(12, self.settings.ai_video_clip_duration_seconds))
            clip_count = _ai_video_clip_count(
                self.settings,
                template,
                requested_clip_seconds=request.options.clip_duration_seconds,
            )
            prompts = _ai_scene_video_prompts(request, template, clip_count)
            image_references = tuple(asset for asset in reference_assets if asset.media_type == "image")
            if request.options.generation_direction == "avatar_product_pitch" and request.avatar_asset_id:
                image_references = tuple(
                    asset for asset in image_references if asset.asset_id == request.avatar_asset_id
                )
            reference_paths: list[Path | None] = []
            reference_dir = work_dir / "reference"
            reference_dir.mkdir(parents=True, exist_ok=True)
            for index in range(clip_count):
                if not image_references:
                    reference_paths.append(None)
                    continue
                asset = image_references[index % len(image_references)]
                suffix = Path(asset.object_key).suffix or ".jpg"
                destination = reference_dir / f"scene-{index + 1:02d}{suffix}"
                await self.store.download(asset.object_key, destination)
                reference_paths.append(destination)
            local_videos = await self.video_generator.generate_videos(
                prompts,
                work_dir,
                ratio=_aspect_ratio(template),
                duration_seconds=clip_duration,
                reference_images=tuple(reference_paths),
                on_progress=lambda completed, total: self._workflow_progress(
                    run,
                    0.08 + (0.02 * completed / max(1, total)),
                    "generate_dynamic_scene_videos",
                    f"动态镜头已生成 {completed}/{total}",
                ),
            )
            assets: list[AssetManifestEntry] = []
            for index, local in enumerate(local_videos, start=1):
                asset_id = f"asset_ai_video_{run.run_id[-10:]}_{index:02d}"
                raw = await self.render.runner.probe(local)
                duration_seconds = float(raw.get("format", {}).get("duration") or clip_duration)
                has_audio = any(stream.get("codec_type") == "audio" for stream in raw.get("streams", []))
                sha256 = sha256_file(local)
                object_key = (
                    f"{self.settings.environment_object_prefix}/{tenant_id}/material/"
                    f"{asset_id}/v1/ai-generated{local.suffix.lower() or '.mp4'}"
                )
                await self.store.upload(local, object_key, "video/mp4")
                assets.append(
                    AssetManifestEntry(
                        assetId=asset_id,
                        objectKey=object_key,
                        sha256=sha256,
                        durationMs=max(1000, round(duration_seconds * 1000)),
                        sizeBytes=local.stat().st_size,
                        licenseId=f"ai-media-provider-{tenant_id}",
                        mediaType="video",
                        hasAudio=has_audio,
                        labels=("AI生成", "动态视频", request.activity_type),
                        qualityScore=86,
                    )
                )
            if not assets:
                raise PlatformError(ErrorCode.MODEL_OUTPUT_INVALID, "AI 模型未返回可用视频素材")
            return tuple(assets)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _create_provider_image_assets(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        run: RenderRun,
        template: VideoTemplate,
    ) -> tuple[AssetManifestEntry, ...]:
        if not self.image_generator:
            raise PlatformError(ErrorCode.MODEL_UNAVAILABLE, "未配置 AI 图片生成客户端")
        self.settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(mkdtemp(prefix=f"studio-ai-image-{tenant_id}-", dir=self.settings.app_work_dir))
        try:
            scene_count = (
                max(
                    5,
                    _ai_video_clip_count(
                        self.settings,
                        template,
                        requested_clip_seconds=request.options.clip_duration_seconds,
                    ),
                )
                if request.options.generation_direction in {"knowledge_stickman", "knowledge_pencil"}
                else _ai_video_clip_count(
                    self.settings,
                    template,
                    requested_clip_seconds=request.options.clip_duration_seconds,
                )
            )
            prompts = _ai_scene_image_prompts(request, template, scene_count)
            local_images = await self.image_generator.generate_images(
                prompts,
                work_dir,
                width=template.width,
                height=template.height,
                on_progress=lambda completed, total: self._workflow_progress(
                    run,
                    0.05 + (0.02 * completed / max(1, total)),
                    "generate_storyboard_images",
                    f"分镜画面已生成 {completed}/{total}",
                ),
            )
            assets: list[AssetManifestEntry] = []
            direction = _generation_direction(request.options.generation_direction)
            for index, local in enumerate(local_images, start=1):
                if not local.is_file() or local.stat().st_size == 0:
                    continue
                asset_id = f"asset_ai_image_{run.run_id[-10:]}_{index:02d}"
                suffix = local.suffix.lower() or ".jpg"
                object_key = (
                    f"{self.settings.environment_object_prefix}/{tenant_id}/material/"
                    f"{asset_id}/v1/ai-generated{suffix}"
                )
                await self.store.upload(
                    local,
                    object_key,
                    mimetypes.guess_type(local.name)[0] or "image/jpeg",
                )
                assets.append(
                    AssetManifestEntry(
                        assetId=asset_id,
                        objectKey=object_key,
                        sha256=sha256_file(local),
                        durationMs=90_000,
                        sizeBytes=local.stat().st_size,
                        licenseId=f"ai-media-provider-{tenant_id}",
                        mediaType="image",
                        hasAudio=False,
                        labels=(
                            "AI生成",
                            "分镜图片",
                            direction["name"],
                            request.activity_type,
                        ),
                        qualityScore=88,
                    )
                )
            if not assets:
                raise PlatformError(ErrorCode.MODEL_OUTPUT_INVALID, "AI 模型未返回可用图片素材")
            return tuple(assets)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _create_generated_visual_assets(
        self,
        request: StudioGenerateRequest,
        *,
        tenant_id: int,
        run: RenderRun,
        template: VideoTemplate,
    ) -> tuple[AssetManifestEntry, ...]:
        self.settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(mkdtemp(prefix=f"studio-ai-visual-{tenant_id}-", dir=self.settings.app_work_dir))
        try:
            title = request.topic or request.activity_title or request.user_goal
            direction_id = request.options.generation_direction
            direction = _generation_direction(direction_id)
            subject = _content_subject(request)
            selling_points = tuple(item for item in request.selling_points if item.strip())
            if not selling_points:
                selling_points = _infer_selling_points(request.user_goal, request.activity_type)
            scenes = _fallback_visual_scenes(
                request,
                subject=subject,
                title=title,
                selling_points=selling_points,
                direction=direction,
            )
            assets: list[AssetManifestEntry] = []
            for index, (kicker, headline) in enumerate(scenes, start=1):
                asset_id = f"asset_ai_visual_{run.run_id[-10:]}_{index:02d}"
                local = work_dir / f"{asset_id}.jpg"
                _render_ai_visual_card(
                    local,
                    width=template.width,
                    height=template.height,
                    accent=template.accent,
                    merchant_name=subject,
                    kicker=kicker,
                    headline=headline,
                    footer=request.activity_title,
                    direction_id=direction_id,
                )
                sha256 = sha256_file(local)
                object_key = (
                    f"{self.settings.environment_object_prefix}/{tenant_id}/material/"
                    f"{asset_id}/v1/generated.jpg"
                )
                await self.store.upload(local, object_key, "image/jpeg")
                assets.append(
                    AssetManifestEntry(
                        assetId=asset_id,
                        objectKey=object_key,
                        sha256=sha256,
                        durationMs=90_000,
                        sizeBytes=local.stat().st_size,
                        licenseId=f"ai-generated-{tenant_id}",
                        mediaType="image",
                        hasAudio=False,
                        labels=("AI生成", direction["name"], request.activity_type, kicker),
                        qualityScore=82,
                    )
                )
            return tuple(assets)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _create_narration_asset(
        self,
        text: str,
        *,
        tenant_id: int,
        task_id: int,
        run_id: str,
        tts_voice: str,
    ) -> AssetManifestEntry:
        if not self.tts:
            raise PlatformError(ErrorCode.MODEL_UNAVAILABLE, "未配置语音合成客户端")
        self.settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(mkdtemp(prefix=f"studio-tts-{tenant_id}-", dir=self.settings.app_work_dir))
        try:
            asset_id = f"asset_voice_{run_id[-12:]}"
            local = work_dir / "voiceover.mp3"
            voice = _tts_voice_preset(tts_voice)
            await self.tts.synthesize(
                text,
                local,
                cuid=f"hongying-{tenant_id}-{task_id}",
                voice=int(voice["voice"]),
                speed=int(voice["speed"]),
                pitch=int(voice["pitch"]),
                volume=int(voice["volume"]),
            )
            raw = await self.render.runner.probe(local)
            duration_seconds = float(raw.get("format", {}).get("duration") or 0)
            duration_ms = max(1000, round(duration_seconds * 1000))
            sha256 = sha256_file(local)
            object_key = (
                f"{self.settings.environment_object_prefix}/{tenant_id}/task/{task_id}/"
                f"run/{run_id}/voiceover.mp3"
            )
            await self.store.upload(local, object_key, "audio/mpeg")
            return AssetManifestEntry(
                assetId=asset_id,
                objectKey=object_key,
                sha256=sha256,
                durationMs=duration_ms,
                sizeBytes=local.stat().st_size,
                licenseId="baidu-tts-short",
                mediaType="audio",
                hasAudio=True,
                labels=("百度配音", "AI口播", str(voice["label"])),
                qualityScore=80,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _create_fallback_audio_asset(
        self,
        *,
        tenant_id: int,
        task_id: int,
        run_id: str,
        duration_ms: int,
    ) -> AssetManifestEntry:
        self.settings.app_work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(mkdtemp(prefix=f"studio-audio-bed-{tenant_id}-", dir=self.settings.app_work_dir))
        try:
            asset_id = f"asset_audio_bed_{run_id[-10:]}"
            local = work_dir / "ambient.m4a"
            duration_seconds = max(15, duration_ms / 1000)
            await self.render.runner.render(
                [
                    "-nostdin",
                    "-hide_banner",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=220:duration={duration_seconds:.3f}",
                    "-filter:a",
                    (
                        "volume=0.045,"
                        "afade=t=in:st=0:d=0.5,"
                        f"afade=t=out:st={max(0, duration_seconds - 0.8):.3f}:d=0.8"
                    ),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    str(local),
                ],
                timeout_seconds=30,
            )
            sha256 = sha256_file(local)
            object_key = (
                f"{self.settings.environment_object_prefix}/{tenant_id}/task/{task_id}/"
                f"run/{run_id}/ambient.m4a"
            )
            await self.store.upload(local, object_key, "audio/mp4")
            return AssetManifestEntry(
                assetId=asset_id,
                objectKey=object_key,
                sha256=sha256,
                durationMs=round(duration_seconds * 1000),
                sizeBytes=local.stat().st_size,
                licenseId="hongying-generated-audio-bed",
                mediaType="audio",
                hasAudio=True,
                labels=("兜底音频", "轻音乐床"),
                qualityScore=70,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def publish(
        self,
        request: StudioPublishRequest,
        *,
        tenant_id: int,
        trace_id: str,
    ) -> StudioPublishResult:
        run = await self.repository.get(request.run_id, tenant_id)
        if not run:
            raise PlatformError(ErrorCode.RUN_NOT_FOUND, "Run 不存在")
        if run.stage != RunStage.COMPLETED or not run.output_object_key:
            raise PlatformError(ErrorCode.INVALID_COMMAND, "作品完成后才能发布")

        publication_id = f"pub_{uuid4().hex}"
        publish_key = (
            f"{self.settings.environment_object_prefix}/{tenant_id}/task/{run.task_id}/"
            f"publish/{publication_id}.json"
        )
        platforms = tuple(
            StudioPublishPlatformResult(
                platform=platform,
                status="ACCOUNT_BINDING_REQUIRED",
                message="发布任务已创建；绑定平台开放接口账号后可自动提交",
            )
            for platform in request.platforms
        )
        manifest = {
            "schemaVersion": "1.0",
            "publicationId": publication_id,
            "tenantId": tenant_id,
            "traceId": trace_id,
            "runId": run.run_id,
            "taskId": run.task_id,
            "outputObjectKey": run.output_object_key,
            "title": request.title,
            "description": request.description,
            "hashtags": list(request.hashtags),
            "platforms": [item.model_dump(by_alias=True, mode="json") for item in platforms],
            "createdAt": datetime.now(UTC).isoformat(),
        }
        await self.store.put_json(manifest, publish_key)
        await self.bus.publish(
            "video.publish.requested",
            EventEnvelope(
                eventType="VIDEO_PUBLISH_REQUESTED",
                tenantId=tenant_id,
                traceId=trace_id,
                taskId=run.task_id,
                runId=run.run_id,
                payload={"publicationObjectKey": publish_key},
            ).model_dump(by_alias=True, mode="json"),
            "hongying.ai.exchange",
        )
        return StudioPublishResult(
            publicationId=publication_id,
            runId=run.run_id,
            publishObjectKey=publish_key,
            platforms=platforms,
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


def _compose_generation_goal(request: StudioGenerateRequest) -> str:
    parts = [request.user_goal.strip()]
    if request.topic:
        parts.append(f"视频主题：{request.topic.strip()}")
    if request.target_platforms:
        parts.append(f"发布平台：{'、'.join(request.target_platforms)}")
    if request.material_terms:
        parts.append(f"素材关键词：{'、'.join(request.material_terms)}")
    if request.script:
        parts.append(f"已确认视频文案：{request.script.strip()}")
    return "\n".join(part for part in parts if part)


def _template_with_options(
    template: VideoTemplate,
    request: StudioGenerateRequest,
) -> VideoTemplate:
    width, height = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }[request.options.video_aspect]
    transition = (
        template.transition
        if request.options.transition_mode == "template"
        else request.options.transition_mode
    )
    return replace(
        template,
        duration_ms=max(
            MIN_STUDIO_VIDEO_DURATION_MS,
            (
                request.options.duration_seconds * 1000
                if request.options.duration_seconds
                else template.duration_ms
            ),
        ),
        width=width,
        height=height,
        transition=transition,
    )


def _generation_direction(direction: str) -> dict[str, str]:
    return GENERATION_DIRECTIONS.get(direction, GENERATION_DIRECTIONS["merchant_promo"])


def _tts_voice_preset(voice_id: str) -> dict[str, Any]:
    return TTS_VOICE_PRESETS.get(voice_id, TTS_VOICE_PRESETS["baidu_hot_female"])


def _content_subject(request: StudioGenerateRequest | StudioScriptRequest) -> str:
    name = request.merchant_name.strip()
    if name and name != "当前注册商户":
        return name
    topic = request.topic.strip() if getattr(request, "topic", None) else ""
    return topic or request.activity_title.strip() or "本条视频主题"


def _default_voice_for_direction(direction: str) -> str:
    return {
        "avatar_product_pitch": "baidu_hot_female",
        "knowledge_stickman": "baidu_hot_male",
        "knowledge_pencil": "baidu_story_male",
        "miniature_world": "baidu_hot_female",
        "orange_cat_daily": "baidu_energy_female",
        "anime_drama": "baidu_story_male",
        "children_picture_book": "baidu_child",
    }.get(direction, "baidu_hot_female")


def _infer_generation_direction(goal: str, activity_type: str) -> str:
    if any(word in goal for word in ("人物口播", "真人口播", "数字人口播", "主播介绍")):
        return "avatar_product_pitch"
    if any(word in goal for word in ("火柴人", "简笔人")):
        return "knowledge_stickman"
    if any(word in goal for word in ("铅笔画", "手绘", "线稿")):
        return "knowledge_pencil"
    if any(word in goal for word in ("小人国", "微缩", "微观")):
        return "miniature_world"
    if any(word in goal for word in ("橘猫", "猫咪", "猫的日常")):
        return "orange_cat_daily"
    if any(word in goal for word in ("动漫", "漫画", "短剧", "剧情")):
        return "anime_drama"
    if any(word in goal for word in ("绘本", "儿童故事", "睡前故事")):
        return "children_picture_book"
    if any(word in goal for word in ("知识", "科普", "讲解", "教程", "避坑")):
        return "knowledge_stickman"
    return "merchant_promo" if activity_type in {"餐饮促销", "门店探店", "新品上市"} else "merchant_promo"


def _apply_clip_duration_limit(
    storyboard: Any,
    manifest: InputManifest,
    max_duration_ms: int,
    target_duration_ms: int,
    transition_ms: int = 0,
) -> Any:
    if max_duration_ms <= 0:
        return storyboard
    assets = manifest.by_id()
    source_shots = tuple(
        shot
        for shot in storyboard.shots
        if shot.selected_asset_id and shot.selected_asset_id in assets
    )
    if not source_shots:
        return storyboard
    unique_shots = []
    seen_asset_ids: set[str] = set()
    for shot in source_shots:
        if shot.selected_asset_id in seen_asset_ids:
            continue
        seen_asset_ids.add(shot.selected_asset_id)
        unique_shots.append(shot)

    # 长视频允许被切成多个连续、互不重叠的源区间；短生成片则每条通常
    # 只贡献一个镜头。apply_template 会按素材累计 sourceInMs。
    shots = []
    consumed_by_asset: dict[str, int] = {shot.selected_asset_id: 0 for shot in unique_shots}
    visible_duration_ms = 0
    pass_index = 0
    while visible_duration_ms < target_duration_ms:
        added = False
        for source in unique_shots:
            asset_id = source.selected_asset_id
            asset = assets[asset_id]
            consumed = consumed_by_asset[asset_id]
            available = asset.duration_ms - consumed
            duration = min(max_duration_ms, available)
            if duration <= transition_ms:
                continue
            contribution = duration if not shots else duration - transition_ms
            remaining_visible = target_duration_ms - visible_duration_ms
            if contribution > remaining_visible:
                duration -= contribution - remaining_visible
                contribution = remaining_visible
            if duration <= transition_ms:
                continue
            segment_index = 1 + sum(
                1 for shot in shots if shot.selected_asset_id == asset_id
            )
            shots.append(
                source.model_copy(
                    update={
                        "id": (
                            source.id
                            if segment_index == 1
                            else f"{source.id}_segment_{segment_index}"
                        ),
                        "duration_ms": duration,
                        "explain": (
                            f"{source.explain or '素材规则匹配'}；"
                            f"使用第 {segment_index} 个连续非重复源区间"
                        ),
                    }
                )
            )
            consumed_by_asset[asset_id] = consumed + duration
            visible_duration_ms += contribution
            added = True
            if visible_duration_ms >= target_duration_ms:
                break
        pass_index += 1
        if not added or pass_index > 64:
            break
    return storyboard.model_copy(update={"shots": tuple(shots)})


def _validate_material_limits(assets: list[AssetManifestEntry]) -> None:
    video_count = sum(1 for asset in assets if asset.media_type == "video")
    image_count = sum(1 for asset in assets if asset.media_type == "image")
    if video_count > MAX_STUDIO_VIDEO_ASSETS:
        raise PlatformError(
            ErrorCode.INVALID_COMMAND,
            f"视频素材最多选择 {MAX_STUDIO_VIDEO_ASSETS} 段，当前 {video_count} 段",
        )
    if image_count > MAX_STUDIO_IMAGE_ASSETS:
        raise PlatformError(
            ErrorCode.INVALID_COMMAND,
            f"图片素材最多选择 {MAX_STUDIO_IMAGE_ASSETS} 张，当前 {image_count} 张",
        )


def _visual_assets(
    assets: tuple[AssetManifestEntry, ...],
    logo_asset_id: str | None,
) -> tuple[AssetManifestEntry, ...]:
    return tuple(
        asset
        for asset in assets
        if asset.media_type in {"video", "image"} and asset.asset_id != logo_asset_id
    )


def _manifest_has_sound(manifest: InputManifest, logo_asset_id: str | None) -> bool:
    for asset in manifest.assets:
        if asset.asset_id == logo_asset_id:
            continue
        if asset.media_type == "audio":
            return True
        if asset.media_type == "video" and asset.has_audio:
            return True
    return False


def _voiceover_text(request: StudioGenerateRequest) -> str:
    text = (request.script or request.user_goal or request.topic).strip()
    if not text:
        return ""
    normalized = " ".join(text.replace("\r", "\n").split())
    return normalized[:480]


def _aspect_ratio(template: VideoTemplate) -> str:
    if template.width == template.height:
        return "1:1"
    return "9:16" if template.height > template.width else "16:9"


def _ai_video_clip_count(
    settings: Settings,
    template: VideoTemplate,
    *,
    requested_clip_seconds: int | None = None,
) -> int:
    generated_seconds = max(4, min(12, settings.ai_video_clip_duration_seconds))
    usable_seconds = min(generated_seconds, requested_clip_seconds or generated_seconds)
    usable_ms = max(1000, usable_seconds * 1000)
    overlap_ms = 0 if template.transition == "cut" else template.transition_ms
    first_contribution = usable_ms
    later_contribution = max(500, usable_ms - overlap_ms)
    required = 1
    if template.duration_ms > first_contribution:
        required += (
            template.duration_ms - first_contribution + later_contribution - 1
        ) // later_contribution
    count = max(settings.ai_video_min_clip_count, required)
    # 60 秒、3 秒镜头最多约需 23 个独立镜头。这里宁可增加独立生成
    # 次数，也不能通过循环一个片段来伪造目标时长。
    safety_limit = max(24, settings.ai_video_max_clip_count)
    return max(1, min(safety_limit, count))


def _ai_scene_video_prompts(
    request: StudioGenerateRequest,
    template: VideoTemplate,
    clip_count: int,
) -> tuple[str, ...]:
    topic = request.topic or request.activity_title or request.user_goal
    script = (request.script or request.user_goal or topic).strip()
    direction = _generation_direction(request.options.generation_direction)
    subject = _content_subject(request)
    points = tuple(item for item in request.selling_points if item.strip())
    if not points:
        points = _infer_selling_points(request.user_goal, request.activity_type)
    scenes = [
        (
            f"围绕制作目标“{request.user_goal}”生成爆款开场镜头，画面必须服务主题“{topic}”和主体“{subject}”。"
            "前3秒有视觉钩子，镜头轻微运动，画面干净高级。"
        ),
        (
            f"根据文案“{script[:120]}”生成卖点证明镜头，重点表现"
            f"“{points[0] if points else request.activity_type}”。"
            "适合抖音快手视频号信息流，必须与所选生成方向一致。"
        ),
        (
            "生成细节镜头，紧扣关键词"
            f"“{points[min(1, len(points) - 1)] if points else request.activity_title}”。"
            "真实门店宣传片风格，能让用户理解为什么值得看、值得收藏。"
        ),
        (
            f"生成场景代入镜头，围绕“{topic}”和“{request.activity_title}”，"
            "呈现真实门店环境、产品/服务氛围和用户到店想象。"
        ),
        (
            f"生成 CTA 收尾镜头，服务目标“{request.user_goal}”，"
            "画面预留字幕区域，适合叠加标题、行动号召和平台话题。"
        ),
        (f"生成补充镜头，关键词：{', '.join(points[:4])}。视觉风格与前面一致，有传播感、真实感、商业可用。"),
    ]
    if request.options.generation_direction == "avatar_product_pitch":
        pitch_lines = _split_script_for_scenes(script, clip_count)
        motion_plans = (
            "正面胸像，先稳定直视镜头，再自然眨眼一次，句末轻微点头；双手不入镜",
            "左前方约十度半侧身，视线回到镜头，右手只做一次小幅产品指引动作",
            "正面近景，肩颈放松，连续自然说话，句中轻微抬眉，手部保持静止",
            "右前方约十度半侧身，左手托住产品，句末轻微微笑，不做重复手势",
            "中近景稳定机位，身体轻微前倾强调重点，再回到自然站姿",
            "正面特写收尾，目光稳定、一次轻点头，嘴型结束后保持自然微笑",
        )
        scenes = [
            (
                f"人物口播第 {index + 1}/{clip_count} 个独立镜头。"
                f"本镜头唯一口播内容：{pitch_lines[index][:100]}。"
                f"本镜头动作方案：{motion_plans[index % len(motion_plans)]}。"
                "嘴唇持续按这段中文的音节自然开合，停顿位置与标点一致，不能只张合一次；"
                "动作幅度小且真实，不复用上一镜头的动作轨迹，不摇摆、不抽动、不循环；"
                "保持同一人物的五官、年龄、发型、服装、产品和背景连续一致，镜头稳定。"
            )
            for index in range(clip_count)
        ]
    else:
        camera_plans = (
            "独立广角建立镜头，缓慢向前推进",
            "独立中景横向小幅移动，突出主体与环境关系",
            "独立近景细节镜头，只做一次平滑拉近",
            "独立低机位展示镜头，主体动作自然完整",
            "独立俯拍细节镜头，构图简洁",
            "独立收尾镜头，稳定后轻微拉远并预留 CTA 区域",
        )
        scenes = [
            (
                f"{scenes[index % len(scenes)]}"
                f"这是第 {index + 1}/{clip_count} 个独立镜头，镜头设计："
                f"{camera_plans[index % len(camera_plans)]}。"
                "内容、机位、动作起止和背景细节必须与前后镜头有明确差异；"
                "禁止复制、倒放、循环或换色复用任何已有片段。"
            )
            for index in range(clip_count)
        ]
    prompt_prefix = (
        f"生成 {template.width}x{template.height} {direction['name']} 短视频素材。"
        f"类型配方：{direction['recipe']}。视觉要求：{direction['prompt']}。"
        f"反向约束：{direction.get('negative', '禁止无关画面、测试画面和杂乱拼贴。')}"
        "只生成可用于二次剪辑的视频画面；不要生成字幕文字、二维码、电话、网址、平台 Logo 或联系方式。"
        "画面必须紧扣用户制作目标和已确认文案，不要泛化成无关品牌宣传。"
        "所有镜头必须风格统一、主体连续、商业可用，不要随机素材混剪。"
    )
    return tuple(f"{prompt_prefix}{scene}" for scene in scenes[:clip_count])


def _split_script_for_scenes(script: str, scene_count: int) -> tuple[str, ...]:
    normalized = " ".join(script.split()).strip() or "请自然介绍本次产品和核心卖点。"
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])", normalized)
        if part.strip()
    ]
    if len(sentences) >= scene_count:
        groups = [[] for _ in range(scene_count)]
        for index, sentence in enumerate(sentences):
            groups[min(scene_count - 1, index * scene_count // len(sentences))].append(sentence)
        return tuple("".join(group)[:120] for group in groups)

    # 文案只有一两句话时按字符均匀拆分，避免每个生成请求收到完全相同的全文。
    size = max(1, (len(normalized) + scene_count - 1) // scene_count)
    chunks = [
        normalized[index : index + size].strip()
        for index in range(0, len(normalized), size)
        if normalized[index : index + size].strip()
    ]
    if not chunks:
        chunks = [normalized]
    return tuple(
        chunks[index] if index < len(chunks) else f"{normalized[:80]}（镜头重点 {index + 1}）"
        for index in range(scene_count)
    )


def _ai_scene_image_prompts(
    request: StudioGenerateRequest,
    template: VideoTemplate,
    scene_count: int,
) -> tuple[str, ...]:
    direction = _generation_direction(request.options.generation_direction)
    subject = _content_subject(request)
    title = request.topic or request.activity_title or request.user_goal
    points = tuple(item for item in request.selling_points if item.strip())
    if not points:
        points = _infer_selling_points(request.user_goal, request.activity_type)
    scenes = _fallback_visual_scenes(
        request,
        subject=subject,
        title=title,
        selling_points=points,
        direction=direction,
    )
    character_bible = (
        f"统一主体设定：{subject}；所有图片必须保持同一角色外观、服装、配色、"
        "画风、光线和世界观，不得随机更换人物或主体。"
    )
    common = (
        f"生成 {template.width}x{template.height} 竖屏短视频分镜图片。"
        f"内容目标：{request.user_goal}。视频主题：{title}。"
        f"类型：{direction['name']}；配方：{direction['recipe']}。"
        f"视觉要求：{direction['prompt']}。反向约束：{direction['negative']}。"
        f"{character_bible}"
        "画面需可直接商用、构图清楚、主体完整、留出字幕安全区；"
        "禁止文字、二维码、电话、网址、平台 Logo、水印、测试彩条和随机拼贴。"
    )
    composition_variants = (
        "广角建立场景",
        "中景展示主体关系",
        "近景突出关键细节",
        "侧面构图推进故事",
        "俯拍构图总结信息",
        "留白构图完成行动引导",
    )
    prompts = []
    for index in range(scene_count):
        kicker, headline = scenes[index % len(scenes)]
        prompts.append(
            f"{common} 当前分镜 {index + 1}/{scene_count}：{kicker}，{headline}。"
            f"本张采用{composition_variants[index % len(composition_variants)]}；"
            "必须与前后分镜形成连续叙事，但人物动作、机位和背景细节不能重复。"
        )
    return tuple(prompts)


def _fallback_visual_scenes(
    request: StudioGenerateRequest,
    *,
    subject: str,
    title: str,
    selling_points: tuple[str, ...],
    direction: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    first = selling_points[0] if selling_points else request.activity_type
    second = selling_points[min(1, len(selling_points) - 1)] if selling_points else request.activity_title
    if request.options.generation_direction == "knowledge_stickman":
        return (
            ("问题钩子", f"{title}到底怎么讲清？"),
            ("核心结论", first),
            ("三步拆解", second),
            ("避坑提醒", "别让观众看完还没记住"),
            ("行动引导", "收藏这条，照着讲就行"),
        )
    if request.options.generation_direction == "knowledge_pencil":
        return (
            ("纸上推演", f"{title}的关键逻辑"),
            ("第一重点", first),
            ("第二重点", second),
            ("清单总结", "把复杂内容讲成一张图"),
            ("行动引导", "按这套结构直接拍"),
        )
    if request.options.generation_direction in {
        "miniature_world",
        "orange_cat_daily",
        "anime_drama",
        "children_picture_book",
    }:
        return (
            ("故事开场", title),
            ("角色登场", first),
            ("冲突出现", second),
            ("转折推进", request.activity_title),
            ("温暖收束", "留下一个想继续看的结尾"),
        )
    return (
        ("开场吸引", title),
        ("核心卖点", first),
        ("细节强化", second),
        ("信任背书", subject),
        ("行动引导", f"现在就来{subject}看看"),
    )


def _render_ai_visual_card(
    path: Path,
    *,
    width: int,
    height: int,
    accent: str,
    merchant_name: str,
    kicker: str,
    headline: str,
    footer: str,
    direction_id: str = "merchant_promo",
) -> None:
    if direction_id == "knowledge_stickman":
        _render_stickman_visual_card(
            path,
            width=width,
            height=height,
            kicker=kicker,
            headline=headline,
            footer=footer,
        )
        return
    if direction_id == "knowledge_pencil":
        _render_pencil_visual_card(
            path,
            width=width,
            height=height,
            kicker=kicker,
            headline=headline,
            footer=footer,
        )
        return
    background = "#10141b"
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    accent_rgb = _hex_to_rgb(accent)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = tuple(
            round(background_channel * (0.86 + blend * 0.08) + accent_channel * (0.14 - blend * 0.08))
            for background_channel, accent_channel in zip(_hex_to_rgb(background), accent_rgb, strict=True)
        )
        draw.line([(0, y), (width, y)], fill=color)

    margin = round(width * 0.08)
    title_font = _load_card_font(round(height * 0.056), bold=True)
    headline_font = _load_card_font(round(height * 0.052), bold=True)
    body_font = _load_card_font(round(height * 0.026))
    small_font = _load_card_font(round(height * 0.022))

    draw.rounded_rectangle(
        (margin, round(height * 0.1), width - margin, round(height * 0.9)),
        radius=28,
        fill=(24, 29, 38),
        outline=accent_rgb,
        width=4,
    )
    draw.text((margin + 44, round(height * 0.16)), "HONGYING AI", fill=accent_rgb, font=small_font)
    draw.text((margin + 44, round(height * 0.23)), merchant_name[:24], fill="#FFFFFF", font=title_font)

    headline_lines = _wrap_cn(headline, max(8, round(width / 55)))
    y = round(height * 0.42)
    for line in headline_lines[:3]:
        draw.text((margin + 44, y), line, fill="#FFFFFF", font=headline_font)
        y += round(height * 0.07)

    draw.rounded_rectangle(
        (margin + 44, y + 20, width - margin - 44, y + round(height * 0.1)),
        radius=18,
        fill=accent_rgb,
    )
    draw.text(
        (margin + 72, y + 38),
        kicker[:20],
        fill="#16100c",
        font=body_font,
    )
    draw.text(
        (margin + 44, round(height * 0.8)),
        footer[:30] or "AI 自动生成默认视觉",
        fill="#D7DDE8",
        font=body_font,
    )
    image.save(path, "JPEG", quality=92, optimize=True)


def _render_stickman_visual_card(
    path: Path,
    *,
    width: int,
    height: int,
    kicker: str,
    headline: str,
    footer: str,
) -> None:
    image = Image.new("RGB", (width, height), "#F7F5EF")
    draw = ImageDraw.Draw(image)
    black = "#111111"
    muted = "#5D6470"
    accent = "#FF6B35"
    margin = round(width * 0.08)
    title_font = _load_card_font(round(height * 0.045), bold=True)
    body_font = _load_card_font(round(height * 0.032), bold=True)
    small_font = _load_card_font(round(height * 0.022))

    draw.rounded_rectangle(
        (margin, round(height * 0.08), width - margin, round(height * 0.92)),
        radius=34,
        fill="#FFFFFF",
        outline="#161616",
        width=4,
    )
    draw.text((margin + 42, round(height * 0.13)), "火柴人知识讲解", fill=black, font=small_font)
    draw.text((margin + 42, round(height * 0.19)), kicker[:18], fill=accent, font=title_font)

    center_x = width // 2
    head_y = round(height * 0.34)
    radius = round(width * 0.07)
    draw.ellipse(
        (center_x - radius, head_y - radius, center_x + radius, head_y + radius),
        outline=black,
        width=7,
    )
    body_top = head_y + radius
    body_bottom = round(height * 0.55)
    draw.line((center_x, body_top, center_x, body_bottom), fill=black, width=8)
    draw.line(
        (center_x, round(height * 0.43), center_x - round(width * 0.13), round(height * 0.49)),
        fill=black,
        width=7,
    )
    draw.line(
        (center_x, round(height * 0.43), center_x + round(width * 0.13), round(height * 0.49)),
        fill=black,
        width=7,
    )
    draw.line(
        (center_x, body_bottom, center_x - round(width * 0.11), round(height * 0.66)),
        fill=black,
        width=7,
    )
    draw.line(
        (center_x, body_bottom, center_x + round(width * 0.11), round(height * 0.66)),
        fill=black,
        width=7,
    )
    for offset, label in ((-0.28, "01"), (0.28, "02")):
        x = center_x + round(width * offset)
        y = round(height * 0.38)
        draw.rounded_rectangle((x - 70, y - 34, x + 70, y + 34), radius=24, outline=black, width=4)
        draw.text((x - 22, y - 21), label, fill=black, font=small_font)

    y = round(height * 0.7)
    for line in _wrap_cn(headline, max(9, round(width / 58)))[:3]:
        draw.text((margin + 44, y), line, fill=black, font=body_font)
        y += round(height * 0.05)
    draw.text((margin + 44, round(height * 0.86)), footer[:30], fill=muted, font=small_font)
    image.save(path, "JPEG", quality=94, optimize=True)


def _render_pencil_visual_card(
    path: Path,
    *,
    width: int,
    height: int,
    kicker: str,
    headline: str,
    footer: str,
) -> None:
    image = Image.new("RGB", (width, height), "#FBF8F1")
    draw = ImageDraw.Draw(image)
    graphite = "#363636"
    light = "#CFC7B8"
    margin = round(width * 0.08)
    title_font = _load_card_font(round(height * 0.044), bold=True)
    body_font = _load_card_font(round(height * 0.031), bold=True)
    small_font = _load_card_font(round(height * 0.021))

    for y in range(round(height * 0.12), round(height * 0.88), round(height * 0.055)):
        draw.line((margin, y, width - margin, y), fill=light, width=2)
    draw.rounded_rectangle(
        (margin, round(height * 0.08), width - margin, round(height * 0.92)),
        radius=28,
        outline=graphite,
        width=3,
    )
    draw.text((margin + 42, round(height * 0.13)), "铅笔画知识讲解", fill=graphite, font=small_font)
    draw.text((margin + 42, round(height * 0.2)), kicker[:18], fill=graphite, font=title_font)

    board = (margin + 58, round(height * 0.34), width - margin - 58, round(height * 0.58))
    draw.rounded_rectangle(board, radius=22, outline=graphite, width=4)
    left = board[0] + 44
    top = board[1] + 46
    for index in range(3):
        y = top + index * round(height * 0.055)
        draw.ellipse((left, y - 9, left + 18, y + 9), fill=graphite)
        draw.line((left + 38, y, board[2] - 44, y), fill=graphite, width=5)
    draw.line((board[2] - 72, board[1] + 28, board[2] - 34, board[1] + 66), fill="#FF6B35", width=7)
    draw.line((board[2] - 34, board[1] + 66, board[2] - 88, board[1] + 126), fill="#FF6B35", width=7)

    y = round(height * 0.66)
    for line in _wrap_cn(headline, max(9, round(width / 58)))[:3]:
        draw.text((margin + 44, y), line, fill=graphite, font=body_font)
        y += round(height * 0.05)
    draw.text((margin + 44, round(height * 0.86)), footer[:30], fill="#686153", font=small_font)
    image.save(path, "JPEG", quality=94, optimize=True)


def _load_card_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    noto_path = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
        if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    )
    candidates = (
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path(noto_path),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _wrap_cn(text: str, limit: int) -> list[str]:
    normalized = text.strip() or "AI 自动生成视频素材"
    return [normalized[index : index + limit] for index in range(0, len(normalized), limit)]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return (255, 107, 53)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _fallback_autofill(
    request: StudioAutofillRequest,
    tenant_id: int,
    model_meta: dict[str, Any],
) -> StudioAutofillResult:
    goal = request.creation_goal.strip()
    activity_type = _infer_activity_type(goal)
    template_id = _infer_template_id(goal, activity_type)
    generation_direction = request.generation_direction or _infer_generation_direction(goal, activity_type)
    merchant_id = request.merchant_id or f"M{tenant_id}"
    merchant_name = request.merchant_name or "当前注册商户"
    activity_title = _infer_activity_title(goal, activity_type)
    topic = _infer_topic(goal)
    points = _infer_selling_points(goal, activity_type)
    script = _fallback_script(
        StudioScriptRequest(
            merchantId=merchant_id,
            merchantName=merchant_name,
            activityId=f"ACT{datetime.now(UTC).strftime('%Y%m%d')}",
            activityTitle=activity_title,
            activityType=activity_type,
            topic=topic,
            targetPlatform=request.target_platforms[0] if request.target_platforms else "douyin",
            generationDirection=generation_direction,
            sellingPoints=points,
            durationSeconds=15,
            useAi=False,
        ),
        {"fallback": True, "reason": "AUTOFILL_FALLBACK"},
    )
    return StudioAutofillResult(
        merchantId=merchant_id,
        merchantName=merchant_name,
        activityId=f"ACT{datetime.now(UTC).strftime('%Y%m%d')}",
        activityTitle=activity_title,
        activityType=activity_type,
        userGoal=goal,
        topic=topic,
        targetPlatforms=request.target_platforms,
        templateId=template_id,
        sellingPoints=points,
        scriptTitle=script.title,
        scriptText=script.narration,
        scriptTags=script.hashtags,
        materialTerms=script.material_terms,
        options=StudioGenerationOptions(
            generationDirection=generation_direction,
            videoAspect="9:16",
            durationSeconds=15,
            clipDurationSeconds=4,
            transitionMode="template",
            matchMaterialsToScript=True,
            renderCount=1,
            ttsVoice=_default_voice_for_direction(generation_direction),
        ),
        storyboard=script.storyboard,
        modelMeta=model_meta,
    )


def _infer_activity_type(goal: str) -> str:
    if any(word in goal for word in ("知识", "科普", "讲解", "教程", "避坑")):
        return "知识讲解"
    if any(word in goal for word in ("新品", "上新", "新款", "新菜")):
        return "新品上市"
    if any(word in goal for word in ("探店", "门店", "环境", "到店")):
        return "门店探店"
    if any(word in goal for word in ("节日", "七夕", "中秋", "春节", "国庆")):
        return "节日活动"
    if any(word in goal for word in ("品牌", "形象", "宣传")):
        return "品牌宣传"
    if any(word in goal for word in ("火锅", "菜", "餐", "奶茶", "咖啡", "美食")):
        return "餐饮促销"
    return "营销活动"


def _infer_template_id(goal: str, activity_type: str) -> str:
    if activity_type == "知识讲解":
        return "food-promo-vertical-v1"
    if activity_type == "门店探店":
        return "store-tour-vertical-v1"
    if activity_type in {"节日活动", "餐饮促销"} or any(
        word in goal for word in ("促销", "活动", "优惠", "套餐")
    ):
        return "campaign-flash-v1"
    return "food-promo-vertical-v1"


def _infer_merchant_name(goal: str) -> str:
    markers = ("给", "为", "帮")
    endings = ("做", "生成", "制作", "拍", "出")
    for marker in markers:
        if marker not in goal:
            continue
        text = goal.split(marker, 1)[1]
        for ending in endings:
            if ending in text:
                candidate = text.split(ending, 1)[0].strip(" ：:，,。")
                if 2 <= len(candidate) <= 20:
                    return candidate
    if "火锅" in goal:
        return "宏映火锅"
    if "咖啡" in goal:
        return "宏映咖啡"
    if "奶茶" in goal:
        return "宏映茶饮"
    return "宏映商户"


def _infer_activity_title(goal: str, activity_type: str) -> str:
    if activity_type == "知识讲解":
        return "知识讲解视频"
    if "套餐" in goal:
        return "套餐推广活动"
    if "新品" in goal or "上新" in goal:
        return "新品上市活动"
    if "开业" in goal:
        return "开业引流活动"
    if activity_type == "门店探店":
        return "门店探店活动"
    return f"{activity_type}活动"


def _infer_selling_points(goal: str, activity_type: str) -> tuple[str, ...]:
    candidates = []
    for word in (
        "现切鲜肉",
        "手工锅底",
        "双人套餐",
        "门店环境",
        "新品上新",
        "限时优惠",
        "同城到店",
        "招牌菜品",
    ):
        if any(part in goal for part in word[:2].split()) or word in goal:
            candidates.append(word)
    if not candidates:
        defaults = {
            "门店探店": ("门店环境", "招牌产品", "真实体验"),
            "新品上市": ("新品亮相", "口味亮点", "到店尝鲜"),
            "品牌宣传": ("品牌形象", "门店服务", "产品特色"),
            "节日活动": ("节日氛围", "限时活动", "到店体验"),
        }
        candidates.extend(defaults.get(activity_type, ("招牌产品", "优惠活动", "到店体验")))
    return tuple(dict.fromkeys(candidates))[:5]


def _infer_topic(goal: str) -> str:
    normalized = goal.strip()
    for marker in ("主题是", "主题：", "主题:", "围绕", "关于"):
        if marker not in normalized:
            continue
        candidate = normalized.split(marker, 1)[1].strip(" ：:，,。")
        if 2 <= len(candidate) <= 120:
            return candidate
    return normalized[:120]


def _fallback_title(request: StudioScriptRequest, subject: str) -> str:
    if request.generation_direction == "avatar_product_pitch":
        return f"真人出镜讲清楚｜{request.topic}"
    if request.generation_direction == "knowledge_stickman":
        return f"{request.topic}：用火柴人讲清楚"
    if request.generation_direction == "knowledge_pencil":
        return f"{request.topic}：一张铅笔画看懂"
    if request.generation_direction in {
        "miniature_world",
        "orange_cat_daily",
        "anime_drama",
        "children_picture_book",
    }:
        return f"{request.topic}｜15秒故事短片"
    return f"{request.topic}｜{subject}这条视频把重点讲透"


def _fallback_opening(request: StudioScriptRequest, subject: str) -> str:
    if request.generation_direction == "avatar_product_pitch":
        return f"镜头前直接说重点：为什么最近大家都在关注{request.topic}？"
    if request.generation_direction in {"knowledge_stickman", "knowledge_pencil"}:
        return f"“{request.topic}”别再硬讲了，换成这套结构，观众一眼就懂。"
    if request.generation_direction in {
        "miniature_world",
        "orange_cat_daily",
        "anime_drama",
        "children_picture_book",
    }:
        return f"今天给{subject}做一个有记忆点的短故事，开头就要让人停下来。"
    return f"附近想找{subject}的人，{request.topic}这条先看完，重点真的很直接。"


def _fallback_script_lines(
    request: StudioScriptRequest,
    subject: str,
    direction: dict[str, str],
    points: tuple[str, ...],
    opening: str,
) -> list[str]:
    first = points[0]
    second = points[min(1, len(points) - 1)]
    if request.generation_direction == "avatar_product_pitch":
        return [
            opening,
            f"我是{request.merchant_name}的产品介绍人，今天只讲两个你最关心的点。",
            f"第一，{first}，这不是空口推荐，使用场景和细节都可以直接看见。",
            f"第二，{second}，适合真正关心{request.topic}的人。",
            f"想进一步了解{request.activity_title}，现在就收藏这条，到{subject}实际体验。",
        ]
    if request.generation_direction in {"knowledge_stickman", "knowledge_pencil"}:
        return [
            opening,
            f"第一步，先把问题说清楚：为什么大家会关心{request.topic}？",
            f"第二步，只讲一个核心结论：{first}，不要把信息塞满屏。",
            f"第三步，用{second}做例子，画面跟着逻辑走，观众才愿意看完。",
            "最后给一个能立刻照做的动作：收藏这条，下次直接按这个顺序拍。",
        ]
    if request.generation_direction in {
        "miniature_world",
        "orange_cat_daily",
        "anime_drama",
        "children_picture_book",
    }:
        return [
            opening,
            f"主角遇到的第一个问题，是{first}。",
            f"故事往前推，{second}变成观众最想继续看的转折。",
            f"把{request.topic}藏进画面细节里，让内容不是随机镜头，而是一个完整小故事。",
            "结尾留一个温暖动作，让观众愿意点赞、收藏，等下一集。",
        ]
    return [
        opening,
        f"先给你看最该记住的点：{first}，不是一句口号，而是画面里能看到的理由。",
        f"再看{second}，把真实场景和体验感拍出来，用户才知道为什么值得来。",
        f"如果你正在关注{request.activity_type}，这条可以先收藏，方便之后对比。",
        f"{request.activity_title}已经准备好，想了解就来{subject}看看。",
    ]


def _fallback_script(
    request: StudioScriptRequest,
    model_meta: dict[str, Any],
) -> StudioScriptResult:
    platform_name = {
        "douyin": "抖音",
        "kuaishou": "快手",
        "wechat_channels": "视频号",
    }.get(request.target_platform, "短视频平台")
    direction = _generation_direction(request.generation_direction)
    subject = _content_subject(request)
    points = tuple(item.strip() for item in request.selling_points if item.strip())
    if not points:
        points = (request.activity_title, request.activity_type, "到店体验")
    title = _fallback_title(request, subject)[:100]
    opening = _fallback_opening(request, subject)
    lines = _fallback_script_lines(request, subject, direction, points, opening)
    hashtags = tuple(
        dict.fromkeys(
            (
                f"#{request.activity_type}",
                f"#{request.topic}",
                f"#{subject}",
                f"#{platform_name}同城",
            )
        )
    )
    return StudioScriptResult(
        title=title[:100],
        opening=opening[:160],
        narration="\n".join(lines),
        cta=f"立即参与{request.activity_title}"[:100],
        hashtags=hashtags,
        materialTerms=tuple(dict.fromkeys((*points, request.topic, request.activity_type)))[:8],
        storyboard=tuple(
            (
                f"开场 0-3 秒：用{direction['name']}视觉钩住“{request.topic}”",
                f"中段：围绕{', '.join(points[:3])}做画面证明/知识拆解",
                "收尾：保留字幕安全区，突出收藏/到店/关注行动",
            )
        ),
        modelMeta=model_meta,
    )
