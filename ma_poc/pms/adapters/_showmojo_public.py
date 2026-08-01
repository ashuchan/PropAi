"""Fail-closed ShowMojo recovery through an official manager chain.

This is deliberately not a generic ShowMojo portfolio scraper.  It runs only
when the configured property's own page proves all of the following:

* exact configured name/address/city/state/ZIP identity;
* an explicit ``Managed by ...`` link to one external manager;
* one property-published RHR application site id;
* a reciprocal property link on that manager's site;
* one same-manager ``All Properties`` page with one ShowMojo iframe/account.

The ShowMojo account can contain many unrelated properties, so every admitted
row must independently match the configured property name and exact
city/state/ZIP.  Native UID, provider detail links, positive rent, explicit
availability, and the first-party RHR application id must also agree.  All
requests are ordinary direct GETs: no proxy, unlocker, render, CAPTCHA solver,
fingerprint rotation, or LLM is used.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters.base import AdapterContext

_SHOWMOJO_HOST = "showmojo.com"
_RHRIS_HOST = "rhris.com"
_SHOWMOJO_ID_RE = re.compile(r"^[0-9a-f]{10}$", re.IGNORECASE)
_SHOWMOJO_IFRAME_PATH_RE = re.compile(
    r"^/(?P<account>[0-9a-f]{10})/listings/mapsearch/?$",
    re.IGNORECASE,
)
_MAX_PAGES = 5
_MAX_CARDS_PER_PAGE = 50
_MAX_HTML_BYTES = 2_000_000
_ATTEMPTED_ATTR = "_showmojo_public_attempted"
_TELEMETRY_ATTR = "_showmojo_official_chain"

_NAME_STOPWORDS = frozenset(
    {
        "apartment",
        "apartments",
        "at",
        "community",
        "homes",
        "of",
        "the",
    }
)
_MANAGER_STOPWORDS = frozenset(
    {
        "and",
        "company",
        "group",
        "management",
        "managed",
        "properties",
        "property",
        "realty",
        "residential",
    }
)
_ADDRESS_ALIASES = {
    "avenue": "ave",
    "boulevard": "blvd",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
}


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _norm_address(value: object) -> str:
    return " ".join(
        _ADDRESS_ALIASES.get(token, token) for token in _norm(value).split()
    )


def _host(value: str) -> str:
    host = (urlparse(value).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _canonical_http_url(base_url: str, value: object) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    try:
        parsed = urlparse(urljoin(base_url, raw))
    except Exception:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


def _body_from_ctx(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None) if fetch_result is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body if isinstance(body, str) else ""


def _page_url(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    final_url = (
        getattr(fetch_result, "final_url", "") if fetch_result is not None else ""
    )
    return str(final_url or getattr(ctx, "base_url", "") or "")


def _page_identity_matches(html: str, ctx: AdapterContext) -> bool:
    """Require the configured property's full identity in first-party HTML."""
    if not html:
        return False
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    metadata = " ".join(
        str(node.get("content") or "") for node in soup.select("meta[content]")
    )
    visible_raw = f"{soup.get_text(' ', strip=True)} {metadata}"
    visible = _norm(visible_raw)
    visible_words = set(visible.split())
    name_tokens = [
        token
        for token in _norm(getattr(ctx, "property_name", "")).split()
        if token not in _NAME_STOPWORDS
    ]
    address = _norm_address(getattr(ctx, "address", ""))
    normalized_page_address = _norm_address(visible_raw)
    city = _norm(getattr(ctx, "city", ""))
    state = _norm(getattr(ctx, "state", ""))
    zip_code = str(getattr(ctx, "zip_code", "") or "").strip()
    return bool(
        name_tokens
        and all(token in visible_words for token in name_tokens)
        and address
        and f" {address} " in f" {normalized_page_address} "
        and city
        and f" {city} " in f" {visible} "
        and state
        and state in visible_words
        and zip_code
        and zip_code in visible_words
    )


