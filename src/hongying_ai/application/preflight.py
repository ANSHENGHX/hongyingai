from __future__ import annotations

import math

from hongying_ai.contracts.api import RenderPreflightResult
from hongying_ai.domain.models import InputManifest, OutputProfile, Timeline
from hongying_ai.domain.timeline import validate_timeline


def render_preflight(
    timeline: Timeline,
    manifest: InputManifest,
    output_profile: OutputProfile | None = None,
) -> RenderPreflightResult:
    output = output_profile or timeline.output
    issues = validate_timeline(timeline, manifest)
    pixels = output.width * output.height
    duration_seconds = timeline.duration_ms / 1000
    clip_count = sum(len(track.clips) for track in timeline.tracks)
    effects = sum(
        1
        for track in timeline.tracks
        for clip in track.clips
        if clip.transform.crop
        or clip.transform.rotation
        or clip.transform.speed != 1
        or clip.transform.scale_mode == "blur"
    )
    complexity = round(
        max(1.0, pixels / (1280 * 720))
        * max(1.0, output.fps / 30)
        * (1 + effects * 0.1 + len(timeline.transitions) * 0.08 + clip_count * 0.02),
        2,
    )
    estimated_seconds = math.ceil(duration_seconds * complexity * (0.35 if output.preview else 0.8))
    bitrate = output.video_bitrate_kbps + output.audio_bitrate_kbps
    output_bytes = math.ceil(duration_seconds * bitrate * 1000 / 8)
    input_bytes = sum(item.size_bytes for item in manifest.assets)
    disk_bytes = math.ceil((input_bytes + output_bytes) * 1.3)
    warnings = [issue.message for issue in issues]
    if output.video_codec == "h265":
        warnings.append("H.265 兼容性低于 H.264，发布端需确认目标平台")
    return RenderPreflightResult(
        accepted=not issues,
        estimatedSeconds=max(1, estimated_seconds),
        estimatedDiskBytes=disk_bytes,
        complexityScore=complexity,
        encoder="libx264" if output.video_codec == "h264" else "libx265",
        warnings=tuple(warnings),
    )

