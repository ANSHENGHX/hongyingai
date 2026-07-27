from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass

from .models import InputManifest, Timeline, TrackType


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


SUPPORTED_TRANSITIONS = {"cut", "crossfade", "fade", "slide", "zoom"}


def validate_timeline(timeline: Timeline, manifest: InputManifest | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    assets = manifest.by_id() if manifest else {}
    clips_by_id = {}
    transition_pairs = {
        (item.from_clip_id, item.to_clip_id): item for item in timeline.transitions
    }

    if timeline.canvas.width != timeline.output.width or timeline.canvas.height != timeline.output.height:
        issues.append(
            ValidationIssue(
                "/output",
                "OUTPUT_CANVAS_MISMATCH",
                "输出分辨率必须与画布分辨率一致",
            )
        )

    for track_index, track in enumerate(timeline.tracks):
        clips = sorted(track.clips, key=lambda item: item.timeline_start_ms)
        if track.type == TrackType.OVERLAY and track.z_index == 0:
            issues.append(
                ValidationIssue(
                    f"/tracks/{track_index}/zIndex",
                    "OVERLAY_Z_INDEX_REQUIRED",
                    "叠加轨道的 zIndex 必须大于 0",
                )
            )
        for clip_index, clip in enumerate(clips):
            path = f"/tracks/{track_index}/clips/{clip_index}"
            if clip.id in clips_by_id:
                issues.append(ValidationIssue(f"{path}/id", "DUPLICATE_CLIP_ID", "clip id 必须唯一"))
            clips_by_id[clip.id] = clip
            if clip.timeline_start_ms + clip.duration_ms > timeline.duration_ms:
                issues.append(
                    ValidationIssue(path, "CLIP_OUT_OF_TIMELINE", "片段结束时间超过 Timeline 总时长")
                )
            if manifest:
                asset = assets.get(clip.asset_id)
                if asset is None:
                    issues.append(
                        ValidationIssue(f"{path}/assetId", "ASSET_NOT_IN_MANIFEST", "素材不在输入清单中")
                    )
                elif clip.source_out_ms > asset.duration_ms:
                    issues.append(
                        ValidationIssue(path, "CLIP_OUT_OF_RANGE", "片段结束时间超过素材时长")
                    )
        if track.type == TrackType.VIDEO:
            for previous, current in zip(clips, clips[1:], strict=False):
                previous_end = previous.timeline_start_ms + previous.duration_ms
                if current.timeline_start_ms < previous_end:
                    overlap = previous_end - current.timeline_start_ms
                    transition = transition_pairs.get((previous.id, current.id))
                    allowed = (
                        transition is not None
                        and transition.type != "cut"
                        and overlap == transition.duration_ms
                    )
                    if not allowed:
                        issues.append(
                            ValidationIssue(
                                f"/tracks/{track_index}/clips",
                                "PRIMARY_TRACK_OVERLAP",
                                "视频主轨仅允许与相邻转场时长一致的重叠",
                            )
                        )

    for index, transition in enumerate(timeline.transitions):
        path = f"/transitions/{index}"
        left = clips_by_id.get(transition.from_clip_id)
        right = clips_by_id.get(transition.to_clip_id)
        if not left or not right:
            issues.append(ValidationIssue(path, "TRANSITION_CLIP_NOT_FOUND", "转场引用的片段不存在"))
            continue
        if transition.type not in SUPPORTED_TRANSITIONS:
            issues.append(ValidationIssue(path, "TRANSITION_UNSUPPORTED", "不支持的转场类型"))
        max_duration = min(left.duration_ms, right.duration_ms) // 2
        if transition.duration_ms > max_duration:
            issues.append(
                ValidationIssue(path, "TRANSITION_TOO_LONG", "转场时长不得超过相邻短片段的一半")
            )

    subtitle_end = 0
    for index, cue in enumerate(sorted(timeline.subtitles, key=lambda item: item.start_ms)):
        path = f"/subtitles/{index}"
        if cue.end_ms > timeline.duration_ms:
            issues.append(ValidationIssue(path, "SUBTITLE_OUT_OF_TIMELINE", "字幕超出总时长"))
        if cue.start_ms < subtitle_end:
            issues.append(ValidationIssue(path, "SUBTITLE_OVERLAP", "字幕时间发生重叠"))
        if len(cue.text) > 60 * cue.style.max_lines:
            issues.append(ValidationIssue(path, "SUBTITLE_TOO_LONG", "字幕文本超出行数预算"))
        subtitle_end = max(subtitle_end, cue.end_ms)

    return issues


def assert_safe_object_key(object_key: str, tenant_id: int, environment: str = "prod") -> None:
    expected_prefixes: Iterable[str] = (
        f"{environment}/{tenant_id}/",
        f"tenant/{tenant_id}/",
    )
    if object_key.startswith("/") or ".." in object_key.split("/") or not object_key.startswith(
        tuple(expected_prefixes)
    ):
        raise ValueError(f"objectKey 必须位于租户前缀下: {tuple(expected_prefixes)}")
