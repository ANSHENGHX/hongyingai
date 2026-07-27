from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat, UnidentifiedImageError

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
        mediaType="video",
        bitrateKbps=(
            round(int(format_data["bit_rate"]) / 1000) if format_data.get("bit_rate") else None
        ),
    )


def image_profile(
    path: Path,
    *,
    object_key: str,
    sha256: str,
    size_bytes: int,
    asset_id: str | None,
) -> MediaProfile:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image_format = (image.format or path.suffix.removeprefix(".") or "image").lower()
    except (UnidentifiedImageError, OSError) as exc:
        raise PlatformError(ErrorCode.MEDIA_UNSUPPORTED, "图片已损坏或格式不受支持") from exc
    return MediaProfile(
        assetId=asset_id,
        objectKey=object_key,
        sha256=sha256,
        container=image_format,
        durationMs=0,
        width=width,
        height=height,
        sizeBytes=size_bytes,
        mediaType="image",
        analyzerVersion="image-v1",
    )


def audio_profile_from_probe(
    raw: dict[str, Any],
    *,
    object_key: str,
    sha256: str,
    size_bytes: int,
    asset_id: str | None,
) -> MediaProfile:
    format_data = raw.get("format", {})
    audio = next(
        (stream for stream in raw.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    if not audio or not format_data.get("duration"):
        raise PlatformError(ErrorCode.MEDIA_UNSUPPORTED, "无法读取音频素材")
    return MediaProfile(
        assetId=asset_id,
        objectKey=object_key,
        sha256=sha256,
        container=str(format_data.get("format_name", "audio")).split(",")[0],
        audioCodec=audio.get("codec_name"),
        durationMs=round(float(format_data["duration"]) * 1000),
        hasAudio=True,
        audioSampleRate=(
            int(audio["sample_rate"]) if audio.get("sample_rate") else None
        ),
        sizeBytes=int(format_data.get("size") or size_bytes),
        mediaType="audio",
        bitrateKbps=(
            round(int(format_data["bit_rate"]) / 1000) if format_data.get("bit_rate") else None
        ),
        analyzerVersion="audio-probe-v1",
    )


def analyze_image(path: Path) -> dict[str, Any]:
    with Image.open(path) as opened:
        image = opened.convert("RGB")
        image.thumbnail((960, 960))
        gray = image.convert("L")
        brightness = ImageStat.Stat(gray).mean[0] / 255
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_stddev = ImageStat.Stat(edges).stddev[0]
        blur_score = max(0.0, min(1.0, 1 - edge_stddev / 48))
        exposure_score = max(0.0, 1 - abs(brightness - 0.55) / 0.55)
        sharpness_score = 1 - blur_score
        quality_score = round((exposure_score * 0.4 + sharpness_score * 0.6) * 100, 1)

        corner = image.resize((1, 1), Image.Resampling.BILINEAR)
        background = Image.new("RGB", image.size, corner.getpixel((0, 0)))
        difference = ImageChops.difference(image, background).convert("L")
        bbox = difference.point(lambda value: 255 if value > 24 else 0).getbbox()
        if bbox:
            left, top, right, bottom = bbox
            subject = {
                "x": round(left / image.width, 4),
                "y": round(top / image.height, 4),
                "width": round((right - left) / image.width, 4),
                "height": round((bottom - top) / image.height, 4),
            }
        else:
            subject = {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}

        histogram = image.resize((128, 128)).histogram()
        channel_size = 256
        compact_histogram = [
            [
                round(
                    sum(histogram[channel * channel_size + start : channel * channel_size + start + 16])
                    / (128 * 128),
                    6,
                )
                for start in range(0, 256, 16)
            ]
            for channel in range(3)
        ]
        dominant = image.resize((1, 1), Image.Resampling.BILINEAR).getpixel((0, 0))
        return {
            "brightness": round(brightness, 4),
            "blurScore": round(blur_score, 4),
            "sharpnessScore": round(sharpness_score, 4),
            "qualityScore": quality_score,
            "isBlurry": blur_score >= 0.72,
            "orientation": (
                "landscape"
                if image.width > image.height
                else "portrait"
                if image.height > image.width
                else "square"
            ),
            "subjectBox": subject,
            "dominantColor": "#{:02x}{:02x}{:02x}".format(*dominant),
            "colorHistogram": {
                "red": compact_histogram[0],
                "green": compact_histogram[1],
                "blue": compact_histogram[2],
            },
        }


def create_image_variant(source: Path, destination: Path, size: tuple[int, int]) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
        image.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, (20, 23, 31))
        canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
        destination.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(destination, "JPEG", quality=88, optimize=True)


def create_audio_thumbnail(destination: Path, size: tuple[int, int]) -> None:
    image = Image.new("RGB", size, (21, 24, 33))
    pixels = image.load()
    center = size[1] // 2
    for x in range(size[0]):
        amplitude = int((0.12 + ((x * 37) % 100) / 250) * size[1])
        color = (255, 107 + (x % 70), 53)
        for y in range(max(0, center - amplitude // 2), min(size[1], center + amplitude // 2)):
            if pixels is not None:
                pixels[x, y] = color
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=88, optimize=True)


def _basic_labels(object_key: str, media: MediaProfile) -> list[dict[str, Any]]:
    keywords = {
        "火锅": ("火锅", "hotpot"),
        "菜品": ("菜", "food", "dish", "meal"),
        "门店": ("门店", "店面", "store", "shop"),
        "员工": ("员工", "staff", "chef"),
        "环境": ("环境", "room", "interior"),
        "顾客": ("顾客", "customer", "people"),
        "活动": ("活动", "促销", "campaign", "sale"),
    }
    lowered = object_key.casefold()
    labels = [
        {"name": label, "score": 0.86, "source": "filename-rule-v1"}
        for label, words in keywords.items()
        if any(word.casefold() in lowered for word in words)
    ]
    if media.width and media.height:
        labels.append(
            {
                "name": "竖屏素材" if media.height > media.width else "横屏素材",
                "score": 1.0,
                "source": "media-rule-v1",
            }
        )
    if media.has_audio:
        labels.append({"name": "含原声", "score": 1.0, "source": "media-rule-v1"})
    return labels


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
            try:
                raw = await self.runner.probe(local_file)
                if any(
                    stream.get("codec_type") == "video"
                    for stream in raw.get("streams", [])
                ):
                    return profile_from_probe(
                        raw,
                        object_key=object_key,
                        sha256=actual_sha,
                        size_bytes=actual_size,
                        asset_id=asset_id,
                    )
                return audio_profile_from_probe(
                    raw,
                    object_key=object_key,
                    sha256=actual_sha,
                    size_bytes=actual_size,
                    asset_id=asset_id,
                )
            except PlatformError:
                return image_profile(
                    local_file,
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
            try:
                raw_probe = await self.runner.probe(source)
                if any(
                    stream.get("codec_type") == "video"
                    for stream in raw_probe.get("streams", [])
                ):
                    media = profile_from_probe(
                        raw_probe,
                        object_key=object_key,
                        sha256=actual_sha,
                        size_bytes=expected_size,
                        asset_id=asset_id,
                    )
                else:
                    media = audio_profile_from_probe(
                        raw_probe,
                        object_key=object_key,
                        sha256=actual_sha,
                        size_bytes=expected_size,
                        asset_id=asset_id,
                    )
            except PlatformError:
                media = image_profile(
                    source,
                    object_key=object_key,
                    sha256=actual_sha,
                    size_bytes=expected_size,
                    asset_id=asset_id,
                )
            thumbnail = work_dir / "thumbnail.jpg"
            thumbnail_200 = work_dir / "thumbnail-200.jpg"
            thumbnail_400 = work_dir / "thumbnail-400.jpg"
            proxy = work_dir / "proxy.mp4"
            if media.media_type == "image":
                create_image_variant(source, thumbnail, (640, 640))
                create_image_variant(source, thumbnail_200, (200, 200))
                create_image_variant(source, thumbnail_400, (400, 400))
            elif media.media_type == "audio":
                create_audio_thumbnail(thumbnail, (640, 640))
                create_audio_thumbnail(thumbnail_200, (200, 200))
                create_audio_thumbnail(thumbnail_400, (400, 400))
            else:
                await self.runner.create_thumbnail(
                    source,
                    thumbnail,
                    min(1.0, media.duration_ms / 2000),
                )
                create_image_variant(thumbnail, thumbnail_200, (200, 200))
                create_image_variant(thumbnail, thumbnail_400, (400, 400))
                await self.runner.create_proxy(source, proxy)
            prefix = (
                f"{self.settings.environment_object_prefix}/{tenant_id}/material/{asset_id}/"
                f"analysis/{actual_sha[:16]}"
            )
            thumbnail_key = f"{prefix}/thumbnail.jpg"
            thumbnail_200_key = f"{prefix}/thumbnail-200.jpg"
            thumbnail_400_key = f"{prefix}/thumbnail-400.jpg"
            proxy_key = f"{prefix}/proxy.mp4" if media.media_type == "video" else None
            await self.store.upload(thumbnail, thumbnail_key, "image/jpeg")
            await self.store.upload(thumbnail_200, thumbnail_200_key, "image/jpeg")
            await self.store.upload(thumbnail_400, thumbnail_400_key, "image/jpeg")
            if proxy_key:
                await self.store.upload(proxy, proxy_key, "video/mp4")
            metrics = (
                analyze_image(source if media.media_type == "image" else thumbnail)
                if media.media_type != "audio"
                else {
                    "brightness": 0.0,
                    "blurScore": 0.0,
                    "sharpnessScore": 0.0,
                    "qualityScore": 100.0,
                    "isBlurry": False,
                    "orientation": "audio",
                    "subjectBox": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "dominantColor": "#151821",
                    "colorHistogram": {"red": [], "green": [], "blue": []},
                }
            )
            scan = (
                await self.runner.scan_quality(source)
                if media.media_type == "video"
                else {"returnCode": 0, "blackSeconds": 0, "freezeSeconds": 0, "silenceSeconds": 0}
            )
            boundaries = (
                await self.runner.detect_scenes(source, media.duration_ms)
                if media.media_type == "video"
                else [0, media.duration_ms or 15_000]
            )
            shots = [
                {
                    "id": f"shot_{index + 1:03d}",
                    "startMs": start,
                    "endMs": end,
                    "keyframeMs": start + (end - start) // 2,
                }
                for index, (start, end) in enumerate(
                    zip(boundaries, boundaries[1:], strict=False)
                )
                if end > start
            ]
            result: dict[str, Any] = {
                "schemaVersion": "1.0",
                "analysisProfile": profile.value,
                "mediaProfile": media.model_dump(by_alias=True, mode="json"),
                "proxyObjectKey": proxy_key,
                "thumbnailObjectKey": thumbnail_key,
                "thumbnailObjectKeys": {
                    "200x200": thumbnail_200_key,
                    "400x400": thumbnail_400_key,
                },
                "shotList": shots,
                "visual": {
                    "labels": _basic_labels(object_key, media),
                    "detections": [
                        {
                            "label": "主体区域",
                            "score": round(metrics["qualityScore"] / 100, 3),
                            "boundingBox": metrics["subjectBox"],
                            "model": "contrast-subject-v1",
                        }
                    ],
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
                        **metrics,
                    },
                    "riskItems": list(media.risk_flags),
                    "modelStatus": "HEURISTIC_V1",
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
