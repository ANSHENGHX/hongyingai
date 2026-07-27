from __future__ import annotations

from pydantic import Field

from hongying_ai.domain.models import AssetManifestEntry, ContractModel


class StudioGenerateRequest(ContractModel):
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str = Field(min_length=1, max_length=200)
    activity_id: str = Field(min_length=1, max_length=128)
    activity_title: str = Field(min_length=1, max_length=200)
    activity_type: str = Field(default="营销活动", max_length=100)
    user_goal: str = Field(min_length=1, max_length=500)
    template_id: str
    assets: tuple[AssetManifestEntry, ...] = Field(min_length=1)
    logo_asset_id: str | None = None
    bgm_asset_id: str | None = None
    selling_points: tuple[str, ...] = ()
    forbidden_words: tuple[str, ...] = ()
    use_ai: bool = True


class StudioGenerateResult(ContractModel):
    task_id: int
    run_id: str
    stage: str
    status_url: str


class StudioAssetResult(ContractModel):
    asset: AssetManifestEntry
    file_name: str
    thumbnail_url: str | None = None
    analysis_object_key: str
    analysis: dict
