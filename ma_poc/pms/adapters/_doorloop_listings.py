"""Strict recovery for property-published DoorLoop rental listings.

Some Wix marketing sites publish a normal ``Apply`` anchor to DoorLoop's
public rental-applications page.  That page loads a MITS-shaped JSON feed with
one ``PhysicalProperty.Property`` entry per available apartment, but the Wix
adapter previously stopped at ``SYNDICATION_ONLY_WIX``.

The recovery is intentionally narrow:

* the marketing HTML must publish an absolute ``*.app.doorloop.com`` company
  listing URL containing a 24-hex ``companyId``;
* the public feed is derived only from that published host/id pair;
* every emitted row must match the configured street, city, state, and ZIP;
* every row must have a native 24-hex listing id, a visible unit label, and a
  sane positive rent (the live provider sometimes publishes ``$1`` placeholder
  rows, which are rejected).

Live schema probes on 2026-08-01 covered three independent accounts: Park
Place Luxury Apartments, Inbound Properties, and Blue Spruce Property
Management.  Only Park Place is in the exact FAILED_NO_DATA cohort; the other
two establish the provider-wide envelope and placeholder behaviour.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
    money_to_int,
    rent_in_sanity_range,
)

_DOORLOOP_HOST_RE = re.compile(
    r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.app\.doorloop\.com$",
    re.IGNORECASE,
)
_DOORLOOP_LISTING_PATH = "/tenant-portal/rental-applications/listing"
_DOORLOOP_FEED_PATH_RE = re.compile(
    r"/api/units/listings/mits/json/[a-f0-9]{24}/1$", re.IGNORECASE
)
_MONGO_ID_RE = re.compile(r"^[a-f0-9]{24}$", re.IGNORECASE)
_RAW_LISTING_URL_RE = re.compile(
    r"https?://[a-z0-9][a-z0-9-]*\.app\.doorloop\.com/"
    r"tenant-portal/rental-applications/listing\?[^\s\"'<>]+",
    re.IGNORECASE,
)


def _canonical_listing_url(value: str) -> str:
    """Return an exact DoorLoop company-listing URL, or ``""``."""
    try:
        parsed = urlsplit(unescape(str(value or "")).strip())
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").casefold()
    if not _DOORLOOP_HOST_RE.fullmatch(host):
        return ""
    if parsed.path.rstrip("/") != _DOORLOOP_LISTING_PATH:
        return ""
    query = parse_qs(parsed.query, keep_blank_values=True)
    company_ids = [str(item).strip() for item in query.get("companyId", [])]
    if len(company_ids) != 1 or not _MONGO_ID_RE.fullmatch(company_ids[0]):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def extract_published_doorloop_listing_urls(body: str) -> list[str]:
    """Extract exact company-listing URLs explicitly present in ``body``."""
    if not body:
        return []
    candidates: list[str] = []
    try:
        soup = BeautifulSoup(body, "lxml")
        candidates.extend(
            str(anchor.get("href") or "") for anchor in soup.find_all("a", href=True)
        )
    except Exception:
        pass
    candidates.extend(match.group(0) for match in _RAW_LISTING_URL_RE.finditer(unescape(body)))

    out: list[str] = []
    for candidate in candidates:
        canonical = _canonical_listing_url(candidate)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def build_doorloop_feed_url(listing_url: str) -> str:
    """Derive DoorLoop's public MITS feed from a validated listing URL."""
    canonical = _canonical_listing_url(listing_url)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    host_match = _DOORLOOP_HOST_RE.fullmatch((parsed.hostname or "").casefold())
    if not host_match:
        return ""
    company_id = parse_qs(parsed.query).get("companyId", [""])[0]
    query = urlencode(
        {
            "partnerKey": "doorLoopListingSites",
            "subdomain": host_match.group("tenant"),
            "filter_rentalAppListed": "true",
            "filter_showPropertyList": "false",
        }
    )
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            f"/api/units/listings/mits/json/{company_id}/1",
            query,
            "",
        )
    )


