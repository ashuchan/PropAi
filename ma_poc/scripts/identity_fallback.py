"""Identity fallback — deterministic SHA256-based unit ID when natural key is missing.

Two functions:
  compute_fallback_unit_id  — v2, stable (physical attributes only, NO rent, NO date)
  compute_fallback_id       — v1, legacy (kept for backward compat, still rent-volatile)

RULE: unit identity = what the unit IS physically (floor_plan, beds, baths, sqft).
      Never hash rent or available_date — those are mutable attributes, not identity fields.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def compute_fallback_unit_id(unit: dict[str, Any], property_id: str) -> str | None:
    """Compute a stable SHA256-based unit_id from physical attributes only.

    Hash inputs (rent and available_date deliberately excluded):
        property_id | floor_plan_name | beds | baths | sqft_rounded_to_10

    Accepts both v2 canonical field names (floor_plan_name, beds, baths, area)
    AND the _-prefixed scrape_properties names (_floor_plan, _bedrooms, _bathrooms, _sqft)
    so this function can be called from scrape_properties._add() BEFORE the
    daily_runner _ stripping occurs.

    Returns None when fewer than 2 non-empty identifying fields are present.
    Prefix 'inferred_' marks the ID as non-natural.
    """
    def _n(v: Any) -> str:
        if v is None or v == -1:   # -1 is the area sentinel — treat as absent
            return ""
        return re.sub(r"\s+", " ", str(v).strip().lower())

    # Accept both canonical and _-prefixed field names
    floor_plan = (
        _n(unit.get("floor_plan_name"))
        or _n(unit.get("_floor_plan"))
        or _n(unit.get("floor_plan_type"))
        or _n(unit.get("floorplan_name"))
    )
    beds = (
        _n(unit.get("beds"))
        or _n(unit.get("bedrooms"))
        or _n(unit.get("_bedrooms"))
    )
    baths = (
        _n(unit.get("baths"))
        or _n(unit.get("bathrooms"))
        or _n(unit.get("_bathrooms"))
    )

    raw_sqft = (
        unit.get("_sqft")
        if unit.get("_sqft") is not None
        else unit.get("area")
        if unit.get("area") is not None
        else unit.get("sqft")
        if unit.get("sqft") is not None
        else unit.get("square_feet")
    )
    sqft_bucket = ""
    if raw_sqft is not None and raw_sqft != -1:
        try:
            a = int(float(str(raw_sqft)))
            sqft_bucket = str(round(a / 10) * 10) if a > 0 else ""
        except (TypeError, ValueError):
            sqft_bucket = ""

    # Require floor_plan AND at least one other identifying field
    identifying = [floor_plan, beds, baths, sqft_bucket]
    if not floor_plan or sum(1 for p in identifying if p) < 2:
        return None

    parts = [property_id or "", floor_plan, beds, baths, sqft_bucket]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"inferred_{digest}"


def compute_fallback_id(record: dict[str, Any]) -> str | None:
    """Legacy v1 fallback — retained for backward compatibility with existing state files.

    WARNING: includes rent in hash (rounded to nearest $25) — rent-volatile.
    Do not use for new code. Migrate callers to compute_fallback_unit_id.

    Hash input tuple:
        (normalised_floor_plan_name, bedrooms, bathrooms, sqft_rounded_10, rent_rounded_25)
    """
    floor_plan = record.get("floor_plan_type") or record.get("floorplan_name")
    bedrooms = record.get("bedrooms") or record.get("beds")

    if not floor_plan:
        return None
    if bedrooms is None:
        return None

    normalised_plan = re.sub(r"\s+", " ", str(floor_plan).strip().lower())
    bathrooms = record.get("bathrooms") or record.get("baths") or 0
    sqft = record.get("sqft") or record.get("square_feet") or 0
    sqft_rounded = _round_to(int(sqft), 10)
    rent = record.get("asking_rent") or record.get("market_rent_low") or record.get("rent") or 0
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


def _round_to(value: int, step: int) -> int:
    if step <= 0:
        return value
    return round(value / step) * step
