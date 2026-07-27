from __future__ import annotations

from typing import Any

from pydantic import Field

from hongying_ai.domain.models import ContractModel, InputManifest, OutputProfile, Timeline


class ApiResponse[T](ContractModel):
    code: str = "SUCCESS"
    request_id: str
    data: T


class ErrorBody(ContractModel):
    path: str | None = None
    code: str
    message: str


class ApiErrorResponse(ContractModel):
    code: str
    request_id: str
    message: str
    errors: tuple[ErrorBody, ...] = ()


class MediaProbeRequest(ContractModel):
    schema_version: str = "1.0"
    asset_id: str | None = None
    object_key: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(gt=0)


class TimelineValidateRequest(ContractModel):
    schema_version: str = "1.0"
    task_id: int = Field(gt=0)
    timeline: Timeline
    input_manifest: InputManifest | None = None


class TimelineValidateResult(ContractModel):
    valid: bool
    errors: tuple[ErrorBody, ...] = ()


class RenderPreflightRequest(ContractModel):
    schema_version: str = "1.0"
    task_id: int = Field(gt=0)
    timeline: Timeline
    input_manifest: InputManifest
    output_profile: OutputProfile | None = None


class RenderPreflightResult(ContractModel):
    accepted: bool
    estimated_seconds: int
    estimated_disk_bytes: int
    complexity_score: float
    encoder: str
    warnings: tuple[str, ...] = ()


class CancelRequest(ContractModel):
    reason: str = Field(min_length=1, max_length=500)
    requested_by: str = Field(min_length=1, max_length=128)


class ReadinessResult(ContractModel):
    ready: bool
    dependencies: dict[str, bool]
    version: str = "1.0.0"


class ProbeResult(ContractModel):
    profile: dict[str, Any]
