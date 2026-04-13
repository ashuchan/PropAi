"""
ExtractionResult model — output of any extraction tier.

Acceptance criteria (CLAUDE.md PR-03):
- ExtractionTier enum with values 1..5 in priority order
- ExtractionStatus enum SUCCESS / FAILED / SKIPPED
- confidence_score in [0.0, 1.0] (Pydantic Field constraints)
- succeeded property: True only if status==SUCCESS AND confidence >= 0.7
- field_confidences and low_confidence_fields populated by tiers that report
  per-field confidence
"""
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExtractionTier(IntEnum):
    API_INTERCEPTION = 1
    JSON_LD = 2
    PLAYWRIGHT_TPL = 3
    LLM_GPT4O_MINI = 4
    VISION_FALLBACK = 5


class ExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # Change detection determined no change


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ExtractionResult(BaseModel):
    property_id: str
    tier: ExtractionTier | None = None
    status: ExtractionStatus
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    raw_fields: dict[str, Any] = Field(default_factory=dict)
    field_confidences: dict[str, float] = Field(default_factory=dict)
    low_confidence_fields: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utcnow)
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        """True only if status SUCCESS and confidence meets 0.7 threshold."""
        return self.status == ExtractionStatus.SUCCESS and self.confidence_score >= 0.7
