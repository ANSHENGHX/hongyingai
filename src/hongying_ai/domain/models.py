from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class AnalysisProfile(StrEnum):
    FAST = "FAST"
    STANDARD = "STANDARD"
    DEEP = "DEEP"


class TrackType(StrEnum):
    VIDEO = "video"
    OVERLAY = "overlay"
    AUDIO = "audio"
    SUBTITLE = "subtitle"


class RunStage(StrEnum):
    CREATED = "CREATED"
    WAITING = "WAITING"
    PROBING = "PROBING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    DOWNLOADING = "DOWNLOADING"
    COMPILING = "COMPILING"
    RENDERING = "RENDERING"
    QUALITY = "QUALITY"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


TERMINAL_STAGES = {RunStage.COMPLETED, RunStage.FAILED, RunStage.CANCELLED, RunStage.TIMEOUT}


class Canvas(ContractModel):
    width: int = Field(ge=240, le=7680)
    height: int = Field(ge=240, le=7680)
    fps: int = Field(default=30, ge=1, le=120)
    background: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")


class Crop(ContractModel):
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)

    @model_validator(mode="after")
    def inside_frame(self) -> Crop:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("crop must remain inside normalized frame")
        return self


class Transform(ContractModel):
    crop: Crop | None = None
    rotation: Literal[0, 90, 180, 270] = 0
    scale_mode: Literal["fit", "fill", "stretch", "blur"] = "fill"
    speed: float = Field(default=1, ge=0.25, le=4)
    opacity: float = Field(default=1, ge=0, le=1)
    brightness: float = Field(default=0, ge=-1, le=1)
    contrast: float = Field(default=1, ge=0, le=3)
    saturation: float = Field(default=1, ge=0, le=3)
    freeze_at_ms: int | None = Field(default=None, ge=0)
    position: Literal[
        "center", "top_left", "top_right", "bottom_left", "bottom_right"
    ] = "center"
    overlay_scale: float = Field(default=0.2, gt=0, le=1)
    margin: int = Field(default=32, ge=0, le=500)


class Clip(ContractModel):
    id: str = Field(min_length=1, max_length=96)
    asset_id: str = Field(min_length=1, max_length=128)
    timeline_start_ms: int = Field(ge=0)
    source_in_ms: int = Field(default=0, ge=0)
    source_out_ms: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    transform: Transform = Field(default_factory=Transform)
    volume: float = Field(default=1, ge=0, le=4)
    fade_in_ms: int = Field(default=0, ge=0)
    fade_out_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def duration_matches_source(self) -> Clip:
        source_duration = self.source_out_ms - self.source_in_ms
        if source_duration <= 0:
            raise ValueError("sourceOutMs must be greater than sourceInMs")
        expected = round(source_duration / self.transform.speed)
        if abs(expected - self.duration_ms) > 2:
            raise ValueError("durationMs must equal source duration adjusted by speed")
        if self.fade_in_ms + self.fade_out_ms > self.duration_ms:
            raise ValueError("audio fades exceed clip duration")
        return self


class Track(ContractModel):
    id: str = Field(min_length=1, max_length=96)
    type: TrackType
    clips: tuple[Clip, ...] = ()
    muted: bool = False
    z_index: int = Field(default=0, ge=0, le=32)


class Transition(ContractModel):
    from_clip_id: str
    to_clip_id: str
    type: Literal["cut", "crossfade", "fade", "slide", "zoom"] = "cut"
    duration_ms: int = Field(default=0, ge=0, le=3000)


class SubtitleStyle(ContractModel):
    font: str = "Noto Sans CJK SC"
    font_size: int = Field(default=48, ge=16, le=160)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_width: int = Field(default=2, ge=0, le=12)
    position_y: float = Field(default=0.82, ge=0.05, le=0.95)
    max_lines: int = Field(default=2, ge=1, le=3)
    highlight_mode: Literal["none", "word"] = "none"


class SubtitleCue(ContractModel):
    id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=200)
    translation: str | None = Field(default=None, max_length=200)
    style: SubtitleStyle = Field(default_factory=SubtitleStyle)

    @model_validator(mode="after")
    def positive_interval(self) -> SubtitleCue:
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle endMs must be greater than startMs")
        return self