def _application_site_ids(html: str, page_url: str) -> set[str]:
    """RHR site ids explicitly linked by the configured property page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    found: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = _canonical_http_url(page_url, anchor.get("href"))
        parsed = urlparse(url)
        if (
            _host(url) != _RHRIS_HOST
            or parsed.path.casefold() != "/applynowrhr/applynowrhr.cfm"
        ):
            continue
        query = parse_qs(parsed.query)
        values = next(
            (value for key, value in query.items() if key.casefold() == "siteid"),
            [],
        )
        for value in values:
            site_id = str(value or "").strip()
            if re.fullmatch(r"[A-Za-z0-9-]{3,64}", site_id):
                found.add(site_id)
    return found


def _anchor_context(anchor: Any) -> str:
    pieces = [
        str(anchor.get("href") or ""),
        str(anchor.get("title") or ""),
        anchor.get_text(" ", strip=True),
    ]
    for image in anchor.select("img"):
        pieces.extend(
            [str(image.get("alt") or ""), str(image.get("title") or "")]
        )
    parent = getattr(anchor, "parent", None)
    if parent is not None:
        pieces.extend(
            [str(parent.get("title") or ""), str(parent.get("aria-label") or "")]
        )
    return _norm(" ".join(pieces))


def _managed_by_links(html: str, page_url: str) -> list[str]:
    """Return external links immediately associated with ``Managed by``."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    page_host = _host(page_url)
    found: list[str] = []
    for text_node in soup.find_all(string=re.compile(r"\bmanaged\s+by\b", re.I)):
        match = re.search(r"\bmanaged\s+by\s+(.+)", str(text_node), re.I)
        if not match:
            continue
        manager_tokens = [
            token
            for token in _norm(match.group(1)).split()
            if token not in _MANAGER_STOPWORDS
        ]
        if not manager_tokens:
            continue

        block = text_node.parent
        while block is not None and getattr(block, "name", "") not in {
            "div",
            "footer",
            "li",
            "section",
        }:
            block = block.parent
        if block is None:
            continue
        scopes = [block, *list(block.find_next_siblings(limit=3))]
        for scope in scopes:
            for anchor in scope.select("a[href]"):
                url = _canonical_http_url(page_url, anchor.get("href"))
                if not url or _host(url) in {"", page_host}:
                    continue
                context = _anchor_context(anchor).replace(" ", "")
                if not all(token.replace(" ", "") in context for token in manager_tokens):
                    continue
                if url not in found:
                    found.append(url)
    return found


def _property_link_matches(anchor: Any, configured_url: str, property_name: str) -> bool:
    url = _canonical_http_url(configured_url, anchor.get("href"))
    if not url or _host(url) != _host(configured_url):
        return False
    context_words = set(_norm(_anchor_context(anchor)).split())
    name_tokens = [
        token
        for token in _norm(property_name).split()
        if token not in _NAME_STOPWORDS
    ]
    return bool(name_tokens and all(token in context_words for token in name_tokens))


def _all_properties_url(
    manager_html: str,
    manager_url: str,
    configured_url: str,
    property_name: str,
) -> str:
    """Require a reciprocal property link and one manager-wide roster URL."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(manager_html or "", "lxml")
    anchors = soup.select("a[href]")
    if not any(
        _property_link_matches(anchor, configured_url, property_name)
        for anchor in anchors
    ):
        return ""
    manager_host = _host(manager_url)
    candidates: list[str] = []
    for anchor in anchors:
        if _norm(anchor.get_text(" ", strip=True)) != "all properties":
            continue
        url = _canonical_http_url(manager_url, anchor.get("href"))
        if url and _host(url) == manager_host and url not in candidates:
            candidates.append(url)
    return candidates[0] if len(candidates) == 1 else ""


def _showmojo_embed(
    listings_html: str,
    listings_url: str,
    configured_url: str,
    property_name: str,
) -> tuple[str, str]:
    """Require one reciprocal manager page and one exact ShowMojo iframe."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(listings_html or "", "lxml")
    if not any(
        _property_link_matches(anchor, configured_url, property_name)
        for anchor in soup.select("a[href]")
    ):
        return "", ""
    candidates: list[tuple[str, str]] = []
    for iframe in soup.select("iframe[src]"):
        url = _canonical_http_url(listings_url, iframe.get("src"))
        parsed = urlparse(url)
        match = _SHOWMOJO_IFRAME_PATH_RE.fullmatch(parsed.path or "")
        if (
            not url
            or parsed.scheme.casefold() != "https"
            or _host(url) != _SHOWMOJO_HOST
            or parsed.query
            or match is None
        ):
            continue
        account = match.group("account").casefold()
        canonical = f"https://{_SHOWMOJO_HOST}/{account}/listings/mapsearch"
        pair = (canonical, account)
        if pair not in candidates:
            candidates.append(pair)
    return candidates[0] if len(candidates) == 1 else ("", "")


