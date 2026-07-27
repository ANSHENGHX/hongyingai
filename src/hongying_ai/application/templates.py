from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from hongying_ai.domain.models import (
    Clip,
    InputManifest,
    OutputProfile,
    Storyboard,
    SubtitleCue,
    SubtitleStyle,
    Timeline,
    Track,
    TrackType,
    Transform,
    Transition,
)


@dataclass(frozen=True, slots=True)
class VideoTemplate:
    id: str
    name: str
    description: str
    duration_ms: int
    width: int
    height: int
    transition: Literal["cut", "crossfade", "fade", "slide", "zoom"]
    transition_ms: int
    scale_mode: Literal["fit", "fill", "stretch", "blur"]
    accent: str
    subtitle_color: str = "#FFFFFF"
    subtitle_position_y: float = 0.82

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {
            "id": value["id"],
            "name": value["name"],
            "description": value["description"],
            "durationMs": value["duration_ms"],
            "width": value["width"],
            "height": value["height"],
            "transition": value["transition"],
            "transitionMs": value["transition_ms"],
            "scaleMode": value["scale_mode"],
            "accent": value["accent"],
        }


TEMPLATES = (
    VideoTemplate(
        id="food-promo-vertical-v1",
        name="爆款菜品",
        description="9:16 快节奏菜品展示，适合团购、上新和限时优惠。",
        duration_ms=15_000,
        width=1080,
        height=1920,
        transition="zoom",
        transition_ms=350,
        scale_mode="fill",
        accent="#FF6B35",
    ),
    VideoTemplate(
        id="store-tour-vertical-v1",
        name="门店探店",
        description="从门头到环境再到招牌产品，节奏自然、画面完整。",
        duration_ms=30_000,
        width=1080,
        height=1920,
        transition="crossfade",
        transition_ms=500,
        scale_mode="blur",
        accent="#27C499",
    ),
    VideoTemplate(
        id="campaign-flash-v1",
        name="活动快闪",
        description="高密度活动信息与强 CTA，适合开业、节日和促销。",
        duration_ms=20_000,
        width=1080,
        height=1920,
        transition="slide",
        transition_ms=300,
        scale_mode="fill",
        accent="#7C5CFC",
    ),
)


def get_template(template_id: str) -> VideoTemplate:
    template = next((item for item in TEMPLATES if item.id == template_id), None)
    if template is None:
        raise ValueError(f"未知视频模板: {template_id}")
    return template


def apply_template(
    base: Timeline,
    storyboard: Storyboard,
    manifest: InputManifest,
    template: VideoTemplate,
    *,
    logo_asset_id: str | None = None,
    bgm_asset_id: str | None = None,
) -> Timeline:
    assets = manifest.by_id()
    primary = next(track for track in base.tracks if track.type == TrackType.VIDEO)
    source_clips = tuple(sorted(primary.clips, key=lambda item: item.timeline_start_ms))
    clips: list[Clip] = []
    transitions: list[Transition] = []
    cursor = 0
    for source in source_clips:
        transform = source.transform.model_copy(update={"scale_mode": template.scale_mode})
        duration = source.duration_ms
        overlap = 0
        if clips and template.transition != "cut":
            overlap = min(template.transition_ms, clips[-1].duration_ms // 2, duration // 2)
            cursor -= overlap
        clip = source.model_copy(
            update={
                "timeline_start_ms": cursor,
                "transform": transform,
            }
        )
        if clips and overlap:
            transitions.append(
                Transition(
                    fromClipId=clips[-1].id,
                    toClipId=clip.id,
                    type=template.transition,
                    durationMs=overlap,
                )
            )
        clips.append(clip)
        cursor += duration

    duration_ms = max(1, cursor)
    tracks: list[Track] = [Track(id="video-main", type=TrackType.VIDEO, clips=tuple(clips))]

    original_audio = tuple(
        clip.model_copy(
            update={
                "id": f"audio-{clip.id}",
                "volume": 0.9 if bgm_asset_id else 1.0,
                "fade_in_ms": min(150, clip.duration_ms // 4),
                "fade_out_ms": min(150, clip.duration_ms // 4),
            }
        )
        for clip in clips
        if assets[clip.asset_id].media_type == "video" and assets[clip.asset_id].has_audio
    )
    if original_audio:
        tracks.append(Track(id="audio-original", type=TrackType.AUDIO, clips=original_audio))

    if bgm_asset_id:
        bgm = assets[bgm_asset_id]
        bgm_duration = min(duration_ms, bgm.duration_ms)
        tracks.append(
            Track(
                id="audio-bgm",
                type=TrackType.AUDIO,
                clips=(
                    Clip(
                        id="audio-bgm-1",
                        assetId=bgm.asset_id,
                        timelineStartMs=0,
                        sourceInMs=0,
                        sourceOutMs=bgm_duration,
                        durationMs=bgm_duration,
                        volume=0.22,
                        fadeInMs=min(800, bgm_duration // 4),
                        fadeOutMs=min(1200, bgm_duration // 4),
                    ),
                ),
            )
        )

    if logo_asset_id:
        logo = assets[logo_asset_id]
        logo_duration = min(duration_ms, logo.duration_ms)
        tracks.append(
            Track(
                id="overlay-logo",
                type=TrackType.OVERLAY,
                zIndex=10,
                clips=(
                    Clip(
                        id="overlay-logo-1",
                        assetId=logo.asset_id,
                        timelineStartMs=0,
                        sourceInMs=0,
                        sourceOutMs=logo_duration,
                        durationMs=logo_duration,
                        transform=Transform(
                            position="top_right",
                            overlayScale=0.18,
                            opacity=0.92,
                            margin=36,
                            scaleMode="fit",
                        ),
                    ),
                ),
            )
        )

    cues = []
    for index, clip in enumerate(clips):
        if index >= len(storyboard.shots):
            break
        text = storyboard.shots[index].narration.strip()
        if not text:
            continue
        next_start = (
            clips[index + 1].timeline_start_ms if index + 1 < len(clips) else duration_ms
        )
        cue_end = min(
            duration_ms,
            clip.timeline_start_ms + clip.duration_ms,
            next_start,
        )
        if cue_end <= clip.timeline_start_ms:
            continue
        cues.append(
            SubtitleCue(
                id=f"subtitle-{index + 1}",
                startMs=clip.timeline_start_ms,
                endMs=cue_end,
                text=text[:120],
                style=SubtitleStyle(
                    color=template.subtitle_color,
                    positionY=template.subtitle_position_y,
                ),
            )
        )

    return Timeline(
        durationMs=duration_ms,
        canvas={
            "width": template.width,
            "height": template.height,
            "fps": 30,
            "background": "#0B0D12",
        },
        tracks=tuple(tracks),
        transitions=tuple(transitions),
        subtitles=tuple(cues),
        output=OutputProfile(
            width=template.width,
            height=template.height,
            fps=30,
            videoCodec="h264",
            audioCodec="aac",
            videoBitrateKbps=6000,
            audioBitrateKbps=192,
        ),
    )
