"""Shared parsing helpers used by multiple adapters.

Extracted from ``scripts/entrata.py`` so that each adapter can import
lightweight helpers without depending on the full scraper engine.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


def money_to_int(s: str) -> int | None:
    """Parse '$1,450', '1450.00', '1,450 USD' -> 1450. Returns None on failure.

    For range strings like ``"$1,200 - $1,400"`` returns the LOW value
    (1200). Callers that need both bounds must use ``parse_rent_range``.

    2026-05-13 port (project_run_2026_05_11 deposit->rent leakage):
    pre-port the implementation was ``re.sub(r"[^\\d.]", "", s)`` which
    concatenated "1,200 - 1,400" to "12001400" and emitted 12_001_400 --
    a value that failed downstream sanity bounds (200..50_000) and
    silently produced null rent. The fix matches the first numeric
    token instead, returning the semantically correct low bound.
    """
    if not s:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", s)
    if not m:
        return None
    cleaned = m.group(0).replace(",", "")
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


# Patterns for inferring bed/bath count from a floor-plan / unit name.
# Runs in declared order; first match wins. Patterns are designed to be
# narrow enough that they don't false-positive on unrelated marketing
# copy ("Five Star View" should NOT yield beds=5). The corpus they were
# tuned against is the union of:
#   - ma_poc/config/Floorplan-comparisons.csv     (vendor CSV side)
#   - distinct units.floor_plan_name in production (DB side)
# When extending, add a regression test in
# tests/pms/adapters/test_infer_bed_bath_from_name.py rather than
# loosening an existing pattern.
_STUDIO_RE = re.compile(
    r"\b(?:studio|micro[- ]?studio|efficiency|jr\s*studio|junior\s*studio|"
    r"open[- ]?one|0\s*(?:bd|br|bed|bedroom)s?)\b",
    re.IGNORECASE,
)
# "2BR/2BA", "3 BR / 2.5 BA", "1 Bed 1 Bath" — second token is a bath
# token ("ba" / "bath").
_BED_BATH_PAIR_RE = re.compile(
    r"\b(\d)\s*(?:bd|br|bed(?:room)?s?)\s*[/\\]?\s*"
    r"(\d(?:\.\d)?)\s*(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)
# Vendor CSV quirk: "1BD/1BR-1", "2BD/1BR-2", "3BD/2.5BR-3" — the second
# "BR" actually means bathroom in this notation. Surfaced from the
# Floorplan-comparisons.csv corpus. A trailing "-N" suffix is allowed
# (the vendor's plan-variant index) so it doesn't break the boundary.
_BED_BATH_VENDOR_BR_RE = re.compile(
    r"\b(\d)\s*BD\s*/\s*(\d(?:\.\d)?)\s*BR\b",
    re.IGNORECASE,
)
# Word-form pair: "One Bedroom One Bath", "Two Bedroom Two Bathroom".
_WORD_BED_BATH_PAIR_RE = re.compile(
    r"\b(one|two|three|four|five)\s*(?:bd|br|bed(?:room)?s?)\s+"
    r"(one|two|three|four|five)\s*(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)
# "1x1", "2x2.5", "3 x 2" — common shorthand on RentCafe / Yardi / SightMap.
# Anchored on word boundaries so "1x12" isn't read as bed=1, bath=12.
_X_PAIR_RE = re.compile(r"(?<![\d.])(\d)\s*[xX]\s*(\d(?:\.\d)?)(?!\d)")
# "1/1", "2/2.5" — same shorthand with a slash separator. Excluded:
# "1/2/3" (looks like a date or list, not a bed/bath).
_SLASH_PAIR_RE = re.compile(r"(?<![\d./])(\d)\s*/\s*(\d(?:\.\d)?)(?!\s*/)")
# Standalone bed counts: "1BR", "2 Bed", "Three Bedroom".
_BED_ONLY_RE = re.compile(
    r"\b(\d)\s*(?:bd|br|bed(?:room)?s?)\b",
    re.IGNORECASE,
)
_WORD_BED_RE = re.compile(
    r"\b(one|two|three|four|five)\s*(?:bd|br|bed(?:room)?s?)\b",
    re.IGNORECASE,
)
# Standalone bath count: "1 Bath", "2.5 BA". Used only when bed was
# already inferred by a separate pass — never seeds beds from a bath.
_BATH_ONLY_RE = re.compile(
    r"\b(\d(?:\.\d)?)\s*(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)
_WORD_TO_INT = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def infer_bed_bath_from_name(
    name: str | None,
) -> tuple[int | None, float | None]:
    """Best-effort inference of (beds, baths) from a plan / unit name.

    Returns ``(None, None)`` when nothing reliable can be inferred. The
    function never raises and never invents data — every output value is
    grounded in a literal substring of ``name``.

    Apply only as a *fallback* when the adapter / API didn't deliver
    these fields. Never overwrite a non-null source value with the
    inferred one — extraction is always more authoritative than name
    parsing.
    """
    if not name or not isinstance(name, str):
        return None, None
    s = name.strip()
    if not s:
        return None, None

    # 1. Studio / micro-studio: beds=0, baths left to its own pass.
    is_studio = bool(_STUDIO_RE.search(s))

    beds: int | None = None
    baths: float | None = None

    # 2a. Vendor "BD/BR" pair (where second BR == bath) — runs before the
    # standard pair so the vendor notation isn't mis-read as bed/bed.
    m = _BED_BATH_VENDOR_BR_RE.search(s)
    if m:
        try:
            beds = int(m.group(1))
            baths = float(m.group(2))
        except ValueError:
            beds = baths = None

    # 2b. Standard explicit bed/bath pair.
    if beds is None and baths is None:
        m = _BED_BATH_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 2c. Word-form pair: "One Bedroom One Bath".
    if beds is None and baths is None:
        m = _WORD_BED_BATH_PAIR_RE.search(s)
        if m:
            beds = _WORD_TO_INT.get(m.group(1).lower())
            baths_word = _WORD_TO_INT.get(m.group(2).lower())
            baths = float(baths_word) if baths_word is not None else None

    # 3. NxN shorthand. Run only if we don't have a pair already.
    if beds is None and baths is None:
        m = _X_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 4. N/N slash shorthand — skip on studios since "0/1" wouldn't be
    # written that way conventionally; falls through to single-bed pass.
    if beds is None and baths is None:
        m = _SLASH_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 5. Single-field passes.
    if beds is None:
        m = _BED_ONLY_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
            except ValueError:
                beds = None
    if beds is None:
        m = _WORD_BED_RE.search(s)
        if m:
            beds = _WORD_TO_INT.get(m.group(1).lower())
    if baths is None:
        m = _BATH_ONLY_RE.search(s)
        if m:
            try:
                baths = float(m.group(1))
            except ValueError:
                baths = None

    # 6. Studio overrides bed count if no explicit "1 BR" type pattern
    # contradicted it. Studios are sometimes labelled "Studio - 1 Bath",
    # so set beds=0 only when the bed pass found nothing.
    if is_studio and beds is None:
        beds = 0

    # 7. Sanity bounds — apartment plans realistically span 0–7 beds and
    # 0–10 baths. Anything outside is signal that the regex matched a
    # number that wasn't a bed/bath count (e.g. "Loft 12B").
    if beds is not None and not (0 <= beds <= 7):
        beds = None
    if baths is not None and not (0 <= baths <= 10):
        baths = None

    return beds, baths


def compute_floor_plan_id(
    canonical_id: str | None,
    floor_plan_name: str | None,
    beds: int | None,
    baths: float | None,
) -> str | None:
    """Deterministic 12-char id grouping units that share a floor plan.

    Returns ``None`` when there is not enough signal to identify a plan
    — specifically: when neither ``floor_plan_name`` nor any of
    ``beds``/``baths`` is set. Without one of those, two units of the
    same property would collapse to the same id even though they are
    clearly distinct plans.

    Inputs are normalised: name is lowercased + whitespace-collapsed,
    canonical_id is stringified, missing fields use the literal token
    ``"-"`` so e.g. ``(beds=None, baths=1.0)`` doesn't collide with
    ``(beds=1, baths=None)``.

    Two units with the same (canonical_id, name, beds, baths) always
    return the same id, regardless of unit_number / area / extraction
    timestamp. Per-unit area variation is intentionally NOT part of the
    key — that's the whole point of grouping unit-level rows back to
    plan-level rows.
    """
    cid = (canonical_id or "").strip()
    raw_name = floor_plan_name or ""
    name_norm = re.sub(r"\s+", " ", raw_name.strip().lower())

    # Plan-anchor signal: must have at least a name OR a bed/bath value.
    if not name_norm and beds is None and baths is None:
        return None

    beds_part = "-" if beds is None else str(int(beds))
    baths_part = "-" if baths is None else f"{float(baths):g}"
    name_part = name_norm or "-"

    payload = f"{cid}|{name_part}|{beds_part}|{baths_part}".encode()
    return hashlib.sha256(payload).hexdigest()[:12]


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


# 2026-05-13 port (project_run_2026_05_11 floor-plan blob taxonomy):
# Some API payloads pack a JSON object into the floor-plan-name field,
# e.g. ``{"name":"B06","provider_id":"abc"}``. Storing that verbatim as
# a name produces 2,534 unreadable rows (observed in prior runs).
_NAME_BLOB_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _unwrap_name_blob(name: Any) -> str:
    """Strip a JSON-object blob from a floor-plan-name string.

    For input like ``'{"name":"B06","provider_id":"..."}'`` returns
    ``"B06"``. Plain strings pass through. Non-strings are stringified
    if possible (with the empty default). Never raises.

    Cheap heuristic: if the trimmed string starts with ``{`` and contains
    a JSON-style ``"name":"..."`` token, extract that token. Anything
    else is returned as the original input (str-coerced).
    """
    if name is None:
        return ""
    s = str(name).strip()
    if not s:
        return s
    if not s.startswith("{"):
        return s
    m = _NAME_BLOB_NAME_RE.search(s)
    return m.group(1).strip() if m else s


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
    ``rent_range`` is, the numeric values are parsed from the string so
    the downstream transform doesn't silently drop rent.

    ``lease_term`` and ``move_in_date`` are plumbed through so parsers
    that learn to extract them don't need further format changes.

    2026-05-13 port:
      * ``floor_plan_name`` is unwrapped via ``_unwrap_name_blob`` so
        JSON-object names like ``{"name":"B06","provider_id":"..."}``
        ship as ``"B06"`` instead of the literal blob.
      * Emits BOTH ``availability_date`` (legacy v1) AND ``available_date``
        (v2 schema-reader convention). ``ma_poc/core/schema_v2.py:282``
        reads ``unit.get("available_date")`` first; pre-port many
        adapters set only ``availability_date`` and the date silently
        dropped at the v2 boundary (the jugnu v2 formatter has an
        alias resolver but it doesn't run on every code path).
    """
    # Unwrap JSON-blob names before anything else looks at them.
    floor_plan_name = _unwrap_name_blob(floor_plan_name)

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
        # Dual emission: the legacy v1 key + the v2 schema-reader key.
        # Both carry the same value so schema_v2._format_v2_unit (which
        # reads ``available_date`` first) gets the date regardless of
        # which adapter produced the row.
        "availability_date": availability_date,
        "available_date": availability_date,
        "lease_term": lease_term,
        "move_in_date": move_in_date,
        "source_api_url": source_api_url,
        "extraction_tier": extraction_tier,
    }