class OutputProfile(ContractModel):
    width: int = Field(default=1080, ge=240, le=7680)
    height: int = Field(default=1920, ge=240, le=7680)
    fps: int = Field(default=30, ge=1, le=120)
    video_codec: Literal["h264", "h265"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    video_bitrate_kbps: int = Field(default=6000, ge=300, le=100000)
    audio_bitrate_kbps: int = Field(default=192, ge=32, le=512)
    preview: bool = False


class Timeline(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    duration_ms: int = Field(gt=0, le=90_000)
    canvas: Canvas
    tracks: tuple[Track, ...]
    transitions: tuple[Transition, ...] = ()
    subtitles: tuple[SubtitleCue, ...] = ()
    output: OutputProfile = Field(default_factory=OutputProfile)


class AssetManifestEntry(ContractModel):
    asset_id: str
    object_key: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    duration_ms: int = Field(gt=0)
    size_bytes: int = Field(gt=0)
    etag: str | None = None
    license_id: str | None = None
    focus_x: float | None = Field(default=None, ge=0, le=1)
    focus_y: float | None = Field(default=None, ge=0, le=1)
    media_type: Literal["video", "image", "audio"] = "video"
    has_audio: bool = True
    labels: tuple[str, ...] = ()
    quality_score: float | None = Field(default=None, ge=0, le=100)


class InputManifest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    tenant_id: int = Field(gt=0)
    assets: tuple[AssetManifestEntry, ...]

    @model_validator(mode="after")
    def unique_assets(self) -> InputManifest:
        ids = [asset.asset_id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("assetId must be unique in InputManifest")
        return self

    def by_id(self) -> dict[str, AssetManifestEntry]:
        return {asset.asset_id: asset for asset in self.assets}


class TaskConstraints(ContractModel):
    duration_ms: int = Field(default=30_000, ge=15_000, le=90_000)
    width: int = Field(default=1080, ge=240, le=7680)
    height: int = Field(default=1920, ge=240, le=7680)
    language: str = Field(default="zh-CN", max_length=16)
    max_model_calls: int = Field(default=8, ge=0, le=20)
    max_tokens: int = Field(default=12_000, ge=0, le=100_000)
    max_cost_usd: float = Field(default=2, ge=0, le=100)
    deadline: datetime | None = None


class TaskSnapshot(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: int = Field(gt=0)
    tenant_id: int = Field(gt=0)
    template_version: str = Field(default="default-v1", max_length=64)
    material_versions: tuple[str, ...] = ()
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    billing_ref: str | None = Field(default=None, max_length=128)


class MediaProfile(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: str | None = None
    object_key: str
    sha256: str
    container: str
    video_codec: str | None = None
    audio_codec: str | None = None
    duration_ms: int
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pixel_format: str | None = None
    rotation: int = 0
    has_audio: bool = False
    audio_sample_rate: int | None = None
    size_bytes: int
    risk_flags: tuple[str, ...] = ()
    analyzer_version: str = "probe-v1"
    media_type: Literal["video", "image", "audio"] = "video"
    bitrate_kbps: int | None = None


class CreativeBrief(ContractModel):
    audience: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=500)
    tone: str = Field(default="自然、有吸引力", max_length=100)
    selling_points: tuple[str, ...] = ()
    cta: str = Field(default="立即了解", max_length=100)
    brand_rules: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


class StoryboardShot(ContractModel):
    id: str
    narration: str = Field(max_length=500)
    visual_intent: str = Field(max_length=500)
    duration_ms: int = Field(gt=0)
    asset_query: str = Field(max_length=300)
    selected_asset_id: str | None = None
    match_score: float | None = Field(default=None, ge=0, le=1)
    explain: str | None = Field(default=None, max_length=500)


class Storyboard(ContractModel):
    title: str = Field(max_length=100)
    cta: str = Field(max_length=100)
    shots: tuple[StoryboardShot, ...]
    risk_notes: tuple[str, ...] = ()


class QualityItem(ContractModel):
    code: str
    passed: bool
    severity: Literal["INFO", "WARNING", "ERROR"]
    message: str
    value: float | int | str | bool | None = None
    threshold: float | int | str | bool | None = None
    auto_fixable: bool = False


class QualityReport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_version: str
    technical: tuple[QualityItem, ...] = ()
    visual: tuple[QualityItem, ...] = ()
    audio: tuple[QualityItem, ...] = ()
    subtitle: tuple[QualityItem, ...] = ()
    compliance: tuple[QualityItem, ...] = ()
    business: tuple[QualityItem, ...] = ()
    decision: Literal["PASS", "REJECT", "MANUAL_REVIEW"]
    revision_count: int = Field(default=0, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RenderRun(ContractModel):
    run_id: str
    task_id: int
    tenant_id: int
    run_no: int
    stage: RunStage
    progress: float = Field(default=0, ge=0, le=1)
    sequence: int = Field(default=0, ge=0)
    worker_id: str | None = None
    lease_until: datetime | None = None
    attempt: int = Field(default=0, ge=0)
    output_object_key: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