async def _fetch_direct_html(url: str, referer: str) -> tuple[str, str] | None:
    """Fetch one bounded HTML page directly; never use a proxy/unlocker."""
    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = await asyncio.to_thread(
            probe_get,
            url,
            timeout=25,
            unlocker=False,
            retries=0,
            proxies={},
            headers={"Referer": referer},
        )
    except Exception:
        return None
    status = int(getattr(response, "status_code", 0) or 0)
    final_url = str(getattr(response, "url", "") or url)
    if status != 200:
        return None
    body = getattr(response, "content", None)
    if isinstance(body, bytes):
        if not body or len(body) > _MAX_HTML_BYTES:
            return None
        raw = body
        html = body.decode("utf-8", "replace")
    else:
        html = str(getattr(response, "text", "") or "")
        raw = html.encode("utf-8", "replace")
        if not raw or len(raw) > _MAX_HTML_BYTES:
            return None
    try:
        from ma_poc.fetch.captcha_detect import looks_like_captcha

        if looks_like_captcha(raw)[0]:
            return None
    except Exception:
        pass
    return html, final_url


def _money(value: object) -> int | None:
    match = re.search(r"\$\s*([0-9][0-9,]*)", str(value or ""))
    if not match:
        return None
    parsed = int(match.group(1).replace(",", ""))
    return parsed if parsed > 0 else None


def _number(value: object) -> str:
    match = re.search(r"\b(\d+(?:\.\d+)?)\b", str(value or ""))
    return match.group(1) if match else ""


def _sqft(value: object) -> str:
    match = re.search(r"\b([0-9][0-9,]*)\s+SF\b", str(value or ""), re.I)
    return match.group(1).replace(",", "") if match else ""


def _site_id_from_url(value: str) -> str:
    parsed = urlparse(value)
    if _host(value) != _RHRIS_HOST:
        return ""
    query = parse_qs(parsed.query)
    values = next(
        (items for key, items in query.items() if key.casefold() == "siteid"),
        [],
    )
    return str(values[0] or "").strip() if values else ""


def _detail_uid_from_url(value: str) -> str:
    if _host(value) != _SHOWMOJO_HOST:
        return ""
    match = re.match(r"^/l/([0-9a-f]{10})(?:/|$)", urlparse(value).path, re.I)
    return match.group(1).casefold() if match else ""


