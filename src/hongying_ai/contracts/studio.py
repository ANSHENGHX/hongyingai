from __future__ import annotations

from typing import Literal

from pydantic import Field

from hongying_ai.domain.models import AssetManifestEntry, ContractModel

PublishPlatform = Literal["douyin", "kuaishou", "wechat_channels"]
GenerationDirection = Literal[
    "merchant_promo",
    "avatar_product_pitch",
    "knowledge_stickman",
    "knowledge_pencil",
    "miniature_world",
    "orange_cat_daily",
    "anime_drama",
    "children_picture_book",
]


class StudioScriptRequest(ContractModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=200)
    activity_id: str = Field(min_length=1, max_length=128)
    activity_title: str = Field(min_length=1, max_length=200)
    activity_type: str = Field(default="营销活动", max_length=100)
    topic: str = Field(min_length=1, max_length=200)
    target_platform: PublishPlatform = "douyin"
    generation_direction: GenerationDirection = "merchant_promo"
    selling_points: tuple[str, ...] = ()
    tone: str = Field(default="真实、有记忆点、强转化", max_length=100)
    duration_seconds: int = Field(default=15, ge=15, le=90)
    use_ai: bool = True


class StudioScriptResult(ContractModel):
    title: str = Field(min_length=1, max_length=100)
    opening: str = Field(min_length=1, max_length=160)
    narration: str = Field(min_length=1, max_length=1200)
    cta: str = Field(min_length=1, max_length=100)
    hashtags: tuple[str, ...] = ()
    material_terms: tuple[str, ...] = ()
    storyboard: tuple[str, ...] = ()
    model_meta: dict = Field(default_factory=dict)


class StudioGenerationOptions(ContractModel):
    generation_direction: GenerationDirection = "merchant_promo"
    video_aspect: Literal["9:16", "16:9", "1:1"] = "9:16"
    duration_seconds: int | None = Field(default=None, ge=15, le=90)
    clip_duration_seconds: int = Field(default=4, ge=1, le=12)
    transition_mode: Literal["template", "cut", "fade", "crossfade", "slide", "zoom"] = "template"
    match_materials_to_script: bool = True
    render_count: int = Field(default=1, ge=1, le=3)
    tts_voice: str = Field(default="baidu_hot_female", max_length=64)


class StudioAutofillRequest(ContractModel):
    creation_goal: str = Field(min_length=1, max_length=500)
    merchant_id: str | None = Field(default=None, max_length=128)
    merchant_name: str | None = Field(default=None, max_length=200)
    generation_direction: GenerationDirection | None = None
    target_platforms: tuple[PublishPlatform, ...] = (
        "douyin",
        "kuaishou",
        "wechat_channels",
    )
    use_ai: bool = True


class StudioAutofillResult(ContractModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=200)
    activity_id: str = Field(min_length=1, max_length=128)
    activity_title: str = Field(min_length=1, max_length=200)
    activity_type: str = Field(default="营销活动", max_length=100)
    user_goal: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=200)
    target_platforms: tuple[PublishPlatform, ...] = ()
    template_id: str
    selling_points: tuple[str, ...] = ()
    script_title: str = Field(min_length=1, max_length=100)
    script_text: str = Field(min_length=1, max_length=1200)
    script_tags: tuple[str, ...] = ()
    material_terms: tuple[str, ...] = ()
    options: StudioGenerationOptions = Field(default_factory=StudioGenerationOptions)
    storyboard: tuple[str, ...] = ()
    model_meta: dict = Field(default_factory=dict)


class StudioGenerateRequest(ContractModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=200)
    activity_id: str = Field(min_length=1, max_length=128)
    activity_title: str = Field(min_length=1, max_length=200)
    activity_type: str = Field(default="营销活动", max_length=100)
    user_goal: str = Field(min_length=1, max_length=500)
    topic: str | None = Field(default=None, max_length=200)
    script: str | None = Field(default=None, max_length=2000)
    target_platforms: tuple[PublishPlatform, ...] = ()
    template_id: str
    assets: tuple[AssetManifestEntry, ...] = ()
    avatar_asset_id: str | None = None
    avatar_commercial_consent: bool = False
    logo_asset_id: str | None = None
    bgm_asset_id: str | None = None
    selling_points: tuple[str, ...] = ()
    forbidden_words: tuple[str, ...] = ()
    use_ai: bool = True
    material_terms: tuple[str, ...] = ()
    options: StudioGenerationOptions = Field(default_factory=StudioGenerationOptions)


class StudioRunResult(ContractModel):
    task_id: int
    run_id: str
    stage: str
    status_url: str


class StudioGenerateResult(StudioRunResult):
    runs: tuple[StudioRunResult, ...] = ()


class StudioAssetResult(ContractModel):
    asset: AssetManifestEntry
    file_name: str
    thumbnail_url: str | None = None
    analysis_object_key: str
    analysis: dict


class StudioPublishRequest(ContractModel):
    run_id: str = Field(min_length=1, max_length=128)
    platforms: tuple[PublishPlatform, ...] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    hashtags: tuple[str, ...] = ()


class StudioPublishPlatformResult(ContractModel):
    platform: PublishPlatform
    status: Literal["QUEUED", "ACCOUNT_BINDING_REQUIRED"]
    message: str


class StudioPublishResult(ContractModel):
    publication_id: str
    run_id: str
    publish_object_key: str
    platforms: tuple[StudioPublishPlatformResult, ...]
