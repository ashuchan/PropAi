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

# plan-name tokens (Studio / N Bed[room] [N Bath] / One-Two-Three-Four Bed…)
_WORDNUM = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_PLAN_RE = re.compile(
    r"^\s*(studio|efficiency|(?:one|two|three|four|five|\d)\s*-?\s*(?:bed(?:room)?s?|br)"
    r"(?:\s*[,/&-]?\s*(?:(?:one|two|three|four|\d)\s*-?\s*bath(?:room)?s?|\d\s*ba))?)\s*:?\s*$",
    re.I,
)
_SQFT_RE = re.compile(r"(\d{2,4})\s*(?:sq\.?\s?ft\.?|sqft|square\s?f(?:ee)?t|ft\s?[²³⁲-⁹²2])", re.I)
_RENT_RE = re.compile(r"(?:from|starting\s*at)\s*\$\s?([\d,]{3,})", re.I)
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


def parse_marketing_plan_text(html: str, url: str = "") -> list[dict[str, Any]]:
    """Extract plan-level records from a marketing floor-plan text section.

    Returns a list of plan dicts (``unit_number=""``, ``_floor_plan``, ``_sqft``,
    ``bedrooms``, ``bathrooms``, ``market_rent_low``, ``rent_range``,
    ``extraction_tier``). Empty list when the pattern isn't present. Never raises.
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

    n = len(lines)
    for i, ln in enumerate(lines):
        # a plan-name line → look at the next 1-2 lines for sqft + rent
        if _PLAN_RE.match(ln):
            for j in (i + 1, i + 2):
                if j >= n:
                    break
                nxt = lines[j]
                sm = _SQFT_RE.search(nxt)
                if not sm:
                    continue
                if _FEE_RE.search(nxt):
                    continue
                rm = _RENT_RE.search(nxt) or _BARE_RENT_RE.search(nxt)
                rent = _to_int(rm.group(1)) if rm else None
                _emit(ln, _to_int(sm.group(1)), rent)
                break
        # a single line carrying plan-name + sqft + rent together
        elif _SQFT_RE.search(ln) and _BED_RE.search(ln) and not _FEE_RE.search(ln):
            sm = _SQFT_RE.search(ln)
            rm = _RENT_RE.search(ln) or _BARE_RENT_RE.search(ln)
            _emit(ln, _to_int(sm.group(1)), _to_int(rm.group(1)) if rm else None)

    return plans
