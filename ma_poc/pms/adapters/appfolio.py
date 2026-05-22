"""
AppFolio adapter.

Research log
------------
Web sources consulted:
  - https://www.appfolio.com/ — AppFolio property management platform (accessed 2026-04-17)
  - https://www.appfolio.com/property-manager — Listing format documentation
Real payloads inspected (from data/runs/*/raw_api/):
  - 12617 (Stoney Brook) — /api/v1/community_info/ returning community-level metadata
    (name, address, total_unit_count, available_unit_count) but no unit-level data
  - 12807 — /api/v3/tokens/lists/ returning pagination wrapper with community info
Key findings:
  - API endpoint: /api/v1/community_info/, /api/v1/community_extra_info/,
    /api/v3/tokens/lists/ — these return property-level metadata only
  - AppFolio listing pages typically embed unit data in HTML or use the
    tenant-application form endpoint under /listings/ for individual unit
    detail (path is on the resolver blacklist — see ma_poc/pms/resolver.py)
  - Response envelope: {meta: {limit, total_count, offset}, objects: [...]}
  - Unit ID field: not available in captured community-level APIs
  - Rent field(s): not available in community-level responses; unit pages have price in HTML
  - Known gotchas: AppFolio community API does not contain unit-level pricing; unit data
    comes from DOM parsing of the listing page. AppFolio uses a standard listing card
    layout with .js-listing-card containers. Less than 3 real payloads with unit data
    available — adapter handles API where present and falls through to DOM parsing.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._merge_fns import _UNIT_SIGNAL_KEYS as _MERGE_UNIT_SIGNAL_KEYS
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# Bug 7 (2026-05-09 deep-dive): AppFolio /listings/detail/<uuid> single-property
# detail pages aren't matched by ``data-listing-id=`` (that's the index marker)
# and don't return the ``{"objects": [...]}`` API envelope. Today the adapter
# bails on these pages even though the unit data is right there in the HTML.
# These regexes pull the standard rent/bed/bath/sqft fields from the detail
# page's main content.
_DETAIL_RENT_RE = re.compile(
    r"\$([0-9,]{3,7})(?:\s*/?\s*month|\s*/?\s*mo)?", re.IGNORECASE
)
_DETAIL_BED_RE = re.compile(r"(\d+)\s*(?:bed|bd|br)\b", re.IGNORECASE)
_DETAIL_BATH_RE = re.compile(r"(\d+(?:\.\d)?)\s*(?:bath|ba)\b", re.IGNORECASE)
_DETAIL_SQFT_RE = re.compile(
    r"([0-9,]{3,5})\s*(?:sq\s*ft|sqft|sf)\b", re.IGNORECASE
)
_DETAIL_H1_RE = re.compile(
    r"<h1[^>]*>(?P<txt>[^<]{1,200})</h1>", re.IGNORECASE | re.DOTALL
)
_DETAIL_MAIN_RE = re.compile(
    r"<main[^>]*>(?P<inner>.*?)</main>", re.IGNORECASE | re.DOTALL
)
_DETAIL_TAG_RE = re.compile(r"<[^>]+>")

# 2026-05-13 port: vanity-domain slug discovery. Every AppFolio-hosted
# vanity site references its PMC subdomain at least once (a ``connect`` /
# ``request_access`` link). Extract that slug and follow it to
# ``<slug>.appfolio.com/listings``. Source: 2026-04-30 failure-recovery
# investigation (33/46 recoveries).
_APPFOLIO_SLUG_RE = re.compile(
    r"https?://([a-z0-9-]+)\.appfolio\.com", re.IGNORECASE
)
_APPFOLIO_SKIP_SLUGS = frozenset({
    "www", "app", "support", "secure", "tenant", "tenants", "owner", "owners", "demo",
})


def find_appfolio_slug(html: str) -> str | None:
    """Return the first PMC slug referenced in *html* (or ``None``).

    Skips known infra subdomains (www, app, support, etc.). Used by the
    vanity-domain fallback path when the page being scraped is the
    property's marketing site rather than an ``<slug>.appfolio.com``
    subdomain. 2026-05-13 port from fix/resolver-path-patterns-may13.
    """
    if not html or "appfolio.com" not in html.lower():
        return None
    for m in _APPFOLIO_SLUG_RE.finditer(html):
        slug = m.group(1).lower()
        if slug and slug not in _APPFOLIO_SKIP_SLUGS:
            return slug
    return None


def parse_appfolio_detail_page(html: str, source_url: str) -> list[dict[str, Any]]:
    """Bug 7: parse a single AppFolio /listings/detail/<uuid> page into one unit.

    Returns ``[]`` when the page doesn't carry an extractable rent — pages
    without a recognisable ``$XXX`` token are almost certainly the
    sign-in/auth interstitial, not a real listing.
    """
    if not html:
        return []

    main_match = _DETAIL_MAIN_RE.search(html)
    main_html = main_match.group("inner") if main_match else html
    text = _DETAIL_TAG_RE.sub(" ", main_html)
    text = re.sub(r"\s+", " ", text).strip()

    rent = _DETAIL_RENT_RE.search(text)
    if not rent:
        return []
    try:
        rent_val = int(rent.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return []

    h1 = _DETAIL_H1_RE.search(html)
    floor_plan_name = ""
    if h1:
        floor_plan_name = _DETAIL_TAG_RE.sub("", h1.group("txt")).strip()

    beds = _DETAIL_BED_RE.search(text)
    baths = _DETAIL_BATH_RE.search(text)
    sqft = _DETAIL_SQFT_RE.search(text)

    return [
        {
            "unit_number": "",
            "floor_plan_name": floor_plan_name,
            "bedrooms": beds.group(1) if beds else "",
            "bathrooms": baths.group(1) if baths else "",
            "sqft": sqft.group(1).replace(",", "") if sqft else "",
            "rent_range": f"${rent_val:,}",
            "market_rent_low": rent_val,
            "market_rent_high": rent_val,
            "source_api_url": source_url,
            "extraction_tier": "TIER_1_DOM_APPFOLIO_DETAIL",
        }
    ]


# F11 — SSR DOM card extractors. Verified live against:
#   richelsonmanagement.appfolio.com (8 cards), becovic (300), pillarrei (23),
#   blackrealtymanagement (82), plentyofplaces (44).
# All five tenants emit identical class names; absent classes survive as None.
_LISTING_BLOCK_RE = re.compile(
    r'<[^>]*data-listing-id="(?P<id>[0-9]+)"[^>]*>(?P<body>.*?)(?=<[^>]*data-listing-id="[0-9]+"|<footer|</main|$)',
    re.IGNORECASE | re.DOTALL,
)
_RENT_RE = re.compile(r'js-listing-blurb-rent[^>]*>([^<]+)<', re.IGNORECASE)
_BED_BATH_RE = re.compile(r'js-listing-blurb-bed-bath[^>]*>([^<]+)<', re.IGNORECASE)
_SQFT_RE = re.compile(r'js-listing-square-feet[^>]*>([^<]+)<', re.IGNORECASE)
_AVAIL_RE = re.compile(r'js-listing-available[^>]*>([^<]+)<', re.IGNORECASE)
# F1 (2026-05-20 PID 67736 regression): the address text on the live AppFolio
# template is a direct child of the span (`<span class="u-pad-rm js-listing-address">3749 Arbor</span>`),
# not wrapped in a nested link/inner tag. Verified live against
# meridiapm.appfolio.com/listings 2026-05-20 — 0/300 addresses parsed under
# the old `>\s*<TAG>([^<]+)<` pattern, falling back to junk
# `floor_plan_name="AppFolio listing {id}"` for every unit. The optional
# `(?:<[^>]+>)?` middle group accepts both shapes (richelsonmanagement has
# the nested-tag style, meridiapm has direct text) so the existing
# fixture-based test plus the live-verified meridiapm shape both pass.
_ADDRESS_RE = re.compile(
    r'js-listing-address[^>]*>\s*(?:<[^>]+>)?\s*([^<]+?)\s*<',
    re.IGNORECASE,
)
# Verified against pablogroup.appfolio.com on 2026-05-05: the redirect lands
# on https://www.appfolio.com/page-not-found-sub which renders a page whose
# <title> includes "AppFolio - Page Not Found" alongside the canonical URL.
# Either signal alone is sufficient to classify the tenant as offboarded.
_OFFBOARDED_RE = re.compile(
    r'appfolio\.com/page-not-found-sub'
    r'|<title>\s*AppFolio\s*-\s*Page\s+Not\s+Found',
    re.IGNORECASE,
)


def _parse_bed_bath(text: str) -> tuple[int | None, float | None]:
    """Parse 'X bd / Y ba' or 'Studio / Y ba' into (beds, baths).

    AppFolio's bed_bath blurb is the only place beds/baths are reliable on
    the SSR card; sqft and rent live in dedicated divs.
    """
    if not text:
        return None, None
    s = text.strip().lower()
    beds: int | None = None
    baths: float | None = None
    if "studio" in s:
        beds = 0
    else:
        m = re.search(r'(\d+)\s*bd', s)
        if m:
            try:
                beds = int(m.group(1))
            except ValueError:
                beds = None
    m = re.search(r'(\d+(?:\.\d+)?)\s*ba', s)
    if m:
        try:
            baths = float(m.group(1))
        except ValueError:
            baths = None
    return beds, baths


def _parse_sqft_blurb(text: str) -> str:
    """Parse 'Square Feet: 1,342' to '1342'."""
    if not text:
        return ""
    m = re.search(r'([0-9][0-9,]*)', text)
    if not m:
        return ""
    return m.group(1).replace(",", "")


def parse_appfolio_listings_ssr(html: str, url: str) -> list[dict[str, str]]:
    """F11: parse AppFolio /listings SSR HTML into unit dicts.

    Uses regex on stable `js-listing-*` class names — every AppFolio tenant
    we sampled emits these identically. Skipping a full HTML parser keeps
    the path dependency-free and fast.
    """
    units: list[dict[str, str]] = []
    for m in _LISTING_BLOCK_RE.finditer(html):
        body = m.group("body")
        listing_id = m.group("id")
        rent_m = _RENT_RE.search(body)
        bb_m = _BED_BATH_RE.search(body)
        sqft_m = _SQFT_RE.search(body)
        avail_m = _AVAIL_RE.search(body)
        addr_m = _ADDRESS_RE.search(body)

        rent_val = money_to_int(rent_m.group(1).strip()) if rent_m else None
        beds, baths = _parse_bed_bath(bb_m.group(1) if bb_m else "")
        sqft = _parse_sqft_blurb(sqft_m.group(1) if sqft_m else "")
        avail_raw = avail_m.group(1).strip() if avail_m else ""
        address = addr_m.group(1).strip() if addr_m else ""

        units.append(
            make_unit_dict(
                floor_plan_name=address or f"AppFolio listing {listing_id}",
                bed_label=bed_label_from(beds, address),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=listing_id,
                rent_range=format_rent_range(rent_val, rent_val),
                availability_status="AVAILABLE",
                availability_date=avail_raw or "",
                source_api_url=url,
                extraction_tier="TIER_1_DOM_APPFOLIO_SSR",
            )
        )
    return units


def parse_appfolio_listings(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse AppFolio listing/floorplan objects into standard unit dicts.

    Handles both /listings endpoint (bedrooms, price, sqft) and
    /floorplans/all endpoint (bed, bath, rent, sq_ft).
    """
    units: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = get_field(item, "name", "listing_type", "property_type", "apartment_type")
        beds_str = get_field(item, "bed", "bedrooms", "beds", "bedroom_count")
        baths_str = get_field(item, "bath", "bathrooms", "baths", "bathroom_count")
        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        sqft = get_field(item, "sq_ft", "sqft", "square_feet", "squareFeet", "area")

        rent_lo = money_to_int(get_field(item, "price", "rent", "minRent", "asking_rent"))
        rent_hi = money_to_int(get_field(item, "maxRent", "max_rent"))

        unit_num = get_field(item, "unit_number", "unitNumber", "unit_id", "id", "label")
        avail_date = get_field(item, "available_date", "availableDate", "move_in_date")
        status = get_field(item, "status", "availability_status")

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bed_label_from(beds, name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=unit_num,
                rent_range=format_rent_range(rent_lo, rent_hi),
                availability_status="AVAILABLE"
                if not status or "avail" in status.lower()
                else status.upper(),
                availability_date=avail_date,
                source_api_url=url,
                extraction_tier="TIER_1_API_APPFOLIO",
            )
        )
    return units


