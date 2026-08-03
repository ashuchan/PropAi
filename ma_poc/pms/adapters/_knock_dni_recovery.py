"""Strict recovery for Knock Doorway ``dniId`` configuration embeds.

Some operator templates declare a Knock community id in a small config object
and then initialise Doorway with variables instead of string literals::

    const config = {dniId: "0962eddf11eb03b8", dniApiKey: "..."};
    knockDoorway.init(config.dniApiKey, "community", config.dniId);

The primary Knock parser intentionally recognises literal arguments only, so
these pages are commonly routed through a marketing/generic adapter.  This
module handles only the exact variable-call shape and then uses Knock's public
JSON API.  It is deliberately isolated from ``_probe``: requests are direct,
bounded, and cannot inherit a proxy or browser fingerprint configuration.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ma_poc.pms.adapters.base import AdapterContext


_MAX_CONTEXT_BODY_BYTES = 3_000_000
_MAX_RESPONSE_BYTES = 3_000_000
_FETCH_CONCURRENCY = 4
_REQUEST_TIMEOUT_SECONDS = 15.0

_DNI_ID_RE = re.compile(
    r"\bdniId\s*:\s*(?P<quote>['\"])(?P<id>[a-f0-9]{14,18})(?P=quote)",
    re.IGNORECASE,
)
_DNI_KEY_RE = re.compile(
    r"\bdniApiKey\s*:\s*['\"][A-Za-z0-9+/=_-]{20,60}['\"]",
    re.IGNORECASE,
)
_DNI_VARIABLE_CALL_RE = re.compile(
    r"\b(?:window\.)?knockDoorway\.init\s*\(\s*"
    r"config\.dniApiKey\s*,\s*['\"]community['\"]\s*,\s*"
    r"config\.dniId\s*\)",
    re.IGNORECASE,
)

_STREET_CANONICAL_WORDS: tuple[tuple[str, str], ...] = (
    ("north", "n"),
    ("south", "s"),
    ("east", "e"),
    ("west", "w"),
    ("street", "st"),
    ("road", "rd"),
    ("drive", "dr"),
    ("avenue", "ave"),
    ("boulevard", "blvd"),
    ("circle", "cir"),
    ("court", "ct"),
    ("lane", "ln"),
    ("parkway", "pkwy"),
    ("highway", "hwy"),
    ("place", "pl"),
    ("terrace", "ter"),
)

_fetch_semaphore: asyncio.Semaphore | None = None
_fetch_semaphore_loop: asyncio.AbstractEventLoop | None = None


def find_knock_dni_community_id(html: str) -> str | None:
    """Return the community id from the exact config-variable embed shape."""
    if not html or "knockdoorway" not in html.lower():
        return None
    if not _DNI_KEY_RE.search(html) or not _DNI_VARIABLE_CALL_RE.search(html):
        return None
    match = _DNI_ID_RE.search(html)
    return match.group("id") if match else None


def _context_html(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        if len(body) > _MAX_CONTEXT_BODY_BYTES:
            return ""
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str) and len(body) <= _MAX_CONTEXT_BODY_BYTES:
        return body
    return ""


def _plain_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _street_key(value: object) -> str:
    text = str(value or "").casefold()
    for source, replacement in _STREET_CANONICAL_WORDS:
        text = re.sub(rf"\b{source}\b", replacement, text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _leading_street_number(value: str) -> str:
    match = re.match(r"\s*(\d+)", value)
    return match.group(1) if match else ""


def _within_one_edit(left: str, right: str) -> bool:
    """Return whether two compact strings differ by at most one edit."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) <= 1
    index_left = index_right = differences = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
            index_right += 1
            continue
        differences += 1
        index_right += 1
        if differences > 1:
            return False
    return True


