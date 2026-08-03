"""Strict same-origin Brookfield RentCafe unit-roster recovery.

Brookfield's public property pages expose two WordPress middleware routes:

* ``getFloorplans`` returns aggregate floor-plan rows.  Its ``floorplanId``
  and ``availableUnitsCount`` are not apartment identity.
* ``getUnits`` returns the available-apartment roster, including a unique
  ``apartmentId`` and ``apartmentName`` for every row.

This module only reaches ``getUnits`` when the requested page is the exact
Brookfield host and its marketing HTML contains a property object whose name
*and* street address match the canonical input row.  The request is direct,
same-origin, bounded, and does not inherit proxy or browser configuration.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin, urlsplit

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    make_unit_dict,
    money_to_int,
)

if TYPE_CHECKING:
    from ma_poc.pms.adapters.base import AdapterContext


_BROOKFIELD_HOST = "rent.brookfieldproperties.com"
_GET_UNITS_PATH = "/wp-json/middleware/v1/getUnits"
_MAX_CONTEXT_BODY_BYTES = 3_000_000
_MAX_RESPONSE_BYTES = 3_000_000
_MAX_ROWS = 500
_MAX_REDIRECTS = 2
_REQUEST_TIMEOUT_SECONDS = 15.0

# The operator repeats this compact JSON object in its property-page HTML.
# Restricting the match through ``state`` keeps the regex from spanning across
# neighbouring entries in the page-wide property catalogue.
_PROPERTY_OBJECT_RE = re.compile(
    r'\{"propertyId":"(?P<property_id>\d{3,10})",'
    r'"parentId":"(?P<parent_id>\d{3,10})",'
    r'"propertyName":"(?P<name>(?:[^"\\]|\\.){1,160})",'
    r'"address":"(?P<address>(?:[^"\\]|\\.){1,200})",'
    r'"city":"(?P<city>(?:[^"\\]|\\.){1,120})",'
    r'"state":"(?P<state>[A-Za-z]{2})"'
)


@dataclass(frozen=True)
class BrookfieldBinding:
    property_id: str
    property_name: str
    address: str
    city: str
    state: str


def _context_html(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        if len(body) > _MAX_CONTEXT_BODY_BYTES:
            return ""
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str) and len(body.encode("utf-8")) <= _MAX_CONTEXT_BODY_BYTES:
        return body
    return ""


def _json_string(raw: str) -> str:
    """Decode one captured JSON-string body without accepting structures."""
    try:
        decoded = json.loads(f'"{raw}"')
    except (json.JSONDecodeError, TypeError):
        return ""
    return html_lib.unescape(decoded) if isinstance(decoded, str) else ""


def _plain_key(value: object) -> str:
    text = html_lib.unescape(str(value or "")).casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", text)


def _origin_from_ctx(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    candidate = str(getattr(fetch_result, "final_url", "") or "")
    if not candidate:
        candidate = str(getattr(ctx, "base_url", "") or "")
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https" or (parts.hostname or "").lower() != _BROOKFIELD_HOST:
        return ""
    if parts.username or parts.password or parts.port not in (None, 443):
        return ""
    return f"https://{_BROOKFIELD_HOST}"


def find_brookfield_binding(ctx: AdapterContext) -> BrookfieldBinding | None:
    """Return the one property object bound to the canonical name/address.

    Fail closed when either canonical field is absent, when no exact-normalized
    object matches, or when multiple property IDs claim the same scope.
    """
    if not _origin_from_ctx(ctx):
        return None
    expected_name = _plain_key(getattr(ctx, "property_name", ""))
    expected_address = _plain_key(getattr(ctx, "address", ""))
    if not expected_name or not expected_address:
        return None

    bindings: dict[str, BrookfieldBinding] = {}
    for match in _PROPERTY_OBJECT_RE.finditer(_context_html(ctx)):
        property_id = match.group("property_id")
        if property_id != match.group("parent_id"):
            continue
        name = _json_string(match.group("name"))
        address = _json_string(match.group("address"))
        if _plain_key(name) != expected_name or _plain_key(address) != expected_address:
            continue
        bindings[property_id] = BrookfieldBinding(
            property_id=property_id,
            property_name=name,
            address=address,
            city=_json_string(match.group("city")),
            state=match.group("state").upper(),
        )
    return next(iter(bindings.values())) if len(bindings) == 1 else None


def _positive_rent(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    rent = money_to_int(str(value or ""))
    return rent if rent is not None and 200 <= rent <= 50_000 else None


def _strict_unit_rows(
    payload: object,
    binding: BrookfieldBinding,
    source_url: str,
) -> list[dict[str, Any]]:
    """Validate and parse a complete Brookfield ``getUnits`` response."""
    if not isinstance(payload, list) or not 1 <= len(payload) <= _MAX_ROWS:
        return []

    prepared: list[tuple[dict[str, Any], str, str, int, int]] = []
    apartment_ids: set[str] = set()
    apartment_names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            return []
        if str(item.get("propertyId") or "") != binding.property_id:
            return []
        if str(item.get("parentId") or "") != binding.property_id:
            return []
        if _plain_key(item.get("propertyName")) != _plain_key(binding.property_name):
            return []

        apartment_id = str(item.get("apartmentId") or "").strip()
        apartment_name = str(item.get("apartmentName") or "").strip()
        floorplan_id = str(item.get("floorplanId") or "").strip()
        if not re.fullmatch(r"\d{1,16}", apartment_id):
            return []
        if not apartment_name or not re.search(r"\d", apartment_name) or not floorplan_id:
            return []
        id_key = apartment_id.casefold()
        name_key = apartment_name.casefold()
        if id_key in apartment_ids or name_key in apartment_names:
            return []
        apartment_ids.add(id_key)
        apartment_names.add(name_key)

        rent_low = _positive_rent(item.get("minimumRent"))
        rent_high = _positive_rent(item.get("maximumRent")) or rent_low
        if rent_low is None or rent_high is None or rent_high < rent_low:
            return []
        prepared.append((item, apartment_id, apartment_name, rent_low, rent_high))

    units: list[dict[str, Any]] = []
    for item, apartment_id, apartment_name, rent_low, rent_high in prepared:
        beds_raw = str(item.get("beds") or "").strip()
        try:
            beds_number = int(float(beds_raw))
        except ValueError:
            beds_number = None
        units.append(
            make_unit_dict(
                floor_plan_name=str(item.get("floorplanName") or "").strip(),
                bed_label=bed_label_from(beds_number, str(item.get("floorplanName") or "")),
                bedrooms=beds_raw,
                bathrooms=str(item.get("baths") or "").strip(),
                sqft=str(item.get("sqft") or "").strip(),
                unit_number=apartment_name,
                building=str(item.get("buildingNumber") or "").strip(),
                rent_range=format_rent_range(rent_low, rent_high),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_status="AVAILABLE",
                availability_date=str(item.get("availableDate") or "").strip(),
                source_api_url=source_url,
                extraction_tier="TIER_1_API_RENTCAFE_BROOKFIELD_UNITS",
                source_ids={
                    "securecafe_apartment_id": apartment_id,
                    "rentcafe_floorplan_id": str(item.get("floorplanId") or "").strip(),
                },
            )
        )
    return units


def _safe_redirect(current_url: str, location: str) -> str:
    candidate = urljoin(current_url, location)
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https" or (parts.hostname or "").lower() != _BROOKFIELD_HOST:
        return ""
    if parts.username or parts.password or parts.port not in (None, 443):
        return ""
    if parts.path.rstrip("/") != _GET_UNITS_PATH:
        return ""
    return candidate


async def _fetch_public_json(client: Any, url: str) -> tuple[int, object | None, str]:
    """Fetch one bounded JSON document with manual same-endpoint redirects."""
    current = url
    for hop in range(_MAX_REDIRECTS + 1):
        try:
            async with client.stream("GET", current) as response:
                status = int(response.status_code)
                if status in (301, 302, 303, 307, 308):
                    if hop >= _MAX_REDIRECTS:
                        return status, None, current
                    current = _safe_redirect(current, response.headers.get("location", ""))
                    if not current:
                        return status, None, ""
                    continue
                if status != 200:
                    return status, None, current
                content_length = response.headers.get("content-length", "")
                if content_length.isdigit() and int(content_length) > _MAX_RESPONSE_BYTES:
                    return status, None, current
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        return status, None, current
        except Exception:
            return 0, None, current
        try:
            return status, json.loads(body), current
        except (json.JSONDecodeError, UnicodeDecodeError):
            return status, None, current
    return 0, None, current


async def recover_brookfield_units(ctx: AdapterContext) -> tuple[list[dict[str, Any]], str]:
    """Return strict Brookfield apartment rows and their source URL."""
    binding = find_brookfield_binding(ctx)
    if binding is None:
        return [], ""

    import httpx

    query = urlencode(
        (
            ("propertyId[]", binding.property_id),
            ("has_availability", "true"),
            ("order", "ASC"),
        )
    )
    url = f"https://{_BROOKFIELD_HOST}{_GET_UNITS_PATH}?{query}"
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0),
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    ) as client:
        status, payload, final_url = await _fetch_public_json(client, url)
    if status != 200 or payload is None or not final_url:
        return [], ""
    return _strict_unit_rows(payload, binding, final_url), final_url