def _is_appfolio_response(body: Any) -> bool:
    """Check if a response body looks like AppFolio listing/floorplan data.

    Requires at least two unit-signal keys (price/rent + beds/sqft) to avoid
    false positives on community-level metadata endpoints.

    AppFolio/Apts247 uses: bed, bath, rent, sq_ft, name (floorplans endpoint)
    or bedrooms, price, sqft (listings endpoint).
    """
    def _has_signals(items: list[dict[str, Any]]) -> bool:
        if not items or not isinstance(items[0], dict):
            return False
        # Use the centralized unit-signal key set from _merge_fns so any
        # new field-name additions automatically apply here too.
        return len(_MERGE_UNIT_SIGNAL_KEYS & set(items[0].keys())) >= 2

    if isinstance(body, dict):
        objects = body.get("objects") or body.get("results") or body.get("listings")
        if isinstance(objects, list):
            return _has_signals(objects)
    if isinstance(body, list):
        return _has_signals(body)
    return False


class AppFolioAdapter:
    """AppFolio PMS adapter."""

    pms_name: str = "appfolio"
    _fingerprints: list[str] = ["appfolio.com"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Extract units from AppFolio API or fall back to SSR DOM parse.

        Path:
          1. Try captured API responses (community_info etc.); succeeds on
             tenants where the API tier serves unit data.
          2. F11 fallback: when the API path returns 0 units, parse the SSR
             /listings HTML using js-listing-* selectors. Production
             verification: this path recovers ~449 units across the
             becovic/pillarrei/blackrealtymanagement/plentyofplaces cohort
             that today fails as BOT_BLOCKED.
          3. Detect offboarded tenants (302 → page-not-found-sub) and emit
             TENANT_OFFBOARDED so the run report distinguishes "tenant gone"
             from a real fetch failure.
        """
        from ma_poc.pms.adapters._adapter_telemetry import log_adapter_stage

        pid_for_log = str(getattr(ctx, "property_id", "") or "unknown")
        result = AdapterResult(tier_used="TIER_1_API_APPFOLIO")
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        n_matched_shape = 0
        for resp in api_responses:
            body = resp.get("body")
            if _is_appfolio_response(body):
                n_matched_shape += 1
                url = resp.get("url", "")
                items: list[dict[str, Any]] = []
                if isinstance(body, dict):
                    items = body.get("objects") or body.get("results") or body.get("listings") or []
                elif isinstance(body, list):
                    items = body
                units = parse_appfolio_listings(items, url)
                if units:
                    all_units.extend(units)
                    result.api_responses.append(resp)

        log_adapter_stage(
            "appfolio",
            pid_for_log,
            "xhr_capture",
            "ok" if all_units else "no_shape_match" if api_responses else "no_api_responses",
            units=len(all_units),
            reason=f"network_responses={len(api_responses)} matched={n_matched_shape}",
            n_responses=len(api_responses),
            matched_shape=n_matched_shape,
        )

        if all_units:
            # Stage 1 validity gate — drops dim-less rows.
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                # D16: strict unit-level / plan-level partition.
                result.units = list(_pp.units)
                result.plan_summaries = list(_pp.plan_summaries)
                result.post_process_meta = _pp.to_meta()
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                return result
            result.errors.append(
                f"APPFOLIO_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # F11 SSR fallback. Pull HTML from fetch_result first (Jugnu's
        # cached body avoids re-fetching), falling back to live page.
        page_html: str | None = None
        fetch_result = getattr(ctx, "fetch_result", None)
        if fetch_result is not None:
            body = getattr(fetch_result, "body", None)
            if isinstance(body, bytes):
                try:
                    page_html = body.decode("utf-8", errors="replace")
                except Exception:
                    page_html = None
            elif isinstance(body, str):
                page_html = body
        if page_html is None and page is not None:
            try:
                page_html = await page.content()
            except Exception:
                page_html = None

        if page_html and _OFFBOARDED_RE.search(page_html):
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.TENANT_OFFBOARDED,
                    getattr(ctx, "property_id", "unknown"),
                    base_url=getattr(ctx, "base_url", ""),
                )
            except (ImportError, AttributeError):
                pass
            result.confidence = 0.0
            result.errors.append("AppFolio tenant offboarded (page-not-found-sub)")
            result.tier_used = "TIER_1_APPFOLIO_TENANT_OFFBOARDED"
            return result

        if page_html and "data-listing-id=" in page_html:
            ssr_units = parse_appfolio_listings_ssr(page_html, getattr(ctx, "base_url", ""))
            if ssr_units:
                result.units = ssr_units
                result.tier_used = "TIER_1_DOM_APPFOLIO_SSR"
                result.winning_url = getattr(ctx, "base_url", "") or None
                result.confidence = min(0.95, 0.7 + 0.05 * len(ssr_units))
                return result

        # Bug 7 (2026-05-09 deep-dive): /listings/detail/<uuid> single-listing
        # pages don't carry data-listing-id and aren't an API envelope, but
        # the unit data is in the HTML. Parse it directly when the URL shape
        # signals a detail page. Confidence stays at 0.85 (single record →
        # we can't cross-check) so downstream gates still treat it as a
        # provisional Tier-1 result.
        if page_html:
            current_url = ""
            ff = getattr(ctx, "fetch_result", None)
            if ff is not None:
                current_url = getattr(ff, "final_url", "") or ""
            if not current_url:
                current_url = getattr(ctx, "base_url", "") or ""
            if "/listings/detail/" in current_url:
                detail_units = parse_appfolio_detail_page(page_html, current_url)
                if detail_units:
                    result.units = detail_units
                    result.tier_used = "TIER_1_DOM_APPFOLIO_DETAIL"
                    result.winning_url = current_url or None
                    result.confidence = 0.85
                    return result

        # 2026-05-13 port (Commit 14 wiring): cross-origin embed-recovery.
        # When the page being scraped is a Wix/Squarespace marketing shell
        # with a cross-origin iframe to ``{tenant}.appfolio.com/listings``,
        # the SSR fallback above misses the iframe contents. ``recover_
        # appfolio_embed`` uses ``page.evaluate`` to (a) scan the live
        # page for the iframe URL, then (b) fetch the iframe HTML in
        # session and hand it to the existing SSR parser. Net-new for
        # ~26 Wix/Squarespace shells (memory project_run_2026_05_19).
        if page is not None:
            try:
                from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
                embed_units = await recover_appfolio_embed(page, ctx)
            except Exception as exc:  # never raise out of an adapter
                embed_units = []
                result.errors.append(
                    f"appfolio-embed-recovery-error: {type(exc).__name__}: {str(exc)[:120]}"
                )
            if embed_units:
                result.units = embed_units
                result.tier_used = "TIER_1_DOM_APPFOLIO_EMBED"
                result.confidence = min(0.90, 0.7 + 0.04 * len(embed_units))
                return result

        result.confidence = 0.0
        result.errors.append("No AppFolio unit data found in captured API responses or SSR DOM")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
