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
            for sub_k in ("min", "low", "amount", "value", "effectiveRent", "max", "high"):
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


# ── Source-grounded validation (2026-05-07) ─────────────────────────────
# LLM tiers (TIER_4_LLM_API / _DOM / monolithic) can hallucinate rent values
# that aren't anywhere in the source HTML or captured API bodies. Investigation
# of canary-batch-12 regressions (16 properties that flipped SUCCESS→FAILED
# with identical fetched URLs / captures / adapter selection) confirmed this
# is the dominant variance failure mode: the LLM emits a unit with a
# plausible-looking rent that never appears in the input.
#
# The remedy: before emitting LLM-derived units, drop any whose rent values
# can't be found in the source. The source is the union of (page HTML) +
# (captured API response bodies, JSON-stringified). We accept several
# numeric formats — "$1,275", "1275", "1,275", "$1275.00" — because the LLM
# may render the same value differently from how the page wrote it.
def is_rent_grounded(rent: int | None, source_text: str) -> bool:
    """True when *rent* (e.g. 1275) appears as a number in *source_text*.

    Returns True when ``rent is None`` (no rent to ground — used by units
    that legitimately lack a rent value). Accepts comma-separated, plain,
    and decimal forms.

    Examples (rent=1275):
      - "$1,275"         → True (canonical comma form)
      - "$1,275.00"      → True (decimal form)
      - "1275"           → True (plain integer)
      - "1,275 monthly"  → True (no $ prefix, comma form)
      - "$12,750"        → False (different number — must NOT match
                                  via "1275" being a substring of "12750")
      - "1275" inside "12750" → False (word-boundary required)
    """
    if rent is None:
        return True
    if not source_text:
        return False
    # Word-boundary on both sides so "1275" doesn't false-match inside
    # "12750" or "01275a". Allow optional leading "$" and optional trailing
    # ".00" decimal. The comma form requires a 4-digit hundred (1,275 has
    # exactly 1 digit before the comma, 12,750 has 2 digits) — we generate
    # the canonical comma string for *rent* and look for that exact form.
    plain = str(rent)
    comma = f"{rent:,}"
    # Word boundaries: positive lookbehind/lookahead asserting digit
    # neighbour is absent.
    plain_re = re.compile(rf"(?<!\d)\$?{re.escape(plain)}(?:\.\d{{1,2}})?(?!\d)")
    if plain_re.search(source_text):
        return True
    if comma != plain:
        comma_re = re.compile(rf"(?<!\d)\$?{re.escape(comma)}(?:\.\d{{1,2}})?(?!\d)")
        if comma_re.search(source_text):
            return True
    return False


def filter_llm_units_grounded(
    units: list[dict[str, Any]],
    source_text: str,
) -> tuple[list[dict[str, Any]], int]:
    """Drop LLM-emitted units whose rent values aren't in *source_text*.

    Returns ``(filtered_units, dropped_count)``. A unit passes when EITHER:
      - It has no rent_low and no rent_high (nothing to ground), OR
      - Its rent_low (or, if rent_low absent, rent_high) is grounded in source.

    Caller surfaces ``dropped_count`` so an LLM tier that hallucinated 5 of 7
    units still emits the 2 grounded ones, with the drops logged for
    diagnosis. Apply ONLY to LLM-emitted unit lists — DOM-scraped and API-
    parsed units are by definition grounded in their source and don't need
    this check (and would be incorrectly filtered when their source HTML
    differs from the page HTML, e.g. iframe content).
    """
    if not units:
        return units, 0
    filtered: list[dict[str, Any]] = []
    dropped = 0
    for u in units:
        rent_lo = u.get("market_rent_low") or u.get("rent_low")
        rent_hi = u.get("market_rent_high") or u.get("rent_high")
        # Convert to int if it's a string
        if isinstance(rent_lo, str) and rent_lo.strip():
            rent_lo = money_to_int(rent_lo)
        if isinstance(rent_hi, str) and rent_hi.strip():
            rent_hi = money_to_int(rent_hi)
        # Pick the non-null rent to validate — prefer rent_low.
        check = rent_lo if rent_lo else rent_hi
        if check is None or is_rent_grounded(int(check), source_text):
            filtered.append(u)
        else:
            dropped += 1
    return filtered, dropped


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
_JUNK_UNIT_TOKENS = frozenset(
    {
        "left",
        "right",
        "up",
        "down",
        "top",
        "bottom",
        "new",
        "more",
        "view",
        "learn",
        "click",
        "here",
        "now",
        "all",
        "one",
        "any",
        "unit",
        "home",
        "page",
        "menu",
        "s",
        "a",
        "an",
        "the",
    }
)


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
    nums = [
        int(float(n.replace(",", ""))) for n in re.findall(r"\d[\d,]*", rent_range) if n and n[0].isdigit()
    ]
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
