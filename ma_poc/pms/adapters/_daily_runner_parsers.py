"""Bridge to daily_runner's pure-data parsers for Jugnu adapter reuse.

Re-exports the host-aware parsers from scripts/entrata.py and
scripts/scrape_properties.py so adapters have a single source of truth with
daily_runner. Functions exposed here operate on raw JSON bodies only — no
Playwright page arguments — even though they live in modules that do import
Playwright at module load time for other reasons.

Output shape matches what Jugnu adapters already emit (`floor_plan_name`,
`bed_label`, `rent_range`, etc. — the `make_unit_dict` schema), not the
`unit_id`/`market_rent_low`/`market_rent_high` internal shape used by
daily_runner's property-record builder.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure scripts/ is importable (scripts live at ma_poc/scripts/…).
_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# parse_api_responses is the 50+ key-variant parser with SightMap host
# routing and nested-envelope unwrapping. Lives in entrata.py. Emits the
# adapter-compatible shape.
from entrata import (  # noqa: E402
    TARGET_JSONLD_TYPES,
    _jsonld_floor_size,
    _jsonld_item_has_unit_signal,
    _parse_sightmap_payload as _sightmap_adapter_shape,
    _walk_jsonld,
    parse_api_responses,
)

# _realpage_units_from_body lives in scrape_properties.py and handles the
# RealPage /units endpoint (which can return null / [] / {response: [...]}).
# It emits the *internal* shape (unit_id / market_rent_low / market_rent_high),
# not the adapter shape. Callers must translate before adding to AdapterResult.
from scrape_properties import (  # noqa: E402
    _RENT_MAX,
    _RENT_MIN,
    _RENT_KEYS,
    _UNIT_ID_KEYS,
    _extract_rent,
    _money_to_int,
    _realpage_units_from_body as _realpage_units_internal_shape,
)


def parse_sightmap_payload(body: Any, url: str) -> list[dict[str, Any]]:
    """SightMap floorplan+unit join, adapter-compatible output shape."""
    return _sightmap_adapter_shape(body, url)


def realpage_units_to_adapter_shape(body: Any, url: str) -> list[dict[str, Any]]:
    """RealPage /units endpoint parser translated to the adapter shape.

    daily_runner's ``_realpage_units_from_body`` returns records keyed by
    ``unit_id``/``market_rent_low``/``market_rent_high``/``available_date``.
    Adapters emit ``unit_number``/``rent_range``/``availability_date``/etc.
    Translate here so callers can drop the output straight into
    ``AdapterResult.units``.

    Null / empty bodies return ``[]`` without raising.
    """
    internal = _realpage_units_internal_shape(body, url) or []
    out: list[dict[str, Any]] = []
    for u in internal:
        lo = u.get("market_rent_low")
        hi = u.get("market_rent_high")
        if lo is not None and hi is not None and lo != hi:
            rent_range = f"${lo:,} - ${hi:,}"
        elif lo is not None:
            rent_range = f"${lo:,}"
        else:
            rent_range = ""

        beds = u.get("_bedrooms")
        name = u.get("_floor_plan") or ""
        if beds == 0 or (not beds and "studio" in str(name).lower()):
            bed_label = "Studio"
        elif beds not in (None, ""):
            bed_label = f"{beds} Bedroom"
        else:
            bed_label = ""

        sqft = u.get("_sqft")
        out.append({
            "floor_plan_name":    str(name),
            "bed_label":          bed_label,
            "bedrooms":           str(beds) if beds not in (None, "") else "",
            "bathrooms":          "",
            "sqft":               str(sqft) if sqft is not None else "",
            "unit_number":        str(u.get("unit_id") or ""),
            "floor":              "",
            "building":           "",
            "rent_range":         rent_range,
            "deposit":            "",
            "concession":         str(u.get("concessions") or ""),
            "availability_status": "AVAILABLE",
            "available_units":    "",
            "availability_date":  str(u.get("available_date") or ""),
            "source_api_url":     url,
            "extraction_tier":    "TIER_1_API",
        })
    return out


__all__ = [
    "parse_api_responses",
    "parse_sightmap_payload",
    "realpage_units_to_adapter_shape",
    "TARGET_JSONLD_TYPES",
    "_jsonld_floor_size",
    "_jsonld_item_has_unit_signal",
    "_walk_jsonld",
    "_UNIT_ID_KEYS",
    "_RENT_KEYS",
    "_extract_rent",
    "_money_to_int",
    "_RENT_MIN",
    "_RENT_MAX",
]
