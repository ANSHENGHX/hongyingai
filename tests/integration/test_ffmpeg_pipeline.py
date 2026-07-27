from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from hongying_ai.application.compiler import compile_timeline
from hongying_ai.application.quality import QualityService
from hongying_ai.domain.models import (
    AssetManifestEntry,
    Canvas,
    Clip,
    InputManifest,
    OutputProfile,
    Timeline,
    Track,
    TrackType,
    Transition,
)
from hongying_ai.domain.timeline import validate_timeline
from hongying_ai.infrastructure.ffmpeg import FfmpegRunner

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _source(path: Path, color: str, frequency: int) -> None:
    assert FFMPEG
    subprocess.run(  # noqa: S603 - executable and all arguments are test-controlled
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
            f"color=c={color}:s=320x240:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=2",
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_real_crossfade_render_and_quality_scan(tmp_path: Path) -> None:
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    _source(source_a, "red", 440)
    _source(source_b, "blue", 660)
    assets = InputManifest(
        tenantId=10001,
        assets=(
            AssetManifestEntry(
                assetId="a",
                objectKey="prod/10001/material/a.mp4",
                sha256=_sha(source_a),
                durationMs=2000,
                sizeBytes=source_a.stat().st_size,
            ),
            AssetManifestEntry(
                assetId="b",
                objectKey="prod/10001/material/b.mp4",
                sha256=_sha(source_b),
                durationMs=2000,
                sizeBytes=source_b.stat().st_size,
            ),
        ),
    )
    video_clips = (
        Clip(
            id="v-a",
            assetId="a",
            timelineStartMs=0,
            sourceInMs=0,
            sourceOutMs=2000,
            durationMs=2000,
        ),
        Clip(
            id="v-b",
            assetId="b",
            timelineStartMs=1500,
            sourceInMs=0,
            sourceOutMs=2000,
            durationMs=2000,
        ),
    )
    audio_clips = (
        video_clips[0].model_copy(update={"id": "a-a"}),
        video_clips[1].model_copy(update={"id": "a-b"}),
    )
    output_profile = OutputProfile(
        width=320,
        height=240,
        fps=30,
        videoBitrateKbps=800,
        audioBitrateKbps=96,
    )
    timeline = Timeline(
        durationMs=3500,
        canvas=Canvas(width=320, height=240, fps=30),
        tracks=(
            Track(id="video", type=TrackType.VIDEO, clips=video_clips),
            Track(id="audio", type=TrackType.AUDIO, clips=audio_clips),
        ),
        transitions=(
            Transition(
                fromClipId="v-a",
                toClipId="v-b",
                type="crossfade",
                durationMs=500,
            ),
        ),
        output=output_profile,
    )
    assert validate_timeline(timeline, assets) == []
    output = tmp_path / "output.mp4"
    compiled = compile_timeline(
        timeline,
        assets,
        {"a": source_a, "b": source_b},
        tmp_path,
        output,
    )
    runner = FfmpegRunner()
    await runner.render(list(compiled.args), timeout_seconds=30)
    assert output.exists() and output.stat().st_size > 0
    report = await QualityService(runner).inspect(
        output,
        output_profile,
        expected_duration_ms=3500,
    )
    assert report.decision == "PASS"