def _parse_card(
    card: Any,
    *,
    source_url: str,
    application_site_id: str,
    property_name: str,
    city: str,
    state: str,
    zip_code: str,
) -> tuple[dict[str, Any], list[str]]:
    """Parse one mixed-roster card and return its explicit rejection reasons."""
    uid = str(card.get("data-listing-uid") or "").strip().casefold()
    address_nodes = card.select(".address p")
    address = (
        address_nodes[0].get_text(" ", strip=True) if address_nodes else ""
    )
    city_state_zip = (
        address_nodes[1].get_text(" ", strip=True)
        if len(address_nodes) > 1
        else ""
    )
    highlights_node = card.select_one(".listing_highlights")
    highlights = (
        highlights_node.get_text(" ", strip=True) if highlights_node else ""
    )
    rent_node = card.select_one("li.rent")
    rent_text = rent_node.get_text(" ", strip=True) if rent_node else ""
    rent = _money(rent_text)
    options = [
        node.get_text(" ", strip=True) for node in card.select("ul.options > li")
    ]
    availability_text = options[0] if options else ""
    bedrooms_node = card.select_one("ul.price_rooms li.br")
    bathrooms_node = card.select_one("ul.price_rooms li.ba")
    bedrooms_text = (
        bedrooms_node.get_text(" ", strip=True) if bedrooms_node else ""
    )
    bathrooms_text = (
        bathrooms_node.get_text(" ", strip=True) if bathrooms_node else ""
    )
    sqft_text = ""
    for node in card.select("ul.price_rooms > li"):
        candidate = node.get_text(" ", strip=True)
        if re.fullmatch(r"[0-9,]+\s+SF", candidate, re.I):
            sqft_text = candidate
            break

    detail_links: list[str] = []
    for anchor in card.select("a.schedule-a-showing[href]"):
        url = _canonical_http_url(source_url, anchor.get("href"))
        if url and url not in detail_links:
            detail_links.append(url)
    slug_detail_links = [
        url
        for url in detail_links
        if _detail_uid_from_url(url) == uid
        and len([part for part in urlparse(url).path.split("/") if part]) >= 3
    ]
    short_detail_links = [
        url
        for url in detail_links
        if _detail_uid_from_url(url) == uid
        and len([part for part in urlparse(url).path.split("/") if part]) == 2
    ]
    detail_url = slug_detail_links[0] if len(slug_detail_links) == 1 else ""

    promo = card.select_one("[data-recheck-url]")
    promo_url = _canonical_http_url(
        source_url, promo.get("data-recheck-url") if promo is not None else ""
    )
    promo_uid = ""
    if _host(promo_url) == _SHOWMOJO_HOST:
        promo_uid = str(parse_qs(urlparse(promo_url).query).get("uid", [""])[0])
        promo_uid = promo_uid.casefold()

    apply = card.select_one("a.apply_btn[href]")
    apply_url = _canonical_http_url(
        source_url, apply.get("href") if apply is not None else ""
    )
    reasons: list[str] = []
    if not _SHOWMOJO_ID_RE.fullmatch(uid):
        reasons.append("invalid_native_uid")
    if str(card.get("id") or "").casefold() != f"uid_{uid}":
        reasons.append("card_id_uid_mismatch")
    if len(slug_detail_links) != 1:
        reasons.append("slug_detail_uid_mismatch")
    if len(short_detail_links) != 1:
        reasons.append("short_detail_uid_mismatch")
    if promo_uid != uid:
        reasons.append("promo_uid_mismatch")
    if _norm(property_name) not in _norm(highlights):
        reasons.append("canonical_property_name_absent")
    expected_city_state_zip = _norm(f"{city}, {state} {zip_code}")
    if _norm(city_state_zip) != expected_city_state_zip:
        reasons.append("canonical_city_state_zip_mismatch")
    if not address or not re.search(r"\d", address):
        reasons.append("native_address_absent")
    if rent is None:
        reasons.append("no_positive_rent")
    if not re.match(r"^available\b", availability_text, re.I):
        reasons.append("no_explicit_provider_availability")
    if _site_id_from_url(apply_url) != application_site_id:
        reasons.append("application_site_id_mismatch")
    if not bedrooms_text or not bathrooms_text or not sqft_text:
        reasons.append("unit_dimensions_absent")

    return (
        {
            "provider_listing_uid": uid,
            "provider_unit_address": address,
            "city_state_zip": city_state_zip,
            "rent": rent,
            "rent_text": rent_text,
            "availability_text": availability_text,
            "bedrooms_text": bedrooms_text,
            "bathrooms_text": bathrooms_text,
            "sqft_text": sqft_text,
            "detail_url": detail_url,
            "apply_url": apply_url,
            "highlights": highlights,
            "source_url": source_url,
        },
        reasons,
    )


def _set_telemetry(ctx: AdapterContext, **values: Any) -> None:
    try:
        current = getattr(ctx, _TELEMETRY_ATTR, None)
        payload = dict(current) if isinstance(current, dict) else {}
        payload.update(values)
        setattr(ctx, _TELEMETRY_ATTR, payload)
    except Exception:
        pass


