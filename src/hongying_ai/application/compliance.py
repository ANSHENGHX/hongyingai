from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from hongying_ai.domain.models import CreativeBrief, InputManifest, Storyboard


@dataclass(frozen=True, slots=True)
class ComplianceResult:
    decision: Literal["PASS", "BLOCK", "MANUAL_REVIEW"]
    reasons: tuple[str, ...]
    policy_version: str = "compliance-v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def review_plan(
    brief: CreativeBrief,
    storyboard: Storyboard,
    manifest: InputManifest,
    brand_knowledge: dict[str, Any],
) -> ComplianceResult:
    text = " ".join(
        (
            brief.objective,
            brief.cta,
            *brief.selling_points,
            *(shot.narration for shot in storyboard.shots),
        )
    ).casefold()
    forbidden = tuple(
        str(item).strip()
        for item in brand_knowledge.get("forbiddenWords", [])
        if str(item).strip()
    )
    matched = tuple(item for item in forbidden if item.casefold() in text)
    if matched:
        return ComplianceResult(
            decision="BLOCK",
            reasons=tuple(f"命中品牌禁用词：{item}" for item in matched),
        )
    high_risk_terms = ("保证治愈", "绝对安全", "稳赚不赔", "未成年人联系方式")
    risks = tuple(item for item in high_risk_terms if item.casefold() in text)
    if risks:
        return ComplianceResult(
            decision="BLOCK",
            reasons=tuple(f"命中高风险表达：{item}" for item in risks),
        )
    unlicensed = tuple(asset.asset_id for asset in manifest.assets if not asset.license_id)
    if unlicensed:
        return ComplianceResult(
            decision="MANUAL_REVIEW",
            reasons=(f"素材缺少 licenseId：{', '.join(unlicensed)}",),
        )
    return ComplianceResult(decision="PASS", reasons=())

