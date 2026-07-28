"""Marketing-page floor-plan TEXT parser (task #21 plan-level recovery).

Many small/independent property sites (Wix / Squarespace / custom) publish their
floor plans as free TEXT in a "Floor Plans" / "Unit Layouts" section — plan name,
sqft, and a "from $X" / "starting at $X" rent — NOT in a structured unit table or
an API. The generic cascade's CSS-selector dom_scan misses this, so these props
mis-verdict FAILED_NO_DATA when they actually publish PLAN-LEVEL data (a coverage
SUCCESS).

``parse_marketing_plan_text(html)`` recovers those as plan-level records
(``unit_number=""``, so downstream stamps SUCCESS_PLAN_LEVEL, not gold). Robust to
the sqft/rent formatting variety that tripped a naive regex ("sq. ft." / "ft²" /
"square feet"; "from $" / "starting at $"). Never raises.

v1 targets the free-text 2-line form (plan-name line, then "<sqft> sq ft | from
$<rent>"), plus single-line variants. Table-structured plan grids (e.g. Princeton
Management) are a v2 extension.
"""

from __future__ import annotations

import re
from typing import Any

from ma_poc.pms.adapters._parsing import parse_area

# plan-name tokens (Studio / N Bed[room] [N Bath] / One-Two-Three-Four Bed…)
_WORDNUM = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_PLAN_RE = re.compile(
    r"^\s*(studio|efficiency|(?:one|two|three|four|five|\d)\s*-?\s*(?:bed(?:room)?s?|br)"
    r"(?:\s*[,/&-]?\s*(?:(?:one|two|three|four|\d)\s*-?\s*bath(?:room)?s?|\d\s*ba))?)\s*:?\s*$",
    re.I,
)
# Anchor DETECTOR only — "does this line mention an area at all". The VALUE
# comes from ``_parsing.parse_area``; this regex must not be used to read one.
# Its ``(\d{2,4})`` group silently truncates a thousands separator, so
# "1,152 sq ft" yielded 152 and "$1145 Sq Ft" yielded the rent as an area.
_SQFT_RE = re.compile(r"(\d{2,4})\s*(?:sq\.?\s?ft\.?|sqft|square\s?f(?:ee)?t|ft\s?[²³⁲-⁹²2])", re.I)
_RENT_RE = re.compile(r"(?:from|starting\s*at|rent:?)\s*\$\s?([\d,]{3,})", re.I)
_RANGE_RE = re.compile(r"\$\s?([\d,]{3,})\s*[-–—]\s*\$?\s?[\d,]{3,}", re.I)
_BARE_RENT_RE = re.compile(r"\$\s?([\d,]{3,})", re.I)
# lines that carry a $ but are FEES, not rent — never treat as a plan
_FEE_RE = re.compile(
    r"\b(fee|deposit|application|admin|move[\s-]?in|holding|pet|trash|community|pest|"
    r"liability|utilit|waiver|per applicant|per animal|non[\s-]?refundable)\b",
    re.I,
)
_BED_RE = re.compile(r"(studio|one|two|three|four|five|\d)\s*-?\s*bed", re.I)
_BATH_RE = re.compile(r"(one|two|three|four|\d)\s*-?\s*bath", re.I)


def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    if tok in _WORDNUM:
        return _WORDNUM[tok]
    m = re.match(r"\d+", tok)
    return int(m.group(0)) if m else None


def _beds(plan_name: str) -> int | None:
    if re.search(r"\bstudio|efficiency\b", plan_name, re.I):
        return 0
    m = _BED_RE.search(plan_name)
    return _num(m.group(1)) if m else None


def _baths(plan_name: str) -> float | None:
    m = _BATH_RE.search(plan_name)
    return float(_num(m.group(1))) if m and _num(m.group(1)) is not None else None


def _to_int(s: str) -> int | None:
    try:
        v = int(str(s).replace(",", "").strip())
        return v if 200 <= v <= 50_000 else None
    except (TypeError, ValueError):
        return None


def _text_lines(html: str) -> list[str]:
    try:
        from bs4 import BeautifulSoup

        txt = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    except Exception:
        txt = re.sub(r"<[^>]+>", "\n", html)
    return [ln.strip() for ln in txt.split("\n") if ln.strip()]