async def recover_showmojo_public(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Return property-scoped native rows from an official ShowMojo chain."""
    if bool(getattr(ctx, _ATTEMPTED_ATTR, False)):
        return []
    html = _body_from_ctx(ctx)
    configured_url = _page_url(ctx)
    if not _page_identity_matches(html, ctx):
        return []
    application_site_ids = _application_site_ids(html, configured_url)
    manager_links = _managed_by_links(html, configured_url)
    manager_hosts = {_host(url) for url in manager_links}
    if (
        len(application_site_ids) != 1
        or len(manager_links) != 1
        or len(manager_hosts) != 1
    ):
        return []

    application_site_id = next(iter(application_site_ids))
    manager_url = manager_links[0]
    try:
        setattr(ctx, _ATTEMPTED_ATTR, True)
    except Exception:
        pass
    _set_telemetry(
        ctx,
        attempted=True,
        configured_url=configured_url,
        manager_url=manager_url,
        application_site_id=application_site_id,
    )

    manager_response = await _fetch_direct_html(manager_url, configured_url)
    if manager_response is None:
        _set_telemetry(ctx, failure_reason="manager_fetch_failed")
        return []
    manager_html, manager_final_url = manager_response
    if _host(manager_final_url) != _host(manager_url):
        _set_telemetry(ctx, failure_reason="manager_host_redirect_mismatch")
        return []
    listings_url = _all_properties_url(
        manager_html,
        manager_final_url,
        configured_url,
        str(getattr(ctx, "property_name", "") or ""),
    )
    if not listings_url:
        _set_telemetry(ctx, failure_reason="manager_reciprocal_or_roster_missing")
        return []

    listings_response = await _fetch_direct_html(listings_url, manager_final_url)
    if listings_response is None:
        _set_telemetry(
            ctx,
            manager_listings_url=listings_url,
            failure_reason="manager_listings_fetch_failed",
        )
        return []
    listings_html, listings_final_url = listings_response
    if _host(listings_final_url) != _host(manager_final_url):
        _set_telemetry(ctx, failure_reason="manager_listings_host_mismatch")
        return []
    embed_url, account = _showmojo_embed(
        listings_html,
        listings_final_url,
        configured_url,
        str(getattr(ctx, "property_name", "") or ""),
    )
    if not embed_url or not _SHOWMOJO_ID_RE.fullmatch(account):
        _set_telemetry(ctx, failure_reason="showmojo_iframe_boundary_failed")
        return []

    seen_uids: set[str] = set()
    parsed_rows: list[tuple[dict[str, Any], list[str]]] = []
    page_telemetry: list[dict[str, Any]] = []
    terminated = False
    for page_number in range(1, _MAX_PAGES + 1):
        page_url = f"{embed_url}?{urlencode({'page': page_number})}"
        roster_response = await _fetch_direct_html(page_url, listings_final_url)
        if roster_response is None:
            _set_telemetry(
                ctx,
                manager_listings_url=listings_final_url,
                showmojo_embed_url=embed_url,
                showmojo_account=account,
                pages=page_telemetry,
                failure_reason="showmojo_roster_fetch_failed",
            )
            return []
        roster_html, final_page_url = roster_response
        parsed_final = urlparse(final_page_url)
        if (
            _host(final_page_url) != _SHOWMOJO_HOST
            or parsed_final.path.rstrip("/")
            != f"/{account}/listings/mapsearch"
        ):
            _set_telemetry(ctx, failure_reason="showmojo_roster_redirect_mismatch")
            return []
        from bs4 import BeautifulSoup

        roster_soup = BeautifulSoup(roster_html, "lxml")
        cards = roster_soup.select("div.cnt_box[data-listing-uid]")
        page_telemetry.append(
            {"page": page_number, "url": page_url, "cards": len(cards)}
        )
        if len(cards) > _MAX_CARDS_PER_PAGE:
            _set_telemetry(ctx, failure_reason="showmojo_page_card_cap_exceeded")
            return []
        if not cards:
            terminated = True
            break
        for card in cards:
            row, reasons = _parse_card(
                card,
                source_url=final_page_url,
                application_site_id=application_site_id,
                property_name=str(getattr(ctx, "property_name", "") or ""),
                city=str(getattr(ctx, "city", "") or ""),
                state=str(getattr(ctx, "state", "") or ""),
                zip_code=str(getattr(ctx, "zip_code", "") or ""),
            )
            uid = str(row.get("provider_listing_uid") or "")
            if uid in seen_uids:
                _set_telemetry(ctx, failure_reason="duplicate_showmojo_uid")
                return []
            seen_uids.add(uid)
            parsed_rows.append((row, reasons))
    if not terminated:
        _set_telemetry(ctx, failure_reason="showmojo_pagination_not_bounded")
        return []

    accepted = [row for row, reasons in parsed_rows if not reasons]
    rejected = [
        {
            "provider_listing_uid": row.get("provider_listing_uid") or "",
            "provider_unit_address": row.get("provider_unit_address") or "",
            "city_state_zip": row.get("city_state_zip") or "",
            "reasons": reasons,
        }
        for row, reasons in parsed_rows
        if reasons
    ]
    native_ids = [str(row["provider_listing_uid"]).casefold() for row in accepted]
    addresses = [_norm_address(row["provider_unit_address"]) for row in accepted]
    if (
        not accepted
        or len(native_ids) != len(set(native_ids))
        or len(addresses) != len(set(addresses))
    ):
        _set_telemetry(
            ctx,
            failure_reason="no_unique_property_scoped_rows",
            rejected_rows=rejected,
        )
        return []

    units: list[dict[str, Any]] = []
    for raw in accepted:
        rent = int(raw["rent"])
        unit = make_unit_dict(
            # ShowMojo publishes no floor-plan name.  Keep the roster URL (not
            # the slugged detail URL) as source_api_url so the shared helper
            # cannot mistake a street-address slug for a plan name.
            floor_plan_name="",
            bed_label=bed_label_from(
                _number(raw["bedrooms_text"]),
                "",
            ),
            bedrooms=_number(raw["bedrooms_text"]),
            bathrooms=_number(raw["bathrooms_text"]),
            sqft=_sqft(raw["sqft_text"]),
            unit_number=str(raw["provider_unit_address"]),
            unit_name=str(raw["provider_unit_address"]),
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date="",
            source_api_url=str(raw["source_url"]),
            extraction_tier="TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN",
            source_ids={
                "showmojo_account": account,
                "showmojo_listing_uid": str(raw["provider_listing_uid"]),
                "rhr_application_site_id": application_site_id,
            },
            data_gaps=["floor_plan_name", "availability_date"],
            data_quality_flag="SHOWMOJO_PLAN_NAME_AND_EXACT_DATE_NOT_PUBLISHED",
        )
        unit.update(
            {
                "provider_unit_address": str(raw["provider_unit_address"]),
                "availability_text": str(raw["availability_text"]),
                "availability_date_provenance": (
                    "provider_relative_text_preserved_no_year_invented"
                ),
                "floor_plan_name_provenance": (
                    "provider_roster_does_not_publish_floor_plan_name"
                ),
                "source_listing_url": str(raw["detail_url"]),
                "source_portal_url": embed_url,
                "source_manager_url": manager_final_url,
                "source_manager_listings_url": listings_final_url,
                "source_property_name": str(
                    getattr(ctx, "property_name", "") or ""
                ),
                "source_property_provenance": (
                    "exact_configured_identity_managed_by_reciprocal_manager_"
                    "showmojo_iframe_name_city_state_zip_filter"
                ),
            }
        )
        units.append(unit)

    _set_telemetry(
        ctx,
        manager_url=manager_final_url,
        manager_listings_url=listings_final_url,
        showmojo_embed_url=embed_url,
        showmojo_account=account,
        pages=page_telemetry,
        portfolio_rows=len(parsed_rows),
        accepted_rows=len(units),
        rejected_rows=rejected,
        native_listing_ids=native_ids,
        failure_reason="",
    )
    return units


__all__ = ["recover_showmojo_public"]
