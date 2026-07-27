from __future__ import annotations

from pathlib import Path
from typing import Any

from hongying_ai.domain.models import OutputProfile, QualityItem, QualityReport
from hongying_ai.domain.ports import MediaRunner


class QualityService:
    def __init__(self, runner: MediaRunner) -> None:
        self.runner = runner

    async def inspect(
        self,
        path: Path,
        expected: OutputProfile,
        *,
        expected_duration_ms: int | None = None,
        policy_version: str = "quality-v1",
    ) -> QualityReport:
        probe = await self.runner.probe(path)
        scan = await self.runner.scan_quality(path)
        streams = probe.get("streams", [])
        format_data = probe.get("format", {})
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        duration_ms = round(float(format_data.get("duration", 0)) * 1000)
        technical = [
            _item("VIDEO_STREAM", video is not None, "必须包含视频流"),
            _item("AUDIO_STREAM", audio is not None, "必须包含音频流"),
            _item(
                "RESOLUTION",
                bool(video)
                and video.get("width") == expected.width
                and video.get("height") == expected.height,
                "分辨率必须符合输出配置",
                value=f"{video.get('width')}x{video.get('height')}" if video else "missing",
                threshold=f"{expected.width}x{expected.height}",
            ),
            _item(
                "PIXEL_FORMAT",
                bool(video) and video.get("pix_fmt") == "yuv420p",
                "像素格式应为 yuv420p",
                value=video.get("pix_fmt") if video else "missing",
                threshold="yuv420p",
            ),
        ]
        if expected_duration_ms is not None:
            technical.append(
                _item(
                    "DURATION",
                    abs(duration_ms - expected_duration_ms) <= 250,
                    "成片时长偏差不得超过 250ms",
                    value=duration_ms,
                    threshold=expected_duration_ms,
                )
            )
        duration_seconds = max(0.001, duration_ms / 1000)
        visual = [
            _item(
                "BLACK_FRAME_RATIO",
                scan.get("blackSeconds", 0) / duration_seconds <= 0.1,
                "黑帧比例不得超过 10%",
                value=round(scan.get("blackSeconds", 0) / duration_seconds, 4),
                threshold=0.1,
            ),
            _item(
                "FREEZE_DURATION",
                scan.get("freezeSeconds", 0) <= 3,
                "连续冻结画面不得超过 3 秒",
                value=scan.get("freezeSeconds", 0),
                threshold=3,
            ),
        ]
        audio_items = [
            _item(
                "SILENCE_RATIO",
                scan.get("silenceSeconds", 0) / duration_seconds <= 0.8,
                "静音比例不得超过 80%",
                value=round(scan.get("silenceSeconds", 0) / duration_seconds, 4),
                threshold=0.8,
                auto_fixable=False,
            )
        ]
        all_items = [*technical, *visual, *audio_items]
        decision = "PASS" if all(item.passed for item in all_items) else "REJECT"
        return QualityReport(
            policyVersion=policy_version,
            technical=tuple(technical),
            visual=tuple(visual),
            audio=tuple(audio_items),
            decision=decision,
        )


def _item(
    code: str,
    passed: bool,
    message: str,
    *,
    value: Any = None,
    threshold: Any = None,
    auto_fixable: bool = False,
) -> QualityItem:
    return QualityItem(
        code=code,
        passed=passed,
        severity="INFO" if passed else "ERROR",
        message=message,
        value=value,
        threshold=threshold,
        autoFixable=auto_fixable,
    )

