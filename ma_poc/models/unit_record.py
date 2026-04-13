"""
UnitRecord — canonical unit record. Forward-compatible with Phase B PostgreSQL.

Acceptance criteria (CLAUDE.md):
- Phase A populates only extractable fields
- effective_rent / concession / days_on_market / availability_periods are None
  in Phase A — Phase B PR-07 fills them
- Field names are FROZEN — Phase B imports this model directly
- confidence_score in [0.0, 1.0]
"""
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AvailabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class DataQualityFlag(StrEnum):
    CLEAN = "CLEAN"
    SMOOTHED = "SMOOTHED"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    QA_HELD = "QA_HELD"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UnitRecord(BaseModel):
    unit_id: str | None = None  # Phase B PR-06 entity resolution
    property_id: str
    unit_number: str
    floor_plan_id: str | None = None
    floor: int | None = None
    building: str | None = None
    sqft: int | None = None
    floor_plan_type: str | None = None  # "1/1", "2/2", "Studio"
    asking_rent: float | None = None
    effective_rent: float | None = None  # Phase B PR-07
    concession: dict[str, Any] | None = None  # Phase B PR-07
    availability_status: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    availability_date: date | None = None
    days_on_market: int | None = None  # Phase B PR-07
    availability_periods: list[dict[str, Any]] = Field(default_factory=list)  # Phase B PR-07
    scrape_timestamp: datetime = Field(default_factory=_utcnow)
    extraction_tier: int | None = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    data_quality_flag: DataQualityFlag = DataQualityFlag.CLEAN
    source: str = "DIRECT_SITE"
    carryforward_days: int = 0
