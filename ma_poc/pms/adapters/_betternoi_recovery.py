"""Strict BetterNOI floor-plan-button to apartment-roster recovery.

BetterNOI property sites render one ``data-property`` client UUID and one
``data-fpcode`` UUID on each floor-plan availability button.  Their own page
JavaScript sends those exact values to the public, read-only endpoint below::

    https://ares.betternoi.com/api/pub/v1/client/building/unit/

The ordinary DOM cascade sees the surrounding floor-plan cards but never
executes that request, so the property remains plan-level even though the
response contains canonical apartment identifiers, rent, area, and dates.

This recovery is deliberately narrow and direct:

* the exact BetterNOI endpoint marker and paired UUID attributes must be in
  the property's already-fetched HTML;
* a page carrying more than one client UUID is ambiguous and fails closed;
* every response row must echo both requested UUIDs and match the CSV
  property on at least two of city/state/ZIP, with no location mismatch;
* requests use plain ``httpx`` with ``trust_env=False``.  No proxy, Web
  Unlocker, browser fingerprint rotation, CAPTCHA solver, or FlareSolverr is
  reachable from this module.

Live validation (2026-08-01): Magnolia Ridge (7 units), KRC Reserve (25), and
Chester Place (49).  Bayside Villas is the negative control: it ships the
common endpoint JavaScript but only contact-for-availability plan cards, so it
has no UUID pairs and this recovery performs zero requests.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import math
import re
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlsplit

if TYPE_CHECKING:
    from ma_poc.pms.adapters.base import AdapterContext


_API_ORIGIN = "https://ares.betternoi.com"
_API_PATH = "/api/pub/v1/client/building/unit/"
_API_URL = f"{_API_ORIGIN}{_API_PATH}"
_TIER = "TIER_1_API_BETTERNOI"

_MAX_CONTEXT_BODY_BYTES = 4_000_000
_MAX_RESPONSE_BYTES = 3_000_000
_MAX_TARGETS = 32
_MAX_PAGES_PER_TARGET = 20
_MAX_RESULTS_PER_TARGET = 5_000
_FETCH_CONCURRENCY = 6
_REQUEST_TIMEOUT_SECONDS = 15.0

_UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_UUID_RE = re.compile(rf"^{_UUID_TEXT}$", re.IGNORECASE)
_API_MARKER_RE = re.compile(
    r"(?:https?:)?//ares\.betternoi\.com/api/pub/v1/client/building/unit/?"
    r"\?client_uuid\s*=",
    re.IGNORECASE,
)
_BUTTON_TAG_RE = re.compile(r"<(?:a|button)\b[^>]{0,4096}>", re.IGNORECASE)
_TARGET_ATTR_RE = re.compile(
    rf"\b(?P<name>data-property|data-fpcode)\s*=\s*"
    rf"(?P<quote>['\"])(?P<value>{_UUID_TEXT})(?P=quote)",
    re.IGNORECASE,
)


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


def _context_source_url(ctx: AdapterContext) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    final_url = str(getattr(fetch_result, "final_url", "") or "").strip()
    return final_url or str(getattr(ctx, "base_url", "") or "").strip()


def find_betternoi_targets(html: str) -> list[tuple[str, str]]:
    """Extract unambiguous ``(client_uuid, floorplan_uuid)`` button pairs.

    Attribute pairing is tag-local.  Collecting the two attributes globally
    could combine a property UUID from one embed with a plan UUID from another
    widget, so pages with multiple clients are rejected outright.
    """
    if not html or len(html) > _MAX_CONTEXT_BODY_BYTES or not _API_MARKER_RE.search(html):
        return []

    pairs: dict[tuple[str, str], None] = {}
    for raw_tag in _BUTTON_TAG_RE.findall(html):
        tag = html_lib.unescape(raw_tag)
        attrs = {
            match.group("name").lower(): match.group("value").lower()
            for match in _TARGET_ATTR_RE.finditer(tag)
        }
        client_uuid = attrs.get("data-property", "")
        floorplan_uuid = attrs.get("data-fpcode", "")
        if not _UUID_RE.fullmatch(client_uuid) or not _UUID_RE.fullmatch(floorplan_uuid):
            continue
        pairs.setdefault((client_uuid, floorplan_uuid), None)
        if len(pairs) > _MAX_TARGETS:
            return []

    if not pairs:
        return []
    clients = {client_uuid for client_uuid, _ in pairs}
    if len(clients) != 1:
        return []
    return list(pairs)


def _plain_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


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


def _street_key(value: object) -> str:
    text = str(value or "").casefold()
    for source, replacement in _STREET_CANONICAL_WORDS:
        text = re.sub(rf"\b{source}\b", replacement, text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _leading_street_number(value: object) -> str:
    match = re.match(r"\s*(\d+)", str(value or ""))
    return match.group(1) if match else ""


def _zip_key(value: object) -> str:
    match = re.search(r"\d{5}", str(value or ""))
    return match.group(0) if match else ""


def property_scope_matches(ctx: AdapterContext, item: dict[str, Any]) -> bool:
    """Fail closed unless response address/geography corroborates the CSV.

    BetterNOI sometimes publishes an internal complex street with no number
    (KRC: ``HUNTERS CLUB LN`` versus CSV ``4200 Jimmy Carter Blvd``).  In that
    shape exact city/state/ZIP are required.  When the API does publish a
    street number, the number and normalized street must agree with the CSV;
    this prevents a portfolio-wide response in the same metro from passing.
    """
    comparisons = (
        (getattr(ctx, "city", ""), item.get("building_city"), _plain_key),
        (getattr(ctx, "state", ""), item.get("building_state"), _plain_key),
        (getattr(ctx, "zip_code", ""), item.get("building_postal_code"), _zip_key),
    )
    matched = 0
    for expected_raw, observed_raw, normalizer in comparisons:
        expected = normalizer(expected_raw)
        if not expected:
            continue
        observed = normalizer(observed_raw)
        if not observed or observed != expected:
            return False
        matched += 1
    if matched < 2:
        return False

    expected_address = str(getattr(ctx, "address", "") or "").strip()
    observed_address = str(item.get("building_address") or "").strip()
    observed_number = _leading_street_number(observed_address)
    if expected_address and observed_number:
        expected_number = _leading_street_number(expected_address)
        if not expected_number or expected_number != observed_number:
            return False
        expected_key = _street_key(expected_address)
        observed_key = _street_key(observed_address)
        if not expected_key or not observed_key:
            return False
        if not (
            expected_key == observed_key
            or expected_key.startswith(observed_key)
            or observed_key.startswith(expected_key)
        ):
            return False
    return True


def _item_scope_is_exact(
    item: dict[str, Any],
    *,
    ctx: AdapterContext,
    client_uuid: str,
    floorplan_uuid: str,
) -> bool:
    if str(item.get("client_uuid") or "").strip().lower() != client_uuid:
        return False
    floor_plan = item.get("floor_plan")
    if not isinstance(floor_plan, dict):
        return False
    if str(floor_plan.get("uuid") or "").strip().lower() != floorplan_uuid:
        return False
    return property_scope_matches(ctx, item)


def _payload_scope_is_exact(
    payload: dict[str, Any],
    *,
    ctx: AdapterContext,
    client_uuid: str,
    floorplan_uuid: str,
) -> bool:
    """Reject the whole page if even one row belongs to another scope."""
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    return all(
        isinstance(item, dict)
        and _item_scope_is_exact(
            item,
            ctx=ctx,
            client_uuid=client_uuid,
            floorplan_uuid=floorplan_uuid,
        )
        for item in results
    )


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    integer = int(parsed)
    if not minimum <= integer <= maximum:
        return None
    return integer


def _dimension_text(value: object, *, allow_zero: bool) -> str:
    if isinstance(value, bool) or value in (None, ""):
        return ""
    try:
        parsed = float(str(value).strip())
    except ValueError:
        return ""
    lower = 0.0 if allow_zero else 0.25
    if not math.isfinite(parsed) or not lower <= parsed <= 20:
        return ""
    return str(int(parsed)) if parsed.is_integer() else f"{parsed:g}"


def _iso_date(value: object) -> str:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _strict_item(
    item: dict[str, Any],
    *,
    ctx: AdapterContext,
    client_uuid: str,
    floorplan_uuid: str,
    source_url: str,
) -> dict[str, Any] | None:
    from ma_poc.pms.adapters._parsing import (
        bed_label_from,
        is_junk_unit_number,
        make_unit_dict,
    )

    if not _item_scope_is_exact(
        item,
        ctx=ctx,
        client_uuid=client_uuid,
        floorplan_uuid=floorplan_uuid,
    ):
        return None
    floor_plan = item["floor_plan"]

    unit_uuid = str(item.get("uuid") or "").strip().lower()
    if not _UUID_RE.fullmatch(unit_uuid):
        return None
    if item.get("unit_to_skip") is True:
        return None
    status = str(item.get("availability_status") or "").strip().lower()
    if status not in {"available", "on_notice"}:
        return None
    unit_number = str(item.get("unit_identifier") or item.get("unit_number") or "").strip()
    if not unit_number or len(unit_number) > 80 or is_junk_unit_number(unit_number):
        return None

    plan_name = str(floor_plan.get("name") or "").strip()
    # BetterNOI's explicit floor-plan field legitimately uses names such as
    # ``1 Bed 1 Bath`` (KRC and Magnolia; RP publishes the same name).  The
    # shared generic-DOM junk filter rejects that string because it can be a
    # fabricated fallback in unstructured HTML, but it is not fabricated on
    # this UUID-bound API surface.
    if not plan_name or len(plan_name) > 200 or plan_name.casefold() in {"~", "null", "none"}:
        return None

    rent_low = _bounded_int(item.get("min_rent"), minimum=200, maximum=50_000)
    if rent_low is None:
        return None
    rent_high = _bounded_int(item.get("max_rent"), minimum=200, maximum=50_000)
    if rent_high is None or rent_high < rent_low:
        rent_high = rent_low

    sqft = _bounded_int(item.get("min_square_feet"), minimum=150, maximum=10_000)
    if sqft is None:
        return None
    bedrooms = _dimension_text(item.get("bedroom_count"), allow_zero=True)
    bathrooms = _dimension_text(item.get("bathroom_count"), allow_zero=False)
    if not bedrooms or not bathrooms:
        return None
    beds_int = int(float(bedrooms))

    display_unit = str(item.get("unit_identifier") or "").strip()
    row = make_unit_dict(
        floor_plan_name=plan_name,
        bed_label=bed_label_from(beds_int, plan_name),
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        sqft=str(sqft),
        unit_number=unit_number,
        unit_name=display_unit,
        building=str(item.get("building_number") or "").strip(),
        rent_low=rent_low,
        rent_high=rent_high,
        availability_status="AVAILABLE",
        available_units="1",
        availability_date=_iso_date(item.get("adjusted_available_date")),
        source_api_url=source_url,
        extraction_tier=_TIER,
        source_ids={
            "betternoi_unit_uuid": unit_uuid,
            "betternoi_floorplan_uuid": floorplan_uuid,
            "betternoi_client_uuid": client_uuid,
        },
    )
    # BetterNOI publishes this value in the UUID-bound ``floor_plan.name``
    # field.  Preserve that distinction for the production formatter: its
    # generic-DOM hygiene rule intentionally rejects labels such as
    # ``1 Bed 1 Bath`` because unstructured HTML often fabricates them from a
    # URL slug.  On this strict API surface the value is explicit operator
    # data, not a fallback.  The formatter only honors this exact provenance
    # token, so arbitrary adapters cannot opt themselves out of junk hygiene.
    row["_floor_plan_name_provenance"] = "betternoi.floor_plan.name"
    return row


def parse_betternoi_payload(
    payload: dict[str, Any],
    *,
    ctx: AdapterContext,
    client_uuid: str,
    floorplan_uuid: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse one page only when every row has the exact requested scope."""
    results = payload.get("results")
    if not isinstance(results, list) or not _payload_scope_is_exact(
        payload,
        ctx=ctx,
        client_uuid=client_uuid,
        floorplan_uuid=floorplan_uuid,
    ):
        return []
    parsed: list[dict[str, Any]] = []
    for item in results:
        assert isinstance(item, dict)  # established by the whole-page gate
        row = _strict_item(
            item,
            ctx=ctx,
            client_uuid=client_uuid,
            floorplan_uuid=floorplan_uuid,
            source_url=source_url,
        )
        if row is not None:
            parsed.append(row)
    return parsed