# A marketing page that publishes a per-APARTMENT availability grid — the
# columns a leasing table always carries, in any order.
_UNIT_COL_RE = re.compile(r"\b(bldg\s*/\s*unit|unit\s*(?:#|no\.?|number)?|apt\.?)\b", re.I)
_BED_COL_RE = re.compile(r"\bbed(room)?s?\b", re.I)
_PRICE_COL_RE = re.compile(r"\b(rents?|price|pricing)\b", re.I)
# A real apartment designator: 302, PH14, 03-0712, 01E-104, B-12. Deliberately
# NOT a bare floor-plan code (JRA1, C23B) — those are layouts, not apartments.
_UNIT_TOKEN_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{1,4}(?:[-–][A-Za-z0-9]{1,5})?$")
_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4}|now|available)\b", re.I)


def parse_unit_table(html: str, url: str = "") -> list[dict[str, Any]]:
    """Extract UNIT-LEVEL rows from a marketing availability grid.

    Many marketing sites publish a per-apartment table — ``Bldg/Unit | Bed |
    Bath | Rents From | Available Date`` — rendered as a CSS grid of divs rather
    than a ``<table>``. ``parse_marketing_plan_text`` reads those pages as free
    text, captures the rents correctly, but has no concept of a unit column, so
    it emits PLAN-level rows and the apartment numbers are discarded.

    Measured cost (2026-07-25, user-validated against the live site): The
    Majestic (majesticvernonhills.com) publishes ``03-0712 / 1 Bedroom / 1 Bath
    / $2,623 / 07/27/2026``. Our rents matched the site exactly, but all 48 rows
    shipped as SUCCESS_PLAN_LEVEL with ``inferred_*`` ids — a gold-level
    property demoted to plan-level purely because the unit column was dropped.

    Runs BEFORE the plan-text parser so a page that publishes apartments yields
    apartments. Returns [] when no unit grid is present (the overwhelmingly
    common case), so the plan-text path is unaffected. Never raises.
    """
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        # Find a header row that names a unit column AND a price column — both
        # required, so a plain floor-plan comparison table can't match.
        for head in soup.find_all(True):
            cells = [c.get_text(" ", strip=True) for c in head.find_all(True, recursive=False)]
            if len(cells) < 3:
                continue
            joined = " ".join(cells)
            if not (_UNIT_COL_RE.search(joined) and _PRICE_COL_RE.search(joined)):
                continue
            unit_i = next((i for i, c in enumerate(cells) if _UNIT_COL_RE.search(c)), None)
            price_i = next((i for i, c in enumerate(cells) if _PRICE_COL_RE.search(c)), None)
            bed_i = next((i for i, c in enumerate(cells) if _BED_COL_RE.search(c)), None)
            if unit_i is None or price_i is None:
                continue
            # Find data rows by SHAPE, not by structure. Real grids nest the
            # rows arbitrarily (The Majestic wraps them in a `units-body` div,
            # so they are the header's NIECES, not its siblings). Scan the
            # container's descendants for anything whose direct-child cells
            # align with the header arity — that survives markup variation.
            parent = head.parent
            if parent is None:
                continue
            for row in parent.find_all(True):
                if row is head or row.find(class_=head.get("class") or []) is not None:
                    continue
                vals = [c.get_text(" ", strip=True) for c in row.find_all(True, recursive=False)]
                if len(vals) <= max(unit_i, price_i):
                    continue
                unit = vals[unit_i].strip()
                if not unit or not _UNIT_TOKEN_RE.match(unit) or unit in seen:
                    continue
                rent = _to_int((_BARE_RENT_RE.search(vals[price_i]) or [None, ""])[1]) \
                    if _BARE_RENT_RE.search(vals[price_i]) else None
                if not rent:
                    continue
                seen.add(unit)
                beds = _beds(vals[bed_i]) if bed_i is not None and bed_i < len(vals) else None
                baths = next(
                    (_baths(v) for v in vals if _baths(v) is not None), None
                )
                avail = next((v for v in vals if _DATE_RE.search(v)), "")
                rec: dict[str, Any] = {
                    "unit_number": unit,
                    "unit_id": unit,
                    "market_rent_low": rent,
                    "market_rent_high": rent,
                    "asking_rent": rent,
                    "rent": rent,
                    "rent_range": f"${rent:,}",
                    "availability_status": "AVAILABLE",
                    "extraction_tier": "TIER_1_DOM_UNIT_TABLE",
                }
                if beds is not None:
                    rec["beds"] = beds
                    rec["bedrooms"] = beds
                if baths is not None:
                    rec["baths"] = baths
                    rec["bathrooms"] = baths
                if avail:
                    rec["availability_date"] = avail
                    rec["available_date"] = avail
                out.append(rec)
            if out:
                break
    except Exception:
        return []
    # A single stray row is noise; a real grid lists several apartments.
    return out if len(out) >= 2 else []


