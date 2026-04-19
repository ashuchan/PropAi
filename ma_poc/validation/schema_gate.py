"""Schema gate — validates unit records against the UnitRecord Pydantic model.

Two paths:
  1. Strict: record has unit_id, rent, all required fields -> accept.
  2. Soft: record missing unit_id -> call identity_fallback; if fallback
     returns an id, accept with inferred_id=True; else reject.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .identity_fallback import compute_fallback_id

log = logging.getLogger(__name__)

_MAX_RENT = 50_000
_MAX_SQFT = 20_000


@dataclass(frozen=True)
class SchemaGateResult:
    """Result of validating one unit record."""

    accepted: dict[str, Any] | None = None  # Populated on accept
    rejection_reasons: list[str] = field(default_factory=list)
    inferred_id: bool = False


def check(record: dict[str, Any]) -> SchemaGateResult:
    """Validate a single unit record against the schema.

    Args:
        record: Raw unit record dict from L3 extraction.

    Returns:
        SchemaGateResult with accepted record or rejection reasons.
    """
    reasons: list[str] = []

    # Rent validation
    rent = record.get("asking_rent") or record.get("market_rent_low") or record.get("rent")
    if rent is not None:
        try:
            rent_val = float(rent)
            if rent_val < 0:
                reasons.append("INVALID_RENT_NEGATIVE")
            elif rent_val > _MAX_RENT:
                reasons.append("INVALID_RENT_ABSURD")
        except (ValueError, TypeError):
            reasons.append("INVALID_RENT_NEGATIVE")

    # Sqft validation
    sqft = record.get("sqft") or record.get("square_feet")
    if sqft is not None:
        try:
            sqft_val = float(sqft)
            if sqft_val < 0:
                reasons.append("INVALID_SQFT_NEGATIVE")
            elif sqft_val > _MAX_SQFT:
                reasons.append("INVALID_SQFT_ABSURD")
        except (ValueError, TypeError):
            pass

    # Date validation
    avail_date = record.get("availability_date") or record.get("available_date")
    if avail_date is not None and isinstance(avail_date, str):
        try:
            datetime.fromisoformat(avail_date.replace("Z", "+00:00"))
        except ValueError:
            try:
                date.fromisoformat(avail_date)
            except ValueError:
                reasons.append("INVALID_DATE_FORMAT")

    # Unit ID: if missing, try fallback
    unit_id = record.get("unit_id") or record.get("unit_number")
    inferred = False
    if not unit_id:
        fallback_id = compute_fallback_id(record)
        if fallback_id:
            record = dict(record)  # Don't mutate original
            record["unit_id"] = fallback_id
            inferred = True
        else:
            reasons.append("IDENTITY_FALLBACK_INSUFFICIENT")

    if reasons:
        return SchemaGateResult(accepted=None, rejection_reasons=reasons)

    return SchemaGateResult(accepted=record, inferred_id=inferred)
