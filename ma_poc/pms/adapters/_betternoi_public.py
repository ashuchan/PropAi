"""Property-scoped BetterNOI public availability recovery.

Some marketing templates publish their floor-plan/client UUID pairs inside a
server-side JavaScript HTML fragment, then call BetterNOI's public unit API
when the visitor clicks ``View Availability``.  This recovery replays only
that exact published property route and rejects mixed clients, foreign
addresses, unlisted floor plans, or duplicate native identities.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext

_UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_PUBLISHED_PAIR_RE = re.compile(
    rf"data-property\s*=\s*[\\\"']+(?P<client>{_UUID_PATTERN})[\\\"']+"
    rf".{{0,600}}?data-fpcode\s*=\s*[\\\"']+(?P<floorplan>{_UUID_PATTERN})"
    r"[\\\"']+",
    re.IGNORECASE | re.DOTALL,
)
_API_HOST = "ares.betternoi.com"
_API_PATH = "/api/pub/v1/client/building/unit"
_MAX_FLOORPLANS = 40
_MAX_PAGES = 10
_ATTEMPTED_ATTR = "_betternoi_public_attempted"


def _body_from_ctx(ctx: AdapterContext) -> str:
    fr = getattr(ctx, "fetch_result", None)
    body = getattr(fr, "body", None) if fr is not None else None
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body if isinstance(body, str) else ""


def _page_url(ctx: AdapterContext) -> str:
    fr = getattr(ctx, "fetch_result", None)
    return str((getattr(fr, "final_url", "") if fr is not None else "") or getattr(ctx, "base_url", "") or "")


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


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


def _norm_address(value: object) -> str:
    return " ".join(_ADDRESS_ALIASES.get(token, token) for token in _norm(value).split())


def _page_identity_matches(html: str, ctx: AdapterContext) -> bool:
    if not html:
        return False
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    metadata = " ".join(str(node.get("content") or "") for node in soup.select("meta[content]"))
    visible = _norm(f"{soup.get_text(' ', strip=True)} {metadata}")
    visible_words = set(visible.split())
    name_tokens = [
        token
        for token in _norm(getattr(ctx, "property_name", "")).split()
        if token
        not in {
            "apartment",
            "apartments",
            "at",
            "community",
            "homes",
            "of",
            "the",
        }
    ]
    address = _norm_address(getattr(ctx, "address", ""))
    normalized_page_address = _norm_address(f"{soup.get_text(' ', strip=True)} {metadata}")
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


def _published_pairs(html: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _PUBLISHED_PAIR_RE.finditer(html or ""):
        pair = (
            match.group("client").casefold(),
            match.group("floorplan").casefold(),
        )
        if pair not in found:
            found.append(pair)
    return found


async def _fetch_betternoi_page(url: str, referer: str) -> tuple[dict[str, Any] | None, str]:
    """Fetch one public API page directly; never use an unlocker."""
    try:
        from ma_poc.pms.adapters._probe import probe_get

        response = await asyncio.to_thread(
            probe_get,
            url,
            timeout=25,
            unlocker=False,
            retries=1,
            headers={"Referer": referer},
        )
    except Exception:
        return None, url
    final_url = str(getattr(response, "url", "") or url)
    parsed = urlparse(final_url)
    if (
        int(getattr(response, "status_code", 0) or 0) != 200
        or (parsed.hostname or "").casefold() != _API_HOST
        or parsed.path.rstrip("/") != _API_PATH
    ):
        return None, final_url
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(str(getattr(response, "text", "") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, final_url
    return (payload if isinstance(payload, dict) else None), final_url


def _positive_money(value: object) -> int | None:
    try:
        parsed = int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _same_api_page(url: str, client_id: str) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return bool(
        (parsed.hostname or "").casefold() == _API_HOST
        and parsed.path.rstrip("/") == _API_PATH
        and query.get("client_uuid") == [client_id]
    )


async def recover_betternoi_public(
    ctx: AdapterContext,
) -> list[dict[str, Any]]:
    """Return strict native units from the page-published BetterNOI client."""
    # One bounded public-API replay per scrape context.  The recovery is wired
    # both as a precise fetch-only bridge and inside the broader universal
    # recovery chain; without this guard a transient/empty response could be
    # requested twice during the same property attempt.
    if bool(getattr(ctx, _ATTEMPTED_ATTR, False)):
        return []
    html = _body_from_ctx(ctx)
    page_url = _page_url(ctx)
    if not _page_identity_matches(html, ctx):
        return []

    pairs = _published_pairs(html)
    clients = {client for client, _ in pairs}
    floorplans = {floorplan for _, floorplan in pairs}
    if not pairs or len(clients) != 1 or not floorplans or len(floorplans) > _MAX_FLOORPLANS:
        return []
    try:
        setattr(ctx, _ATTEMPTED_ATTR, True)
    except Exception:
        pass
    client_id = next(iter(clients))
    next_url = f"https://{_API_HOST}{_API_PATH}?" + urlencode(
        {"client_uuid": client_id, "is_available": "true"}
    )
    seen_pages: set[str] = set()
    raw_rows: list[tuple[dict[str, Any], str]] = []
    page_payloads: list[tuple[dict[str, Any], str, int]] = []
    for _ in range(_MAX_PAGES):
        if next_url in seen_pages or not _same_api_page(next_url, client_id):
            return []
        seen_pages.add(next_url)
        payload, final_url = await _fetch_betternoi_page(next_url, page_url)
        if payload is None:
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        page_payloads.append((payload, final_url, len(results)))
        raw_rows.extend((raw, final_url) for raw in results if isinstance(raw, dict))
        following = payload.get("next")
        if not following:
            break
        next_url = urljoin(final_url, str(following))
    else:
        return []
    if not raw_rows:
        return []

    canonical_address = _norm_address(getattr(ctx, "address", ""))
    canonical_city = _norm(getattr(ctx, "city", ""))
    canonical_state = _norm(getattr(ctx, "state", ""))
    canonical_zip = str(getattr(ctx, "zip_code", "") or "").strip()
    from bs4 import BeautifulSoup

    page_identity_text = _norm(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    page_identity_words = set(page_identity_text.split())
    units: list[dict[str, Any]] = []
    native_ids: list[str] = []
    unit_numbers: list[str] = []
    for raw, source_url in raw_rows:
        raw_client = str(raw.get("client_uuid") or "").strip().casefold()
        floorplan = raw.get("floor_plan")
        if not isinstance(floorplan, dict):
            return []
        floorplan_id = str(floorplan.get("uuid") or "").strip().casefold()
        provider_zip = str(raw.get("building_postal_code") or "").strip()
        # Any foreign property/address row is contamination, not a row to
        # filter quietly.  Reject the entire response.
        if (
            raw_client != client_id
            or floorplan_id not in floorplans
            or _norm_address(raw.get("building_address")) != canonical_address
            or _norm(raw.get("building_city")) != canonical_city
            or _norm(raw.get("building_state")) != canonical_state
            # Some historical config ZIPs are stale (Vista Pointe: configured
            # and page publish 31204 while the exact client API publishes
            # 31210). Exact street/city/state + the page-published client and
            # floor-plan UUIDs remain the property boundary; tolerate only a
            # same-USPS-prefix discrepancy, never an arbitrary foreign ZIP.
            or not provider_zip
            or (
                provider_zip not in page_identity_words
                and (len(provider_zip) < 3 or len(canonical_zip) < 3 or provider_zip[:3] != canonical_zip[:3])
            )
        ):
            return []
        if raw.get("unit_to_skip") is True:
            continue
        native_id = str(raw.get("uuid") or "").strip()
        unit_number = str(raw.get("unit_number") or raw.get("unit_identifier") or "").strip()
        rent_low = _positive_money(
            raw.get("min_rent") or raw.get("min_effective_rent") or raw.get("display_rent")
        )
        rent_high = _positive_money(raw.get("max_rent") or raw.get("max_effective_rent")) or rent_low
        if not native_id or not unit_number or rent_low is None:
            continue
        floor_plan_name = str(floorplan.get("name") or "").strip()
        bedrooms = str(raw.get("bedroom_count") or "").strip()
        bathrooms = str(raw.get("bathroom_count") or "").strip()
        sqft = str(raw.get("min_square_feet") or raw.get("max_square_feet") or "").strip()
        if not (bedrooms or bathrooms or sqft):
            continue
        native_ids.append(native_id.casefold())
        unit_numbers.append(unit_number.casefold())
        units.append(
            make_unit_dict(
                floor_plan_name=floor_plan_name,
                bed_label=bed_label_from(bedrooms, floor_plan_name),
                bedrooms=bedrooms,
                bathrooms=bathrooms,
                sqft=sqft,
                unit_number=unit_number,
                rent_range=format_rent_range(rent_low, rent_high),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_status=str(raw.get("availability_status") or "AVAILABLE").upper(),
                availability_date=str(raw.get("adjusted_available_date") or ""),
                source_api_url=source_url,
                extraction_tier="TIER_1_PUBLIC_BETTERNOI_API",
                source_ids={
                    "betternoi_unit_uuid": native_id,
                    "betternoi_unit_id": str(raw.get("id") or ""),
                    "property_id": client_id,
                    "floor_plan_id": floorplan_id,
                },
            )
        )
        units[-1]["source_property_id"] = client_id
        units[-1]["source_property_name"] = str(getattr(ctx, "property_name", "") or "")
        units[-1]["source_property_provenance"] = "exact_property_page_published_betternoi_client"
        units[-1]["source_portal_url"] = page_url

    if not units or len(native_ids) != len(set(native_ids)) or len(unit_numbers) != len(set(unit_numbers)):
        return []
    from collections import Counter

    from ma_poc.pms.source_provenance import (
        build_unit_source_provenance,
        record_context_unit_source_provenance,
    )

    admitted_by_url = Counter(str(unit.get("source_api_url") or "") for unit in units)
    for payload, source_url, source_count in page_payloads:
        admitted_count = admitted_by_url.get(source_url, 0)
        if admitted_count <= 0:
            continue
        record_context_unit_source_provenance(
            ctx,
            build_unit_source_provenance(
                provider="betternoi",
                source_url=source_url,
                body=payload,
                unit_count=admitted_count,
                identity={
                    "status": "MATCH",
                    "evidence": [
                        "configured_property_identity",
                        "page_published_client_uuid",
                        "page_published_floor_plan_uuids",
                        "street_city_state_boundary",
                    ],
                    "configured_property_id": str(ctx.property_id or ""),
                    "betternoi_client_uuid": client_id,
                    "published_floor_plan_count": len(floorplans),
                    "source_count": source_count,
                    "admitted_count": admitted_count,
                },
                response_kind="available_unit_roster_page",
            ),
        )
    return units


__all__ = ["recover_betternoi_public"]
