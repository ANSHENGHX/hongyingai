from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from hongying_ai.config import Settings
from hongying_ai.domain.errors import ErrorCode, PlatformError
from hongying_ai.domain.models import AnalysisProfile, MediaProfile
from hongying_ai.domain.ports import MediaRunner, ObjectStore
from hongying_ai.domain.timeline import assert_safe_object_key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fraction(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    numerator, _, denominator = value.partition("/")
    if denominator:
        return float(numerator) / float(denominator)
    return float(numerator)


def profile_from_probe(
    raw: dict[str, Any],
    *,
    object_key: str,
    sha256: str,
    size_bytes: int,
    asset_id: str | None = None,
) -> MediaProfile:
    format_data = raw.get("format", {})
    streams = raw.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        raise PlatformError(ErrorCode.MEDIA_UNSUPPORTED, "素材不包含视频流")
    duration_seconds = format_data.get("duration") or video.get("duration")
    if not duration_seconds:
        raise PlatformError(ErrorCode.MEDIA_UNSUPPORTED, "无法读取素材时长")
    rotation = 0
    for side_data in video.get("side_data_list", []):
        if "rotation" in side_data:
            rotation = int(side_data["rotation"]) % 360
    return MediaProfile(
        assetId=asset_id,
        objectKey=object_key,
        sha256=sha256,
        container=str(format_data.get("format_name", "unknown")).split(",")[0],
        videoCodec=video.get("codec_name"),
        audioCodec=audio.get("codec_name") if audio else None,
        durationMs=round(float(duration_seconds) * 1000),
        width=video.get("width"),
        height=video.get("height"),
        fps=_fraction(video.get("avg_frame_rate")),
        pixelFormat=video.get("pix_fmt"),
        rotation=rotation,
        hasAudio=audio is not None,
        audioSampleRate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        sizeBytes=int(format_data.get("size") or size_bytes),
    )


class MediaService:
    def __init__(self, settings: Settings, store: ObjectStore, runner: MediaRunner) -> None:
        self.settings = settings
        self.store = store
        self.runner = runner

    async def probe_object(
        self,
        *,
        tenant_id: int,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
        asset_id: str | None = None,
    ) -> MediaProfile:
        assert_safe_object_key(object_key, tenant_id, self.settings.environment_object_prefix)
        if expected_size > self.settings.max_media_bytes:
            raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "素材大小超过 V1.0 限制")
        work_dir = Path(
            mkdtemp(prefix=f"probe-{tenant_id}-", dir=self.settings.app_work_dir.resolve())
        )
        try:
            local_file = work_dir / "input.media"
            await self.store.download(object_key, local_file)
            actual_size = local_file.stat().st_size
            if actual_size != expected_size:
                raise PlatformError(ErrorCode.INVALID_COMMAND, "素材大小与命令不一致")
            actual_sha = sha256_file(local_file)
            if actual_sha.lower() != expected_sha256.lower():
                raise PlatformError(ErrorCode.INVALID_COMMAND, "素材 sha256 校验失败")
            raw = await self.runner.probe(local_file)
            return profile_from_probe(
                raw,
                object_key=object_key,
                sha256=actual_sha,
                size_bytes=actual_size,
                asset_id=asset_id,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def analyze(
        self,
        *,
        tenant_id: int,
        asset_id: str,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
        profile: AnalysisProfile,
    ) -> dict[str, Any]:
        assert_safe_object_key(object_key, tenant_id, self.settings.environment_object_prefix)
        if expected_size > self.settings.max_media_bytes:
            raise PlatformError(ErrorCode.RESOURCE_EXHAUSTED, "素材大小超过 V1.0 限制")
        work_dir = Path(
            mkdtemp(prefix=f"analyze-{tenant_id}-", dir=self.settings.app_work_dir.resolve())
        )
        try:
            source = work_dir / "input.media"
            await self.store.download(object_key, source)
            if source.stat().st_size != expected_size:
                raise PlatformError(ErrorCode.INVALID_COMMAND, "素材大小与命令不一致")
            actual_sha = sha256_file(source)
            if actual_sha.lower() != expected_sha256.lower():
                raise PlatformError(ErrorCode.INVALID_COMMAND, "素材 sha256 校验失败")
            media = profile_from_probe(
                await self.runner.probe(source),
                object_key=object_key,
                sha256=actual_sha,
                size_bytes=expected_size,
                asset_id=asset_id,
            )
            thumbnail = work_dir / "thumbnail.jpg"
            proxy = work_dir / "proxy.mp4"
            await self.runner.create_thumbnail(
                source,
                thumbnail,
                min(1.0, media.duration_ms / 2000),
            )
            await self.runner.create_proxy(source, proxy)
            prefix = (
                f"{self.settings.environment_object_prefix}/{tenant_id}/material/{asset_id}/"
                f"analysis/{actual_sha[:16]}"
            )
            thumbnail_key = f"{prefix}/thumbnail.jpg"
            proxy_key = f"{prefix}/proxy.mp4"
            await self.store.upload(thumbnail, thumbnail_key, "image/jpeg")
            await self.store.upload(proxy, proxy_key, "video/mp4")
            scan = await self.runner.scan_quality(source)
            result: dict[str, Any] = {
                "schemaVersion": "1.0",
                "analysisProfile": profile.value,
                "mediaProfile": media.model_dump(by_alias=True, mode="json"),
                "proxyObjectKey": proxy_key,
                "thumbnailObjectKey": thumbnail_key,
                "shotList": [
                    {
                        "id": "shot_001",
                        "startMs": 0,
                        "endMs": media.duration_ms,
                        "keyframeMs": min(1000, media.duration_ms // 2),
                    }
                ],
                "visual": {
                    "labels": [],
                    "detections": [],
                    "ocr": [],
                    "faces": [],
                    "logos": [],
                    "safeArea": {
                        "left": 0.05,
                        "top": 0.05,
                        "right": 0.95,
                        "bottom": 0.9,
                    },
                    "quality": {
                        "blackSeconds": scan.get("blackSeconds", 0),
                        "freezeSeconds": scan.get("freezeSeconds", 0),
                    },
                    "riskItems": list(media.risk_flags),
                    "modelStatus": "NOT_CONFIGURED",
                },
                "audio": {
                    "transcript": [],
                    "speakers": [],
                    "silenceSeconds": scan.get("silenceSeconds", 0),
                    "beatGrid": [],
                    "loudness": None,
                    "modelStatus": "NOT_CONFIGURED",
                },
                "models": [],
            }
            if profile == AnalysisProfile.FAST:
                result["visual"].update(
                    {"labels": None, "detections": None, "ocr": None, "faces": None, "logos": None}
                )
                result["audio"].update(
                    {"transcript": None, "speakers": None, "beatGrid": None}
                )
            elif profile == AnalysisProfile.DEEP:
                result["embedding"] = {"value": None, "modelStatus": "NOT_CONFIGURED"}
                result["emotion"] = {"items": [], "modelStatus": "NOT_CONFIGURED"}
            return result
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
