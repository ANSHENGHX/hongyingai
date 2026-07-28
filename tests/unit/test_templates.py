from __future__ import annotations

from hongying_ai.application.planner import _asset_match_score
from hongying_ai.application.templates import apply_template, get_template
from hongying_ai.domain.models import (
    AssetManifestEntry,
    InputManifest,
    Storyboard,
    StoryboardShot,
    TrackType,
)
from hongying_ai.domain.timeline import validate_timeline


def test_template_adds_original_audio_transitions_and_non_overlapping_subtitles(
    timeline,
) -> None:
    assets = InputManifest(
        tenantId=10001,
        assets=(
            AssetManifestEntry(
                assetId="asset-1",
                objectKey="prod/10001/material/asset-1.mp4",
                sha256="1" * 64,
                durationMs=10_000,
                sizeBytes=1000,
                licenseId="license-1",
                hasAudio=True,
                labels=("菜品",),
                qualityScore=92,
            ),
            AssetManifestEntry(
                assetId="asset-2",
                objectKey="prod/10001/material/asset-2.mp4",
                sha256="2" * 64,
                durationMs=10_000,
                sizeBytes=1000,
                licenseId="license-2",
                hasAudio=True,
                labels=("环境",),
                qualityScore=88,
            ),
        ),
    )
    storyboard = Storyboard(
        title="门店活动",
        cta="立即到店",
        shots=(
            StoryboardShot(
                id="shot-1",
                narration="招牌菜现点现做",
                visualIntent="菜品",
                durationMs=5000,
                assetQuery="菜品",
                selectedAssetId="asset-1",
            ),
            StoryboardShot(
                id="shot-2",
                narration="舒适环境欢迎到店",
                visualIntent="环境",
                durationMs=5000,
                assetQuery="环境",
                selectedAssetId="asset-2",
            ),
        ),
    )
    result = apply_template(
        timeline,
        storyboard,
        assets,
        get_template("store-tour-vertical-v1"),
    )

    assert any(track.type == TrackType.AUDIO for track in result.tracks)
    assert result.transitions
    assert result.subtitles
    assert validate_timeline(result, assets) == []


def test_rule_matcher_prefers_label_and_quality() -> None:
    matched = AssetManifestEntry(
        assetId="dish",
        objectKey="prod/10001/material/dish.mp4",
        sha256="3" * 64,
        durationMs=3000,
        sizeBytes=1000,
        licenseId="license-dish",
        labels=("火锅", "菜品"),
        qualityScore=95,
    )
    unrelated = matched.model_copy(update={"asset_id": "room", "labels": ("环境",), "quality_score": 60})
    assert _asset_match_score("火锅菜品特写", matched, False) > _asset_match_score(
        "火锅菜品特写", unrelated, False
    )


def test_template_rebuilds_clips_and_syncs_subtitles_to_narration(
    timeline,
) -> None:
    narration_text = "为什么视频会被划走？先给结果，再讲原因。"
    assets = InputManifest(
        tenantId=10001,
        assets=(
            AssetManifestEntry(
                assetId="asset-1",
                objectKey="prod/10001/material/asset-1.mp4",
                sha256="1" * 64,
                durationMs=5000,
                sizeBytes=1000,
                licenseId="license-1",
                mediaType="video",
                hasAudio=False,
            ),
            AssetManifestEntry(
                assetId="asset-2",
                objectKey="prod/10001/material/asset-2.mp4",
                sha256="2" * 64,
                durationMs=5000,
                sizeBytes=1000,
                licenseId="license-2",
                mediaType="video",
                hasAudio=False,
            ),
            AssetManifestEntry(
                assetId="narration",
                objectKey="prod/10001/audio/narration.mp3",
                sha256="3" * 64,
                durationMs=6500,
                sizeBytes=1000,
                licenseId="baidu-tts",
                mediaType="audio",
                hasAudio=True,
            ),
        ),
    )
    storyboard = Storyboard(
        title="黄金三秒",
        cta="收藏",
        shots=(
            StoryboardShot(
                id="shot-1",
                narration="模型改写的字幕不应出现",
                visualIntent="开场",
                durationMs=4000,
                assetQuery="开场",
                selectedAssetId="asset-1",
            ),
            StoryboardShot(
                id="shot-2",
                narration="第二条模型字幕也不应出现",
                visualIntent="结论",
                durationMs=4000,
                assetQuery="结论",
                selectedAssetId="asset-2",
            ),
        ),
    )

    result = apply_template(
        timeline,
        storyboard,
        assets,
        get_template("food-promo-vertical-v1"),
        narration_asset_id="narration",
        narration_text=narration_text,
    )

    video = next(track for track in result.tracks if track.type == TrackType.VIDEO)
    assert [clip.duration_ms for clip in video.clips] == [4000, 4000]
    assert "".join(cue.text for cue in result.subtitles) == narration_text
    assert result.subtitles[0].start_ms == 0
    assert result.subtitles[-1].end_ms == 6500
    assert all(
        left.end_ms <= right.start_ms
        for left, right in zip(result.subtitles, result.subtitles[1:], strict=False)
    )
    assert validate_timeline(result, assets) == []


def test_template_uses_non_overlapping_source_ranges_and_hard_target_duration(
    timeline,
) -> None:
    asset = AssetManifestEntry(
        assetId="long-video",
        objectKey="prod/10001/material/long-video.mp4",
        sha256="9" * 64,
        durationMs=20_000,
        sizeBytes=1000,
        licenseId="license-long-video",
        mediaType="video",
        hasAudio=True,
    )
    assets = InputManifest(tenantId=10001, assets=(asset,))
    storyboard = Storyboard(
        title="连续非重复区间",
        cta="立即了解",
        shots=(
            StoryboardShot(
                id="shot-1",
                narration="第一段",
                visualIntent="开场",
                durationMs=8000,
                assetQuery="开场",
                selectedAssetId=asset.asset_id,
            ),
            StoryboardShot(
                id="shot-2",
                narration="第二段",
                visualIntent="讲解",
                durationMs=8000,
                assetQuery="讲解",
                selectedAssetId=asset.asset_id,
            ),
            StoryboardShot(
                id="shot-3",
                narration="第三段",
                visualIntent="收尾",
                durationMs=4000,
                assetQuery="收尾",
                selectedAssetId=asset.asset_id,
            ),
        ),
    )

    result = apply_template(
        timeline,
        storyboard,
        assets,
        get_template("food-promo-vertical-v1"),
    )

    video = next(track for track in result.tracks if track.type == TrackType.VIDEO)
    assert result.duration_ms == 15_000
    assert [(clip.source_in_ms, clip.source_out_ms) for clip in video.clips] == [
        (0, 8000),
        (8000, 15_350),
    ]
    assert max(clip.timeline_start_ms + clip.duration_ms for clip in video.clips) == 15_000
    assert validate_timeline(result, assets) == []