def _target_url(client_uuid: str, floorplan_uuid: str) -> str:
    query = urlencode(
        {
            "client_uuid": client_uuid,
            "floorplan_uuid": floorplan_uuid,
            "is_available": "true",
        }
    )
    return f"{_API_URL}?{query}"


def _valid_next_url(url: str, client_uuid: str, floorplan_uuid: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() != "ares.betternoi.com"
        or parts.path.rstrip("/") != _API_PATH.rstrip("/")
        or parts.fragment
    ):
        return False
    query = parse_qs(parts.query, keep_blank_values=True)
    if not set(query).issubset({"client_uuid", "floorplan_uuid", "is_available", "page"}):
        return False
    return (
        query.get("client_uuid") == [client_uuid]
        and query.get("floorplan_uuid") == [floorplan_uuid]
        and query.get("is_available") == ["true"]
    )


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


async def _fetch_target(
    client: Any,
    client_uuid: str,
    floorplan_uuid: str,
) -> tuple[list[tuple[str, int, dict[str, Any] | None]], bool]:
    """Fetch every result page and prove the roster is not truncated."""
    observations: list[tuple[str, int, dict[str, Any] | None]] = []
    url = _target_url(client_uuid, floorplan_uuid)
    seen_urls: set[str] = set()
    expected_count: int | None = None
    observed_count = 0
    for _ in range(_MAX_PAGES_PER_TARGET):
        if url in seen_urls:
            return observations, False
        seen_urls.add(url)
        status, payload = await _fetch_public_json(client, url)
        observations.append((url, status, payload))
        if not payload:
            return observations, False

        count = payload.get("count")
        results = payload.get("results")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= _MAX_RESULTS_PER_TARGET
            or not isinstance(results, list)
        ):
            return observations, False
        if expected_count is None:
            expected_count = count
        elif count != expected_count:
            return observations, False
        observed_count += len(results)
        if observed_count > expected_count:
            return observations, False

        next_url = payload.get("next")
        if next_url is None:
            return observations, observed_count == expected_count
        if not isinstance(next_url, str):
            return observations, False
        if not next_url.strip():
            return observations, observed_count == expected_count
        next_url = next_url.strip()
        if not _valid_next_url(next_url, client_uuid, floorplan_uuid):
            return observations, False
        url = next_url
    # A still-paginated result after the hard page cap is incomplete and must
    # never be emitted as if it represented the property's full availability.
    return observations, False