def _one_or_many(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _clean_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _address_matches_context(address: dict[str, Any], ctx: Any) -> bool:
    """Fail-closed property boundary for a potentially multi-property feed."""
    expected_street = str(getattr(ctx, "address", "") or "").strip()
    if not expected_street:
        return False

    line1 = str(address.get("AddressLine1") or "").strip()
    city = str(address.get("City") or "").strip()
    state = str(address.get("StateCode") or address.get("State") or "").strip()
    zip_code = str(address.get("PostalCode") or "").strip()
    if not line1:
        return False

    expected_city = str(getattr(ctx, "city", "") or "").strip()
    expected_state = str(getattr(ctx, "state", "") or "").strip()
    expected_zip = str(getattr(ctx, "zip_code", "") or "").strip()
    if expected_city and city and _clean_token(expected_city) != _clean_token(city):
        return False
    if expected_state and state and _clean_token(expected_state) != _clean_token(state):
        return False
    if expected_zip and zip_code and expected_zip[:5] != zip_code[:5]:
        return False

    full_address = ", ".join(
        part for part in (line1, city, f"{state} {zip_code}".strip()) if part
    )
    try:
        from ma_poc.pms.adapters.appfolio import _address_matches

        return _address_matches(full_address, expected_street, expected_zip, 92)
    except Exception:
        return _clean_token(line1) == _clean_token(expected_street)


def _money(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        rent = int(round(float(value)))
    else:
        rent = money_to_int(str(value))
    return rent if rent_in_sanity_range(rent) else None


def _iso_date(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    try:
        year = int(value.get("_Year") or value.get("Year") or 0)
        month = int(value.get("_Month") or value.get("Month") or 0)
        day = int(value.get("_Day") or value.get("Day") or 0)
        return date(year, month, day).isoformat()
    except (TypeError, ValueError):
        return ""


def parse_doorloop_mits(
    payload: Any,
    ctx: Any,
    source_url: str,
    *,
    published_url: str = "",
) -> list[dict[str, Any]]:
    """Parse exact-property native, priced rows from a DoorLoop MITS feed."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(payload, dict):
        return []
    physical = payload.get("PhysicalProperty")
    if not isinstance(physical, dict):
        return []
    properties = _one_or_many(physical.get("Property"))
    if not properties:
        return []

    rows: list[dict[str, Any]] = []
    seen_listing_ids: set[str] = set()
    for entry in properties:
        listing_id = str(entry.get("_IDValue") or entry.get("_ListingID") or "").strip()
        if not _MONGO_ID_RE.fullmatch(listing_id) or listing_id in seen_listing_ids:
            continue
        property_info = entry.get("PropertyID")
        ils_unit = entry.get("ILS_Unit")
        if not isinstance(property_info, dict) or not isinstance(ils_unit, dict):
            continue
        addresses = _one_or_many(property_info.get("Address"))
        address = addresses[0] if addresses else {}
        if not _address_matches_context(address, ctx):
            continue

        unit_payloads = _one_or_many((ils_unit.get("Units") or {}).get("Unit"))
        if len(unit_payloads) != 1:
            continue
        unit_payload = unit_payloads[0]
        rent = _money(unit_payload.get("UnitRent")) or _money(
            unit_payload.get("MarketRent")
        )
        if rent is None:
            continue

        unit_number = str(
            property_info.get("unitName")
            or address.get("AddressLine2")
            or property_info.get("MarketingName")
            or ""
        ).strip()
        if not unit_number:
            continue

        bedrooms = str(unit_payload.get("UnitBedrooms") or "").strip()
        bathrooms = str(unit_payload.get("UnitBathrooms") or "").strip()
        sqft = str(
            unit_payload.get("MinSquareFeet")
            or unit_payload.get("MaxSquareFeet")
            or ""
        ).strip()
        try:
            bedrooms_int = int(float(bedrooms)) if bedrooms else None
        except ValueError:
            bedrooms_int = None
        floor_plan_name = str(
            unit_payload.get("FloorPlanName")
            or unit_payload.get("UnitType")
            or entry.get("FloorPlanName")
            or ""
        ).strip()
        availability = ils_unit.get("Availability") or {}
        available_date = _iso_date(availability.get("MadeReadyDate")) or _iso_date(
            availability.get("VacateDate")
        )
        source_property_id = str(property_info.get("propertyId") or "").strip()
        if not _MONGO_ID_RE.fullmatch(source_property_id):
            continue
        observed_address = ", ".join(
            part
            for part in (
                str(address.get("AddressLine1") or "").strip(),
                str(address.get("City") or "").strip(),
                (
                    f"{address.get('StateCode') or address.get('State') or ''} "
                    f"{address.get('PostalCode') or ''}"
                ).strip(),
            )
            if part
        )

        row = make_unit_dict(
            floor_plan_name=floor_plan_name,
            bed_label=bed_label_from(bedrooms_int, floor_plan_name),
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            sqft=sqft,
            unit_number=unit_number,
            unit_name=unit_number,
            building=str(address.get("AddressLine1") or "").strip(),
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=available_date,
            source_api_url=source_url,
            extraction_tier="TIER_1_API_DOORLOOP_MITS",
            source_ids={
                "doorloop_listing_id": listing_id,
                "doorloop_property_id": source_property_id,
            },
        )
        row.update(
            {
                "source_portal_url": published_url,
                "source_property_id": source_property_id,
                "source_property_name": str(
                    entry.get("_OrganizationName") or payload.get("companyName") or ""
                ).strip(),
                "source_property_address": observed_address,
                "source_property_provenance": (
                    "published_doorloop_company_link_address_bound"
                ),
            }
        )
        rows.append(row)
        seen_listing_ids.add(listing_id)
    return rows


async def recover_doorloop_listings(ctx: Any) -> list[dict[str, Any]]:
    """Recover a published DoorLoop roster without LLM or browser solving."""
    # If the rendered fetch already captured DoorLoop's feed, parse it first.
    for response in getattr(ctx, "_api_responses", []) or []:
        if not isinstance(response, dict):
            continue
        source_url = str(response.get("url") or "")
        try:
            parsed = urlsplit(source_url)
        except Exception:
            continue
        if not (
            _DOORLOOP_HOST_RE.fullmatch((parsed.hostname or "").casefold())
            and _DOORLOOP_FEED_PATH_RE.fullmatch(parsed.path)
        ):
            continue
        rows = parse_doorloop_mits(response.get("body"), ctx, source_url)
        if rows:
            return rows

    from ma_poc.pms.adapters._probe import body_html_from_ctx, probe_fetch_status

    body = body_html_from_ctx(ctx)
    published_urls = extract_published_doorloop_listing_urls(body)
    for published_url in published_urls[:3]:
        source_url = build_doorloop_feed_url(published_url)
        if not source_url:
            continue
        status, response_body = await probe_fetch_status(source_url)
        if status != 200 or not response_body:
            continue
        rows = parse_doorloop_mits(
            response_body,
            ctx,
            source_url,
            published_url=published_url,
        )
        if rows:
            return rows
    return []


__all__ = [
    "build_doorloop_feed_url",
    "extract_published_doorloop_listing_urls",
    "parse_doorloop_mits",
    "recover_doorloop_listings",
]
