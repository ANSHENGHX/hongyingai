from __future__ import annotations

import pytest
from pydantic import ValidationError

from hongying_ai.domain.models import Clip, Timeline
from hongying_ai.domain.timeline import assert_safe_object_key, validate_timeline


def test_valid_timeline_has_no_issues(timeline: Timeline, manifest) -> None:
    assert validate_timeline(timeline, manifest) == []


def test_out_of_range_asset_is_rejected(timeline: Timeline, manifest) -> None:
    clip = timeline.tracks[0].clips[0].model_copy(
        update={"source_out_ms": 11_000, "duration_ms": 11_000}
    )
    track = timeline.tracks[0].model_copy(update={"clips": (clip,)})
    changed = timeline.model_copy(update={"duration_ms": 12_000, "tracks": (track,)})
    issues = validate_timeline(changed, manifest)
    assert any(issue.code == "CLIP_OUT_OF_RANGE" for issue in issues)


def test_primary_track_overlap_is_rejected(timeline: Timeline, manifest) -> None:
    second = timeline.tracks[0].clips[1].model_copy(update={"timeline_start_ms": 4000})
    track = timeline.tracks[0].model_copy(
        update={"clips": (timeline.tracks[0].clips[0], second)}
    )
    changed = timeline.model_copy(update={"tracks": (track,)})
    issues = validate_timeline(changed, manifest)
    assert any(issue.code == "PRIMARY_TRACK_OVERLAP" for issue in issues)


def test_clip_duration_must_match_speed() -> None:
    with pytest.raises(ValidationError):
        Clip(
            id="bad",
            assetId="asset-1",
            timelineStartMs=0,
            sourceInMs=0,
            sourceOutMs=5000,
            durationMs=4000,
        )


def test_tenant_object_key_guard() -> None:
    assert_safe_object_key("prod/10001/material/a.mp4", 10001)
    with pytest.raises(ValueError):
        assert_safe_object_key("prod/10002/material/a.mp4", 10001)
    with pytest.raises(ValueError):
        assert_safe_object_key("prod/10001/../10002/a.mp4", 10001)

