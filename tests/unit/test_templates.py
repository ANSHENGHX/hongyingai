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
    unrelated = matched.model_copy(
        update={"asset_id": "room", "labels": ("环境",), "quality_score": 60}
    )
    assert _asset_match_score("火锅菜品特写", matched, False) > _asset_match_score(
        "火锅菜品特写", unrelated, False
    )
