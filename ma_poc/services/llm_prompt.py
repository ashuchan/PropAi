"""Pure, side-effect-free helpers for LLM prompt building and response parsing.

These functions contain no I/O, no async, and no external dependencies beyond
the stdlib — making them fully unit-testable without mocking.

Extracted from services/llm_extractor.py (PR-3).
"""

from __future__ import annotations

import json
import re
from typing import Any

# Unit-like field names used to rank API responses by relevance.
UNIT_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "rent",
        "price",
        "sqft",
        "bed",
        "bath",
        "available",
        "unit",
        "floor",
        "plan",
        "bedroom",
        "bathroom",
        "floorplan",
        "floorPlan",
        "unitNumber",
        "unit_number",
        "asking_rent",
        "market_rent",
        "availability",
        "availableDate",
        "available_date",
    }
)

_RENT_SANITY_MIN = 200
_RENT_SANITY_MAX = 50_000

_VALID_STATUSES = frozenset({"AVAILABLE", "UNAVAILABLE", "WAITLIST", "UNKNOWN"})


def rank_api_responses(api_responses: list[dict]) -> list[dict]:
    """Rank API responses by overlap with unit-like field names. Return top 3."""
    if not api_responses:
        return []

    def _score(resp: dict) -> int:
        body = resp.get("body")
        if not body:
            return 0
        text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        return sum(1 for key in UNIT_SIGNAL_KEYS if key in text.lower())

    scored = [(resp, _score(resp)) for resp in api_responses]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [resp for resp, s in scored[:3] if s > 0]


def parse_llm_response(text: str) -> dict[str, Any]:
    """Parse LLM response, handling markdown fences and other formatting."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return {"units": [], "profile_hints": {}}


def normalize_units(raw_units: list[dict]) -> list[dict]:
    """Normalize LLM-extracted units through parse and sanity-check functions."""
    normalized: list[dict] = []
    for u in raw_units:
        unit: dict[str, Any] = {}

        unit["unit_id"] = u.get("unit_id") or u.get("unit_number")
        unit["floor_plan_name"] = u.get("floor_plan_name") or u.get("floor_plan_type")

        beds = u.get("bedrooms")
        if beds is not None:
            try:
                unit["bedrooms"] = int(float(beds))
            except (ValueError, TypeError):
                unit["bedrooms"] = None
        else:
            unit["bedrooms"] = None

        baths = u.get("bathrooms")
        if baths is not None:
            try:
                unit["bathrooms"] = float(baths)
            except (ValueError, TypeError):
                unit["bathrooms"] = None
        else:
            unit["bathrooms"] = None

        sqft = u.get("sqft")
        if sqft is not None:
            try:
                unit["sqft"] = int(float(sqft))
            except (ValueError, TypeError):
                unit["sqft"] = None
        else:
            unit["sqft"] = None

        rent_low = u.get("market_rent_low") or u.get("asking_rent")
        rent_high = u.get("market_rent_high") or rent_low
        if rent_low is not None:
            try:
                unit["market_rent_low"] = float(rent_low)
            except (ValueError, TypeError):
                unit["market_rent_low"] = None
        else:
            unit["market_rent_low"] = None

        if rent_high is not None:
            try:
                unit["market_rent_high"] = float(rent_high)
            except (ValueError, TypeError):
                unit["market_rent_high"] = None
        else:
            unit["market_rent_high"] = None

        for key in ("market_rent_low", "market_rent_high"):
            val = unit.get(key)
            if val is not None and (val < _RENT_SANITY_MIN or val > _RENT_SANITY_MAX):
                unit[key] = None

        unit["available_date"] = u.get("available_date")
        status = u.get("availability_status", "UNKNOWN")
        if isinstance(status, str):
            status = status.upper()
            if status not in _VALID_STATUSES:
                status = "UNKNOWN"
        unit["availability_status"] = status

        conf = u.get("confidence", 0.7)
        try:
            unit["confidence"] = max(0.0, min(1.0, float(conf)))
        except (ValueError, TypeError):
            unit["confidence"] = 0.5

        # Pass-through fields the schema_v2 mapper consumes downstream.
        # The LLM prompts ask for these on every record; previously the
        # normalizer dropped them, so concessions and amenities never
        # reached the units table even though storage exists for them.
        amenities_raw = u.get("amenities")
        if isinstance(amenities_raw, list):
            unit["amenities"] = [
                str(item).strip() for item in amenities_raw
                if isinstance(item, str) and item.strip()
            ] or None
        else:
            unit["amenities"] = None

        ctext = u.get("concession_text")
        unit["concession_text"] = ctext if isinstance(ctext, str) and ctext.strip() else None

        cval = u.get("concession_value")
        try:
            unit["concession_value"] = float(cval) if cval not in (None, "", "null") else None
        except (ValueError, TypeError):
            unit["concession_value"] = None

        csrc = u.get("concession_source")
        unit["concession_source"] = csrc if isinstance(csrc, str) and csrc.strip() else None

        # Floor-plan provenance — tells downstream identity matching whether
        # the LLM read the name off a confident source (page label) or
        # synthesized it (inferred). Used to gate identity stability.
        fpsrc = u.get("floor_plan_source")
        unit["floor_plan_source"] = fpsrc if isinstance(fpsrc, str) and fpsrc.strip() else None

        # Per-floor-plan availability count: when the page lists "5 units
        # available" instead of 5 individual rows, the LLM emits ONE record
        # with availability_count=5. Schema_v2 reads this field; without
        # the pass-through it always reached schema as None and we lost the
        # property's true unit count.
        ac = u.get("availability_count")
        try:
            unit["availability_count"] = int(ac) if ac not in (None, "", "null") else None
        except (ValueError, TypeError):
            unit["availability_count"] = None

        # Lease term + move-in date — schema_v2 expects them
        # (schema_v2.py: ``unit.get("lease_term") or unit.get("_lease_term")``).
        lt = u.get("lease_term")
        try:
            unit["lease_term"] = int(lt) if lt not in (None, "", "null") else None
        except (ValueError, TypeError):
            unit["lease_term"] = None

        mid = u.get("move_in_date")
        unit["move_in_date"] = mid if isinstance(mid, str) and mid.strip() else None

        if unit.get("unit_id") or unit.get("floor_plan_name") or unit.get("market_rent_low"):
            normalized.append(unit)

    return normalized
