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

import asyncio
import json
import re
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

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
_TIER_PUBLISHED_LISTINGS = f"{_TIER_BASE}_PUBLISHED_LISTINGS"
# Funnel "Spaces" frontend widget SSR fallback. Funnel customers on the
# WordPress "Spaces" theme (Windsor et al.) render every unit server-side
# as <article class="spaces-unit" data-spaces-*> and call nestiolistings
# server-side — so no nestio XHR is ever captured and the API path above
# yields LIST_EMPTY/SHAPE_REJECTED even though full unit-level data is in
# the page HTML. 2026-05-18 (HAR www.windsorcommunities.com): proven
# 46/46 units on windsor-addison, all with unit#+price+bed/bath+area+
# plan+avail-date. Deterministic — pure data-attribute extraction.
#
# A second, live-verified renderer spells the BEM class ``spaces__unit``
# while retaining the exact same ``data-spaces-*`` schema. 2026-08-01 probes:
# Arrivé Seattle (15 modern cards), Windsor Addison (42 legacy cards), and
# Windsor Sugarloaf (10 legacy cards). Keep the accepted classes exact; a
# broad ``spaces`` substring would collide with ordinary amenity copy.
_TIER_SPACES_SSR = "TIER_1_DOM_FUNNEL_SPACES"

_SPACES_ARTICLE_RE = re.compile(
    r'<article[^>]*\bclass="[^"]*(?:\bspaces-unit\b|\bspaces__unit\b)'
    r'[^"]*"[^>]*>',
    re.IGNORECASE,
)


def has_funnel_spaces_unit_markup(html: str) -> bool:
    """Return whether *html* contains an exact Funnel Spaces unit card."""
    return bool(html and _SPACES_ARTICLE_RE.search(html))


