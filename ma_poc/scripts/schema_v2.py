"""
V2 schema adapters — input parsing and output formatting.
==========================================================

The scraper core is schema-agnostic. This module provides pure transformer
layers that convert between V2 external formats and the internal canonical
representation that the pipeline already uses.

  1. parse_v2_csv_row()   — maps V2 CSV columns → internal canonical dict
  2. build_v2_property()  — maps internal scrape result → V2 JSON output
  3. validate_v2_property() — V2-specific post-transform validation

No scraping logic, no profile logic, no state tracking lives here.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from typing import Any

import validation as V

# ── V2 CSV column mapping ────────────────────────────────────────────────────
#
# The V2 CSV ("Apartments v2") has exactly 7 columns:
#   apartmentid, name, address, city, state, zip, website
#
# We map these to the internal keys that daily_runner / identity.py expect.

V2_CSV_COLUMN_MAP: dict[str, list[str]] = {
    "apartment_id":    ["apartmentid", "apartment_id", "ApartmentID"],
    "property_name":   ["name", "Name"],
    "property_address": ["address", "Address"],
    "city":            ["city", "City"],
    "state":           ["state", "State"],
    "zip_code":        ["zip", "Zip", "zip_code"],
    "website":         ["website", "Website"],
}

# Key aliases for csv_get() — used by daily_runner when schema_version == "v2"
V2_ID_KEYS      = ("apartmentid", "apartment_id", "ApartmentID")
V2_NAME_KEYS    = ("name", "Name")
V2_ADDRESS_KEYS = ("address", "Address")
V2_CITY_KEYS    = ("city", "City")
V2_STATE_KEYS   = ("state", "State")
V2_ZIP_KEYS     = ("zip", "Zip", "zip_code")
V2_WEBSITE_KEYS = ("website", "Website")


def get_schema_version(args: Any = None) -> str:
    """Resolve schema version from CLI args > env > default.

    Args:
        args: argparse namespace with optional ``schema_version`` attribute.

    Returns:
        ``"v1"`` or ``"v2"``.
    """
    if args and getattr(args, "schema_version", None):
        return args.schema_version
    return os.getenv("SCHEMA_VERSION", "v1").strip().lower()


# ── Output adapter ───────────────────────────────────────────────────────────

# Platform guess → human-readable website design label
_PLATFORM_LABELS: dict[str, str] = {
    "entrata":    "Powered by Entrata",
    "rentcafe":   "Powered by RentCafe",
    "appfolio":   "Powered by AppFolio",
    "yardi":      "Powered by RentCafe (Yardi)",
    "realpage":   "Powered by RealPage",
    "sightmap":   "Powered by SightMap",
    "knock":      "Powered by Knock",
    "respage":    "Powered by Respage",
}


def build_v2_property(
    row: dict,
    ident: Any,
    scrape_result: dict,
    target_units: list[dict],
    scrape_ts: datetime | None = None,
) -> dict:
    """Transform internal property + units into V2 output schema.

    Takes the SAME row dict, identity, scrape_result, and unit list that
    ``build_property_record()`` receives. Returns a V2-shaped dict.

    The scraper core is untouched — this is a pure post-processing step.
    """
    from identity import csv_get

    if scrape_ts is None:
        scrape_ts = datetime.now(UTC)

    md = scrape_result.get("property_metadata") or {}

    # Resolve fields with CSV priority, scraped fallback
    def _pick(csv_val: Any, scraped_val: Any) -> Any:
        if csv_val not in (None, "", "null", "None"):
            return csv_val
        return scraped_val if scraped_val not in (None, "", "null", "None") else None

    # CSV values
    csv_id      = csv_get(row, *V2_ID_KEYS)
    csv_name    = csv_get(row, *V2_NAME_KEYS)
    csv_addr    = csv_get(row, *V2_ADDRESS_KEYS)
    csv_city    = csv_get(row, *V2_CITY_KEYS)
    csv_state   = csv_get(row, *V2_STATE_KEYS)
    csv_zip     = csv_get(row, *V2_ZIP_KEYS)
    csv_website = csv_get(row, *V2_WEBSITE_KEYS)

    # Platform / website design
    platform = (scrape_result.get("platform_detected")
                or (md.get("api_provider") if md else None)
                or "")
    website_design = _PLATFORM_LABELS.get(platform.lower(), platform or None)

    # Concessions — prefer scraped banner text
    concessions_text = scrape_result.get("concessions_text") or md.get("concessions") or None

    prop: dict[str, Any] = {
        # ── Property-level fields ────────────────────────────────────────
        "apartment_id":   _safe_int(csv_id),
        "proj_name":      _pick(csv_name, md.get("name") or md.get("title")),
        "address":        _pick(csv_addr, md.get("address")),
        "city":           _pick(csv_city, md.get("city")),
        "state":          _pick(csv_state, md.get("state")),
        "zip_code":       _format_zip_5(_pick(csv_zip, md.get("zip"))),
        "country":        md.get("country") or None,
        "phone":          _pick(
                              csv_get(row, "Phone", "phone"),
                              md.get("telephone"),
                          ) or None,
        "email_address":  md.get("email") or md.get("email_address") or None,
        "website":        csv_website or scrape_result.get("base_url") or None,
        "pmc":            _pick(
                              csv_get(row, "Management Company", "pmc"),
                              md.get("management_company"),
                          ) or None,
        "website_design": website_design if website_design else None,
        "concessions":    concessions_text,

        # ── Units ────────────────────────────────────────────────────────
        "units": [_format_v2_unit(u, scrape_ts) for u in target_units],
    }

    return prop


def _format_v2_unit(unit: dict, scrape_ts: datetime) -> dict:
    """Transform a single internal unit dict to V2 unit format.

    Internal unit dicts carry private fields (prefixed with ``_``) from
    ``scrape_properties.py`` that are not part of the V1 public schema but
    contain the raw data we need for V2.
    """
    beds_raw = (unit.get("_bedrooms")
                or unit.get("bedrooms")
                or unit.get("beds"))
    baths_raw = (unit.get("_bathrooms")
                 or unit.get("bathrooms")
                 or unit.get("baths"))
    fp_name = (unit.get("_floor_plan")
               or unit.get("floor_plan_name")
               or unit.get("floorplan_name"))
    sqft = (unit.get("_sqft")
            or unit.get("sqft")
            or unit.get("area"))

    return {
        "beds":           _normalize_beds(beds_raw),
        "baths":          _normalize_baths(baths_raw),
        "floor_plan_name": fp_name if fp_name else None,
        "area":           _format_area(sqft),
        "unit_id":        unit.get("unit_id") or None,
        "rent_low":       _format_rent(unit.get("market_rent_low")),
        "rent_high":      _format_rent(unit.get("market_rent_high")),
        "date_captured":  scrape_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "available_date": _format_date(unit.get("available_date")),
        "lease_term":     _safe_lease_term(unit.get("lease_term") or unit.get("_lease_term")),
        "move_in_date":   _format_date(unit.get("move_in_date") or unit.get("_move_in_date")),
    }


# ── Formatting helpers ───────────────────────────────────────────────────────

def _safe_int(val: Any) -> int | None:
    """Convert to int, return None on failure."""
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _normalize_beds(val: Any) -> int:
    """Convert bedroom value to integer. Studio → 0, clamp [0, 7].

    Returns 0 for null/unrecognized (conservative default for Studio-like).
    """
    if val is None or val == "":
        return 0
    s = str(val).strip().lower()
    if s in ("studio", "s", "0"):
        return 0
    try:
        n = int(float(s))
        return max(0, min(n, 7))
    except (ValueError, TypeError):
        return 0


def _normalize_baths(val: Any) -> float:
    """Convert bathroom value to nearest 0.5 multiple, clamp [0, 10].

    Returns 1.0 for null (conservative default).
    """
    if val is None or val == "":
        return 1.0
    try:
        n = float(str(val).strip())
        # Round to nearest 0.5
        n = round(n * 2) / 2
        return max(0.0, min(n, 10.0))
    except (ValueError, TypeError):
        return 1.0


def _format_zip_5(val: Any) -> str | None:
    """Extract first 5 digits from a ZIP code. Strips +4 suffix."""
    if val is None:
        return None
    s = str(val).strip()
    # Match first 5 consecutive digits
    m = re.search(r"\d{5}", s)
    if m:
        return m.group(0)
    # If fewer than 5 digits, left-pad with zeros (e.g. "8854" -> "08854")
    digits = re.sub(r"\D", "", s)
    if digits:
        return digits.zfill(5)[:5]
    return None


def _format_rent(val: Any) -> float | None:
    """Clean rent value: strip currency symbols, commas. Must be > 1 or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if val > 1 else None
    s = str(val).strip().replace("$", "").replace(",", "").strip()
    try:
        n = float(s)
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


