"""Shared parsing helpers used by multiple adapters.

Extracted from ``scripts/entrata.py`` so that each adapter can import
lightweight helpers without depending on the full scraper engine.
"""
from __future__ import annotations

import re
from typing import Any


def money_to_int(s: str) -> int | None:
    """Parse '$1,450', '1450.00', '1,450 USD' -> 1450. Returns None on failure."""
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def get_field(d: dict[str, Any], *keys: str) -> str:
    """Try multiple key names, return first non-empty string found.

    Handles nested rent/sqft objects like ``{rent: {min: 1351, max: 1351}}``
    by extracting the first numeric value from the nested dict.
    """
    for k in keys:
        v = d.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, list):
            continue
        if isinstance(v, dict):
            for sub_k in ("min", "low", "amount", "value", "effectiveRent",
                          "max", "high"):
                sv = v.get(sub_k)
                if sv is not None and sv != "":
                    return str(sv)
            continue
        return str(v)
    return ""


def format_rent_range(lo: int | None, hi: int | None) -> str:
    """Format rent range from low/high integers."""
    if lo and hi and lo != hi:
        return f"${lo:,} - ${hi:,}"
    if lo:
        return f"${lo:,}"
    if hi:
        return f"${hi:,}"
    return ""


def bed_label_from(beds: int | None, name: str = "") -> str:
    """Derive human-readable bed label."""
    if beds == 0 or (isinstance(name, str) and "studio" in name.lower()):
        return "Studio"
    if beds is not None:
        return f"{beds} Bedroom"
    return ""


def rent_in_sanity_range(rent: int | None) -> bool:
    """Check if rent falls within $200-$50,000 sanity bounds."""
    if rent is None:
        return True  # null is acceptable
    return 200 <= rent <= 50000


# ── Junk deny-lists (Phase 5) ──────────────────────────────────────────────
# Floor plan names that match these patterns are CMS widget / vendor
# artefacts, not real apartment plans. Observed in the 2026-04-19 run:
# "MODULE_CONCESSIONMANAGER", "[Riedman] The Dean - Standard Lease Magnet -
# Pop-Up - Mobile -Gravity FORMS". Rejecting here prevents them reaching
# the v2 output and skewing success metrics.
_JUNK_PLAN_PATTERNS = (
    re.compile(r"^(MODULE|WIDGET|COMPONENT|CMS|PLUGIN)[_\- ]", re.I),
    re.compile(r"\b(lease\s*magnet|pop[- ]?up|gravity\s*forms?|mobile\s*form)\b", re.I),
    re.compile(r"^\[[^\]]{2,30}\].*?(magnet|pop|form|mobile)\b", re.I),  # vendor-prefixed CMS entries
)

# Unit number tokens that are obviously navigation text or stop-words, not
# real unit identifiers. Observed DOM-scan false positives: "Left", "s",
# "Right", "new". All-lowercase single-word matches only — real unit IDs
# are alphanumeric with digits.
_JUNK_UNIT_TOKENS = frozenset({
    "left", "right", "up", "down", "top", "bottom",
    "new", "more", "view", "learn", "click", "here", "now",
    "all", "one", "any", "unit", "home", "page", "menu",
    "s", "a", "an", "the",
})


def is_junk_floor_plan(name: Any) -> bool:
    """Return True when a floor-plan name is obviously a CMS artefact.

    Kept lenient to minimise false negatives — only catches the specific
    failure shapes observed in production. Real plan names starting with
    generic words like "Studio" or "The Reserve" still pass through.
    """
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False
    for pat in _JUNK_PLAN_PATTERNS:
        if pat.search(s):
            return True
    return False


def is_junk_unit_number(val: Any) -> bool:
    """Return True when a unit_number is a stop-word / nav token.

    Real unit identifiers contain digits or are 3+ character alphanumeric
    codes. A bare "Left" or "s" is always an extractor mistake.
    """
    if val is None or val == "":
        return False
    s = str(val).strip()
    if not s:
        return False
    # Accept anything containing a digit — that's a real unit number.
    if any(c.isdigit() for c in s):
        return False
    # Short single-word stop-words.
    if s.lower() in _JUNK_UNIT_TOKENS:
        return True
    # Single character tokens with no digits.
    if len(s) <= 1:
        return True
    return False


def parse_rent_range(rent_range: str) -> tuple[int | None, int | None]:
    """Parse "$1,200 - $1,500" / "$1,295" / "1295-1500" to (low, high) ints.

    Returns (None, None) when the string has no recognisable number. Used by
    the v2 schema transform as a fallback when an adapter emits only the
    formatted ``rent_range`` string but not the numeric low/high fields.
    """
    if not rent_range or not isinstance(rent_range, str):
        return None, None
    # Find all numeric tokens; drop thousands separators.
    nums = [int(float(n.replace(",", "")))
            for n in re.findall(r"\d[\d,]*", rent_range)
            if n and n[0].isdigit()]
    if not nums:
        return None, None
    # Rent sanity: reject anything outside the sane band so we don't
    # pick up bedroom counts or sqft that ended up in rent_range.
    nums = [n for n in nums if 200 <= n <= 50000]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums), max(nums)


def make_unit_dict(
    *,
    floor_plan_name: str = "",
    bed_label: str = "",
    bedrooms: str = "",
    bathrooms: str = "",
    sqft: str = "",
    unit_number: str = "",
    floor: str = "",
    building: str = "",
    rent_range: str = "",
    rent_low: int | None = None,
    rent_high: int | None = None,
    deposit: str = "",
    concession: str = "",
    availability_status: str = "AVAILABLE",
    available_units: str = "",
    availability_date: str = "",
    lease_term: str = "",
    move_in_date: str = "",
    source_api_url: str = "",
    extraction_tier: str = "",
) -> dict[str, Any]:
    """Build a standard unit dict in the format expected by the pipeline.

    Emits BOTH the human-readable ``rent_range`` string AND the numeric
    ``market_rent_low`` / ``market_rent_high`` fields that the v2 schema
    transform reads. If ``rent_low`` / ``rent_high`` are not supplied but
    ``rent_range`` is, the numeric values are parsed from the string so the
    downstream transform doesn't silently drop rent.

    ``lease_term`` and ``move_in_date`` are plumbed through so parsers that
    learn to extract them don't need further format changes.
    """
    # Prefer explicit ints when passed; otherwise recover from the string.
    if rent_low is None and rent_high is None and rent_range:
        lo, hi = parse_rent_range(rent_range)
        rent_low, rent_high = lo, hi
    # Keep rent_range populated for human-readable output when ints provided
    # but no string was passed.
    if not rent_range and (rent_low or rent_high):
        rent_range = format_rent_range(rent_low, rent_high)

    return {
        "floor_plan_name": floor_plan_name,
        "bed_label": bed_label,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "sqft": sqft,
        "unit_number": unit_number,
        "floor": floor,
        "building": building,
        "rent_range": rent_range,
        "market_rent_low": rent_low,
        "market_rent_high": rent_high,
        "deposit": deposit,
        "concession": concession,
        "availability_status": availability_status,
        "available_units": available_units,
        "availability_date": availability_date,
        "lease_term": lease_term,
        "move_in_date": move_in_date,
        "source_api_url": source_api_url,
        "extraction_tier": extraction_tier,
    }
