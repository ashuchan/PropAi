"""
scripts/state/property_record.py
=================================
Build the target-schema property record for one property.

Extracted from scripts/daily_runner.py (lines 159-321).
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Make sibling script modules importable regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent.parent  # scripts/
_PROJECT_ROOT = _HERE.parent  # ma_poc/
for _p in (_HERE, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from identity import (  # noqa: E402
    ADDRESS_KEYS,
    CITY_KEYS,
    LAT_KEYS,
    LNG_KEYS,
    NAME_KEYS,
    PROPERTY_ID_KEYS,
    STATE_KEYS,
    UNIQUE_ID_KEYS,
    WEBSITE_KEYS,
    ZIP_KEYS,
    PropertyIdentity,
    csv_get,
)
from scrape_properties import (  # noqa: E402
    _clean,
    aggregate_unit_stats,
)

log = logging.getLogger("daily_runner")

# Full target-schema field list in the order requested by the user.
TARGET_PROPERTY_FIELDS = [
    "Property Name",
    "Type",
    "Unique ID",
    "Average Unit Size (SF)",
    "Property ID",
    "Census Block Id",
    "City",
    "Construction Finish Date",
    "Construction Start Date",
    "Development Company",
    "Latitude",
    "Longitude",
    "Management Company",
    "Market Name",
    "Property Owner",
    "Property Address",
    "Property Status",
    "Property Type",
    "Region",
    "Renovation Finish",
    "Renovation Start",
    "State",
    "Stories",
    "Submarket Name",
    "Total Units",
    "Tract Code",
    "Year Built",
    "ZIP Code",
    "Lease Start Date",
    "First Move-In Date",
    "Property Style",
    "Update Date",
    "Unit Mix",
    "Asset Grade in Submarket",
    "Asset Grade in Market",
    "Phone",
    "Website",
    "Property Image URL",
    "Property Gallery URLs",
]

# Field groups that are pass-through from CSV; runner never tries to extract them.
EXTERNAL_ONLY_FIELDS = {
    "Census Block Id",
    "Tract Code",
    "Construction Start Date",
    "Construction Finish Date",
    "Renovation Start",
    "Renovation Finish",
    "Development Company",
    "Property Owner",
    "Region",
    "Market Name",
    "Submarket Name",
    "Asset Grade in Submarket",
    "Asset Grade in Market",
    "Lease Start Date",
}


def _f(row: dict, *keys: str) -> Any:
    """Return cleaned CSV value or None."""
    return _clean(csv_get(row, *keys)) or None


def _num(row: dict, *keys: str) -> float | None:
    v = csv_get(row, *keys)
    if not v:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def build_property_record(
    row: dict,
    ident: PropertyIdentity,
    scrape_result: dict,
    target_units: list[dict],
    state_snapshot: dict | None,
    carry_forward_used: bool,
) -> dict:
    """
    Produce one target-schema property record. CSV values always take precedence
    for fields that exist in the CSV; scraped values fill in only for fields the
    CSV left blank. Computed aggregates (Average Unit Size, Unit Mix) come from
    today's target_units.
    """
    md = scrape_result.get("property_metadata") or {}
    stats = aggregate_unit_stats(target_units)

    def pick(csv_val: Any, scraped_val: Any) -> Any:
        return csv_val if csv_val not in (None, "", "null", "None") else _clean(scraped_val)

    rec: dict[str, Any] = {f: None for f in TARGET_PROPERTY_FIELDS}

    # ── Identity ─────────────────────────────────────────────────────────────
    rec["Unique ID"] = _f(row, *UNIQUE_ID_KEYS) or ident.canonical_id
    rec["Property ID"] = _f(row, *PROPERTY_ID_KEYS) or ident.canonical_id

    # ── Identity + name ─────────────────────────────────────────────────────
    rec["Property Name"] = pick(_f(row, *NAME_KEYS), md.get("name") or md.get("title"))
    rec["Type"] = _f(row, "Type")
    rec["Property Type"] = _f(row, "Property Type")
    rec["Property Style"] = _f(row, "Property Style") or _f(row, "Building Type")
    rec["Property Status"] = _f(row, "Property Status") or "Active"

    # ── Location ────────────────────────────────────────────────────────────
    rec["Property Address"] = pick(_f(row, *ADDRESS_KEYS), md.get("address"))
    rec["City"] = pick(_f(row, *CITY_KEYS), md.get("city"))
    rec["State"] = pick(_f(row, *STATE_KEYS), md.get("state"))
    rec["ZIP Code"] = pick(_f(row, *ZIP_KEYS), md.get("zip"))
    rec["Latitude"] = _num(row, *LAT_KEYS) if csv_get(row, *LAT_KEYS) else md.get("latitude")
    rec["Longitude"] = _num(row, *LNG_KEYS) if csv_get(row, *LNG_KEYS) else md.get("longitude")

    # ── Structure (from CSV, website rarely has these) ──────────────────────
    rec["Year Built"] = _num(row, "Year Built") or md.get("year_built")
    rec["Stories"] = _num(row, "Stories") or md.get("stories")

    # ── Operations ──────────────────────────────────────────────────────────
    rec["Management Company"] = _f(row, "Management Company")
    rec["Phone"] = pick(_f(row, "Phone"), md.get("telephone"))
    rec["Website"] = _f(row, *WEBSITE_KEYS) or scrape_result.get("base_url")

    # ── Images (scraped from OpenGraph / JSON-LD) ──────────────────────────
    rec["Property Image URL"] = md.get("image_url") or None
    rec["Property Gallery URLs"] = md.get("gallery_urls") or []

    # ── Aggregates from scraped units (computed every run, always wins) ────
    rec["Average Unit Size (SF)"] = stats["average_unit_size_sf"] or _num(row, "Average Unit Size (SF)")
    rec["Total Units"] = stats["total_units_found"] or _num(row, "Total Units")
    rec["Unit Mix"] = stats["unit_mix"] or _f(row, "Unit Mix")
    rec["First Move-In Date"] = stats["first_move_in_date"] or _f(row, "First Move-In Date")

    # ── External-only fields (pass-through from CSV, never scraped) ────────
    for f in EXTERNAL_ONLY_FIELDS:
        rec[f] = _f(row, f)

    rec["Update Date"] = date.today().isoformat()

    rec["units"] = target_units

    # ── Runtime diagnostics (always last so they're easy to find) ──────────
    rec["_meta"] = {
        "canonical_id": ident.canonical_id,
        "identity_source": ident.id_source,
        "identity_confidence": ident.confidence,
        "address_fp": ident.address_fp,
        "geo_fp": ident.geo_fp,
        "website_fp": ident.website_fp,
        "scrape_tier_used": scrape_result.get("extraction_tier_used"),
        "scrape_errors": scrape_result.get("errors") or [],
        "apis_intercepted": len(scrape_result.get("_raw_api_responses") or []),
        "units_extracted": len(target_units),
        "carry_forward_used": carry_forward_used,
        "was_known": bool(state_snapshot),
    }
    return rec
