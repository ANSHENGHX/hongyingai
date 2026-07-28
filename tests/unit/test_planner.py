from __future__ import annotations

import hashlib
from typing import Any

import pytest

from hongying_ai.application.planner import PlannerService
from hongying_ai.domain.models import AssetManifestEntry, InputManifest, TaskSnapshot


class ShortStoryboardModel:
    async def structured_output(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {
                "creativeBrief": {
                    "audience": "附近顾客",
                    "objective": "生成 20 秒宣传视频",
                    "tone": "真实、有行动感",
                    "sellingPoints": ["品牌形象", "门店服务"],
                    "cta": "到店看看",
                    "brandRules": [],
                    "sources": [],
                },
                "storyboard": {
                    "title": "短分镜",
                    "cta": "到店看看",
                    "riskNotes": [],
                    "shots": [
                        {
                            "id": "shot_001",
                            "narration": "品牌形象",
                            "visualIntent": "展示门店",
                            "durationMs": 3000,
                            "assetQuery": "门店 品牌",
                        },
                        {
                            "id": "shot_002",
                            "narration": "门店服务",
                            "visualIntent": "展示服务",
                            "durationMs": 3000,
                            "assetQuery": "服务 环境",
                        },
                        {
                            "id": "shot_003",
                            "narration": "到店看看",
                            "visualIntent": "收尾 CTA",
                            "durationMs": 3000,
                            "assetQuery": "门店 CTA",
                        },
                    ],
                },
            },
            {"provider": "fake", "model": "short-storyboard"},
        )


@pytest.mark.asyncio
async def test_planner_extends_short_model_storyboard_to_target_duration() -> None:
    planner = PlannerService(ShortStoryboardModel())
    manifest = InputManifest(
        tenantId=10001,
        assets=(
            AssetManifestEntry(
                assetId="asset-video-1",
                objectKey="prod/10001/material/asset-video-1/v1/original.mp4",
                sha256=hashlib.sha256(b"asset-video-1").hexdigest(),
                durationMs=8000,
                sizeBytes=1_000_000,
                licenseId="license-1",
                mediaType="video",
                hasAudio=True,
            ),
            AssetManifestEntry(
                assetId="asset-video-2",
                objectKey="prod/10001/material/asset-video-2/v1/original.mp4",
                sha256=hashlib.sha256(b"asset-video-2").hexdigest(),
                durationMs=8000,
                sizeBytes=1_000_000,
                licenseId="license-2",
                mediaType="video",
                hasAudio=True,
            ),
        ),
    )

    _brief, storyboard, timeline, _meta = await planner.generate(
        snapshot=TaskSnapshot(taskId=90001, tenantId=10001),
        user_goal="生成 20 秒品牌宣传视频",
        industry="品牌宣传",
        brand_knowledge={"sellingPoints": ["品牌形象", "门店服务"], "sourceIds": []},
        assets=manifest,
    )

    assert sum(shot.duration_ms for shot in storyboard.shots) == 30_000
    assert timeline.duration_ms == 30_000
    assert len(storyboard.shots) > 3