def parse_marketing_plan_text(html: str, url: str = "") -> list[dict[str, Any]]:
    """Extract plan-level records from a marketing floor-plan text section.

    Returns a list of plan dicts (``unit_number=""``, ``_floor_plan``, ``_sqft``,
    ``bedrooms``, ``bathrooms``, ``market_rent_low``, ``rent_range``,
    ``extraction_tier``). Empty list when the pattern isn't present. Never raises.

    NOTE: callers should try :func:`parse_unit_table` FIRST — a page that
    publishes a per-apartment grid should yield unit-level rows, not plans.
    """
    if not html:
        return []
    try:
        lines = _text_lines(html)
    except Exception:
        return []

    plans: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def _emit(name: str, sqft: int | None, rent: int | None) -> None:
        name = name.strip().rstrip(":").strip()
        if not name:
            return
        # FALSE-POSITIVE guard (measured 2026-07-24): the parser was grabbing
        # marketing PROSE that mentions bed types + sqft ("Choose from studio to
        # 2-bedroom layouts ranging from 600 to 850 square feet…") as a single
        # junk "plan". Real plan names are short. Reject a name that's too long or
        # reads like a sentence (multiple spaces / sentence punctuation).
        if len(name) > 45 or name.count(" ") > 7 or re.search(r"[.,!?;] |\.\s*$", name):
            return
        beds = _beds(name)
        baths = _baths(name)
        key = (name.lower(), sqft, rent)
        if key in seen:
            return
        seen.add(key)
        # Emit the CANONICAL int-typed keys directly (area/beds/baths/asking_rent/
        # rent), not just the underscore-string aliases — infer/sanity/_format_v2
        # read the canonical ones, and a string "_sqft"/"bedrooms" was dropped to
        # None in the v2 output otherwise (traced 2026-07-24).
        rec: dict[str, Any] = {
            "unit_number": "",  # plan-level → SUCCESS_PLAN_LEVEL, not gold
            "_floor_plan": name,
            "floor_plan_name": name,
            "availability_status": "UNKNOWN",
            "extraction_tier": "TIER_3_PLAN_TEXT",
        }
        if sqft:
            rec["area"] = sqft
            rec["sqft"] = sqft
            rec["_sqft"] = str(sqft)
        if beds is not None:
            rec["beds"] = beds
            rec["bedrooms"] = beds
        if baths is not None:
            rec["baths"] = baths
            rec["bathrooms"] = baths
        if rent:
            rec["market_rent_low"] = rent
            rec["market_rent_high"] = rent
            rec["asking_rent"] = rent
            rec["rent"] = rent
            rec["rent_range"] = f"${rent:,}"
        plans.append(rec)

    # v1.1 windowed association. A plan "anchor" is a pure plan-name line (form 1:
    # "Studio") OR a combined bed+sqft line (form 2: "1 Bed | 1 Bath | 623 sqft").
    # Each anchor owns a WINDOW up to the next anchor (capped), within which we
    # find its sqft (if not on the anchor) and its rent. This recovers rent
    # published on SEPARATE lines ("Rent: $815", "Starting at $1420", "$X-$Y")
    # rather than only the "sqft | from $X" adjacency of v1 — 4/5 measured firings
    # publish rent that way. Window is bounded (next anchor + cap 6) and
    # fee-filtered so it can't steal the next plan's rent or a fee amount.
    n = len(lines)

    def _is_anchor(ln: str) -> bool:
        if _PLAN_RE.match(ln):
            return True
        return bool(_SQFT_RE.search(ln) and _BED_RE.search(ln) and not _FEE_RE.search(ln))

    anchors = [i for i in range(n) if _is_anchor(lines[i])]
    for k, i in enumerate(anchors):
        name = lines[i]
        end = anchors[k + 1] if k + 1 < len(anchors) else n
        end = min(end, i + 6)  # cap so a distant rent isn't mis-attributed
        sqft: int | None = None
        rent: int | None = None
        # form-2 anchor carries sqft on its own line
        if not _FEE_RE.search(name):
            sqft = parse_area(name)
        for w in lines[i:end]:
            if _FEE_RE.search(w):
                continue
            if sqft is None:
                sqft = parse_area(w)
            if rent is None:
                rm = _RENT_RE.search(w) or _RANGE_RE.search(w) or _BARE_RENT_RE.search(w)
                if rm:
                    rent = _to_int(rm.group(1))
            if sqft and rent:
                break
        if sqft is not None:  # require sqft — avoids plan-name-only / prose firings
            _emit(name, sqft, rent)

    return plans