def _location_parts(
    property_payload: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    data = property_payload.get("data")
    if not isinstance(data, dict):
        return "", "", "", "", ""
    location = data.get("location")
    if not isinstance(location, dict):
        return "", "", "", "", ""
    address = location.get("address")
    if isinstance(address, dict):
        street = str(address.get("street") or "")
        raw = str(address.get("raw") or "")
        city = str(address.get("city") or location.get("city") or "")
        state = str(address.get("state") or location.get("state") or "")
    else:
        street = str(address or "")
        raw = street
        city = str(location.get("city") or "")
        state = str(location.get("state") or "")
    name = str(location.get("name") or data.get("name") or "")
    return street, raw, city, state or "", name


def property_scope_matches(
    ctx: AdapterContext,
    property_payload: dict[str, Any],
) -> bool:
    """Fail closed unless Knock metadata identifies the requested property."""
    street, raw, observed_city, observed_state, observed_name = _location_parts(property_payload)
    expected_address = str(getattr(ctx, "address", "") or "").strip()
    expected_city = str(getattr(ctx, "city", "") or "").strip()
    expected_state = str(getattr(ctx, "state", "") or "").strip()

    if expected_address:
        expected_key = _street_key(expected_address)
        expected_number = _leading_street_number(expected_address)
        if not expected_key or not expected_number:
            return False
        address_match = False
        fuzzy_address_match = False
        for candidate in (street, raw):
            candidate_key = _street_key(candidate)
            candidate_number = _leading_street_number(candidate)
            if not candidate_key or candidate_number != expected_number:
                continue
            if (
                candidate_key == expected_key
                or candidate_key.startswith(expected_key)
                or expected_key.startswith(candidate_key)
            ):
                address_match = True
                break
            if _within_one_edit(candidate_key, expected_key):
                fuzzy_address_match = True
        if not address_match and fuzzy_address_match:
            # One-character operator/CSV spelling drift is accepted only with
            # exact city and state corroboration.  Live example: ``Springate``
            # in the CSV versus ``Spring Gate`` in Knock metadata.
            address_match = bool(
                expected_city
                and observed_city
                and expected_state
                and observed_state
                and _plain_key(expected_city) == _plain_key(observed_city)
                and _plain_key(expected_state) == _plain_key(observed_state)
            )
        if not address_match:
            return False
        if expected_city and observed_city:
            if _plain_key(expected_city) != _plain_key(observed_city):
                return False
        if expected_state and observed_state:
            if _plain_key(expected_state) != _plain_key(observed_state):
                return False
        return True

    # Address-less rows are admitted only with an exact name plus geographic
    # corroboration.  Strip the operator's optional leading code, e.g.
    # ``(HBR) The Heritage at Boca Raton``.
    expected_name = str(getattr(ctx, "property_name", "") or "").strip()
    observed_name = re.sub(r"^\s*\([^)]{1,12}\)\s*", "", observed_name)
    if not expected_name or _plain_key(expected_name) != _plain_key(observed_name):
        return False
    city_match = bool(
        expected_city and observed_city and _plain_key(expected_city) == _plain_key(observed_city)
    )
    state_match = bool(
        expected_state and observed_state and _plain_key(expected_state) == _plain_key(observed_state)
    )
    return city_match or state_match


def _semaphore() -> asyncio.Semaphore:
    """Return a four-wide semaphore bound to the active event loop."""
    global _fetch_semaphore, _fetch_semaphore_loop
    loop = asyncio.get_running_loop()
    if _fetch_semaphore is None or _fetch_semaphore_loop is not loop:
        _fetch_semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
        _fetch_semaphore_loop = loop
    return _fetch_semaphore


async def _fetch_public_json(
    client: Any,
    url: str,
) -> tuple[int, dict[str, Any] | None]:
    """Read one public JSON response without buffering beyond the byte cap."""
    try:
        async with client.stream("GET", url) as response:
            status = int(response.status_code)
            if not 200 <= status < 300:
                return status, None
            content_length = response.headers.get("content-length", "")
            if content_length.isdigit() and int(content_length) > _MAX_RESPONSE_BYTES:
                return status, None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    return status, None
    except Exception:
        return 0, None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, None
    return status, payload if isinstance(payload, dict) else None


def _strict_units(payload: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
    from ma_poc.pms.adapters.knock import parse_knock_units

    recovered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in parse_knock_units(payload):
        unit_number = str(row.get("unit_number") or "").strip()
        rent = row.get("market_rent_low")
        if not unit_number or isinstance(rent, bool) or not isinstance(rent, (int, float)):
            continue
        if rent <= 0:
            continue
        key = (
            str(row.get("building") or "").strip().casefold(),
            unit_number.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        row["source_api_url"] = source_url
        row["extraction_tier"] = "TIER_1_API_KNOCK_DNI_CONFIG"
        recovered.append(row)
    return recovered


async def recover_knock_dni_config(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Recover a same-property canonical roster from a Knock ``dniId`` embed.

    The exact HTML gate is request-free on non-matches.  A match performs at
    most two sequential requests: community metadata, then its numeric
    property's unit roster.  The second request is unreachable unless the
    community metadata matches the CSV property scope.
    """
    community_id = find_knock_dni_community_id(_context_html(ctx))
    if not community_id:
        return []

    import httpx

    community_url = f"https://doorway-api.knockrentals.com/v1/property/community/{community_id}"
    async with _semaphore():
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0),
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={
                "Accept": "application/json",
                "Origin": "https://doorway.knck.io",
            },
        ) as client:
            _, community_payload = await _fetch_public_json(client, community_url)
            if not community_payload:
                return []
            property_payload = community_payload.get("property")
            if not isinstance(property_payload, dict):
                return []
            if not property_scope_matches(ctx, property_payload):
                return []
            numeric_id = property_payload.get("id")
            numeric_text = str(numeric_id or "").strip()
            if not re.fullmatch(r"\d{1,12}", numeric_text):
                return []
            units_url = f"https://doorway-api.knockrentals.com/v1/property/{numeric_text}/units"
            _, units_payload = await _fetch_public_json(client, units_url)
    if not units_payload:
        return []
    return _strict_units(units_payload, units_url)
