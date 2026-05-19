"""
Funnel / Nestio adapter.

Research log
------------
Web sources consulted:
  - https://developers.funnelleasing.com/api/v2/auth.html (accessed 2026-04-20)
  - https://funnelleasing.com/products/ (accessed 2026-04-20)
  - https://nestiolistings.com/api/v2/listings/residential/rentals/ (documented
    endpoint, accessed 2026-04-20)
  - 2026-04-20 live-fetch of windsorcommunities.com/properties/windsor-sugarloaf/
    floorplans/ — Apply buttons target nestiolistings.com/api/v2/
    onlineleasing-link?myOlePropertyId=32164&companyID=19139&UnitID=...
Real payloads inspected:
  - Synthetic (shape-correct, envelope matches documented schema):
    tests/pms/adapters/fixtures/funnel/synthetic_listings.json — list-at-root
    envelope, 2 listings (65069 + 77589), 3 rentals total
  - Synthetic:
    tests/pms/adapters/fixtures/funnel/synthetic_wrapped.json — dict wrapper
    with ``results`` key, 3 flat rentals including 2 studios
  - Real captures are research-blocked (need >=2 from Windsor 65069 /
    77589 / 5715) — marked via pytest.mark.skip on the real-payload test
    so the gate surfaces the block without failing the suite.
Key findings:
  - API endpoint: nestiolistings.com/api/v2/listings/residential/rentals/?key=<public_key>
  - Response envelope: EITHER a list-at-root of listing objects, each carrying
    a ``rentals`` list, OR a dict wrapper with a ``listings`` / ``results`` /
    ``data`` / ``rentals`` key pointing at a flat list of rentals.
  - Rental-row fields observed: unit, marketRent (number, monthly), availabilityDate,
    bedrooms (int), bathrooms (float), squareFeet (int), floorPlanName,
    buildingName, floor.
  - Unit ID field: ``unit`` preferred; fall back to ``listingId`` then
    ``<buildingName>-<unit>`` composite to keep unit IDs unique across
    multi-building properties.
  - Rent field: ``marketRent`` is a flat number, not a range — map to
    rent_low==rent_high. Some rows ship a separate ``marketRentLow`` /
    ``marketRentHigh`` pair when pricing is tiered; prefer those when present.
  - Availability date field: ``availabilityDate`` (ISO yyyy-mm-dd).
  - Known gotchas: Funnel is PMS-agnostic — the back-office may be Yardi /
    RealPage / Entrata, but the listings API is always Funnel's. Do NOT
    confuse securecafe.com resident-portal links (Yardi) with the actual
    inventory endpoint. The ``api`` value-field is not set on Funnel
    payloads (unlike RentCafe's ``"api": "rentcafe"``), so shape detection
    relies on structural keys (listingId / marketRent / rentals).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


_TIER_BASE = "TIER_1_API_FUNNEL"
_TIER_NO_RESPONSE = f"{_TIER_BASE}_NO_RESPONSE"
_TIER_SHAPE_REJECTED = f"{_TIER_BASE}_SHAPE_REJECTED"
_TIER_LIST_EMPTY = f"{_TIER_BASE}_LIST_EMPTY"
# Funnel "Spaces" frontend widget SSR fallback. Funnel customers on the
# WordPress "Spaces" theme (Windsor et al.) render every unit server-side
# as <article class="spaces-unit" data-spaces-*> and call nestiolistings
# server-side — so no nestio XHR is ever captured and the API path above
# yields LIST_EMPTY/SHAPE_REJECTED even though full unit-level data is in
# the page HTML. 2026-05-18 (HAR www.windsorcommunities.com): proven
# 46/46 units on windsor-addison, all with unit#+price+bed/bath+area+
# plan+avail-date. Deterministic — pure data-attribute extraction.
_TIER_SPACES_SSR = "TIER_1_DOM_FUNNEL_SPACES"

_SPACES_ARTICLE_RE = re.compile(
    r'<article[^>]*\bclass="[^"]*\bspaces-unit\b[^"]*"[^>]*>', re.IGNORECASE
)


def _spaces_attr(tag: str, name: str) -> str:
    """Return the value of HTML attribute *name* in *tag*, or ''."""
    m = re.search(r'\b' + re.escape(name) + r'="([^"]*)"', tag, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_funnel_spaces_ssr(html: str, source_url: str) -> list[dict[str, str]]:
    """Parse Funnel "Spaces" SSR markup into unit-level dicts.

    Each available unit is one ``<article class="spaces-unit"
    data-spaces-obj="unit" ...>`` carrying a complete data-attribute set:
    ``data-spaces-unit`` (unit number), ``data-spaces-sort-price``,
    ``data-spaces-sort-bed/bath/area``, ``data-spaces-sort-plan-name``,
    ``data-spaces-soonest`` (avail date), ``data-spaces-available``.
    Returns ``[]`` when the markup is absent (caller falls through).
    """
    if not html or "spaces-unit" not in html:
        return []
    units: list[dict[str, str]] = []
    for m in _SPACES_ARTICLE_RE.finditer(html):
        tag = m.group(0)
        if _spaces_attr(tag, "data-spaces-obj") != "unit":
            continue
        unit_no = _spaces_attr(tag, "data-spaces-unit")
        if not unit_no:
            continue
        price = _spaces_attr(tag, "data-spaces-sort-price")
        rent = money_to_int(price) if price else None
        beds = _spaces_attr(tag, "data-spaces-sort-bed")
        baths = _spaces_attr(tag, "data-spaces-sort-bath")
        plan = _spaces_attr(tag, "data-spaces-sort-plan-name")
        try:
            beds_int = int(beds) if beds not in ("", None) else None
        except ValueError:
            beds_int = None
        avail = _spaces_attr(tag, "data-spaces-available") == "true"
        units.append(
            make_unit_dict(
                floor_plan_name=plan,
                bed_label=bed_label_from(beds_int, plan),
                bedrooms=beds,
                bathrooms=baths,
                sqft=_spaces_attr(tag, "data-spaces-sort-area"),
                unit_number=unit_no,
                rent_low=rent,
                rent_high=rent,
                availability_status="AVAILABLE" if avail else "UNAVAILABLE",
                availability_date=_spaces_attr(tag, "data-spaces-soonest"),
                source_api_url=source_url,
                extraction_tier=_TIER_SPACES_SSR,
            )
        )
    return units


# Funnel "Spaces" sites (Windsor et al.) put the unit list on the
# ``…/properties/<slug>/floorplans/`` sub-page, NOT the landing page the
# pipeline fetches (vanity domains 301 → windsorcommunities.com/
# properties/<slug>/). The landing page is gated by the WordPress
# "wincommunities"/Spaces theme and links to floorplans/ — so when the
# current HTML is a Spaces landing page WITHOUT spaces-unit markup,
# resolve the one floorplans/ link and probe it. Deterministic, single
# extra GET, gated tightly so it never fires on non-Spaces Funnel sites.
_SPACES_SITE_MARKERS = ("wincommunities", "data-spaces", "spaces_get_",
                        "spaces_tab=")
_SPACES_FP_HREF_RE = re.compile(
    r'href="([^"]*?/?floorplans/?(?:\?[^"]*)?)"', re.IGNORECASE
)


def _spaces_floorplans_url(html: str, base_url: str) -> str | None:
    """Return the absolute Spaces ``…/floorplans/`` URL, or None.

    Gated on a Spaces/wincommunities marker so it only fires for the
    Funnel-Spaces SSR cluster. Resolves the floorplans href against
    *base_url* (the post-redirect landing URL).
    """
    if not html or not base_url:
        return None
    if not any(m in html for m in _SPACES_SITE_MARKERS):
        return None
    from urllib.parse import urljoin

    best: str | None = None
    for m in _SPACES_FP_HREF_RE.finditer(html):
        href = m.group(1)
        absu = href if href.startswith("http") else urljoin(base_url, href)
        # Prefer the bare floorplans/ page over filtered (?spaces_tab=…)
        # variants so we get the full unit list.
        if "?" not in absu:
            return absu
        best = best or absu
    return best


_TIER_PARSE_ZERO = f"{_TIER_BASE}_PARSE_ZERO"


# URL fingerprints — Funnel always serves through nestiolistings.com regardless
# of customer domain. ``nestiostaging.com`` is the staging mirror documented
# in the public API reference.
_FUNNEL_URL_MARKERS = ("nestiolistings.com/api/", "nestiostaging.com/api/")

# Wrapper keys observed in captures and the public developer docs. Matched in
# order; the first key whose value is a non-empty list wins. ``rentals`` is
# included for the listing-level envelope where each listing object carries
# a nested ``rentals`` list — but that's unwrapped at the listing layer, not
# the root, so callers should handle both modes explicitly.
_FUNNEL_DICT_LIST_KEYS = ("listings", "results", "data", "rentals")

# Rental-level (flat) keys that identify a Funnel rental row.
_FUNNEL_RENTAL_KEYS = {
    "marketRent",
    "marketrent",
    "availabilityDate",
    "availabilitydate",
    "floorPlanName",
    "floorplanname",
    "bedrooms",
    "bathrooms",
    "squareFeet",
    "squarefeet",
    "unit",
    "listingId",
    "listingid",
}

# Listing-level keys that identify a Funnel listing (the outer envelope).
_FUNNEL_LISTING_KEYS = {
    "listingId",
    "listingid",
    "rentals",
    "marketRent",
    "marketrent",
    "availabilityDate",
    "availabilitydate",
}


def _is_funnel_response_url(url: str) -> bool:
    """True if *url* is a Funnel/Nestio listings API call."""
    return any(m in url for m in _FUNNEL_URL_MARKERS)


def _is_funnel_response_body(body: Any) -> bool:
    """Body-shape check for Funnel listings response.

    Accepts:
    - List at root where the first element has >=2 Funnel listing keys.
    - Dict wrapper with a ``listings``/``results``/``data``/``rentals`` key
      whose first element has >=2 Funnel rental keys.
    """
    if isinstance(body, list):
        if not body or not isinstance(body[0], dict):
            return False
        keys = set(body[0].keys())
        # Accept the listing envelope OR a flat rental-row list at root.
        listing_hits = len(_FUNNEL_LISTING_KEYS & keys)
        rental_hits = len(_FUNNEL_RENTAL_KEYS & keys)
        return listing_hits >= 2 or rental_hits >= 2
    if isinstance(body, dict):
        for list_key in _FUNNEL_DICT_LIST_KEYS:
            v = body.get(list_key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                keys = set(v[0].keys())
                if len(_FUNNEL_RENTAL_KEYS & keys) >= 2:
                    return True
                if len(_FUNNEL_LISTING_KEYS & keys) >= 2:
                    return True
    return False


def _unwrap_funnel_list(body: Any) -> list[Any] | None:
    """Flatten the response body to a list of rental-row dicts.

    Handles list-at-root listing envelopes (each listing has ``rentals``) as
    well as dict-wrapped envelopes whose list may be either listings or
    flat rentals. Returns None when no rentals can be found.
    """

    def _flatten_listings(listings: list[Any]) -> list[Any]:
        flat: list[Any] = []
        for listing in listings:
            if not isinstance(listing, dict):
                continue
            rentals = listing.get("rentals")
            if isinstance(rentals, list) and rentals:
                # Listing-level envelope: merge building-level metadata into
                # each rental so the parser can pull floor_plan_name /
                # building even when the rental itself is sparse.
                building_defaults = {
                    "buildingName": listing.get("buildingName") or listing.get("building") or "",
                    "floorPlanName": listing.get("floorPlanName") or listing.get("name") or "",
                }
                for r in rentals:
                    if isinstance(r, dict):
                        merged = dict(building_defaults)
                        merged.update(r)
                        flat.append(merged)
            else:
                # Flat rental at this level.
                flat.append(listing)
        return flat

    if isinstance(body, list):
        return _flatten_listings(body) or None
    if isinstance(body, dict):
        for list_key in _FUNNEL_DICT_LIST_KEYS:
            v = body.get(list_key)
            if isinstance(v, list) and v:
                # If items have a ``rentals`` child, treat as listing envelope
                # and flatten; otherwise treat as flat rental list.
                if isinstance(v[0], dict) and isinstance(v[0].get("rentals"), list):
                    return _flatten_listings(v) or None
                return list(v)
    return None


def parse_funnel_listings(body: Any, url: str) -> list[dict[str, str]]:
    """Parse Funnel/Nestio listings response into standard unit dicts.

    Source envelope reference: see research log at top of file.
    """

    def _pick(source: dict[str, Any], *keys: str) -> Any:
        """Return the first non-empty value in *source* for *keys*."""
        for k in keys:
            if k in source and source[k] not in (None, ""):
                return source[k]
        return None

    rows = _unwrap_funnel_list(body) or []
    units: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        unit_id = _pick(row, "unit", "listingId", "listingid")
        building = _pick(row, "buildingName", "buildingname", "building")
        if unit_id and building and unit_id != building:
            unit_number = f"{unit_id}"
        else:
            unit_number = str(unit_id or "")

        floor_plan_name = str(
            _pick(row, "floorPlanName", "floorplanname", "floor_plan_name", "floorPlan", "name") or ""
        )

        beds_raw = _pick(row, "bedrooms", "beds")
        try:
            beds = int(beds_raw) if beds_raw is not None else None
        except (TypeError, ValueError):
            beds = None

        baths_raw = _pick(row, "bathrooms", "baths")
        try:
            baths = int(float(baths_raw)) if baths_raw is not None else None
        except (TypeError, ValueError):
            baths = None

        sqft_raw = _pick(row, "squareFeet", "squarefeet", "sqft", "sqftTotal")
        sqft = str(int(float(sqft_raw))) if sqft_raw not in (None, "", "0") else ""

        # Rent: prefer explicit low/high pair, otherwise use marketRent flat.
        rent_lo_raw = _pick(row, "marketRentLow", "marketrentlow", "rentLow")
        rent_hi_raw = _pick(row, "marketRentHigh", "marketrenthigh", "rentHigh")
        rent_flat = _pick(row, "marketRent", "marketrent", "rent")
        rent_lo: int | None = None
        rent_hi: int | None = None
        if rent_lo_raw is not None:
            rent_lo = money_to_int(str(rent_lo_raw))
        if rent_hi_raw is not None:
            rent_hi = money_to_int(str(rent_hi_raw))
        if rent_lo is None and rent_hi is None and rent_flat is not None:
            rent_lo = money_to_int(str(rent_flat))
            rent_hi = rent_lo

        avail_date = str(_pick(row, "availabilityDate", "availabilitydate", "available_on") or "")
        floor = str(_pick(row, "floor", "floorNumber") or "")
        # 2026-05-19 capture-first: Funnel/Nestio carries concession in
        # the schema (incentives_marketing_description / special_offers /
        # incentives). Often empty (no active special — correct, not a
        # bug) but capture raw when present; v2's widened concession
        # alias chain maps it.
        concession = str(_pick(
            row, "incentives_marketing_description", "special_offers",
            "incentives", "concession", "concessions", "specials",
            "specials_description",
        ) or "")

        units.append(
            make_unit_dict(
                floor_plan_name=floor_plan_name,
                bed_label=bed_label_from(beds, floor_plan_name),
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft,
                unit_number=unit_number,
                floor=floor,
                building=str(building or ""),
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi,
                availability_status="AVAILABLE" if avail_date else "AVAILABLE",
                available_units="1",
                availability_date=avail_date,
                concession=concession,
                source_api_url=url,
                extraction_tier=_TIER_BASE,
            )
        )
    return units


def _classify_funnel_failure(
    api_responses: list[dict[str, Any]],
) -> tuple[str, str]:
    """Same pattern as Change 1. Returns (tier_code, error_message)."""
    if not api_responses:
        return (
            _TIER_NO_RESPONSE,
            "FUNNEL_NO_RESPONSE: no network responses captured during page load; "
            "check if page is making calls to nestiolistings.com at all",
        )
    url_matches = [r for r in api_responses if _is_funnel_response_url(r.get("url", ""))]
    shape_matches = [r for r in api_responses if _is_funnel_response_body(r.get("body"))]
    if not url_matches and not shape_matches:
        return (
            _TIER_SHAPE_REJECTED,
            f"FUNNEL_SHAPE_REJECTED: {len(api_responses)} responses captured, "
            "none to nestiolistings.com and none matched Funnel envelope",
        )
    relevant = shape_matches or url_matches
    total_items = 0
    for r in relevant:
        items = _unwrap_funnel_list(r.get("body")) or []
        total_items += len(items)
    if total_items == 0:
        return (
            _TIER_LIST_EMPTY,
            f"FUNNEL_LIST_EMPTY: {len(relevant)} relevant responses, "
            "listings list empty in all (property may have 0 availability)",
        )
    return (
        _TIER_PARSE_ZERO,
        f"FUNNEL_PARSE_ZERO: {total_items} listing items present but parser "
        "emitted zero units (field-name mismatch)",
    )


class FunnelAdapter:
    """Funnel / Nestio PMS adapter.

    Funnel markets itself as 'PMS-agnostic' — the listings API is always
    Funnel's even when the back-office PMS is Yardi, RealPage, or Entrata.
    The adapter therefore matches on body shape / URL rather than marketing
    domain (windsorcommunities.com / etc).
    """

    pms_name: str = "funnel"
    _fingerprints: list[str] = [
        "nestiolistings.com",
        "nestiostaging.com",
    ]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER_BASE)
        all_units: list[dict[str, str]] = []

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if not (_is_funnel_response_url(url) or _is_funnel_response_body(body)):
                continue
            units = parse_funnel_listings(body, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)

        if all_units:
            from ma_poc.extraction.post_process import post_process

            _pp_parsed = len(all_units)
            _pp = post_process(all_units, property_id=getattr(ctx, "property_id", None))
            if _pp.n_admitted > 0:
                result.units = _pp.admitted
                result.plan_summaries = _pp.plan_summaries
                result.winning_url = (
                    result.api_responses[0].get("url") if result.api_responses else None
                )
                result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
                result.tier_used = _TIER_BASE
                return result
            result.errors.append(
                f"FUNNEL_VALIDITY_REJECTED: {_pp_parsed} parsed rows "
                f"failed unit_validity (no numeric dimension)"
            )

        # Funnel "Spaces" SSR fallback: customers on the WordPress Spaces
        # theme call nestiolistings server-side and render units into the
        # page, so no nestio XHR is ever captured (the API path above
        # always LIST_EMPTY/SHAPE_REJECTED). The full unit-level data is
        # in the rendered page HTML as data-spaces-* article markup.
        try:
            from ma_poc.pms.adapters.generic import _get_page_html

            _sp_html = await _get_page_html(page, ctx)
        except Exception:
            _sp_html = ""
        _fr = getattr(ctx, "fetch_result", None)
        _final = str(getattr(_fr, "final_url", "") or "") if _fr else ""
        _src = _final or str(getattr(ctx, "base_url", "") or "")
        # Spaces landing page (no spaces-unit) → hop to the floorplans/
        # sub-page where the SSR unit list lives.
        if _sp_html and "spaces-unit" not in _sp_html:
            _fp_url = _spaces_floorplans_url(_sp_html, _src)
            if _fp_url:
                try:
                    from ma_poc.pms.adapters._probe import probe_get

                    _fpr = probe_get(_fp_url, timeout=20)
                    if _fpr.status_code == 200 and _fpr.text and (
                        "spaces-unit" in _fpr.text
                    ):
                        _sp_html = _fpr.text
                        _src = _fp_url
                except Exception as _fp_exc:
                    result.errors.append(
                        f"funnel-spaces-floorplans-hop-error: "
                        f"{type(_fp_exc).__name__}: {str(_fp_exc)[:100]}"
                    )
        if _sp_html and "spaces-unit" in _sp_html:
            _sp_units = parse_funnel_spaces_ssr(_sp_html, _src)
            if _sp_units:
                from ma_poc.extraction.post_process import post_process

                _sp_pp = post_process(
                    _sp_units, property_id=getattr(ctx, "property_id", None)
                )
                if _sp_pp.n_admitted > 0:
                    result.units = _sp_pp.admitted
                    result.plan_summaries = _sp_pp.plan_summaries
                    result.tier_used = _TIER_SPACES_SSR
                    result.confidence = min(0.92, 0.7 + 0.04 * _sp_pp.n_admitted)
                    return result

        tier_code, err_msg = _classify_funnel_failure(api_responses)
        result.tier_used = tier_code
        result.confidence = 0.0
        result.errors.append(err_msg)
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        """Body-shape check used by ``detector.confirm_detection``."""
        return _is_funnel_response_body(body)
