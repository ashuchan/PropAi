"""Generic plan-text adapter for bespoke custom-CMS multifamily sites.

A last-resort plan-level extractor for properties whose marketing site
uses no recognizable PMS framework but DOES embed plan info in body
text using common multifamily conventions.

Verified live 2026-05-21 on:
  - www.colonialcourtapts.com/floor-plans (Drupal blocks, plan-level)
  - stargatewest.com/tucson-rental-floor-plans/ (custom WP, plan-level)
  - www.countryvillageapthomes.com (slick carousel, plan-level)

Body-text patterns observed:
  * colonialcourt: "2 Bedroom / 1 Bath Apartment ... $1495"
  * stargatewest: "1 Bedroom / 1 Bathroom From $1275"
                  separately:  "1 BDRM / 1 BTH 675 Sq. Ft."
  * countryvillage: prices interleaved with $300 deposits in carousel

The adapter is gated VERY tightly to avoid false-firing across the
fleet:
  * Detector signal yields at LOW confidence (0.55) — every recognized
    PMS / CMS signal beats this. Only fires as a last resort when no
    stronger adapter detected.
  * Parser requires ≥2 distinct plan rows (avoids single-noise matches
    like "1 BDRM Studio for $500" in an amenity blurb).
  * Each row must have BOTH a bed count AND a $-prefixed price within
    proximity — prevents matching deposit-only lines.

Plan-level only by design. The 2 truly-empty bespoke sites (wildwoodmanor,
princetonmanagement) correctly emit nothing.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


_GENERIC_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '');
  return {ok: true, bodyText: T(document.body)};
}
"""


# Plan-line regex — accept both long-form ("Bedroom") and short-form
# ("BDRM") + both "Bath" and "BTH". The "rest" context (next ~150 chars
# after the bath token) is sliced from the source body string AFTER
# the match — NOT captured inside the regex — so that ``re.finditer``
# can find subsequent plan lines without the rest-window swallowing
# them.
_PLAN_LINE_RE = re.compile(
    r"(?P<beds>\d+|studio|one|two|three|four|five|six)\s*"  # 2026-05-23: word-nums (Country Club "One Bedroom/One Bath")
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"  # 2026-05-23: added "bed" — Alders Grove style "2 Bed, 1.5 Bath"
    r"\s*[/|,\-]?\s*"  # 2026-05-23: added "," — TGM Ridge style "1 bedroom, 1 bathroom"
    r"(?P<baths>\d+(?:\.\d+)?|one|two|three|four|five|six)\s*"  # word-nums for baths too
    r"(?:bathroom|bath|bth|ba)s?\b",
    re.IGNORECASE,
)
_WORD_TO_DIGIT = {
    "studio": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6",
}
# 2026-05-23: single-bed-with-rent pattern. Many sites advertise plans
# in promo-banner style: "1 Bedrooms starting at $1500/month" with no
# range and no bath token. Match when followed within ~80 chars by
# "$NNNN" (and optionally "/month") so we don't pick up amenity blurbs
# whose closest $ is farther away.
_PLAN_LINE_BED_WITH_RENT_RE = re.compile(
    r"(?P<beds>\d+|studio)\s*"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"
    r"(?!\s*[/|,\-]?\s*\d+(?:\.\d+)?\s*(?:bathroom|bath|bth|ba))",
    re.IGNORECASE,
)
# 2026-05-23: bed-only line — many bespoke / aggregator sites
# (Sunflower portal: "1, 2 beds" + "$1959 - $2409" in adjacent <div>s)
# omit the bath token from the same line. Match a bare bed token (incl.
# "1, 2 beds" range form), then the post-match logic picks up the
# nearby $-rent. Studio is matched as 0-bed. Verified live 2026-05-23
# on pleasantviewgardensnj.com (Sunflower aggregator) + 7 other
# bed-only sites in the DOM_GENERIC cohort.
_PLAN_LINE_BED_ONLY_RE = re.compile(
    r"(?P<low>\d+|studio)\s*"
    r"(?:[,&]|\s+(?:and|to|\-)\s*)\s*"  # range separator: ",", "&", "and", "to", "-"
    r"(?P<high>\d+)\s+"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"  # 2026-05-23: added "bed" — "1, 2 beds"
    r"(?!\s*[/|-]?\s*\d+(?:\.\d+)?\s*(?:bathroom|bath|bth|ba))",
    re.IGNORECASE,
)
# Lookahead window after a plan-line match. Sized to capture realistic
# "${rent}" placement (15-80 chars after baths) while rejecting nav-menu
# items whose closest $ is hundreds of chars away in the body.
_REST_WINDOW = 100
# Pattern that signals the start of the NEXT plan in the body — when
# this appears in the lookahead window BEFORE the $, we've crossed a
# plan boundary (the current "plan" was a nav-menu item paired with a
# later body plan's price). Drop the row.
#
# 2026-05-25 (deep-probe FINDINGS.md cluster A): added bare ``bed`` to the
# alternation. ``_PLAN_LINE_RE`` already matches ``"2 Bed | 2 Bath"`` (note
# ``bed`` is in its bed-token alternation) so the boundary check MUST
# also recognise it; otherwise rent from plan-2 leaks back to plan-1.
# Signature property: 1045 on the Park (Elementor "Starting at $X
# 1 Bed | 1 Bath" cards).
_NEXT_PLAN_BOUNDARY_RE = re.compile(
    r"\d+\s*(?:bedroom|bdrm|bed|bd|br)s?\b|\bstudio\b",
    re.IGNORECASE,
)
# 2026-05-25: Elementor-style "Starting at $X" / "From $X" cards put the
# rent IMMEDIATELY BEFORE the bed/bath token rather than after it. The
# forward lookahead (_REST_WINDOW after baths) misses these — so when
# the forward search finds no rent, fall back to a tighter BACKWARDS
# window (~_BACK_REST_WINDOW chars before the beds token). Gate: must
# not cross a prior plan boundary (otherwise rent from plan N-1 would
# leak to plan N). Signature: 1045 on the Park, Princeton Management
# Elementor-themed WP sites, ~30 props in the GENERIC_PLAN_TEXT cohort
# from the 2026-05-25 deep-probe.
_BACK_REST_WINDOW = 80
# Tight backwards window — used to PREFER a close-by backwards rent over
# whatever the forward window might pick up. "Starting at $2,127 1 Bed
# | 1 Bath" — the $X sits within ~25 chars before the beds token.
_BACK_TIGHT_WINDOW = 30
_BACK_PRICE_RE = re.compile(
    # REQUIRE the "starting at" prefix — this is the Elementor-card
    # signature. "From $X" is left out deliberately: Stargate-style
    # plain plan rows use "X Bedroom / Y Bath From $Z" where the
    # "From $Z" TRAILS its own plan, and would leak back into the next
    # plan if accepted here. Forward-pass already handles "From $Z" for
    # those rows.
    r"starting\s+at\s+\$\s*([\d,]+(?:\.\d{1,2})?)\s*$",
    re.IGNORECASE,
)
# Backwards-direction boundary: ANY bed-or-bath token in the look-back
# window signals we've crossed a prior plan's text. Wider than the
# forward boundary (which only checks for the NEXT plan opening) — here
# we need to catch a prior plan's CLOSING tokens too. Without this,
# Stargate-style text ("3 Bedroom / 2 Bathroom From $1655" preceded by
# "2 Bedroom / 1.5 Bathroom From $1425") would have $1425 leak back
# into plan 3's rent via the tight backwards lookup.
_BACK_BOUNDARY_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:bedroom|bdrm|bed|bd|br|bathroom|bath|bth|ba)s?\b|\bstudio\b",
    re.IGNORECASE,
)
_SQFT_RE = re.compile(r"(\d[\d,]{2,4})\s*sq\.?\s*(?:ft|feet)", re.IGNORECASE)
# Match either a single price (From $X / $X) OR a range ($X - $X).
_PRICE_RE = re.compile(
    r"(?:from\s+)?\$\s*([\d,]+)(?:\s*-\s*\$?\s*([\d,]+))?",
    re.IGNORECASE,
)
# Distinguishing deposit-style amounts ($300, $500) from rent ($1,275)
# by magnitude. Rent floor: anything under $400 is unlikely to be a
# multifamily monthly rent in any US market.
_RENT_FLOOR = 400

