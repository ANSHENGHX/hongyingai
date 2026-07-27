from __future__ import annotations

import hashlib

import pytest

from hongying_ai.domain.models import (
    AssetManifestEntry,
    Canvas,
    Clip,
    InputManifest,
    Timeline,
    Track,
    TrackType,
)


@pytest.fixture
def manifest() -> InputManifest:
    return InputManifest(
        tenantId=10001,
        assets=(
            AssetManifestEntry(
                assetId="asset-1",
                objectKey="prod/10001/material/asset-1/v1/original.mp4",
                sha256=hashlib.sha256(b"asset-1").hexdigest(),
                durationMs=10_000,
                sizeBytes=1_000_000,
                licenseId="license-1",
            ),
            AssetManifestEntry(
                assetId="asset-2",
                objectKey="prod/10001/material/asset-2/v1/original.mp4",
                sha256=hashlib.sha256(b"asset-2").hexdigest(),
                durationMs=10_000,
                sizeBytes=1_000_000,
                licenseId="license-2",
            ),
        ),
    )


@pytest.fixture
def timeline() -> Timeline:
    return Timeline(
        durationMs=10_000,
        canvas=Canvas(width=1080, height=1920),
        tracks=(
            Track(
                id="v1",
                type=TrackType.VIDEO,
                clips=(
                    Clip(
                        id="clip-1",
                        assetId="asset-1",
                        timelineStartMs=0,
                        sourceInMs=0,
                        sourceOutMs=5000,
                        durationMs=5000,
                    ),
                    Clip(
                        id="clip-2",
                        assetId="asset-2",
                        timelineStartMs=5000,
                        sourceInMs=0,
                        sourceOutMs=5000,
                        durationMs=5000,
                    ),
                ),
            ),
        ),
    )
