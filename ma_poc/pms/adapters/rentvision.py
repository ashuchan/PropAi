"""
RentVision adapter.

Research log
------------
RentVision is a multifamily marketing-site CMS. The property's own domain
serves a server-side-rendered ``/floorplans`` page whose plan grid carries
full plan-level data in the DOM — no unit XHR/API. jugnu had **no adapter
at all** for it (detector returned NONE), making it the single
highest-frequency missing-adapter cluster in the 2026-05-19 deep probe
(23 confirmed failed cases, all ``tier=NONE``).

Verified live (2026-05-19) against:
  - loftsatlittlecreek.com/floorplans — 45 ``.floorplanItem`` blocks,
    price RANGE shape ("Price $1,075 - $1,100"), "Call for Details!"
  - westgateirving.com/floorplans — 5 blocks, single-price shape
    ("Pricing Starting at $1,339"), "Only 1 Vacant Apartment Left!" /
    "Available" availability variants

Uniform DOM (both sites, identical class names):
  - ``.floorplanItem``           — one per plan; ``data-bedrooms`` /
                                    ``data-floorplan-id`` attributes
  - ``.floorplanName``           — plan name
  - ``.floorplanBeds``           — "Studio Bed" / "2 Bed"
  - ``.floorplanBaths``          — "1 Bath" / "2 Bath"
  - ``.floorplanSquareFootage``  — "926 Sq Ft square feet"
  - ``.floorplanPrice``          — "Pricing Starting at $1,339" |
                                    "Price $1,075 - $1,100" | "Call for Details"
  - ``.floorplanAvailability``   — "Available" | "Only N Vacant Apartment(s)
                                    Left!" | "Call for Details!"

The detection credit ("Powered by RentVision" / "created by RentVision" /
rentvision.com) is site-wide including the landing page, so the detector
fires before any hop; this adapter self-fetches ``/floorplans`` when the
live page is not already there.

2026-05-25 unit-detail drill (user-flagged via Walnut Creek pid 45534)
----------------------------------------------------------------------
The plan-grid page links to per-plan detail pages at
``/floorplans/{bed-tier}/{slug}`` (e.g. ``/floorplans/two-bedroom/greystone``).
Each detail page carries a unit-listing table with real unit numbers,
asking rent, availability status/date, and a move-in date encoded in the
Apply Now button's onclick window.open URL. Pre-fix the adapter emitted
plan-level rows only; post-fix it drills, parses, and prefers unit-level
when the drill returns any rows (falls back to plan-level when the drill
is empty — e.g. heritage plan with no available units).

Per-row DOM (verified live 2026-05-25 on liveatwalnutcreekapts.com
/floorplans/two-bedroom/greystone — 5 units, all identical shape):
  * ``<th class="left wrap">{unit_number}</th>``             — unit ident
  * ``<td class="standard identifiable-links right"><span>${rent}</span>``
                                                              — asking rent
  * inside same <td>, ``<h3>Unit Term Pricing - {unit_number}</h3>``
                                                              — confirms unit
  * ``<td class="standard unit-availability">``              — "Available Now"
    | "Available on <span>{Month DD, YYYY}</span>"
  * ``<td class="unit-actions"><button onclick="...window.open(
    '...moveInDate=MM/DD/YYYY&unit={unit_number}')">Apply Now``
                                                              — move-in date
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

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

# Runs in the page. If the current document is not the /floorplans page,
# fetch it same-origin and parse with DOMParser (avoids a Python-side HTML
# regex on a 4 MB body and reuses the browser's parser). page.evaluate
# supports async functions.
_RENTVISION_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.floorplanItem')) {
    try {
      const r = await fetch(location.origin + '/floorplans', {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through — return [] below */ }
  }
  return Array.from(doc.querySelectorAll('.floorplanItem')).map((it) => ({
    name: T(it.querySelector('.floorplanName')),
    bedsAttr: it.getAttribute('data-bedrooms') || '',
    beds: T(it.querySelector('.floorplanBeds')),
    baths: T(it.querySelector('.floorplanBaths')),
    sqft: T(it.querySelector('.floorplanSquareFootage')),
    price: T(it.querySelector('.floorplanPrice')),
    avail: T(it.querySelector('.floorplanAvailability')),
  }));
}
"""

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_SQFT_RE = re.compile(r"([\d,]+)")
_MONEY_RE = re.compile(r"\$[\s]?[\d,]+")
_VACANT_RE = re.compile(r"(\d+)\s+vacant", re.IGNORECASE)

