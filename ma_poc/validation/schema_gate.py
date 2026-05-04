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

# F1: substantive-field quality gate — v2 canonical names and v1 legacy aliases.
SUBSTANTIVE_FIELDS: tuple[str, ...] = ("beds", "rent_low", "floor_plan_name", "area")
_LEGACY_SUBSTANTIVE_FIELDS: tuple[str, ...] = (
    "bedrooms",
    "asking_rent",
    "market_rent_low",
    "sqft",
    "floor_plan_type",
)


def _is_present(value: Any) -> bool:
    """Return True when a field carries a real value (not None, empty, or -1 sentinel)."""
    if value is None:
        return False
    if value == -1:  # area sentinel used when sqft is absent
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def is_substantive(unit: dict[str, Any]) -> bool:
    """Return True when at least one identifying/pricing field is present.

    Checks both v2 canonical names and v1 legacy aliases so that records
    from either schema generation pass the quality gate correctly.
    """
    all_keys = SUBSTANTIVE_FIELDS + _LEGACY_SUBSTANTIVE_FIELDS
    return any(_is_present(unit.get(k)) for k in all_keys)


def property_passes_quality_gate(units: list[dict[str, Any]], threshold: float = 0.5) -> bool:
    """Return True when >=threshold fraction of units are substantive.

    An empty list always fails — a property with no units has nothing to
    evaluate. A 50 % default allows a few legitimately sparse units alongside
    full records without triggering a false alarm.
    """
    if not units:
        return False
    good = sum(1 for u in units if is_substantive(u))
    return (good / len(units)) >= threshold


@dataclass(frozen=True)
class SchemaGateResult:
    """Result of validating one unit record."""

    accepted: dict[str, Any] | None = None  # Populated on accept
    rejection_reasons: list[str] = field(default_factory=list)
    inferred_id: bool = False


# H5: a record qualifies as having a physical signal when ANY of these
# v2-canonical fields (or v1 aliases) carries a real (non-empty, non -1) value.
_PHYSICAL_SIGNAL_FIELDS: tuple[str, ...] = (
    "floor_plan_name",
    "floor_plan_type",
    "floorplan_name",
    "floor_plan_id",
    "beds",
    "bedrooms",
    "_bedrooms",
    "baths",
    "bathrooms",
    "_bathrooms",
    "sqft",
    "area",
    "_sqft",
    "asking_rent",
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "rent",
)


def _has_physical_signal(record: dict[str, Any]) -> bool:
    """H5: True when at least one identity-bearing physical field is present.

    A record carrying nothing but ``unit_id`` is rejected by ``check`` —
    a unit number alone is not enough identity to merge confidently.
    """
    return any(_is_present(record.get(k)) for k in _PHYSICAL_SIGNAL_FIELDS)


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

    # Sqft validation. H2: the -1 sentinel is "unknown sqft" — treat as null,
    # not as a real negative value (which would otherwise fire
    # INVALID_SQFT_NEGATIVE on every record where the source CSV omitted area).
    sqft = record.get("sqft")
    if sqft in (None, "", -1, "-1"):
        sqft = record.get("square_feet")
    if sqft not in (None, "", -1, "-1"):
        try:
            sqft_val = float(sqft)
            if sqft_val == -1:
                pass  # sentinel → treat as null
            elif sqft_val < 0:
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
    else:
        # H5: a record carrying ONLY unit_id (no rent, no beds, no plan name,
        # no sqft, no anything else identity-bearing) cannot be merged
        # confidently. Reject before the IDENTITY_FALLBACK_INSUFFICIENT path
        # so the failure is diagnosable.
        if not _has_physical_signal(record):
            reasons.append("IDENTITY_REQUIRES_PHYSICAL_SIGNAL")

    if reasons:
        return SchemaGateResult(accepted=None, rejection_reasons=reasons)

    return SchemaGateResult(accepted=record, inferred_id=inferred)
