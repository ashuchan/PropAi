"""
UnitRecord — canonical unit record. Forward-compatible with Phase B PostgreSQL.

Acceptance criteria (CLAUDE.md):
- Phase A populates only extractable fields
- effective_rent / concession / days_on_market / availability_periods are None
  in Phase A — Phase B PR-07 fills them
- Field names are FROZEN — Phase B imports this model directly
- confidence_score in [0.0, 1.0]
"""
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class DataQualityFlag(str, Enum):
    CLEAN = "CLEAN"
    SMOOTHED = "SMOOTHED"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    QA_HELD = "QA_HELD"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UnitRecord(BaseModel):
    unit_id: Optional[str] = None  # Phase B PR-06 entity resolution
    property_id: str
    unit_number: str
    floor_plan_id: Optional[str] = None
    floor: Optional[int] = None
    building: Optional[str] = None
    sqft: Optional[int] = None
    floor_plan_type: Optional[str] = None  # "1/1", "2/2", "Studio"
    asking_rent: Optional[float] = None
    effective_rent: Optional[float] = None  # Phase B PR-07
    concession: Optional[dict[str, Any]] = None  # Phase B PR-07
    availability_status: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    availability_date: Optional[date] = None
    days_on_market: Optional[int] = None  # Phase B PR-07
    availability_periods: list[dict[str, Any]] = Field(default_factory=list)  # Phase B PR-07
    scrape_timestamp: datetime = Field(default_factory=_utcnow)
    extraction_tier: Optional[int] = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    data_quality_flag: DataQualityFlag = DataQualityFlag.CLEAN
    source: str = "DIRECT_SITE"
    carryforward_days: int = 0