# 2026-05-25 — per-plan detail-page URL pattern. RentVision marketing
# sites link each plan card to /floorplans/{bed-tier}/{plan-slug}, where
# bed-tier is one of "studio"/"one-bedroom"/"two-bedroom"/"three-bedroom"/
# "four-bedroom" and plan-slug is the plan's url-safe name. The link
# appears multiple times on the grid (floorplanNameAnchor + Details
# button + header dropdown); the parser dedupes.
_PLAN_DETAIL_HREF_RE = re.compile(
    r'href="(/floorplans/(?:studio|one-bedroom|two-bedroom|three-bedroom|'
    r'four-bedroom|five-bedroom|six-bedroom)/[A-Za-z0-9][A-Za-z0-9_-]*)"',
    re.IGNORECASE,
)

# Per-unit row markers on the detail page (verified live on
# liveatwalnutcreekapts.com/floorplans/two-bedroom/greystone).
_UNIT_TH_RE = re.compile(
    r'<th\b[^>]*class="[^"]*\bleft\b[^"]*\bwrap\b[^"]*"[^>]*>'
    r'([A-Za-z0-9][A-Za-z0-9._/\- ]*?)</th>',
    re.IGNORECASE,
)
_UNIT_RENT_SPAN_RE = re.compile(
    r'<td\b[^>]*class="[^"]*\bidentifiable-links\b[^"]*"[^>]*>[\s\S]{0,250}?'
    r'<span\b[^>]*>\s*\$([\d,]+(?:\.\d+)?)',
    re.IGNORECASE,
)
_UNIT_AVAIL_CELL_RE = re.compile(
    r'<td\b[^>]*class="[^"]*\bunit-availability\b[^"]*"[^>]*>'
    r'([\s\S]*?)</td>',
    re.IGNORECASE,
)
_UNIT_AVAIL_DATE_RE = re.compile(
    r'Available\s+on\s*<span\b[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)
# Apply-button onclick window.open URL. The HTML entities &#61; ('=') and
# &amp; ('&') survive the source HTML; the parser handles both raw and
# entity-encoded forms because the live page uses &#61; but a future
# tenant could swap to plain '='.
_APPLY_MOVE_IN_DATE_RE = re.compile(
    r"moveInDate(?:&#61;|=)(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
# Backup: term-pricing-popup <h3> confirms unit-number (used to validate
# that a <th> match isn't a column-header false positive on edge themes).
_TERM_PRICING_H3_RE = re.compile(
    r'<h3\b[^>]*>\s*Unit\s+Term\s+Pricing\s*[-–]\s*([A-Za-z0-9][^<]+?)\s*</h3>',
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _to_iso_date(text: str) -> str:
    """Convert RentVision date text into ISO YYYY-MM-DD; return '' on fail.

    Accepts the two shapes RentVision emits in the unit-availability cell
    and the moveInDate Apply-URL param:
      * "May 29, 2026"      (Month DD, YYYY)
      * "05/26/2026"        (MM/DD/YYYY — Apply URL move-in date)
    """
    if not text:
        return ""
    t = text.strip()
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", t)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            try:
                day = int(m.group(2))
                year = int(m.group(3))
                if 1 <= day <= 31 and 2000 <= year <= 2100:
                    return f"{year:04d}-{mon:02d}-{day:02d}"
            except ValueError:
                return ""
        return ""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", t)
    if m:
        try:
            mon = int(m.group(1))
            day = int(m.group(2))
            year = int(m.group(3))
            if 1 <= mon <= 12 and 1 <= day <= 31 and 2000 <= year <= 2100:
                return f"{year:04d}-{mon:02d}-{day:02d}"
        except ValueError:
            return ""
    return ""


def find_plan_detail_urls(floorplans_html: str, base_url: str) -> list[str]:
    """Extract per-plan ``/floorplans/{bed-tier}/{slug}`` URLs from the
    plan-grid HTML. The marketing site ships each plan card with multiple
    anchors (title link, details button, header dropdown) pointing at the
    same detail page; we dedupe.

    Returns an empty list when no plan-detail anchors are present (the
    caller falls through to plan-level extraction).
    """
    if not floorplans_html or "/floorplans/" not in floorplans_html:
        return []
    try:
        p = urlparse(base_url)
    except Exception:
        return []
    if not p.scheme or not p.netloc:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _PLAN_DETAIL_HREF_RE.finditer(floorplans_html):
        href = m.group(1)
        # Strip trailing slash so dedupe is robust to both shapes.
        norm = href.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        out.append(urlunparse((p.scheme, p.netloc, norm, "", "", "")))
    return out


def _plan_name_from_url(url: str) -> str:
    """Derive a human-readable plan name from the detail URL path slug.

    Used as a fallback when the caller doesn't pass an explicit name.
    "/floorplans/two-bedroom/greystone" → "Greystone"
    "/floorplans/three-bedroom/the-park-suite" → "The Park Suite"
    """
    try:
        p = urlparse(url)
    except Exception:
        return ""
    parts = [seg for seg in p.path.split("/") if seg]
    if not parts:
        return ""
    slug = parts[-1]
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def _beds_from_url(url: str) -> int | None:
    """Recover bed count from the URL's bed-tier segment.

    "/floorplans/two-bedroom/greystone" → 2; "studio" → 0; unknown → None.
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    parts = [seg for seg in p.path.lower().split("/") if seg]
    if len(parts) < 3:
        return None
    tier = parts[-2]
    mapping = {
        "studio": 0, "one-bedroom": 1, "two-bedroom": 2,
        "three-bedroom": 3, "four-bedroom": 4, "five-bedroom": 5,
        "six-bedroom": 6,
    }
    return mapping.get(tier)


def parse_rentvision_unit_table(
    detail_html: str, source_url: str, floor_plan_name: str = ""
) -> list[dict[str, str]]:
    """Parse a RentVision per-plan detail page's unit-listing table.

    Each available unit is one ``<tr>`` containing:
      * ``<th class="left wrap">{unit_number}</th>``
      * a ``<td>`` with ``<span>${rent}</span>``
      * a ``<td class="standard unit-availability">`` with "Available Now"
        or "Available on <span>{Month DD, YYYY}</span>"
      * a ``<td class="unit-actions">`` with an Apply Now button whose
        onclick contains ``moveInDate=MM/DD/YYYY&unit={unit_number}``

    Strategy: anchor on ``<th class="left wrap">`` matches, then slice the
    HTML between this anchor and the next anchor (or end-of-doc) and pull
    rent / availability / move-in-date out of that slice. This survives
    deeply nested term-pricing-popup markup inside the same row.

    ``floor_plan_name`` is plumbed through to ``make_unit_dict`` so the
    downstream merge & schema-v2 transform see the correct plan label.
    When empty, the parser derives it from the ``source_url`` slug.
    """
    if not detail_html or "left wrap" not in detail_html:
        return []

    plan_name = (floor_plan_name or _plan_name_from_url(source_url)).strip()
    plan_beds = _beds_from_url(source_url)

    # Find every <th class="left wrap"> anchor; each one starts a row.
    anchors: list[tuple[int, int, str]] = []
    for m in _UNIT_TH_RE.finditer(detail_html):
        unit_text = m.group(1).strip()
        # Skip column-header strings ("Apartment", "Unit", "Price", etc.)
        # — the live theme only ever puts real unit numbers here, but be
        # defensive against future tenants that add a header row.
        if unit_text.lower() in {
            "apartment", "unit", "price", "availability", "actions",
        }:
            continue
        # Require alphanumeric content with a digit somewhere — real
        # unit numbers always have at least one digit (e.g. "622-102",
        # "C-708-H", "B-2610").
        if not re.search(r"\d", unit_text):
            continue
        anchors.append((m.start(), m.end(), unit_text))

    if not anchors:
        return []

    out: list[dict[str, str]] = []
    for i, (_start, end, unit_number) in enumerate(anchors):
        slice_end = anchors[i + 1][0] if i + 1 < len(anchors) else len(detail_html)
        block = detail_html[end:slice_end]

        # Asking rent — first $X.XX span in the row (the term-pricing
        # table inside the popup is full of additional $X spans, so we
        # constrain to the FIRST occurrence of the rent-cell pattern).
        rent: int | None = None
        rent_m = _UNIT_RENT_SPAN_RE.search(block)
        if rent_m:
            try:
                rent = int(float(rent_m.group(1).replace(",", "")))
            except (ValueError, TypeError):
                rent = None

        # Availability — first .unit-availability cell in the slice.
        availability_status = "AVAILABLE"
        availability_date = ""
        avail_m = _UNIT_AVAIL_CELL_RE.search(block)
        if avail_m:
            avail_html = avail_m.group(1)
            date_m = _UNIT_AVAIL_DATE_RE.search(avail_html)
            if date_m:
                availability_date = _to_iso_date(date_m.group(1))

        # Move-in date from Apply Now button — backup source for
        # availability_date when the unit-availability cell says
        # "Available Now" (no date). The Apply URL always carries one.
        if not availability_date:
            apply_m = _APPLY_MOVE_IN_DATE_RE.search(block)
            if apply_m:
                availability_date = _to_iso_date(apply_m.group(1))

        out.append(
            make_unit_dict(
                floor_plan_name=plan_name,
                bed_label=bed_label_from(plan_beds, plan_name),
                bedrooms=str(plan_beds) if plan_beds is not None else "",
                bathrooms="",
                sqft="",
                unit_number=unit_number,
                rent_range=format_rent_range(rent, rent),
                rent_low=rent,
                rent_high=rent,
                availability_status=availability_status,
                availability_date=availability_date,
                source_api_url=source_url,
                extraction_tier="TIER_3_DOM_RENTVISION_UNIT_LEVEL",
            )
        )
    return out


def parse_rentvision_cards(
    cards: list[dict[str, str]], url: str
) -> list[dict[str, str]]:
    """Parse RentVision ``.floorplanItem`` rows into plan-level unit dicts.

    Plan-level (one row per floor plan). ``available_units`` carries the
    per-plan vacant count when the page states one ("Only N Vacant
    Apartments Left!"). Rows with no numeric dimension are dropped by the
    caller's post_process validity gate.
    """
    units: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = (card.get("name") or "").strip()
        beds_attr = (card.get("bedsAttr") or "").strip()
        beds_txt = card.get("beds") or ""
        if not name and not beds_attr and not beds_txt:
            continue

        if re.search(r"studio", beds_attr or beds_txt, re.IGNORECASE):
            beds: int | None = 0
        else:
            bm = _NUM_RE.search(beds_attr) or _NUM_RE.search(beds_txt)
            beds = int(float(bm.group(0))) if bm else None

        bath_m = _NUM_RE.search(card.get("baths") or "")
        baths = bath_m.group(0) if bath_m else ""

        sqft_m = _SQFT_RE.search(card.get("sqft") or "")
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        price = card.get("price") or ""
        money = _MONEY_RE.findall(price)
        rent_lo = money_to_int(money[0]) if money else None
        rent_hi = money_to_int(money[-1]) if money else None
        rent_range = format_rent_range(rent_lo, rent_hi)

        avail = card.get("avail") or ""
        vac_m = _VACANT_RE.search(avail)
        available_units = vac_m.group(1) if vac_m else ""
        if re.search(r"waitlist", avail, re.IGNORECASE):
            status = "UNAVAILABLE"
        else:
            status = "AVAILABLE"

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths),
                sqft=sqft,
                unit_number="",
                rent_range=rent_range,
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                available_units=available_units,
                source_api_url=url,
                extraction_tier="TIER_3_DOM_RENTVISION",
            )
        )
    return units


class RentVisionAdapter:
    """RentVision CMS adapter. Parses the SSR ``/floorplans`` plan grid."""

    pms_name: str = "rentvision"
    _fingerprints: list[str] = [
        "created by rentvision",
        "powered by rentvision",
        "rentvision.com",
    ]

    async def extract(self, page: Page | None, ctx: AdapterContext) -> AdapterResult:
        """Extract per-unit (preferred) or plan-level (fallback) data.

        Strategy:
          1. Parse the SSR ``/floorplans`` plan grid (existing path).
          2. Self-fetch ``/floorplans`` + each per-plan detail page
             (``/floorplans/{bed-tier}/{slug}``) and parse the unit-listing
             table. When the drill returns ≥1 unit, prefer it over the
             plan-level rows (unit-level is strictly more informative).
          3. When the drill returns 0 rows (every plan has no availability
             — e.g. Walnut Creek's Heritage plan), fall through to the
             plan-level emit so the property at least surfaces.
        """
        result = AdapterResult(tier_used="TIER_3_DOM_RENTVISION")

        floorplans_url = self._floorplans_url(page, ctx)
        cards: list[dict[str, str]] = []
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                candidate_cards = await evaluate(_RENTVISION_DOM_JS)
            except Exception as exc:
                log.debug("RentVision DOM evaluate failed err=%s", exc)
                candidate_cards = None
            if isinstance(candidate_cards, list):
                cards = [card for card in candidate_cards if isinstance(card, dict)]

        units = parse_rentvision_cards(cards, floorplans_url)

        # 2026-05-25 (user-flagged via Walnut Creek pid 45534): drill into
        # each plan's /floorplans/{bed-tier}/{slug} detail page for real
        # per-unit rows before falling back to plan-level. Pre-fix on
        # Walnut Creek: 3 plan-summary rows (the `inferred_*` rows from
        # the consolidator). Post-fix: 10 real units like 622-102, C-708-H
        # with $1,249 and per-unit move-in dates.
        unit_level_drill: list[dict[str, str]] = []
        # Build a {plan-name: card} index so we can plumb the already-parsed
        # plan-name through to each unit. Fetch-only Jugnu runs have no live
        # Playwright page, so ``cards`` can be empty; the detail parser then
        # derives the plan name from its URL and still emits real unit rows.
        card_by_slug = self._cards_by_slug(cards)
        try:
            floorplans_html = await self._fetch_floorplans_html(
                page, floorplans_url
            )
        except Exception as exc:
            log.debug("RentVision floorplans-self-fetch failed err=%s", exc)
            floorplans_html = ""

        if floorplans_html:
            detail_urls = find_plan_detail_urls(floorplans_html, floorplans_url)
            for detail_url in detail_urls:
                try:
                    detail_html = await self._fetch_detail_html(detail_url)
                except Exception as exc:
                    log.debug(
                        "RentVision detail-fetch failed url=%s err=%s",
                        detail_url, exc,
                    )
                    detail_html = ""
                if not detail_html:
                    continue
                slug = detail_url.rstrip("/").rsplit("/", 1)[-1].lower()
                plan_name = (card_by_slug.get(slug) or "").strip()
                unit_blocks = parse_rentvision_unit_table(
                    detail_html, detail_url, plan_name
                )
                unit_level_drill.extend(unit_blocks)

        if unit_level_drill:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(
                unit_level_drill,
                property_id=getattr(ctx, "property_id", None),
            )
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = floorplans_url
                result.tier_used = "TIER_3_DOM_RENTVISION_UNIT_LEVEL"
                result.confidence = min(0.95, 0.75 + 0.02 * pp.n_admitted)
                return result
            result.errors.append(
                f"RENTVISION_UNIT_LEVEL_VALIDITY_REJECTED: "
                f"{len(unit_level_drill)} rows failed unit_validity"
            )

        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = floorplans_url
                result.confidence = min(0.9, 0.65 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RENTVISION_VALIDITY_REJECTED: {len(units)} rows failed "
                f"unit_validity (no numeric dimension)"
            )

        result.confidence = 0.0
        result.errors.append("RentVision: no parseable plan data or detail rows")
        return result

    @staticmethod
    def _cards_by_slug(cards: list[dict[str, str]]) -> dict[str, str]:
        """Map detail-URL slug → plan name as captured by the grid parser.

        "Greystone" → slug "greystone". When the live grid uses non-trivial
        slug-mangling (whitespace → hyphen, & → and, etc.), the unit-level
        parser falls back to deriving the name from the URL slug itself.
        """
        out: dict[str, str] = {}
        for c in cards:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            if slug and slug not in out:
                out[slug] = name
        return out

    @staticmethod
    async def _fetch_floorplans_html(
        page: Page | None, floorplans_url: str
    ) -> str:
        """Get the ``/floorplans`` SSR HTML.

        Preference order: (1) if the live page is already at that URL, use
        ``page.content()`` — saves a round-trip and uses the same cookies/
        identity. (2) Otherwise self-fetch via httpx (the RentVision pages
        are pure SSR and don't need JS execution to render the plan grid
        or its plan-detail anchors).
        """
        if page is not None:
            try:
                current_url = page.url or ""
            except Exception:
                current_url = ""
            if current_url.rstrip("/").endswith("/floorplans"):
                try:
                    return await page.content()
                except Exception:
                    pass
        try:
            import httpx
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            }
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True, headers=headers
            ) as c:
                r = await c.get(floorplans_url)
            if r.status_code == 200:
                return r.text
        except Exception as exc:
            log.debug("RentVision floorplans httpx-fetch failed err=%s", exc)
        return ""

    @staticmethod
    async def _fetch_detail_html(detail_url: str) -> str:
        """Self-fetch a single per-plan detail page via httpx.

        RentVision detail pages are pure SSR — the unit-listing table is
        in the initial HTML, no JS rendering required.
        """
        try:
            import httpx
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            }
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True, headers=headers
            ) as c:
                r = await c.get(detail_url)
            if r.status_code == 200:
                return r.text
        except Exception as exc:
            log.debug("RentVision detail httpx-fetch failed err=%s", exc)
        return ""

    @staticmethod
    def _floorplans_url(page: Page | None, ctx: AdapterContext) -> str:
        """Best-effort canonical ``{origin}/floorplans`` URL for provenance."""
        candidate = ""
        try:
            candidate = page.url or ""
        except Exception:
            candidate = ""
        if not candidate:
            candidate = getattr(ctx, "base_url", "") or ""
        try:
            p = urlparse(candidate)
        except Exception:
            return candidate
        if not p.scheme or not p.netloc:
            return candidate
        return urlunparse((p.scheme, p.netloc, "/floorplans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