def _spaces_attr(tag: str, name: str) -> str:
    """Return the value of HTML attribute *name* in *tag*, or ''."""
    m = re.search(r'\b' + re.escape(name) + r'="([^"]*)"', tag, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_funnel_spaces_ssr(html: str, source_url: str) -> list[dict[str, str]]:
    """Parse Funnel "Spaces" SSR markup into unit-level dicts.

    Each available unit is one ``<article class="spaces-unit"`` (legacy) or
    ``<article class="spaces__unit"`` (modern)
    data-spaces-obj="unit" ...>`` carrying a complete data-attribute set:
    ``data-spaces-unit`` (unit number), ``data-spaces-sort-price``,
    ``data-spaces-sort-bed/bath/area``, ``data-spaces-sort-plan-name``,
    ``data-spaces-soonest`` (avail date), ``data-spaces-available``.
    Returns ``[]`` when the markup is absent (caller falls through).
    """
    if not has_funnel_spaces_unit_markup(html):
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
_SPACES_SITE_MARKERS = (
    "wincommunities",
    "data-spaces",
    "spaces_get_",
    "spaces_tab=",
    "/wp-content/plugins/ecs-spaces/",
    "spaces_scripts.js",
)
_SPACES_INVENTORY_HREF_RE = re.compile(
    r'\bhref\s*=\s*["\'](?P<href>[^"\']+)["\']',
    re.IGNORECASE,
)


def _spaces_floorplans_url(html: str, base_url: str) -> str | None:
    """Return one authored same-origin Spaces inventory URL, or ``None``.

    Legacy Windsor sites author ``…/floorplans/``; the modern ECS Spaces
    renderer authors ``…/apartments/``. Discovery is gated on a verified
    Spaces marker and accepts only a unique, same-host HTTP(S) anchor whose
    final path segment is exactly one of those inventory routes. Query and
    fragment variants collapse to the same canonical URL.
    """
    if not html or not base_url:
        return None
    low = html.casefold()
    if not any(marker in low for marker in _SPACES_SITE_MARKERS):
        return None
    from urllib.parse import urljoin, urlsplit, urlunsplit

    try:
        source = urlsplit(base_url)
    except ValueError:
        return None
    source_host = (source.hostname or "").lower().rstrip(".")
    if source.scheme.lower() not in {"http", "https"} or not source_host:
        return None

    candidates: dict[str, None] = {}
    for match in _SPACES_INVENTORY_HREF_RE.finditer(html):
        href = unescape(match.group("href")).strip()
        try:
            target = urlsplit(urljoin(base_url, href))
        except ValueError:
            continue
        if (
            target.scheme.lower() not in {"http", "https"}
            or (target.hostname or "").lower().rstrip(".") != source_host
            or target.username
            or target.password
        ):
            continue
        path = target.path or "/"
        final_segment = path.rstrip("/").rsplit("/", 1)[-1].casefold()
        if final_segment not in {"floorplans", "apartments"}:
            continue
        canonical_path = path.rstrip("/") + "/"
        clean = urlunsplit(
            (target.scheme, target.netloc, canonical_path, "", "")
        )
        candidates.setdefault(clean, None)

    if len(candidates) != 1:
        return None
    return next(iter(candidates))


_SPACES_MAX_BODY_BYTES = 3_000_000


async def _fetch_spaces_inventory(url: str) -> tuple[str, str] | None:
    """Fetch one authored Spaces route with plain HTTP only.

    This recovery must stay safe in production: it deliberately opts out of
    environment proxies and has no Web Unlocker, browser, fingerprint or
    CAPTCHA escalation. Redirects may change path but never origin.
    """
    from urllib.parse import urlsplit

    import httpx

    try:
        expected = urlsplit(url)
    except ValueError:
        return None
    expected_host = (expected.hostname or "").lower().rstrip(".")
    if expected.scheme.lower() not in {"http", "https"} or not expected_host:
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            trust_env=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as client:
            response = await client.get(url)
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code != 200 or len(response.content) > _SPACES_MAX_BODY_BYTES:
        return None
    final_url = str(response.url)
    try:
        final = urlsplit(final_url)
    except ValueError:
        return None
    if (final.hostname or "").lower().rstrip(".") != expected_host:
        return None
    final_segment = (final.path or "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    if final_segment not in {"floorplans", "apartments"}:
        return None
    return response.text, final_url


def _spaces_body_from_ctx(ctx: AdapterContext) -> tuple[str, str]:
    fetch_result = getattr(ctx, "fetch_result", None)
    raw = getattr(fetch_result, "body", None)
    if isinstance(raw, bytes):
        body = raw.decode("utf-8", errors="replace")
    else:
        body = raw if isinstance(raw, str) else ""
    source_url = str(getattr(fetch_result, "final_url", "") or "") or str(
        getattr(ctx, "base_url", "") or ""
    )
    return body, source_url


async def recover_funnel_spaces(
    ctx: AdapterContext,
    *,
    html_override: str = "",
    source_url_override: str = "",
) -> list[dict[str, str]]:
    """Recover native Funnel Spaces units from the current or linked page.

    A direct card roster is self-authenticating through its exact schema. A
    landing-page hop additionally requires a verified Spaces plugin/template
    marker and one unique, authored, same-origin inventory link.
    """
    ctx_body, ctx_source_url = _spaces_body_from_ctx(ctx)
    body = html_override or ctx_body
    source_url = source_url_override or ctx_source_url
    direct = parse_funnel_spaces_ssr(body, source_url)
    if direct:
        return direct

    inventory_url = _spaces_floorplans_url(body, source_url)
    if inventory_url is None:
        return []
    fetched = await _fetch_spaces_inventory(inventory_url)
    if fetched is None:
        return []
    inventory_body, final_url = fetched
    return parse_funnel_spaces_ssr(inventory_body, final_url)


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
_FUNNEL_DICT_LIST_KEYS = ("listings", "results", "data", "rentals", "items")

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

# Current Funnel/Nestio ``/api/v2/listings/all/`` response.  This is a
# separate, deliberately strong shape gate: ``items`` is far too generic to
# admit on its own, and the neighborhoods/config endpoints use the same host.
_FUNNEL_CURRENT_ITEM_KEYS = {
    "id",
    "unit_number",
    "price",
    "date_available",
    "layout",
    "bedrooms",
    "bathrooms",
    "building",
}


def _is_funnel_current_items_response(body: Any) -> bool:
    """True for the property-scoped current Nestio listings envelope."""
    if not isinstance(body, dict):
        return False
    items = body.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return False
    first = items[0]
    if len(_FUNNEL_CURRENT_ITEM_KEYS & set(first)) < 7:
        return False
    building = first.get("building")
    community = building.get("community") if isinstance(building, dict) else None
    return bool(
        isinstance(community, dict)
        and str(community.get("id") or "").isdigit()
        and str(community.get("name") or "").strip()
        and str(first.get("unit_number") or "").strip()
        and str(first.get("id") or "").isdigit()
    )

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
        if _is_funnel_current_items_response(body):
            return True
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

        current_building = row.get("building") if isinstance(row.get("building"), dict) else {}
        current_community = (
            current_building.get("community")
            if isinstance(current_building.get("community"), dict)
            else {}
        )
        unit_id = _pick(row, "unit_number", "unit", "listingId", "listingid", "id")
        building = (
            _pick(row, "buildingName", "buildingname")
            or current_building.get("name")
            or ""
        )
        if unit_id and building and unit_id != building:
            unit_number = f"{unit_id}"
        else:
            unit_number = str(unit_id or "")

        floor_plan_name = str(
            _pick(
                row,
                "floorPlanName",
                "floorplanname",
                "floor_plan_name",
                "floorPlan",
                "layout",
                "name",
            )
            or ""
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

        sqft_raw = _pick(
            row,
            "squareFeet",
            "squarefeet",
            "square_footage",
            "sqft",
            "sqftTotal",
        )
        sqft = str(int(float(sqft_raw))) if sqft_raw not in (None, "", "0") else ""

        # Rent: prefer explicit low/high pair, otherwise use marketRent flat.
        rent_lo_raw = _pick(row, "marketRentLow", "marketrentlow", "rentLow")
        rent_hi_raw = _pick(row, "marketRentHigh", "marketrenthigh", "rentHigh")
        rent_flat = _pick(row, "marketRent", "marketrent", "rent", "price")
        rent_lo: int | None = None
        rent_hi: int | None = None
        if rent_lo_raw is not None:
            rent_lo = money_to_int(str(rent_lo_raw))
        if rent_hi_raw is not None:
            rent_hi = money_to_int(str(rent_hi_raw))
        if rent_lo is None and rent_hi is None and rent_flat is not None:
            rent_lo = money_to_int(str(rent_flat))
            rent_hi = rent_lo

        avail_date = str(
            _pick(
                row,
                "availabilityDate",
                "availabilitydate",
                "date_available",
                "available_on",
            )
            or ""
        )
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

        source_ids: dict[str, Any] = {}
        listing_id = str(row.get("id") or row.get("listingId") or row.get("listingid") or "").strip()
        if listing_id:
            source_ids["funnel_listing_id"] = listing_id
        building_id = str(current_building.get("id") or "").strip()
        if building_id:
            source_ids["funnel_building_id"] = building_id
        community_id = str(current_community.get("id") or "").strip()
        if community_id:
            source_ids["funnel_community_id"] = community_id

        unit = make_unit_dict(
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
                extraction_tier=(
                    _TIER_PUBLISHED_LISTINGS
                    if _is_funnel_current_items_response(body)
                    else _TIER_BASE
                ),
                source_ids=source_ids,
            )
        if current_community:
            unit["source_property_id"] = community_id
            unit["source_property_name"] = str(
                current_community.get("name") or current_building.get("name") or ""
            ).strip()
            unit["source_property_website"] = str(
                current_community.get("website_url") or ""
            ).strip()
            unit["source_property_address"] = ", ".join(
                part
                for part in (
                    str(current_community.get("street_address") or "").strip(),
                    str(current_community.get("city") or "").strip(),
                    str(current_community.get("state") or "").strip(),
                    str(current_community.get("postal_code") or "").strip(),
                )
                if part
            )
            unit["source_property_provenance"] = "published_nestio_community"
        units.append(unit)
    return units


_PUBLISHED_NESTIO_LISTINGS_RE = re.compile(
    r"https://nestiolistings\.com/api/v2/listings/all/\?[^\s\"'<>]+",
    re.IGNORECASE,
)


def _published_nestio_listings_url(html: str) -> tuple[str, str] | None:
    """Return one exact page-published listings URL and native community ID.

    Global management sites can mention several communities.  Reject unless
    the current property page publishes exactly one distinct ``key`` /
    ``property`` pair for the current listings endpoint.
    """
    if not html:
        return None
    pairs: dict[tuple[str, str], str] = {}
    for match in _PUBLISHED_NESTIO_LISTINGS_RE.finditer(
        unescape(html.replace("\\/", "/"))
    ):
        raw = match.group(0).rstrip(".,);]")
        try:
            parsed = urlsplit(raw)
        except ValueError:
            continue
        if (
            (parsed.hostname or "").casefold() != "nestiolistings.com"
            or parsed.path.rstrip("/").casefold() != "/api/v2/listings/all"
        ):
            continue
        query = parse_qs(parsed.query)
        keys = query.get("key", [])
        property_ids = query.get("property", [])
        if len(keys) != 1 or len(property_ids) != 1:
            continue
        public_key = str(keys[0]).strip()
        property_id = str(property_ids[0]).strip()
        if (
            not re.fullmatch(r"[a-z0-9]{16,64}", public_key, re.IGNORECASE)
            or not re.fullmatch(r"\d{1,16}", property_id)
        ):
            continue
        canonical = "https://nestiolistings.com/api/v2/listings/all/?" + urlencode(
            {"key": public_key, "property": property_id}
        )
        pairs[(public_key, property_id)] = canonical
    if len(pairs) != 1:
        return None
    (public_key, property_id), url = next(iter(pairs.items()))
    del public_key
    return url, property_id


_FUNNEL_ADDRESS_NOISE = {
    "apartments",
    "avenue",
    "ave",
    "boulevard",
    "blvd",
    "east",
    "e",
    "north",
    "n",
    "road",
    "rd",
    "south",
    "s",
    "street",
    "st",
    "west",
    "w",
}


def _funnel_normalize(value: Any) -> str:
    return "".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _funnel_name_key(value: Any) -> str:
    ignored = {
        "apartment",
        "apartments",
        "community",
        "the",
        *_FUNNEL_ADDRESS_NOISE,
    }
    return "".join(
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if token not in ignored
    )


def _funnel_street_matches(expected: Any, observed: Any) -> bool:
    expected_tokens = re.findall(r"[a-z0-9]+", str(expected or "").casefold())
    observed_tokens = set(re.findall(r"[a-z0-9]+", str(observed or "").casefold()))
    if not expected_tokens or expected_tokens[0] not in observed_tokens:
        return False
    core = {
        token
        for token in expected_tokens[1:]
        if len(token) >= 2 and token not in _FUNNEL_ADDRESS_NOISE
    }
    return bool(core and core <= observed_tokens)


def _funnel_current_items_match_context(
    body: Any,
    native_property_id: str,
    ctx: AdapterContext,
) -> bool:
    """Fail closed unless every item repeats the exact canonical community."""
    if not _is_funnel_current_items_response(body):
        return False
    items = body.get("items") or []
    expected_name = _funnel_name_key(getattr(ctx, "property_name", ""))
    expected_address = str(getattr(ctx, "address", "") or "").strip()
    expected_city = _funnel_normalize(getattr(ctx, "city", ""))
    expected_state = _funnel_normalize(getattr(ctx, "state", ""))
    expected_zip = _funnel_normalize(getattr(ctx, "zip_code", ""))
    if not all(
        (expected_name, expected_address, expected_city, expected_state, expected_zip)
    ):
        return False

    observed_boundaries: set[tuple[str, str, str, str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            return False
        building = item.get("building")
        community = building.get("community") if isinstance(building, dict) else None
        if not isinstance(community, dict):
            return False
        community_id = str(community.get("id") or "").strip()
        community_name = str(
            community.get("name") or building.get("name") or ""
        ).strip()
        street = str(
            community.get("street_address") or building.get("street_address") or ""
        ).strip()
        city = _funnel_normalize(community.get("city"))
        state = _funnel_normalize(community.get("state"))
        zip_code = _funnel_normalize(community.get("postal_code"))
        observed_boundaries.add(
            (community_id, _funnel_name_key(community_name), street, city, state, zip_code)
        )
    if len(observed_boundaries) != 1:
        return False
    community_id, name_key, street, city, state, zip_code = next(
        iter(observed_boundaries)
    )
    return bool(
        community_id == native_property_id
        and name_key == expected_name
        and _funnel_street_matches(expected_address, street)
        and city == expected_city
        and state == expected_state
        and zip_code == expected_zip
    )


def _strict_published_funnel_rows(
    rows: list[dict[str, Any]],
    native_property_id: str,
    expected_count: int,
) -> bool:
    """Require a complete, unique, native, positive-rent property roster."""
    if not rows or len(rows) != expected_count:
        return False
    unit_numbers: list[str] = []
    listing_ids: list[str] = []
    for row in rows:
        unit_number = str(row.get("unit_number") or "").strip()
        source_ids = row.get("source_ids") or {}
        listing_id = str(source_ids.get("funnel_listing_id") or "").strip()
        rent = row.get("market_rent_low")
        if (
            not unit_number
            or not listing_id
            or str(row.get("source_property_id") or "") != native_property_id
            or not isinstance(rent, (int, float))
            or isinstance(rent, bool)
            or rent <= 0
        ):
            return False
        unit_numbers.append(unit_number.casefold())
        listing_ids.append(listing_id)
    return bool(
        len(unit_numbers) == len(set(unit_numbers))
        and len(listing_ids) == len(set(listing_ids))
    )


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

        # Current direct Nestio integration (Dermot and similar): the exact
        # property page publishes a public ``listings/all`` URL in its own
        # availability script.  No XHR is guaranteed to be captured because
        # production may hand the adapter an archived/static body.  Follow
        # only that exact one-property URL and require the payload community
        # to match all canonical identity fields before admitting any row.
        fetch_result = getattr(ctx, "fetch_result", None)
        page_body = getattr(fetch_result, "body", "") if fetch_result is not None else ""
        if isinstance(page_body, bytes):
            page_body = page_body.decode("utf-8", errors="replace")
        published = _published_nestio_listings_url(str(page_body or ""))
        if published is not None:
            published_url, native_property_id = published
            try:
                from ma_poc.pms.adapters._probe import probe_get

                response = await asyncio.to_thread(
                    probe_get,
                    published_url,
                    timeout=20,
                    unlocker=False,
                    proxies={},
                    verify=True,
                    retries=1,
                )
                status = int(getattr(response, "status_code", 0) or 0)
                payload: Any = None
                if status == 200:
                    payload = json.loads(str(getattr(response, "text", "") or ""))
                if (
                    status == 200
                    and _funnel_current_items_match_context(
                        payload,
                        native_property_id,
                        ctx,
                    )
                ):
                    parsed = parse_funnel_listings(payload, published_url)
                    expected_count = len(payload.get("items") or [])
                    if _strict_published_funnel_rows(
                        parsed,
                        native_property_id,
                        expected_count,
                    ):
                        from ma_poc.extraction.post_process import post_process

                        processed = post_process(
                            parsed,
                            property_id=getattr(ctx, "property_id", None),
                        )
                        admitted = [
                            row
                            for row in processed.admitted
                            if isinstance(row, dict)
                        ]
                        if _strict_published_funnel_rows(
                            admitted,
                            native_property_id,
                            expected_count,
                        ):
                            result.units = admitted
                            result.plan_summaries = processed.plan_summaries
                            result.api_responses.append(
                                {
                                    "url": published_url,
                                    "status": status,
                                    "body": payload,
                                    "via": "published_nestio_listings_direct",
                                }
                            )
                            result.winning_url = published_url
                            result.tier_used = _TIER_PUBLISHED_LISTINGS
                            result.confidence = min(
                                0.97,
                                0.84 + 0.01 * len(admitted),
                            )
                            return result
                    result.errors.append(
                        "FUNNEL_PUBLISHED_LISTINGS_STRICT_REJECTED: "
                        "native IDs/rents/completeness failed"
                    )
                elif status == 200:
                    result.errors.append(
                        "FUNNEL_PUBLISHED_LISTINGS_BOUNDARY_REJECTED: "
                        "payload community does not match canonical property"
                    )
                else:
                    result.errors.append(
                        f"FUNNEL_PUBLISHED_LISTINGS_HTTP_{status or 'ERROR'}"
                    )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                result.errors.append(
                    "FUNNEL_PUBLISHED_LISTINGS_PARSE_ERROR: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
                )
            except Exception as exc:
                result.errors.append(
                    "FUNNEL_PUBLISHED_LISTINGS_FETCH_ERROR: "
                    f"{type(exc).__name__}: {str(exc)[:100]}"
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
        # Current page or one unique operator-authored Spaces inventory route.
        # The helper uses direct HTTP only; it cannot enter proxy, unlocker,
        # browser/fingerprint or CAPTCHA paths.
        _sp_units = await recover_funnel_spaces(
            ctx,
            html_override=_sp_html,
            source_url_override=_src,
        )
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
