from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from hongying_ai.domain.models import ContractModel, InputManifest, OutputProfile, TaskSnapshot, Timeline


class EventEnvelope(ContractModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    event_type: str
    event_version: Literal["1.0"] = "1.0"
    tenant_id: int = Field(gt=0)
    trace_id: str
    task_id: int | None = None
    run_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]


class AssetAnalyzeCommand(ContractModel):
    command_id: str
    command_type: Literal["AI_ASSET_ANALYZE_REQUESTED"] = "AI_ASSET_ANALYZE_REQUESTED"
    command_version: Literal["1.0"] = "1.0"
    tenant_id: int
    trace_id: str
    task_id: int | None = None
    material_version: str
    asset_id: str
    object_key: str
    sha256: str
    size_bytes: int
    analysis_profile: Literal["FAST", "STANDARD", "DEEP"] = "STANDARD"


class PlanGenerateCommand(ContractModel):
    command_id: str
    command_type: Literal["AI_PLAN_GENERATE_REQUESTED"] = "AI_PLAN_GENERATE_REQUESTED"
    command_version: Literal["1.0"] = "1.0"
    tenant_id: int
    trace_id: str
    task_snapshot: TaskSnapshot
    user_goal: str
    industry: str
    brand_knowledge: dict[str, Any] = Field(default_factory=dict)
    candidate_assets: InputManifest


class RenderCommand(ContractModel):
    command_id: str
    command_type: Literal["VIDEO_RENDER_REQUESTED"] = "VIDEO_RENDER_REQUESTED"
    command_version: Literal["1.0"] = "1.0"
    tenant_id: int
    trace_id: str
    task_id: int
    run_id: str
    run_no: int = Field(ge=1)
    timeline: Timeline
    input_manifest: InputManifest
    output_profile: OutputProfile
    priority: Literal["high", "normal"] = "normal"
    deadline: datetime | None = None


class QualityCommand(ContractModel):
    command_id: str
    command_type: Literal["VIDEO_QUALITY_REQUESTED"] = "VIDEO_QUALITY_REQUESTED"
    command_version: Literal["1.0"] = "1.0"
    tenant_id: int
    trace_id: str
    task_id: int
    run_id: str
    output_object_key: str
    expected_profile: OutputProfile
    policy_version: str = "quality-v1"


class CancelCommand(ContractModel):
    command_id: str
    command_type: Literal["VIDEO_RUN_CANCEL_REQUESTED"] = "VIDEO_RUN_CANCEL_REQUESTED"
    command_version: Literal["1.0"] = "1.0"
    tenant_id: int
    trace_id: str
    task_id: int
    run_id: str
    reason: str = Field(min_length=1, max_length=500)
    requested_by: str = Field(min_length=1, max_length=128)