def _merge_strict_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate exact repeats and drop identities with conflicting data."""
    by_identity: dict[str, dict[str, Any]] = {}
    signatures: dict[str, tuple[object, ...]] = {}
    conflicts: set[str] = set()
    for row in rows:
        identity = str(row.get("unit_number") or "").strip().casefold()
        if not identity or identity in conflicts:
            continue
        source_ids = row.get("source_ids")
        ids = source_ids if isinstance(source_ids, dict) else {}
        signature = (
            ids.get("betternoi_unit_uuid"),
            ids.get("betternoi_floorplan_uuid"),
            row.get("floor_plan_name"),
            row.get("sqft"),
            row.get("market_rent_low"),
            row.get("market_rent_high"),
            row.get("available_date"),
        )
        if identity not in by_identity:
            by_identity[identity] = row
            signatures[identity] = signature
            continue
        if signatures[identity] != signature:
            by_identity.pop(identity, None)
            signatures.pop(identity, None)
            conflicts.add(identity)
    return list(by_identity.values())


async def recover_betternoi_units(ctx: AdapterContext) -> list[dict[str, Any]]:
    """Recover a property-bound canonical roster from BetterNOI's public API."""
    targets = find_betternoi_targets(_context_html(ctx))
    if not targets:
        return []

    import httpx

    source_url = _context_source_url(ctx)
    headers = {"Accept": "application/json"}
    if source_url.startswith(("http://", "https://")):
        headers["Referer"] = source_url

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS, connect=5.0),
        trust_env=False,
        limits=httpx.Limits(
            max_connections=_FETCH_CONCURRENCY,
            max_keepalive_connections=_FETCH_CONCURRENCY,
        ),
        headers=headers,
    ) as client:

        async def fetch_one(
            client_uuid: str,
            floorplan_uuid: str,
        ) -> tuple[
            str,
            str,
            list[tuple[str, int, dict[str, Any] | None]],
            bool,
        ]:
            async with semaphore:
                observations, complete = await _fetch_target(
                    client,
                    client_uuid,
                    floorplan_uuid,
                )
            return client_uuid, floorplan_uuid, observations, complete

        fetched = await asyncio.gather(
            *(fetch_one(client_uuid, floorplan_uuid) for client_uuid, floorplan_uuid in targets)
        )

    rows: list[dict[str, Any]] = []
    for client_uuid, floorplan_uuid, observations, complete in fetched:
        target_rows: list[dict[str, Any]] = []
        target_scope_exact = complete
        for source_api_url, status, payload in observations:
            if status in {401, 403, 429, 503}:
                # Local import avoids a module-load cycle; universal recovery
                # owns the shared operational telemetry channel.
                from ma_poc.pms.adapters._universal_recovery import mark_blocked

                mark_blocked(ctx, "betternoi", source_api_url, status)
            if not payload:
                continue
            if not _payload_scope_is_exact(
                payload,
                ctx=ctx,
                client_uuid=client_uuid,
                floorplan_uuid=floorplan_uuid,
            ):
                target_scope_exact = False
                break
            target_rows.extend(
                parse_betternoi_payload(
                    payload,
                    ctx=ctx,
                    client_uuid=client_uuid,
                    floorplan_uuid=floorplan_uuid,
                    source_url=source_api_url,
                )
            )
        if target_scope_exact:
            rows.extend(target_rows)
    return _merge_strict_rows(rows)
