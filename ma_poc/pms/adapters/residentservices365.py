"""365 ResidentServices (Apollo / collapsar theme) adapter.

A self-hosted multifamily CMS used by a handful of operators. Identical
template across instances:

  - Marker: ``cdn.365residentservices.com`` in HTML (asset host).
  - Plan page: ``GET {domain}/Marketing/FloorPlans`` — SSR plan cards in
    ``.floorplan-tile`` containers.
  - Unit detail: ``GET {domain}/Marketing/FloorPlans/Units/{guid}`` — SSR
    per-unit rows (unit-level drill, not yet exercised here; future).

Per-card DOM (verified live 2026-05-19 on rusticwoodsapts.com /
waterfordpoint.us — selectors byte-identical):
  - ``.floorplan-tile``         — one per plan
  - ``.title-row``              — "Sedona 1 Bed 1 Bath 675 sqft" (header)
  - ``.list-divider``           — "1 Bed 1 Bath 675 sqft"  (clean specs;
                                   "Studio" → 0 beds)
  - ``.pricing``                — "$759 per month" | "$849 - $899 per month"
  - ``.availability``           — "3 Units Available" | "1 Unit Available" |
                                   "Join Waitlist"

Plan-level for this first cut; the per-unit ``/Units/{guid}`` drill is a
follow-up enhancement (the GUID is captured but not yet fetched).

Probed cluster size: 4 in the 351-row deep-probe sample (16196, 1777,
16754, 60939) — all the same template. Detector_hop_gap was the failure
mode (jugnu tier=NONE because the CMS marker wasn't fingerprinted).
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

# Self-fetch /Marketing/FloorPlans if the live page isn't already there;
# parse the .floorplan-tile cards via DOMParser. Returns [] for non-365rs
# pages so unrelated sites are unaffected.
_RS365_DOM_JS = r"""
async () => {
  const T = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  let doc = document;
  if (!document.querySelector('.floorplan-tile')) {
    try {
      const r = await fetch(location.origin + '/Marketing/FloorPlans', {credentials: 'include'});
      if (r.ok) doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    } catch (e) { /* fall through */ }
  }
  return Array.from(doc.querySelectorAll('.floorplan-tile')).map((t) => ({
    title: T(t.querySelector('.title-row')),
    specs: T(t.querySelector('.list-divider')),
    pricing: T(t.querySelector('.pricing')),
    availability: T(t.querySelector('.availability')),
  }));
}
"""

_BED_RE = re.compile(r"(\d+)\s*Bed", re.IGNORECASE)
_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d[\d,]*)\s*sqft", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$([\d,]+)")
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s+Units?\s+Available", re.IGNORECASE)

# 2026-05-25 (user-flagged via Village Square Wheaton — pid 16196): the
# per-plan unit-detail URL pattern. Each .floorplan-tile's primary anchor
# points to ``/Marketing/FloorPlans/Units/{guid}`` where the per-unit
# data lives (data-unit-id / data-unit-code / data-availabledate /
# data-rent-min / data-rent-max attrs). The plan-tile parser
# captures the GUID but the legacy adapter never followed the link.
_UNIT_DETAIL_HREF_RE = re.compile(
    r'href="(/Marketing/FloorPlans/Units/[0-9a-f-]{36})"',
    re.IGNORECASE,
)

# Unit-detail block markers (per probed 100KB Village Square HTML, also
# byte-identical to other Apollo/collapsar tenants). Pure regex parser —
# no Playwright required.
_UNIT_BLOCK_RE = re.compile(
    r'<div[^>]*\bclass="[^"]*\bunit-details\b[^"]*"'
    r'[^>]*'
    r'(?:\s+data-unit-id="(?P<unit_id>[0-9a-f-]{36})")?'
    r'(?:\s+data-unit-code="(?P<unit_code>[^"]+)")?'
    r'(?:\s+data-availabledate="(?P<avail_epoch_ms>\d+)")?'
    r'[^>]*>',
    re.IGNORECASE,
)
# Some installs swap the data-* attribute order; capture each separately too.
_UNIT_ID_ATTR_RE = re.compile(r'data-unit-id="([0-9a-f-]{36})"', re.IGNORECASE)
_UNIT_CODE_ATTR_RE = re.compile(r'data-unit-code="([^"]+)"', re.IGNORECASE)
_AVAIL_EPOCH_RE = re.compile(r'data-availabledate="(\d+)"', re.IGNORECASE)
_UNIT_RENT_MIN_RE = re.compile(r'data-rent-min="([\d.]+)"', re.IGNORECASE)
_UNIT_RENT_MAX_RE = re.compile(r'data-rent-max="([\d.]+)"', re.IGNORECASE)
# The h3 inside .unit-header carries the human-readable unit number
# ("Apartment 022009-202") and the .list-divider <li>s carry beds/baths/sqft.
_UNIT_H3_RE = re.compile(
    r'<h3\b[^>]*class="[^"]*\bstandard\b[^"]*"[^>]*>([^<]+)</h3>',
    re.IGNORECASE,
)
_UNIT_LIST_DIVIDER_RE = re.compile(
    r'<ul\b[^>]*class="[^"]*\blist-divider\b[^"]*"[^>]*>([\s\S]*?)</ul>',
    re.IGNORECASE,
)
_UNIT_LI_RE = re.compile(r'<li[^>]*>([\s\S]*?)</li>', re.IGNORECASE)
_TAG_STRIP_RE = re.compile(r'<[^>]+>')


def _plan_name(title: str, specs: str) -> str:
    """Extract the plan name from ``title`` by stripping the specs suffix.

    Title is e.g. "Sedona 1 Bed 1 Bath 675 sqft" or "Stafford Studio 1 Bath
    392 sqft Special". The name is everything up to the first occurrence
    of ``Studio`` or ``\\d+\\s*Bed`` — whichever comes first.
    """
    title = (title or "").strip()
    if not title:
        return ""
    studio_m = re.search(r"\bStudio\b", title, re.IGNORECASE)
    bed_m = re.search(r"\d+\s*Bed", title, re.IGNORECASE)
    candidates = [m.start() for m in (studio_m, bed_m) if m]
    if not candidates:
        return title
    return title[: min(candidates)].strip()


def parse_residentservices365_tiles(
    tiles: list[dict[str, str]], url: str
) -> list[dict[str, str]]:
    """Parse 365 ResidentServices ``.floorplan-tile`` rows into plan-level
    unit dicts. ``available_units`` carries the per-plan count when stated.
    """
    units: list[dict[str, str]] = []
    for t in tiles:
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        specs = (t.get("specs") or "").strip()
        name = _plan_name(title, specs)
        if not name:
            continue

        if re.search(r"\bStudio\b", specs, re.IGNORECASE):
            beds: int | None = 0
        else:
            bm = _BED_RE.search(specs)
            beds = int(bm.group(1)) if bm else None
        bath_m = _BATH_RE.search(specs)
        baths = bath_m.group(1) if bath_m else ""
        sqft_m = _SQFT_RE.search(specs)
        sqft = sqft_m.group(1).replace(",", "") if sqft_m else ""

        pricing = t.get("pricing") or ""
        money = _MONEY_RE.findall(pricing)
        rent_lo = money_to_int(money[0]) if money else None
        rent_hi = money_to_int(money[-1]) if money else None

        avail = t.get("availability") or ""
        count_m = _AVAIL_COUNT_RE.search(avail)
        available_units = count_m.group(1) if count_m else ""
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
                unit_number="",  # plan-level
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                available_units=available_units,
                source_api_url=url,
                extraction_tier="TIER_1_DOM_365RESIDENTSERVICES",
            )
        )
    return units


def find_unit_detail_urls(floorplans_html: str, base_url: str) -> list[str]:
    """Extract per-plan ``/Marketing/FloorPlans/Units/{guid}`` URLs from the
    plan-grid HTML. The Apollo/collapsar theme ships each .floorplan-tile
    with an anchor (or "View Units" button) whose href points to the
    plan's per-unit detail page. We return absolute URLs ready for fetch.

    Returns an empty list if no matches found (older /Home/Index/* page
    that hasn't yet linked the FloorPlans grid — caller falls through to
    plan-level extraction).
    """
    if not floorplans_html or "/Marketing/FloorPlans/Units/" not in floorplans_html:
        return []
    try:
        p = urlparse(base_url)
    except Exception:
        return []
    if not p.scheme or not p.netloc:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _UNIT_DETAIL_HREF_RE.finditer(floorplans_html):
        href = m.group(1)
        if href in seen:
            continue
        seen.add(href)
        out.append(urlunparse((p.scheme, p.netloc, href, "", "", "")))
    return out


def parse_rs365_unit_blocks(
    unit_detail_html: str, source_url: str
) -> list[dict[str, str]]:
    """Parse the per-unit-detail page HTML into unit dicts.

    Each ``<div class="unit-details">`` block carries:
      * ``data-unit-id`` — opaque GUID (source_id)
      * ``data-unit-code`` — operator's unit number (e.g. "022009-202")
      * ``data-availabledate`` — Unix epoch milliseconds (move-in date)
      * ``<h3 class="standard">`` — "Apartment {unit_code}" (also has the
        unit number as text — backup parse path when data-unit-code is
        missing)
      * ``<ul class="list-divider">`` — beds / baths / sqft as text
      * ``<span data-rent-min="1818.00" data-rent-max="1818.00">``
        inside ``.unitPricing`` — authoritative rent (the data-* attrs
        beat the rendered ``$1,818`` text because they're unformatted
        and the rent-display script sometimes swaps spans).

    Returns one ``make_unit_dict`` per parseable block. Empty list when
    the page doesn't have a unit-details block (no units available).
    """
    if not unit_detail_html or "unit-details" not in unit_detail_html:
        return []

    out: list[dict[str, str]] = []
    # Split on the unit-details boundary so each subsequent slice is one
    # unit block (plus optional trailing markup we don't care about).
    parts = re.split(
        r'(?=<div[^>]*class="[^"]*\bunit-details\b[^"]*")',
        unit_detail_html,
    )
    for part in parts:
        if "unit-details" not in part[:200]:
            continue

        # Truncate the part at the NEXT unit-details boundary or page end
        # so attr extraction doesn't bleed across blocks.
        unit_id_m = _UNIT_ID_ATTR_RE.search(part)
        unit_code_m = _UNIT_CODE_ATTR_RE.search(part)
        avail_m = _AVAIL_EPOCH_RE.search(part)
        rent_lo_m = _UNIT_RENT_MIN_RE.search(part)
        rent_hi_m = _UNIT_RENT_MAX_RE.search(part)

        # h3 fallback: extracts "Apartment 022009-202" → "022009-202" when
        # data-unit-code attr is missing on the wrapper. Pin-tight regex.
        #
        # 2026-05-25 (Village Square 3rd-plan probe): when the
        # /Marketing/FloorPlans/Units/{guid} page has NO available units
        # the Apollo CMS still renders a single .unit-details block as a
        # "plan summary" placeholder (no data-unit-* attrs, h3 = plan
        # name like "3 Bedrooms + 2 Baths"). REQUIRE either
        # data-unit-code OR an h3 matching "Apartment/Apt/Unit X" — do
        # NOT fall back to using the plan name as a synthetic
        # unit_number (that creates a duplicate of the plan-level row).
        unit_number = unit_code_m.group(1) if unit_code_m else None
        if not unit_number:
            h3 = _UNIT_H3_RE.search(part)
            if h3:
                t = h3.group(1).strip()
                # "Apartment 022009-202" → "022009-202" (must have the
                # "Apartment / Apt / Unit" prefix; plan-name h3s like
                # "3 Bedrooms + 2 Baths" deliberately fall through to
                # the skip-this-block branch below).
                m2 = re.match(
                    r"(?:Apartment|Apt|Unit)\s+(\S+)", t, re.IGNORECASE
                )
                if m2:
                    unit_number = m2.group(1)
        if not unit_number:
            continue

        # Beds / baths / sqft from .list-divider <li>s
        beds_s = ""
        baths_s = ""
        sqft_s = ""
        ld = _UNIT_LIST_DIVIDER_RE.search(part)
        if ld:
            for li_m in _UNIT_LI_RE.finditer(ld.group(1)):
                txt = _TAG_STRIP_RE.sub("", li_m.group(1)).strip()
                if not beds_s and (m_ := _BED_RE.search(txt)):
                    beds_s = m_.group(1)
                if not baths_s and (m_ := _BATH_RE.search(txt)):
                    baths_s = m_.group(1)
                if not sqft_s and (m_ := re.search(r"(\d[\d,]*)\s*(?:sq|square)", txt, re.IGNORECASE)):
                    sqft_s = m_.group(1).replace(",", "")

        # Rent — prefer data-* attrs over rendered text
        rent_lo: int | None = None
        rent_hi: int | None = None
        if rent_lo_m:
            try:
                rent_lo = int(float(rent_lo_m.group(1)))
            except (ValueError, TypeError):
                rent_lo = None
        if rent_hi_m:
            try:
                rent_hi = int(float(rent_hi_m.group(1)))
            except (ValueError, TypeError):
                rent_hi = None

        # Availability date — Unix epoch ms → ISO YYYY-MM-DD
        availability_date = ""
        if avail_m:
            try:
                from datetime import UTC
                from datetime import datetime as _dt
                ts_ms = int(avail_m.group(1))
                # Bound: reject obviously bogus values (< 2010, > 2050)
                if 1262304000000 < ts_ms < 2524608000000:
                    availability_date = _dt.fromtimestamp(
                        ts_ms / 1000, tz=UTC
                    ).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                availability_date = ""

        # Status: if we have data-availabledate AND it's not in the past
        # (or even just present), call it AVAILABLE. The unit-details
        # block is only rendered for available units on this platform.
        status = "AVAILABLE"

        source_ids = {}
        if unit_id_m:
            source_ids["rs365_unit_guid"] = unit_id_m.group(1)

        out.append(
            make_unit_dict(
                floor_plan_name="",  # plan name is from the parent floorplan tile
                bed_label=bed_label_from(
                    int(beds_s) if beds_s.isdigit() else None,
                    "",
                ),
                bedrooms=beds_s,
                bathrooms=baths_s,
                sqft=sqft_s,
                unit_number=unit_number,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status=status,
                availability_date=availability_date,
                source_api_url=source_url,
                source_ids=source_ids,
                extraction_tier="TIER_1_DOM_365RESIDENTSERVICES_UNIT_LEVEL",
            )
        )
    return out


class Residentservices365Adapter:
    """365 ResidentServices Apollo/collapsar CMS adapter."""

    pms_name: str = "residentservices365"
    _fingerprints: list[str] = ["365residentservices.com", "/Marketing/FloorPlans"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract plan-level units from the SSR ``/Marketing/FloorPlans`` grid."""
        result = AdapterResult(tier_used="TIER_1_DOM_365RESIDENTSERVICES")

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            result.confidence = 0.0
            result.errors.append("365rs: no live page to parse")
            return result

        try:
            tiles = await evaluate(_RS365_DOM_JS)
        except Exception as exc:
            log.debug("365rs DOM evaluate failed err=%s", exc)
            tiles = None

        if not isinstance(tiles, list) or not tiles:
            result.confidence = 0.0
            result.errors.append(
                "365rs: no .floorplan-tile blocks found at /Marketing/FloorPlans"
            )
            return result

        units = parse_residentservices365_tiles(tiles, self._winning_url(page, ctx))

        # 2026-05-25 (user-flagged via Village Square Wheaton pid 16196):
        # before falling back to plan-level, drill into each plan's
        # ``/Marketing/FloorPlans/Units/{guid}`` page to extract per-unit
        # data (data-unit-id / data-unit-code / data-rent-min /
        # data-availabledate). The plan-tile parser captures the GUID via
        # the rendered anchor href; self-fetching the detail page yields
        # the same SSR DOM the live page would render under "View Units".
        #
        # On Village Square: plan-level emit was 3 plan-summary rows with
        # NULL rent / NULL unit_number / NULL date. Unit-level emit is 4
        # real units (021927, 022009-202, 022009-303, 021925) with $1,818
        # / $1,862 / $1,978 / etc. + actual move-in dates.
        unit_level_drill: list[dict[str, str]] = []
        floorplans_url = self._winning_url(page, ctx)
        try:
            floorplans_html = await self._fetch_floorplans_html(
                page, floorplans_url
            )
        except Exception as exc:
            log.debug("365rs floorplans-self-fetch failed err=%s", exc)
            floorplans_html = ""

        if floorplans_html:
            detail_urls = find_unit_detail_urls(floorplans_html, floorplans_url)
            for detail_url in detail_urls:
                try:
                    detail_html = await self._fetch_detail_html(detail_url)
                except Exception as exc:
                    log.debug(
                        "365rs unit-detail fetch failed url=%s err=%s",
                        detail_url, exc,
                    )
                    detail_html = ""
                if not detail_html:
                    continue
                unit_blocks = parse_rs365_unit_blocks(detail_html, detail_url)
                unit_level_drill.extend(unit_blocks)

        # When the drill produced real unit-level rows, prefer them over
        # the plan-level summary. When the drill produced nothing (the
        # plan has no available units), fall through to plan-level so
        # the property at least surfaces "data captured, just no
        # availability right now".
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
                result.tier_used = "TIER_1_DOM_365RESIDENTSERVICES_UNIT_LEVEL"
                result.confidence = min(0.95, 0.75 + 0.02 * pp.n_admitted)
                return result
            result.errors.append(
                f"RS365_UNIT_LEVEL_VALIDITY_REJECTED: "
                f"{len(unit_level_drill)} rows failed unit_validity"
            )

        if units:
            from ma_poc.extraction.post_process import post_process

            pp = post_process(units, property_id=getattr(ctx, "property_id", None))
            if pp.n_admitted > 0:
                result.units = pp.admitted
                result.plan_summaries = pp.plan_summaries
                result.winning_url = self._winning_url(page, ctx)
                result.confidence = min(0.9, 0.65 + 0.05 * pp.n_admitted)
                return result
            result.errors.append(
                f"RS365_VALIDITY_REJECTED: {len(units)} rows failed unit_validity"
            )

        result.confidence = 0.0
        result.errors.append("365rs: no parseable plan data")
        return result

    @staticmethod
    async def _fetch_floorplans_html(page: Page | None, floorplans_url: str) -> str:
        """Get the /Marketing/FloorPlans SSR HTML.

        Preference order: (1) if the live page is already at that URL,
        use ``page.content()`` — saves a round-trip + uses the same
        cookies + identity. (2) Otherwise self-fetch via httpx (the
        Apollo CMS pages are pure SSR and don't need JS execution to
        render the floorplan-tile grid + their detail-page anchors).
        """
        if page is not None:
            try:
                current_url = page.url or ""
            except Exception:
                current_url = ""
            if current_url.rstrip("/").endswith("/Marketing/FloorPlans"):
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
            log.debug("365rs floorplans httpx-fetch failed err=%s", exc)
        return ""

    @staticmethod
    async def _fetch_detail_html(detail_url: str) -> str:
        """Self-fetch a single /Marketing/FloorPlans/Units/{guid} detail
        page via httpx. Apollo CMS renders the full unit-details SSR
        DOM without JS execution."""
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
            log.debug("365rs detail httpx-fetch failed err=%s", exc)
        return ""

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
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
        return urlunparse((p.scheme, p.netloc, "/Marketing/FloorPlans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