# 2026-05-23: RealPage embedded-JSON pattern. Many sites (e.g. Aven
# apartments) ship the full unit list inline as JSON blobs with the
# RealPage field names: ``"ApartmentNumber":"3204-11","MinPrice":1174.0,
# "MaxPrice":1730.0,"SqFt":745,"FloorPlanType":"1Bedroom"``. Same data
# the OLL widget XHR returns — directly extractable without rendering.
# Anchored on the RealPage-specific ``MinPrice`` + ``ApartmentNumber``
# combo so non-RealPage sites can't misfire.
_REALPAGE_UNIT_BLOB_RE = re.compile(
    r'\{[^{}]*?"ApartmentNumber"\s*:\s*"([^"]+)"[^{}]*?'
    r'"MinPrice"\s*:\s*(\d+(?:\.\d+)?)[^{}]*?'
    r'(?:"MaxPrice"\s*:\s*(\d+(?:\.\d+)?)[^{}]*?)?'
    r'"SqFt"\s*:\s*(\d+(?:\.\d+)?)[^{}]*?'
    r'(?:"FloorPlanType"\s*:\s*"([^"]*)"[^{}]*?)?'
    r'\}',
    re.IGNORECASE | re.DOTALL,
)
_FP_TYPE_BEDS_RE = re.compile(
    r"(\d+|studio)\s*(?:bedroom|bdrm|bed|br|bd)", re.IGNORECASE
)
# 2026-05-23: JSON-LD priceRange extractor. Pedcor and similar
# operators ship LocalBusiness schema with ``"priceRange":"$1,355 -
# $1,810"`` — property-level rent only, no per-plan breakdown. When no
# other plan-row pattern matches, emit ONE plan-level row carrying the
# rent range so the property crosses FAILED_NO_DATA → SUCCESS. The
# operator-sqft-gap flag downstream marks sqft as NOT_PUBLISHED.
_PRICE_RANGE_JSONLD_RE = re.compile(
    r'"priceRange"\s*:\s*"\$?\s*([\d,]+)\s*-\s*\$?\s*([\d,]+)"',
    re.IGNORECASE,
)
# Labeled "Price: $1,070 a month" / "Price: $X / mo" pattern
# (Westbury Square style). Captures property-level rent when a labeled
# Price field exists. Also matches "Monthly Rent: $N" (Annaberg) and
# "Total Monthly Due: $N" (Augusta Rental Homes style).
_LABELED_PRICE_RE = re.compile(
    r"\b(?:Price|Monthly\s+Rent|Total\s+Monthly\s+Due|Rent)\s*:\s*"
    r"\$\s*([\d,]+(?:\.\d{1,2})?)\s*"
    r"(?:a\s+month|/\s*mo|per\s+month|\.\d{0,2}\s*$|\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Bed-range + rent-range format: "$1,530 - $2,000 / 1 3 Bedroom(s), 1 2 Bath(s)"
# (Harvard Apartments style) — emit one row per bed in the range.
_RENT_RANGE_BED_RANGE_RE = re.compile(
    r"\$\s*(?P<low>[\d,]+(?:\.\d{1,2})?)\s*-\s*\$\s*(?P<high>[\d,]+(?:\.\d{1,2})?)\s*"
    r"/\s*(?P<beds_low>\d+)\s+(?P<beds_high>\d+)\s+"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b",
    re.IGNORECASE,
)
# "20H Kensington Circle-$2600.00" — unit-id + street + rent
# (Tor View Village). Unit-id followed by street name (Circle/Drive/
# Court/etc.) then literal hyphen-or-dash then $rent. Tight gate
# because the street suffix word list is short.
_UNIT_STREET_RENT_RE = re.compile(
    r"\b(?P<unit>[A-Z0-9]{1,8})\s+(?P<street>[A-Z][a-z]+)\s+"
    r"(?:Circle|Drive|Court|Street|Avenue|Way|Lane|Place|Road)\s*"
    r"-+\s*\$\s*(?P<rent>\d{3,5}(?:\.\d{1,2})?)\b"
)

# 2026-05-23: Wix per-bedroom-subpage labeled block — Westgate Village
# Townhouses style. Wix sites with one URL per bedroom count
# (``/onebedroom``, ``/twobedroom``) render a colon-separated key list:
#
#   Bed: 1
#   Bath: 1
#   SQ.FT.: 680
#   Rent: $1150-1200
#
# All four fields appear within ~400 chars of each other (DOTALL gap).
# Rent supports both single ($1150) and range ($1150-1200). Sqft is
# 3-4 digits. Bed/Bath are integers or 0.5 increments.
_WIX_LABELED_BLOCK_RE = re.compile(
    r"Bed\s*:\s*(?P<beds>\d+|Studio)\s*"
    r".{0,200}?"
    r"Bath\s*:\s*(?P<baths>\d+(?:\.\d+)?)\s*"
    r".{0,200}?"
    r"SQ\.?\s*FT\.?\s*:\s*(?P<sqft>\d{2,4})\s*"
    r".{0,200}?"
    r"Rent\s*:\s*\$\s*(?P<rent_low>[\d,]+)\s*"
    r"(?:[-–]\s*\$?\s*(?P<rent_high>[\d,]+))?",
    re.IGNORECASE | re.DOTALL,
)

# 2026-05-23: Wix Allure-style floor-plan section — the operator wraps
# each plan in a Wix section with the canonical text run:
#
#   854 sq. ft. Prices starting at $1714 A1 ONE BEDROOM ONE BATHROOM
#
# Order: SQFT first, then "Prices starting at" prefix + rent, then
# plan_name (1-4 alphanumeric / hyphen chars), then a word-form
# bed/bath descriptor (ONE/TWO/THREE/STUDIO + BEDROOM + ONE/TWO/...
# + BATHROOM). The descriptor is the gate that prevents marketing
# blurbs from false-matching.
_WIX_SECTION_PLAN_RE = re.compile(
    r"(?P<sqft>\d{3,4})\s*sq\.?\s*ft\.?\s*"
    r"(?:Prices?\s+starting\s+at\s+)?"
    r"\$\s*(?P<rent>[\d,]+(?:\.\d{1,2})?)\s*"
    r"(?P<plan>[A-Z][A-Z0-9\-]{0,5})\s*"
    r"(?P<beds_word>STUDIO|ONE|TWO|THREE|FOUR|FIVE)\s+BEDROOM[S]?\s+"
    r"(?P<baths_word>ONE|TWO|THREE|FOUR|FIVE|HALF)\s+BATHROOM",
    re.IGNORECASE,
)

# Word-to-int for the bed/bath word forms above. "HALF" only appears in
# "ONE AND A HALF" / "TWO AND A HALF" idioms which the current regex
# doesn't capture; kept here for completeness.
_WIX_WORD_TO_INT: dict[str, str] = {
    "STUDIO": "0",
    "ONE": "1",
    "TWO": "2",
    "THREE": "3",
    "FOUR": "4",
    "FIVE": "5",
}
# Murphy Varnish availability-table row: "101 2 Bed 2 1,170 $2,450
# Not Available" / "102 Studio 1 708 $2,100 Available". 5-column
# table flattened by HTML strip. Captures unit + beds + bath + sqft +
# rent in one regex.
# "Studio" appears WITHOUT a following "Bed" word; numeric counts
# REQUIRE the Bed[room]s suffix. Optional "+ Den" tolerated.
_AVAIL_TABLE_ROW_RE = re.compile(
    r"\b(?P<unit>\d{1,4}[A-Z]?)\s+"
    r"(?:"
    r"  Studio"
    r"  |"
    r"  (?P<beds_num>\d+)\s+(?:Bed(?:room)?s?\b(?:\s*\+\s*Den)?)"
    r")\s+"
    r"(?P<baths>\d+(?:\.\d+)?)\s+"
    r"(?P<sqft>\d[\d,]{2,4})\s+"
    r"\$\s*(?P<rent>[\d,]+(?:\.\d{1,2})?)\b",
    re.IGNORECASE | re.VERBOSE,
)
# "primary suites" non-standard bedroom alias (Conklin Townhomes).
_PRIMARY_SUITES_RE = re.compile(
    r"(?P<beds>\d+)\s+primary\s+suites?\b[^$]{0,200}?\$\s*"
    r"(?P<rent>[\d,]+(?:\.\d{1,2})?)\s*/?\s*(?:month|mo|per\s+month)?",
    re.IGNORECASE | re.DOTALL,
)
# Bed-count + $rent pattern with "+" suffix and no bath — Forest View
# uses "1 Bed $987+ 2 Bed $1283+ 3 Bed $1772+" (compact summary).
# Strict $-rent-with-plus-suffix gate avoids amenity matches.
_BED_RENT_PLUS_RE = re.compile(
    r"(?P<beds>\d+|studio)\s+"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"
    r"\s*\$\s*(?P<rent>[\d,]+)\s*\+",
    re.IGNORECASE,
)
# Bed + plan letter + $rent (Aster Village Two style):
# "1 Bedroom A $1,350" — table-row format with a one-letter plan code
# between the bed token and the rent. Plan letter is 1-3 chars
# (A / A1 / B2A) to keep the gate tight.
_BED_PLAN_LETTER_RENT_RE = re.compile(
    r"(?P<beds>\d+|studio)\s+"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"
    r"\s+(?P<plan>[A-Z][A-Z0-9]{0,2})\s+\$\s*(?P<rent>[\d,]+(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
# Bed + explicit $-range pattern: "1 Bedroom $965.00 - $1,190.00".
# Windpoint Apartments style. No /month required — the explicit
# low-high $-range is a strong-enough signal on its own.
_BED_RENT_RANGE_RE = re.compile(
    r"(?P<beds>\d+|studio)\s+"
    r"(?:bedroom|bdrm|bed|bd|br)s?\b"
    r"\s+\$\s*(?P<low>[\d,]+(?:\.\d{1,2})?)\s*-\s*\$?\s*(?P<high>[\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def _parse_realpage_unit_blobs(body: str, url: str) -> list[dict]:
    """Extract UNIT-LEVEL records from inline RealPage JSON blobs.

    RealPage's OneSite/OLL widget often SSRs the unit list as a JSON
    array INSIDE the page HTML — same fields as the XHR returns:
    ``{"ApartmentNumber":"3204-11","MinPrice":1174.0,"MaxPrice":1730.0,
    "SqFt":745,"FloorPlanType":"1Bedroom"}``. When the OLL XHR doesn't
    fire during Playwright capture, this blob extraction recovers the
    same data — no second round trip.

    Verified 2026-05-23 live on Aven (pid=3148): the homepage HTML
    carries N unit blobs with full per-unit rent + sqft.
    """
    if not body or '"ApartmentNumber"' not in body or '"MinPrice"' not in body:
        return []
    out: list[dict] = []
    seen_apts: set[str] = set()
    for m in _REALPAGE_UNIT_BLOB_RE.finditer(body):
        apt = m.group(1).strip()
        if not apt or apt in seen_apts:
            continue
        seen_apts.add(apt)
        try:
            rent_low = int(float(m.group(2)))
        except (TypeError, ValueError):
            continue
        try:
            rent_high = int(float(m.group(3))) if m.group(3) else rent_low
        except (TypeError, ValueError):
            rent_high = rent_low
        try:
            sqft = str(int(float(m.group(4))))
        except (TypeError, ValueError):
            sqft = ""
        fp_type = (m.group(5) or "").strip()
        beds = ""
        bath = ""
        if fp_type:
            bm = _FP_TYPE_BEDS_RE.search(fp_type)
            if bm:
                bv = bm.group(1).lower()
                beds = "0" if bv == "studio" else bv
        if rent_low < _RENT_FLOOR:
            continue
        out.append(
            make_unit_dict(
                floor_plan_name=fp_type or "",
                bed_label=bed_label_from(
                    int(beds) if beds.isdigit() else None, fp_type or ""
                ),
                bedrooms=beds,
                bathrooms=bath,
                sqft=sqft,
                unit_number=apt,
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_REALPAGE_BLOB",
            )
        )
    return out


def _unescape_js_entities(s: str) -> str:
    """Replace common JS \\u003e / \\u003c / &amp; / &#8211; entity
    sequences with their literal characters. Wix-rendered pages embed
    plan/rent data inside ``\\u003cstrong\\u003e$1,608\\u003c/strong\\u003e``
    JSON blobs that the text-flatten step doesn't undo. Apply
    defensively across all bodies — no-op when no entities are present.
    """
    if not s:
        return s
    if "\\u003" in s:
        s = (
            s.replace("\\u003e", ">")
             .replace("\\u003c", "<")
             .replace("\\u0026", "&")
             .replace("\\u0022", '"')
             .replace("\\u0027", "'")
        )
    if "&#" in s:
        s = (
            s.replace("&#8211;", "-")
             .replace("&#8212;", "-")
             .replace("&#8217;", "'")
             .replace("&amp;", "&")
             .replace("&nbsp;", " ")
        )
    return s


def _bodytext_from_fetch_result(ctx: AdapterContext) -> str:
    """Synthesise the equivalent of ``document.body.innerText`` from the
    L1 fetch_result body.

    Used by the static-body fallback in ``GenericPlanTextAdapter.extract``
    when the orchestrator dispatches the adapter without a live
    Playwright page (Phase 6 stub case). Mirrors the transform the
    subpage-fallback already applies so both paths feed the same regex
    pipeline. Returns ``""`` when there's no usable body.
    """
    fr = getattr(ctx, "fetch_result", None)
    raw = getattr(fr, "body", None) if fr is not None else None
    if isinstance(raw, bytes):
        try:
            raw_str = raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    elif isinstance(raw, str):
        raw_str = raw
    else:
        return ""
    if not raw_str:
        return ""
    # Drop <script> and <style> blocks first — innerText leaves them out
    # too, and they otherwise pollute the rent/plan regex with code that
    # mentions floor-plan SKUs (e.g. ``var plans = [...]``).
    raw_str = re.sub(
        r"<script\b[^>]*>.*?</script>", " ", raw_str, flags=re.DOTALL | re.IGNORECASE
    )
    raw_str = re.sub(
        r"<style\b[^>]*>.*?</style>", " ", raw_str, flags=re.DOTALL | re.IGNORECASE
    )
    flat = _unescape_js_entities(raw_str)
    flat = re.sub(r"<[^>]+>", " ", flat)
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat


def parse_generic_plan_text(body: str, url: str) -> list[dict]:
    """Extract plan-level rows from page body text. Returns [] when no
    rows pass the ≥2-plan / has-rent guards."""
    if not body:
        return []
    # 2026-05-23: pre-pass entity unescape (Wix encoded JSON blobs).
    body = _unescape_js_entities(body)
    # 2026-05-23: First — try RealPage inline-JSON-blob extraction.
    # This produces UNIT-LEVEL data (not just plan-level) and wins
    # outright when present. Only RealPage-shaped pages match (gated
    # on both "ApartmentNumber" + "MinPrice" markers).
    rp_units = _parse_realpage_unit_blobs(body, url)
    if len(rp_units) >= 2:
        return rp_units
    out: list[dict] = []
    seen_signatures: set[tuple] = set()

    # 2026-05-23: 0.5th pass — availability-table row (UNIT-LEVEL).
    # Runs BEFORE the primary bed+bath pass because it produces
    # higher-quality rows (unit_num + sqft + rent + bed + bath in one
    # match). Lower-quality bed-only/bed+bath passes might otherwise
    # consume the same input and fill out to ≥2 before this pass runs.
    for m in _AVAIL_TABLE_ROW_RE.finditer(body):
        unit = m.group("unit")
        # Two-branch beds: "Studio" literal OR numeric+Bed[room]s
        beds_num = m.group("beds_num")
        if beds_num is None:
            beds = 0  # Studio matched
        else:
            try:
                beds = int(beds_num)
            except ValueError:
                continue
        if beds > 6:
            continue
        baths = m.group("baths")
        sqft = m.group("sqft").replace(",", "")
        rent = money_to_int(m.group("rent"))
        if rent is None or rent < _RENT_FLOOR:
            continue
        try:
            sqft_i = int(sqft)
            if not (150 <= sqft_i <= 10000):
                continue
        except ValueError:
            continue
        sig = (beds, baths, sqft, rent, rent, unit)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        plan_name = f"{beds} Bedroom" if beds else "Studio"
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds),
                bathrooms=baths,
                sqft=sqft,
                unit_number=unit,
                rent_low=rent,
                rent_high=rent,
                rent_range=format_rent_range(rent, rent),
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
            )
        )
    for m in _PLAN_LINE_RE.finditer(body):
        bed_v = m.group("beds").lower()
        if bed_v in _WORD_TO_DIGIT:
            bed_v = _WORD_TO_DIGIT[bed_v]
        try:
            beds = int(bed_v)
        except ValueError:
            continue  # unexpected — skip
        baths = m.group("baths").lower()
        if baths in _WORD_TO_DIGIT:
            baths = _WORD_TO_DIGIT[baths]
        # Pull the lookahead window from the source body — NOT a capture
        # group — so finditer can still find subsequent plan lines.
        rest = body[m.end():m.end() + _REST_WINDOW]
        # Sqft (optional — many bespoke sites omit it).
        sqft = ""
        sq_m = _SQFT_RE.search(rest)
        if sq_m:
            sqft = sq_m.group(1).replace(",", "")
        rent_low: int | None = None
        rent_high: int | None = None
        # 2026-05-25 (deep-probe cluster A): PREFER a tight backwards
        # rent over the forward window. Elementor-style cards (1045 on
        # the Park, Princeton Mgmt WP sites) put rent IMMEDIATELY
        # BEFORE the bed/bath token: "Starting at $2,127 1 Bed | 1
        # Bath". When the forward window picks first, it leaks the
        # NEXT plan's rent (the boundary regex doesn't fire on bare
        # "Starting at $X" between plans). A close backwards match
        # is high-confidence — try it first within the tight window.
        tight_start = max(0, m.start() - _BACK_TIGHT_WINDOW)
        tight = body[tight_start:m.start()]
        # Strip at the most recent prior plan boundary so anything left
        # is text that genuinely leads our current plan. The Elementor
        # signature (Starting at $X N Bed) survives — Stargate-style
        # "From $X N Bedroom" doesn't because _BACK_PRICE_RE requires
        # the "starting at" prefix.
        boundary_iter = list(_BACK_BOUNDARY_RE.finditer(tight))
        if boundary_iter:
            tight = tight[boundary_iter[-1].end():]
        tm = _BACK_PRICE_RE.search(tight)
        if tm:
            low = money_to_int(tm.group(1))
            if low is not None and low >= _RENT_FLOOR:
                rent_low = low
                rent_high = low
        # Forward window — only if tight backwards found nothing.
        # If the forward window contains the start of ANOTHER plan-line
        # BEFORE the first $-amount, we've crossed a plan boundary
        # (current was a nav-menu item, not a real plan row). Drop.
        if rent_low is None:
            for p_m in _PRICE_RE.finditer(rest):
                low = money_to_int(p_m.group(1))
                high = money_to_int(p_m.group(2)) if p_m.group(2) else low
                if low is None or low < _RENT_FLOOR:
                    continue
                preceding = rest[: p_m.start()]
                if _NEXT_PLAN_BOUNDARY_RE.search(preceding):
                    continue
                rent_low = low
                rent_high = high if high is not None else low
                break
        # Wider backwards fallback — same logic as tight, larger window.
        # Catches "Starting at $X .. some text .. N Bed | N Bath" layouts
        # where the rent and bed/bath aren't quite adjacent.
        if rent_low is None:
            back_start = max(0, m.start() - _BACK_REST_WINDOW)
            back = body[back_start:m.start()]
            boundary_iter = list(_BACK_BOUNDARY_RE.finditer(back))
            if boundary_iter:
                back = back[boundary_iter[-1].end():]
            bm = _BACK_PRICE_RE.search(back)
            if bm:
                low = money_to_int(bm.group(1))
                if low is not None and low >= _RENT_FLOOR:
                    rent_low = low
                    rent_high = low
        # Skip rows without a real rent — defends against rows like
        # "1 Bedroom 1 Bath required deposit $300".
        if rent_low is None:
            continue
        sig = (beds, baths, sqft, rent_low, rent_high)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        plan_name = f"{beds} Bedroom / {baths} Bath" if beds else f"Studio / {baths} Bath"
        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(beds, plan_name),
                bedrooms=str(beds),
                bathrooms=baths,
                sqft=sqft,
                unit_number="",  # plan-level only
                rent_low=rent_low,
                rent_high=rent_high,
                rent_range=format_rent_range(rent_low, rent_high),
                availability_status="AVAILABLE",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
            )
        )
    # 2026-05-23: Second pass — bed-only range pattern (Sunflower
    # aggregator style: "1, 2 beds" + "$1959 - $2409" in adjacent
    # divs). Only runs when the primary bed+bath pass produced fewer
    # than 2 rows — strictly additive. Emits one row per distinct bed
    # count in the range (so "1, 2 beds" → two rows: one for 1-bed,
    # one for 2-bed, both at the same rent range).
    if len(out) < 2:
        for m in _PLAN_LINE_BED_ONLY_RE.finditer(body):
            low_v = m.group("low")
            high_v = m.group("high")
            beds_low = 0 if low_v.lower() == "studio" else int(low_v)
            beds_high = int(high_v) if high_v.isdigit() else beds_low
            if beds_high < beds_low or beds_high > 6:
                continue  # implausible range — skip
            rest = body[m.end():m.end() + _REST_WINDOW]
            rent_low = rent_high = None
            for p_m in _PRICE_RE.finditer(rest):
                low = money_to_int(p_m.group(1))
                high = money_to_int(p_m.group(2)) if p_m.group(2) else low
                if low is None or low < _RENT_FLOOR:
                    continue
                preceding = rest[: p_m.start()]
                if _NEXT_PLAN_BOUNDARY_RE.search(preceding):
                    continue
                rent_low = low
                rent_high = high if high is not None else low
                break
            if rent_low is None:
                continue
            # Emit one row per bed count in the range.
            for beds in range(beds_low, beds_high + 1):
                sig = (beds, "", "", rent_low, rent_high)
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                plan_name = (
                    f"{beds} Bedroom" if beds else "Studio"
                )
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(beds, plan_name),
                        bedrooms=str(beds),
                        bathrooms="",
                        sqft="",
                        unit_number="",
                        rent_low=rent_low,
                        rent_high=rent_high,
                        rent_range=format_rent_range(rent_low, rent_high),
                        availability_status="AVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                    )
                )

    # 2026-05-23: Third pass — single-bed-with-rent. Promo-banner
    # style "1 Bedrooms starting at $1500/month" — no range, no bath.
    # Strict $-proximity required to avoid amenity-blurb matches.
    if len(out) < 2:
        TIGHT_WINDOW = 80
        for m in _PLAN_LINE_BED_WITH_RENT_RE.finditer(body):
            bed_v = m.group("beds")
            beds = 0 if bed_v.lower() == "studio" else int(bed_v)
            if beds > 6:
                continue
            rest = body[m.end():m.end() + TIGHT_WINDOW]
            # Require a $ AND a per-month signal nearby (filters out
            # "1 Bedroom apartments starting at $500 deposit" deposits).
            rent_low = rent_high = None
            for p_m in _PRICE_RE.finditer(rest):
                low = money_to_int(p_m.group(1))
                high = money_to_int(p_m.group(2)) if p_m.group(2) else low
                if low is None or low < _RENT_FLOOR:
                    continue
                preceding = rest[: p_m.start()]
                if _NEXT_PLAN_BOUNDARY_RE.search(preceding):
                    continue
                # Strict gate: require /month or /mo in the same window
                # (the promo-banner format always carries it).
                tail = rest[p_m.end():p_m.end() + 30].lower()
                if not (
                    "/month" in tail or "/mo" in tail or "per month" in tail
                    or "monthly" in tail
                ):
                    continue
                rent_low = low
                rent_high = high if high is not None else low
                break
            if rent_low is None:
                continue
            sig = (beds, "", "", rent_low, rent_high)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            plan_name = f"{beds} Bedroom" if beds else "Studio"
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds),
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=rent_low,
                    rent_high=rent_high,
                    rent_range=format_rent_range(rent_low, rent_high),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                )
            )

    # 2026-05-23: 3.25th pass — bed + plan-letter + $rent
    # (Aster Village Two: "1 Bedroom A $1,350"). Plan-letter gate
    # filters out random "$N" attached after a bed token.
    if len(out) < 2:
        for m in _BED_PLAN_LETTER_RENT_RE.finditer(body):
            bed_v = m.group("beds").lower()
            if bed_v in _WORD_TO_DIGIT:
                bed_v = _WORD_TO_DIGIT[bed_v]
            try:
                beds = int(bed_v)
            except ValueError:
                continue
            if beds > 6:
                continue
            rent = money_to_int(m.group("rent"))
            if rent is None or rent < _RENT_FLOOR:
                continue
            plan_letter = m.group("plan").upper()
            sig = (beds, "", "", rent, rent, plan_letter)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            plan_name = (
                f"{beds} Bedroom {plan_letter}" if beds else f"Studio {plan_letter}"
            )
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds),
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=rent,
                    rent_high=rent,
                    rent_range=format_rent_range(rent, rent),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                )
            )

    # 2026-05-23: 3.5th pass — bed + explicit rent range
    # "1 Bedroom $965 - $1,190" (Windpoint style). Explicit
    # $LOW - $HIGH is strong-enough signal alone (no /month gate).
    if len(out) < 2:
        for m in _BED_RENT_RANGE_RE.finditer(body):
            bed_v = m.group("beds").lower()
            if bed_v in _WORD_TO_DIGIT:
                bed_v = _WORD_TO_DIGIT[bed_v]
            try:
                beds = int(bed_v)
            except ValueError:
                continue
            if beds > 6:
                continue
            low = money_to_int(m.group("low"))
            high = money_to_int(m.group("high"))
            if low is None or low < _RENT_FLOOR or high is None or high < low:
                continue
            sig = (beds, "", "", low, high)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            plan_name = f"{beds} Bedroom" if beds else "Studio"
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds),
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=low,
                    rent_high=high,
                    rent_range=format_rent_range(low, high),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                )
            )

    # 2026-05-23: Fourth pass — "X Bed $RENT+" compact summary
    # (Forest View style). Strict + suffix gate. Each pass is additive.
    if len(out) < 2:
        for m in _BED_RENT_PLUS_RE.finditer(body):
            bed_v = m.group("beds")
            beds = 0 if bed_v.lower() == "studio" else int(bed_v)
            if beds > 6:
                continue
            rent = money_to_int(m.group("rent"))
            if rent is None or rent < _RENT_FLOOR:
                continue
            sig = (beds, "", "", rent, rent)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            plan_name = f"{beds} Bedroom" if beds else "Studio"
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds),
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=rent,
                    rent_high=rent,
                    rent_range=format_rent_range(rent, rent),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                )
            )

    # (Murphy-style availability-table row pass moved to the top of
    # this function so it runs BEFORE the primary bed+bath pass.)

    # 2026-05-23: 4.05th pass — unit-id + street + rent
    # ("20H Kensington Circle-$2600.00" — Tor View Village). Emits a
    # UNIT-LEVEL row with the captured unit_id.
    if len(out) < 2:
        for m in _UNIT_STREET_RENT_RE.finditer(body):
            rent = money_to_int(m.group("rent"))
            if rent is None or rent < _RENT_FLOOR:
                continue
            unit = m.group("unit")
            sig = (None, "", "", rent, rent, unit)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            out.append(
                make_unit_dict(
                    floor_plan_name="",
                    bed_label="",
                    bedrooms="",
                    bathrooms="",
                    sqft="",
                    unit_number=unit,
                    rent_low=rent,
                    rent_high=rent,
                    rent_range=format_rent_range(rent, rent),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_UNIT_STREET",
                )
            )

    # 2026-05-23: 4.1th pass — "primary suites" alias (Conklin
    # Townhomes "2 primary suites ... $2,050/month"). Extract sqft from
    # the in-match context so multiple "2 primary suites" matches with
    # the same rent but different sqft (Type I vs Type II floorplan)
    # dedup correctly and BOTH rows emit.
    if len(out) < 2:
        for m in _PRIMARY_SUITES_RE.finditer(body):
            try:
                beds = int(m.group("beds"))
            except ValueError:
                continue
            if beds > 6:
                continue
            rent = money_to_int(m.group("rent"))
            if rent is None or rent < _RENT_FLOOR:
                continue
            # Pull sqft from the between-bed-and-$ window.
            between = m.group(0)
            sqft_m = re.search(r"(\d[\d,]{2,4})\s*sq\.?\s*(?:ft|feet)", between, re.I)
            sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""
            sig = (beds, "", sqft, rent, rent)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            plan_name = f"{beds} Primary Suites Townhome"
            if sqft:
                plan_name += f" {sqft}sf"
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name,
                    bed_label=bed_label_from(beds, plan_name),
                    bedrooms=str(beds),
                    bathrooms="",
                    sqft=sqft,
                    unit_number="",
                    rent_low=rent,
                    rent_high=rent,
                    rent_range=format_rent_range(rent, rent),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                )
            )

    # 2026-05-23: 4.15th pass — Harvard-style "$LOW - $HIGH / N M
    # Bedroom(s)" (rent-range + bed-range). Emit one row per bed in
    # the range.
    if len(out) < 2:
        for m in _RENT_RANGE_BED_RANGE_RE.finditer(body):
            try:
                low = money_to_int(m.group("low"))
                high = money_to_int(m.group("high"))
                bl = int(m.group("beds_low"))
                bh = int(m.group("beds_high"))
            except (TypeError, ValueError):
                continue
            if low is None or low < _RENT_FLOOR or bh > 6 or bh < bl:
                continue
            for beds in range(bl, bh + 1):
                sig = (beds, "", "", low, high)
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)
                plan_name = f"{beds} Bedroom" if beds else "Studio"
                out.append(
                    make_unit_dict(
                        floor_plan_name=plan_name,
                        bed_label=bed_label_from(beds, plan_name),
                        bedrooms=str(beds),
                        bathrooms="",
                        sqft="",
                        unit_number="",
                        rent_low=low,
                        rent_high=high,
                        rent_range=format_rent_range(low, high),
                        availability_status="AVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT",
                    )
                )

    # 2026-05-23: 4.2nd pass — Wix per-bedroom-subpage labeled block
    # (Westgate Village style). Bed/Bath/SQ.FT./Rent labels in a
    # 400-char window. Emits ONE row per matched block — Wix subpages
    # are per-bedroom so each page = one plan. Rent supports ranges
    # ("$1150-1200" → rent_low=1150, rent_high=1200).
    if len(out) < 2:
        for m in _WIX_LABELED_BLOCK_RE.finditer(body):
            beds_raw = m.group("beds")
            beds_str = "0" if beds_raw.lower() == "studio" else beds_raw
            bed_label = "Studio" if beds_raw.lower() == "studio" else f"{beds_str} Bed"
            sqft_str = m.group("sqft")
            rent_low = money_to_int(m.group("rent_low"))
            rent_high_grp = m.group("rent_high")
            rent_high = money_to_int(rent_high_grp) if rent_high_grp else rent_low
            if rent_low is None or rent_low < _RENT_FLOOR:
                continue
            try:
                sqft_int = int(sqft_str)
            except ValueError:
                continue
            if sqft_int < 100 or sqft_int > 10000:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name=f"{bed_label} Plan",
                    bed_label=bed_label,
                    bedrooms=beds_str,
                    bathrooms=m.group("baths"),
                    sqft=sqft_str,
                    rent_low=rent_low,
                    rent_high=rent_high if rent_high else rent_low,
                    rent_range=format_rent_range(rent_low, rent_high or rent_low),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_LABELED_BLOCK",
                )
            )
            break  # one labeled block per Wix subpage (per-bedroom)

    # 2026-05-23: 4.22nd pass — Wix Allure-style floor-plan section.
    # Each plan rendered in a single text run: "854 sq. ft. Prices
    # starting at $1714 A1 ONE BEDROOM ONE BATHROOM". Captures sqft +
    # rent + plan_name + word-form bed/bath. Emits one row per match.
    if len(out) < 2:
        for m in _WIX_SECTION_PLAN_RE.finditer(body):
            sqft_str = m.group("sqft")
            try:
                sqft_int = int(sqft_str)
            except ValueError:
                continue
            if sqft_int < 100 or sqft_int > 10000:
                continue
            rent_low = money_to_int(m.group("rent"))
            if rent_low is None or rent_low < _RENT_FLOOR:
                continue
            beds_word = m.group("beds_word").upper()
            baths_word = m.group("baths_word").upper()
            beds_str = _WIX_WORD_TO_INT.get(beds_word, "")
            baths_str = _WIX_WORD_TO_INT.get(baths_word, "")
            if not beds_str:
                continue
            bed_label = "Studio" if beds_str == "0" else f"{beds_str} Bed"
            plan_name = m.group("plan").strip()
            out.append(
                make_unit_dict(
                    floor_plan_name=plan_name or f"{bed_label} Plan",
                    bed_label=bed_label,
                    bedrooms=beds_str,
                    bathrooms=baths_str,
                    sqft=sqft_str,
                    rent_low=rent_low,
                    rent_range=format_rent_range(rent_low, rent_low),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_WIX_SECTION_PLAN",
                )
            )

    # 2026-05-23: 4.25th pass — "Price: $1,070 a month" labeled
    # format (Westbury Square style). Strong gate: requires "Price:"
    # prefix AND "a month" / "/mo" suffix. Skipped when a Wix labeled
    # block already produced a row — that row carries rent+sqft+beds
    # and is the canonical record; emitting a duplicate rent-only row
    # would clutter the output.
    _wix_block_present = any(
        u.get("extraction_tier", "").endswith("WIX_LABELED_BLOCK")
        for u in out
    )
    if len(out) < 2 and not _wix_block_present:
        for m in _LABELED_PRICE_RE.finditer(body):
            low = money_to_int(m.group(1))
            if low is None or low < _RENT_FLOOR:
                continue
            out.append(
                make_unit_dict(
                    floor_plan_name="Labeled Property Rent",
                    bed_label="",
                    bedrooms="",
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=low,
                    rent_high=low,
                    rent_range=format_rent_range(low, low),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_LABELED_PRICE",
                    data_gaps=["sqft", "beds"],
                    data_quality_flag="PLAN_RANGE_ONLY",
                )
            )
            break  # one labeled price per property

    # 2026-05-23: 4.5th pass — "Fountainview Townhomes from $1400" /
    # generic "from $N" fallback. Single-value property-level rent,
    # often the property name + "from $X". Same shape as priceRange:
    # emit one row with rent low=high=N, data_gaps for sqft+beds.
    # Only fires when no other passes produced rows.
    if len(out) < 2 and not any(
        u.get("extraction_tier", "").endswith("JSONLD_PRICERANGE") for u in out
    ):
        m = re.search(r"\bfrom\s+\$\s*([\d,]+(?:\.\d{1,2})?)", body, re.I)
        # Gate: must NOT be preceded by "bedroom" within 60 chars
        # (filters out amenity blurbs like "1 bedroom from $500"). The
        # property-level "from $N" pattern usually has a property name
        # before it, not a bed count.
        if m:
            preceding = body[max(0, m.start() - 60):m.start()]
            if re.search(r"(?:bedroom|bdrm|bed|bd|br)s?\b", preceding, re.I):
                m = None
        if m:
            low = money_to_int(m.group(1))
            if low is not None and low >= _RENT_FLOOR:
                out.append(
                    make_unit_dict(
                        floor_plan_name="Property Starting Rent",
                        bed_label="",
                        bedrooms="",
                        bathrooms="",
                        sqft="",
                        unit_number="",
                        rent_low=low,
                        rent_high=low,
                        rent_range=format_rent_range(low, low),
                        availability_status="AVAILABLE",
                        source_api_url=url,
                        extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_FROM_PRICE",
                        data_gaps=["sqft", "beds"],
                        data_quality_flag="PLAN_RANGE_ONLY",
                    )
                )

    # 2026-05-23: Fifth pass — JSON-LD priceRange-only fallback.
    # Pedcor + similar operators ship single property-level rent range
    # in LocalBusiness schema. Emit ONE row carrying the rent range so
    # the property crosses FAILED_NO_DATA → SUCCESS_PLAN_LEVEL. Marks
    # sqft + beds as documented data gaps (operator-policy-only).
    if len(out) < 2:
        for m in _PRICE_RANGE_JSONLD_RE.finditer(body):
            low = money_to_int(m.group(1))
            high = money_to_int(m.group(2)) if m.group(2) else low
            if low is None or low < _RENT_FLOOR:
                continue
            # Emit one row. Bed/bath/sqft empty — operator doesn't
            # publish per-plan breakdown.
            out.append(
                make_unit_dict(
                    floor_plan_name="Property Rent Range",
                    bed_label="",
                    bedrooms="",
                    bathrooms="",
                    sqft="",
                    unit_number="",
                    rent_low=low,
                    rent_high=high,
                    rent_range=format_rent_range(low, high),
                    availability_status="AVAILABLE",
                    source_api_url=url,
                    extraction_tier="TIER_1_DOM_GENERIC_PLAN_TEXT_JSONLD_PRICERANGE",
                    # Document the gaps so downstream verdict marks
                    # this as honest plan-level (not SUCCESS_PLAN_LEVEL
                    # downgrade for parser-miss).
                    data_gaps=["sqft", "beds"],
                    data_quality_flag="PLAN_RANGE_ONLY",
                )
            )
            break  # one priceRange per property — don't keep iterating

    # Anti-noise: require ≥1 row when we're in the priceRange-only
    # fallback (a single plan-range entry from JSON-LD is honest
    # operator-published data — emit it). For all other passes, keep
    # the ≥2 guard to defend against single-match amenity blurbs.
    if len(out) == 0:
        return []
    if len(out) == 1:
        # Single row — accept only when it's the property-level
        # priceRange / from-$N / labeled-Price fallback (carries an
        # explicit tier suffix). All other single-match cases are
        # likely noise.
        tier = out[0].get("extraction_tier", "")
        if tier.endswith((
            "JSONLD_PRICERANGE", "FROM_PRICE", "LABELED_PRICE",
            "UNIT_STREET",  # 2026-05-23: a captured unit-id+rent is a strong-enough signal
            # 2026-05-23: Wix subpage patterns — each subpage is a single
            # plan, so a single match is the canonical output, not noise.
            "WIX_LABELED_BLOCK",
            "WIX_SECTION_PLAN",
        )):
            return out
        return []
    return out