def _format_area(val: Any) -> int:
    """Convert sqft to int. Must be > 0, otherwise -1 (absent sentinel)."""
    if val is None:
        return -1
    try:
        n = int(float(str(val)))
        return n if n > 0 else -1
    except (ValueError, TypeError):
        return -1


def _format_date(val: Any) -> str | None:
    """Normalize date to YYYY-MM-DD. Returns None if unparseable."""
    if val is None or val == "":
        return None
    s = str(val).strip()
    # Already ISO format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # Try common formats
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # If it's a datetime string, take just the date part
    if len(s) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return None


def _safe_lease_term(val: Any) -> int | None:
    """Lease term in months. Must be > 1 if present, else None."""
    if val is None:
        return None
    try:
        n = int(float(str(val)))
        return n if n > 1 else None
    except (ValueError, TypeError):
        return None


# ── V2 Validation ────────────────────────────────────────────────────────────
#
# Post-transform validation on the already-formatted V2 output.
# Returns issues using the same ValidationIssue shape as validation.py.

# V2-specific issue codes — defined in validation.py, imported here.
V2_MISSING_REQUIRED    = V.V2_MISSING_REQUIRED
V2_INVALID_APARTMENT_ID = V.V2_INVALID_APARTMENT_ID
V2_INVALID_ZIP         = V.V2_INVALID_ZIP
V2_INVALID_BEDS        = V.V2_INVALID_BEDS
V2_INVALID_BATHS       = V.V2_INVALID_BATHS
V2_INVALID_AREA        = V.V2_INVALID_AREA
V2_INVALID_RENT        = V.V2_INVALID_RENT
V2_INVALID_LEASE_TERM  = V.V2_INVALID_LEASE_TERM

