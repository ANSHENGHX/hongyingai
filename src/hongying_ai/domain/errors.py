from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_COMMAND = "AI_INVALID_COMMAND"
    MEDIA_UNSUPPORTED = "AI_MEDIA_UNSUPPORTED"
    TIMELINE_INVALID = "AI_TIMELINE_INVALID"
    MODEL_UNAVAILABLE = "AI_MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "AI_MODEL_OUTPUT_INVALID"
    OBJECT_STORE_UNAVAILABLE = "AI_OBJECT_STORE_UNAVAILABLE"
    RESOURCE_EXHAUSTED = "AI_RESOURCE_EXHAUSTED"
    RENDER_FAILED = "AI_RENDER_FAILED"
    QUALITY_REJECTED = "AI_QUALITY_REJECTED"
    CANCELLED = "AI_CANCELLED"
    TIMEOUT = "AI_TIMEOUT"
    RUN_NOT_FOUND = "AI_RUN_NOT_FOUND"
    INTERNAL_ERROR = "AI_INTERNAL_ERROR"


@dataclass(slots=True)
class PlatformError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: list[dict[str, Any]] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message


class TimelineInvalid(PlatformError):
    def __init__(self, message: str, details: list[dict[str, Any]]) -> None:
        super().__init__(ErrorCode.TIMELINE_INVALID, message, False, details)