class GenericPlanTextAdapter:
    """Last-resort plan-level text extractor for bespoke custom-CMS sites.

    Detector yields at 0.55 — every recognized PMS / CMS signal beats
    this. Only fires when nothing else routed.
    """

    pms_name: str = "generic_plan_text"
    _fingerprints: list[str] = []  # no host-specific fingerprints

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used="TIER_1_DOM_GENERIC_PLAN_TEXT")
        body = ""

        # 2026-05-24: prior implementation hard-required a live Playwright
        # page (page.evaluate to harvest document.body innerText). Phase
        # 6 of the canary scraper dispatches this adapter with a stub
        # page (no evaluate callable), so all 67 props bucketed as
        # TIER_1_DOM_GENERIC_PLAN_TEXT in focused-3886351 logged the
        # same single error "generic_plan_text: no live page" without
        # ever exercising the parser — even though their captured
        # fetch_result.body carried 6-23 rent signals each.
        #
        # Fix: when page.evaluate isn't available, derive bodyText from
        # ctx.fetch_result.body by mirroring the existing subpage-fallback
        # transform (HTML-entity unescape, drop <script>/<style>, strip
        # tags, collapse whitespace). The DOM-JS path stays the
        # preferred source when live — same regex pipeline downstream.
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                payload = await evaluate(_GENERIC_DOM_JS)
            except Exception as exc:
                log.debug("generic_plan_text evaluate failed err=%s", exc)
                payload = None
            if isinstance(payload, dict):
                body = str(payload.get("bodyText") or "")
            else:
                result.errors.append(
                    "generic_plan_text: DOM JS returned non-dict"
                )

        if not body:
            # Static fallback — read the L1 fetch_result.body and
            # synthesise the same bodyText shape document.body.innerText
            # would yield. Cheap, deterministic, no extra fetch.
            body = _bodytext_from_fetch_result(ctx)
            if body:
                result.errors.append(
                    "generic_plan_text: static-body fallback "
                    "(no live page evaluate)"
                )

        if not body:
            result.confidence = 0.0
            result.errors.append("generic_plan_text: empty body")
            return result
        winning = self._winning_url(page, ctx)
        rows = parse_generic_plan_text(body, winning)
        # 2026-05-23: Subpage fallback. When the homepage produces
        # nothing, try /floorplans/, /floor-plans/, /availability/ via
        # curl_cffi — many bespoke sites publish rent ONLY on these
        # deep pages (Marston Lake, Autumn Oaks, Deer Run, Vineyards,
        # Vineyards — 5 verified live 2026-05-23). Anchored at curl
        # level so the Playwright page state isn't disturbed.
        if not rows:
            from urllib.parse import urlparse as _urlparse

            base_url = str(getattr(ctx, "base_url", "") or "")
            if base_url:
                try:
                    from ma_poc.pms.adapters._probe import probe_get

                    pu = _urlparse(base_url)
                    origin = f"{pu.scheme}://{pu.netloc}"
                    for path in (
                        "/floorplans/", "/floor-plans/",
                        "/availability/", "/apartments/",
                    ):
                        try:
                            r = probe_get(origin + path, timeout=12)
                            if r.status_code != 200 or not r.text:
                                continue
                            sub_unesc = _unescape_js_entities(r.text)
                            sub_flat = re.sub(r"<[^>]+>", " ", sub_unesc)
                            sub_flat = re.sub(r"\s+", " ", sub_flat).strip()
                            sub_rows = parse_generic_plan_text(
                                sub_flat, origin + path
                            )
                            if sub_rows:
                                rows = sub_rows
                                winning = origin + path
                                result.errors.append(
                                    f"generic_plan_text: lifted via subpage "
                                    f"{path} ({len(sub_rows)} rows)"
                                )
                                break
                        except Exception as exc:
                            result.errors.append(
                                f"generic_plan_text: subpage probe error "
                                f"{path}: {type(exc).__name__}"
                            )
                except Exception as exc:
                    result.errors.append(
                        f"generic_plan_text: subpage import error: "
                        f"{type(exc).__name__}"
                    )
        if not rows:
            result.confidence = 0.0
            result.errors.append(
                "generic_plan_text: <2 distinct plan rows found "
                "(anti-noise threshold; falls to LLM)"
            )
            return result
        from ma_poc.extraction.post_process import post_process

        pp = post_process(rows, property_id=getattr(ctx, "property_id", None))
        if pp.n_admitted > 0:
            result.units = pp.admitted
            result.plan_summaries = pp.plan_summaries
            result.winning_url = winning
            # Cap confidence below other plan-level adapters so a real
            # PMS routing always wins when both apply.
            result.confidence = min(0.70, 0.55 + 0.03 * pp.n_admitted)
            return result
        result.confidence = 0.0
        result.errors.append(
            f"generic_plan_text: {len(rows)} rows failed unit_validity"
        )
        return result

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        try:
            return page.url or getattr(ctx, "base_url", "") or ""
        except Exception:
            return getattr(ctx, "base_url", "") or ""

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