_V2_REQUIRED_PROP_FIELDS = ("apartment_id", "proj_name", "address", "city",
                             "state", "zip_code", "website")


def validate_v2_property(prop: dict, canonical_id: str | None = None) -> list[V.ValidationIssue]:
    """Run V2-specific validation on an already-transformed V2 property dict.

    Returns a list of ValidationIssue objects (same shape as validation.py).
    Empty list means the property passes V2 checks.
    """
    issues: list[V.ValidationIssue] = []
    cid = canonical_id or str(prop.get("apartment_id", "unknown"))

    # ── Property-level required fields ───────────────────────────────────
    for field in _V2_REQUIRED_PROP_FIELDS:
        val = prop.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            issues.append(V.error(
                V2_MISSING_REQUIRED,
                f"V2 required field '{field}' is null or empty",
                canonical_id=cid,
                details={"field": field, "value": val},
            ))

    # apartment_id: must be integer > 1
    aid = prop.get("apartment_id")
    if aid is not None and (not isinstance(aid, int) or aid < 1):
        issues.append(V.error(
            V2_INVALID_APARTMENT_ID,
            f"apartment_id must be integer > 1, got {aid!r}",
            canonical_id=cid,
            details={"value": aid},
        ))

    # zip_code: must be exactly 5 digits
    zc = prop.get("zip_code")
    if zc is not None and not re.match(r"^\d{5}$", str(zc)):
        issues.append(V.warning(
            V2_INVALID_ZIP,
            f"zip_code is not 5 digits: {zc!r}",
            canonical_id=cid,
            details={"value": zc},
        ))

    # ── Unit-level validation ────────────────────────────────────────────
    for idx, unit in enumerate(prop.get("units") or []):
        uid = unit.get("unit_id") or f"unit_{idx}"

        # beds: 0-7
        beds = unit.get("beds")
        if beds is not None and (not isinstance(beds, int) or beds < 0 or beds > 7):
            issues.append(V.warning(
                V2_INVALID_BEDS,
                f"beds={beds!r} outside [0, 7]",
                canonical_id=cid,
                details={"unit_id": uid, "value": beds},
            ))

        # baths: 0-10, multiple of 0.5
        baths = unit.get("baths")
        if baths is not None:
            if not isinstance(baths, (int, float)) or baths < 0 or baths > 10:
                issues.append(V.warning(
                    V2_INVALID_BATHS,
                    f"baths={baths!r} outside [0, 10]",
                    canonical_id=cid,
                    details={"unit_id": uid, "value": baths},
                ))
            elif (baths * 2) != int(baths * 2):
                issues.append(V.warning(
                    V2_INVALID_BATHS,
                    f"baths={baths!r} not a multiple of 0.5",
                    canonical_id=cid,
                    details={"unit_id": uid, "value": baths},
                ))

        # area: must be > 0 or exactly -1
        area = unit.get("area")
        if area is not None and area != -1 and (not isinstance(area, int) or area <= 0):
            issues.append(V.warning(
                V2_INVALID_AREA,
                f"area={area!r} must be > 0 or -1",
                canonical_id=cid,
                details={"unit_id": uid, "value": area},
            ))

        # rent: must be > 1 if present
        for rent_field in ("rent_low", "rent_high"):
            rv = unit.get(rent_field)
            if rv is not None and (not isinstance(rv, (int, float)) or rv <= 1):
                issues.append(V.warning(
                    V2_INVALID_RENT,
                    f"{rent_field}={rv!r} must be > 1",
                    canonical_id=cid,
                    details={"unit_id": uid, "field": rent_field, "value": rv},
                ))

        # rent_low <= rent_high
        rl = unit.get("rent_low")
        rh = unit.get("rent_high")
        if (isinstance(rl, (int, float)) and isinstance(rh, (int, float))
                and rl > rh):
            issues.append(V.warning(
                V2_INVALID_RENT,
                f"rent_low ({rl}) > rent_high ({rh})",
                canonical_id=cid,
                details={"unit_id": uid, "low": rl, "high": rh},
            ))

        # lease_term: must be > 1 if present
        lt = unit.get("lease_term")
        if lt is not None and (not isinstance(lt, int) or lt <= 1):
            issues.append(V.warning(
                V2_INVALID_LEASE_TERM,
                f"lease_term={lt!r} must be > 1",
                canonical_id=cid,
                details={"unit_id": uid, "value": lt},
            ))

        # date_captured: NOT NULL
        dc = unit.get("date_captured")
        if not dc:
            issues.append(V.error(
                V2_MISSING_REQUIRED,
                f"V2 required field 'date_captured' is null for unit {uid}",
                canonical_id=cid,
                details={"unit_id": uid, "field": "date_captured"},
            ))

    return issues
