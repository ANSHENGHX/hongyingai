from __future__ import annotations

from hongying_ai.application.compliance import review_plan
from hongying_ai.domain.models import CreativeBrief, InputManifest, Storyboard, StoryboardShot


def _plan(text: str) -> tuple[CreativeBrief, Storyboard]:
    brief = CreativeBrief(
        audience="顾客",
        objective=text,
        sellingPoints=("真实卖点",),
        cta="立即了解",
    )
    storyboard = Storyboard(
        title="测试",
        cta="立即了解",
        shots=(
            StoryboardShot(
                id="shot-1",
                narration=text,
                visualIntent="展示产品",
                durationMs=3000,
                assetQuery="产品",
            ),
        ),
    )
    return brief, storyboard


def test_forbidden_brand_word_blocks_plan(manifest: InputManifest) -> None:
    brief, storyboard = _plan("这是一个禁用承诺")
    result = review_plan(
        brief,
        storyboard,
        manifest,
        {"forbiddenWords": ["禁用承诺"]},
    )
    assert result.decision == "BLOCK"


def test_missing_license_requires_manual_review(manifest: InputManifest) -> None:
    brief, storyboard = _plan("正常营销内容")
    first = manifest.assets[0].model_copy(update={"license_id": None})
    changed = manifest.model_copy(update={"assets": (first,)})
    result = review_plan(brief, storyboard, changed, {})
    assert result.decision == "MANUAL_REVIEW"

