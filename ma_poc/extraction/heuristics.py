"""Shared heuristics for detecting unit-like API responses and URLs.

Consolidated from duplicates that existed in scripts/entrata.py and various
adapter files. All callers should import from here — do not duplicate.
"""

from __future__ import annotations

from typing import Any

_AVAILABILITY_URL_SIGNALS = (
    "/availab",
    "/floor-plan",
    "/floorplan",
    "/pricing",
    "/units",
    "/apartments",
    "/rent",
    "/leasing",
    "/availability",
)

_UNIT_KEY_SIGNALS = frozenset(
    {
        "unit",
        "floor",
        "plan",
        "rent",
        "price",
        "sqft",
        "bed",
        "bath",
        "available",
        "lease",
        "bedroom",
        "bathroom",
        "apartment",
    }
)


def looks_like_availability_api(url: str) -> bool:
    """Return True when the URL path pattern suggests it returns availability data."""
    if not url:
        return False
    lower = url.lower()
    return any(sig in lower for sig in _AVAILABILITY_URL_SIGNALS)


def response_looks_like_units(body: Any) -> bool:
    """Return True when an API response body looks like it contains unit data.

    Checks for unit-like keys at the top level and one level down.
    Never raises on malformed input.
    """
    if not body:
        return False
    try:
        text = str(body).lower()
        return any(k in text for k in _UNIT_KEY_SIGNALS)
    except Exception:
        return False


def _url_contains_unit_signal(url: str) -> bool:
    """Return True when the URL contains at least one unit-data signal substring."""
    return looks_like_availability_api(url)
