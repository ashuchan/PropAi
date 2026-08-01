"""365 ResidentServices (Apollo / collapsar theme) adapter.

A self-hosted multifamily CMS used by a handful of operators. Identical
template across instances:

  - Marker: ``cdn.365residentservices.com`` in HTML (asset host).
  - Plan page: ``GET {domain}/Marketing/FloorPlans`` — SSR plan cards in
    ``.floorplan-tile`` containers.
  - Unit detail: ``GET {domain}/Marketing/FloorPlans/Units/{guid}`` — SSR
    per-unit rows with apartment identity and rent.

Per-card DOM (verified live 2026-05-19 on rusticwoodsapts.com /
waterfordpoint.us — selectors byte-identical):
  - ``.floorplan-tile``         — one per plan
  - ``.title-row``              — "Sedona 1 Bed 1 Bath 675 sqft" (header)
  - ``.list-divider``           — "1 Bed 1 Bath 675 sqft"  (clean specs;
                                   "Studio" → 0 beds)
  - ``.pricing``                — "$759 per month" | "$849 - $899 per month"
  - ``.availability``           — "3 Units Available" | "1 Unit Available" |
                                   "Join Waitlist"

The adapter prefers those unit-detail rows over the plan catalogue.  The
detail drill also works when production dispatches ``page=None``: a bounded,
same-property HTTP lane reads the public SSR pages without a browser, proxy,
or challenge-bypass service.

The exact 2026-07-31 549-property plan-level cohort contained eight marker-
positive sites.  Three exposed strict live unit rows through this drill at
probe time; the other five are retained as zero-inventory/migrated controls,
not promoted from plan-only data.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse, urlunparse

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

_RS365_MARKER = "365residentservices.com"
_MAX_FLOORPLANS_BYTES = 1_000_000
_MAX_DETAIL_BYTES = 1_500_000
_MAX_DETAIL_PAGES = 16
_DETAIL_FETCH_CONCURRENCY = 4
_HTTP_TIMEOUT_SECONDS = 15.0

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
    r'href="(/(?:Marketing/FloorPlans/Units|floorplan)/[0-9a-f-]{36})"',
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


def _html_from_ctx(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _normalized_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _same_property_host(left: str, right: str) -> bool:
    """Treat http/https and a leading ``www.`` as the same property host."""
    left_host = _normalized_host(left)
    right_host = _normalized_host(right)
    return bool(left_host and left_host == right_host)


def _has_rs365_marker(html: str) -> bool:
    return _RS365_MARKER in (html or "").lower()


def _has_strict_unit_identity_and_rent(row: object) -> bool:
    """Return true only for an apartment row, never a plan summary."""
    if not isinstance(row, dict) or not str(row.get("unit_number") or "").strip():
        return False
    for field in ("market_rent_low", "market_rent_high"):
        try:
            if float(row.get(field) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
    """Extract RS365 per-plan detail URLs from the plan-grid HTML.

    Apollo/collapsar uses ``/Marketing/FloorPlans/Units/{guid}``; the newer
    Gemini/cosmic themes use ``/floorplan/{guid}``.  Both routes render the
    same strict ``.unit-details`` rows.  We return absolute URLs ready for a
    same-property fetch.

    Returns an empty list if no matches found (older /Home/Index/* page
    that hasn't yet linked the FloorPlans grid — caller falls through to
    plan-level extraction).
    """
    lower_html = (floorplans_html or "").lower()
    if not lower_html or not any(
        marker in lower_html
        for marker in ("/marketing/floorplans/units/", "/floorplan/")
    ):
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
        """Extract strict units, with the plan catalogue as a fallback."""
        result = AdapterResult(tier_used="TIER_1_DOM_365RESIDENTSERVICES")

        evaluate = getattr(page, "evaluate", None)
        tiles: object = None
        if callable(evaluate):
            try:
                tiles = await evaluate(_RS365_DOM_JS)
            except Exception as exc:
                log.debug("365rs DOM evaluate failed err=%s", exc)
        units = (
            parse_residentservices365_tiles(
                tiles,
                self._winning_url(page, ctx),
            )
            if isinstance(tiles, list) and tiles
            else []
        )

        # A page-less production dispatch is allowed to self-fetch only after
        # the already-fetched property body proves this is the RS365 CMS. This
        # prevents a stale detector label from turning into arbitrary traffic.
        code_only = not callable(evaluate)
        if code_only and not _has_rs365_marker(_html_from_ctx(ctx)):
            result.errors.append("365rs: exact CMS marker absent in fetched body")
            return result

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

        if floorplans_html and (_has_rs365_marker(floorplans_html) or units):
            detail_urls = [
                url
                for url in find_unit_detail_urls(floorplans_html, floorplans_url)
                if _same_property_host(url, floorplans_url)
            ][:_MAX_DETAIL_PAGES]
            detail_documents = await self._fetch_detail_documents(detail_urls)
            for detail_url, detail_html in detail_documents:
                unit_level_drill.extend(
                    row
                    for row in parse_rs365_unit_blocks(detail_html, detail_url)
                    if _has_strict_unit_identity_and_rent(row)
                )

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
        if code_only:
            result.errors.append(
                "365rs: bounded SSR drill found no canonical unit with rent"
            )
        elif not isinstance(tiles, list) or not tiles:
            result.errors.append(
                "365rs: no .floorplan-tile blocks found at /Marketing/FloorPlans"
            )
        else:
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
                    content = await page.content()
                    if len(content.encode("utf-8")) <= _MAX_FLOORPLANS_BYTES:
                        return content
                except Exception:
                    pass
        html, final_url = await Residentservices365Adapter._bounded_html_get(
            floorplans_url,
            max_bytes=_MAX_FLOORPLANS_BYTES,
        )
        if final_url and not _same_property_host(floorplans_url, final_url):
            log.debug(
                "365rs floorplans redirect left property host requested=%s final=%s",
                floorplans_url,
                final_url,
            )
            return ""
        return html

    @staticmethod
    async def _fetch_detail_html(detail_url: str) -> str:
        """Self-fetch a single /Marketing/FloorPlans/Units/{guid} detail
        page via httpx. Apollo CMS renders the full unit-details SSR
        DOM without JS execution."""
        html, final_url = await Residentservices365Adapter._bounded_html_get(
            detail_url,
            max_bytes=_MAX_DETAIL_BYTES,
        )
        if final_url and not _same_property_host(detail_url, final_url):
            log.debug(
                "365rs detail redirect left property host requested=%s final=%s",
                detail_url,
                final_url,
            )
            return ""
        return html

    @staticmethod
    async def _bounded_html_get(
        url: str,
        *,
        max_bytes: int,
    ) -> tuple[str, str]:
        """Fetch one public HTML document with hard time and byte bounds.

        This is deliberately plain HTTP: environment proxies are disabled and
        no browser fingerprint, CAPTCHA solver, unlocker, or alternate fetch
        service is involved.  The final URL is returned so callers can enforce
        the same-property redirect boundary.
        """
        try:
            import httpx

            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return "", ""
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
                headers={"Accept": "text/html,application/xhtml+xml"},
            ) as client:
                current_url = url
                for _redirect in range(6):
                    async with client.stream("GET", current_url) as response:
                        final_url = str(response.url)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                return "", final_url
                            next_url = urljoin(final_url, location)
                            if not _same_property_host(url, next_url):
                                return "", next_url
                            current_url = next_url
                            continue
                        if not 200 <= response.status_code < 300:
                            return "", final_url
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > max_bytes:
                                    return "", final_url
                            except ValueError:
                                pass
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                return "", final_url
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        encoding = response.encoding or "utf-8"
                        try:
                            return (
                                body.decode(encoding, errors="replace"),
                                final_url,
                            )
                        except LookupError:
                            return (
                                body.decode("utf-8", errors="replace"),
                                final_url,
                            )
                return "", current_url
        except Exception as exc:
            log.debug("365rs bounded HTTP fetch failed url=%s err=%s", url, exc)
            return "", ""

    @staticmethod
    async def _fetch_detail_documents(
        detail_urls: list[str],
    ) -> list[tuple[str, str]]:
        """Fetch at most 16 detail pages, with four requests in flight."""
        import asyncio

        semaphore = asyncio.Semaphore(_DETAIL_FETCH_CONCURRENCY)

        async def fetch_one(url: str) -> tuple[str, str]:
            async with semaphore:
                try:
                    return url, await Residentservices365Adapter._fetch_detail_html(
                        url
                    )
                except Exception as exc:  # defensive isolation for one plan
                    log.debug(
                        "365rs unit-detail fetch failed url=%s err=%s",
                        url,
                        exc,
                    )
                    return url, ""

        bounded = list(dict.fromkeys(detail_urls))[:_MAX_DETAIL_PAGES]
        return list(await asyncio.gather(*(fetch_one(url) for url in bounded)))

    @staticmethod
    def _winning_url(page: Page, ctx: AdapterContext) -> str:
        candidate = ""
        try:
            candidate = page.url or ""
        except Exception:
            candidate = ""
        if not candidate:
            fetch_result = getattr(ctx, "fetch_result", None)
            candidate = (
                getattr(fetch_result, "final_url", "")
                or getattr(ctx, "base_url", "")
                or ""
            )
        try:
            p = urlparse(candidate)
        except Exception:
            return candidate
        if not p.scheme or not p.netloc:
            return candidate
        return urlunparse((p.scheme, p.netloc, "/Marketing/FloorPlans", "", "", ""))

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
