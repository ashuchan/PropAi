"""Identity fallback — deterministic SHA256-based unit ID when natural key is missing.

Addresses the 1,014 UNIT_MISSING_ID warnings from the 04-17 run by computing
a stable fingerprint from floor_plan + bedrooms + bathrooms + sqft + rent.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def compute_fallback_id(record: dict[str, Any]) -> str | None:
    """Compute a deterministic fallback unit_id from a unit record.

    Hash input tuple (order matters — do not change without bumping a migration):
      (normalised_floor_plan_name, bedrooms, bathrooms, sqft_rounded_to_10,
       rent_rounded_to_25)

    If any of {floor_plan, bedrooms} is missing, returns None.
    Prefix returned ID with 'inferred_' so it's distinguishable in reports.

    Args:
        record: Unit record dict.

    Returns:
        Fallback ID string (e.g. 'inferred_a3b4c5d6e7f8'), or None.
    """
    floor_plan = record.get("floor_plan_type") or record.get("floorplan_name")
    bedrooms = record.get("bedrooms") or record.get("beds")

    if not floor_plan:
        return None
    if bedrooms is None:
        return None

    normalised_plan = _normalise_floor_plan(str(floor_plan))
    bathrooms = record.get("bathrooms") or record.get("baths") or 0

    sqft = record.get("sqft") or record.get("square_feet") or 0
    sqft_rounded = _round_to(int(sqft), 10)

    rent = (
        record.get("asking_rent")
        or record.get("market_rent_low")
        or record.get("rent")
        or 0
    )
    rent_rounded = _round_to(int(rent), 25)

    hash_input = (
        normalised_plan,
        str(bedrooms),
        str(bathrooms),
        str(sqft_rounded),
        str(rent_rounded),
    )
    digest = hashlib.sha256("|".join(hash_input).encode()).hexdigest()[:12]
    return f"inferred_{digest}"


def compute_fallback_unit_id(unit: dict[str, Any], property_id: str) -> str | None:
    """Compute a stable SHA256-based unit_id from v2 unit fields when no natural key exists.

    Uses the v2 field names (beds, baths, area, rent_low, floor_plan_name, available_date).
    Returns None when fewer than 2 non-empty identifying fields are present — not enough
    content to produce a meaningful stable hash.

    Prefix 'inferred_' marks the ID as non-natural for downstream consumers.
    """
    def _n(v: Any) -> str:
        if v is None:
            return ""
        if v == -1:  # area sentinel
            return ""
        return str(v).strip().lower()

    parts = [
        property_id or "",
        _n(unit.get("floor_plan_name")),
        _n(unit.get("beds")),
        _n(unit.get("baths")),
        _n(unit.get("area")),
        _n(unit.get("rent_low")),
        _n(unit.get("available_date")),
    ]
    identifying = parts[1:]  # exclude property_id itself
    if sum(1 for p in identifying if p) < 2:
        return None
    blob = "|".join(parts)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return f"inferred_{digest}"


def _normalise_floor_plan(name: str) -> str:
    """Normalise floor plan name for consistent hashing.

    Args:
        name: Raw floor plan name.

    Returns:
        Lowercased, whitespace-collapsed string.
    """
    return re.sub(r"\s+", " ", name.strip().lower())


def _round_to(value: int, step: int) -> int:
    """Round a value to the nearest step.

    Args:
        value: Integer value.
        step: Rounding step.

    Returns:
        Rounded integer.
    """
    if step <= 0:
        return value
    return round(value / step) * step
