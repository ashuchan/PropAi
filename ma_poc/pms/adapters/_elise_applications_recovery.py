"""Recover apartment inventory from an operator-authored Elise application.

Some Jonah sites publish plan cards in their own HTML while their exact
``leaseUrl`` points at ``applications.eliseai.com/building/{uuid}``.  The
public application uses two unauthenticated JSON endpoints: one identifies
the building and one lists the currently selectable apartments.

This recovery is deliberately narrow:

* the marketing response must author exactly one HTTPS application URL;
* the response itself must match the configured name, street and ZIP;
* Elise's configuration response must repeat the UUID and property name; and
* every emitted row needs a native unit number/id, plan, dimensions, explicit
  date and positive base rent.

Network access is plain direct HTTP only.  No proxy, browser, unlocker,
fingerprint rotation, CAPTCHA or FlareSolverr path is reachable here.
"""

from __future__ import annotations

import html as html_lib
import math
import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from ma_poc.pms.adapters._parsing import is_junk_unit_number, make_unit_dict

_APPLICATION_HOST = "applications.eliseai.com"
_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_UUID_RE = re.compile(rf"^{_UUID_PATTERN}$", re.IGNORECASE)
_UNIT_ID_RE = re.compile(r"^u[0-9]+$")
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_UNITS = 500
_TIER = "TIER_1_API_ELISE_APPLICATIONS"

_AUTHORED_URL_PATTERNS = (
    re.compile(
        r"<a\b[^>]*\bhref\s*=\s*(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bleaseUrl\s*=\s*(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)",
        re.IGNORECASE,
    ),
)

_STREET_TOKEN_MAP = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "circle": "cir",
    "terrace": "ter",
    "parkway": "pkwy",
    "highway": "hwy",
    "place": "pl",
    "trail": "trl",
}
_GENERIC_PROPERTY_TOKENS = {
    "apartment",
    "apartments",
    "community",
    "residence",
    "residences",
    "townhome",
    "townhomes",
}
_JUNK_UNIT_LABELS = {
    "apartment",
    "apartments",
    "apply now",
    "available now",
    "bathroom",
    "bathrooms",
    "bedroom",
    "bedrooms",
    "contact us",
    "n a",
    "na",
    "unit",
    "units",
}


def _ctx_body(ctx: Any) -> str:
    fetch_result = getattr(ctx, "fetch_result", None)
    body = getattr(fetch_result, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body if isinstance(body, str) else ""


def _text_tokens(value: object, *, street_tokens: bool = False) -> list[str]:
    text = html_lib.unescape(str(value or "")).casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    if street_tokens:
        return [_STREET_TOKEN_MAP.get(token, token) for token in tokens]
    return tokens


def _name_tokens(value: object) -> list[str]:
    return [
        token
        for token in _text_tokens(value)
        if token not in _GENERIC_PROPERTY_TOKENS
    ]


def _names_match(expected: object, observed: object) -> bool:
    expected_tokens = _name_tokens(expected)
    observed_tokens = _name_tokens(observed)
    return bool(expected_tokens and expected_tokens == observed_tokens)


def _elise_uuid_from_url(raw_url: str) -> str | None:
    url = html_lib.unescape(raw_url).replace(r"\/", "/").strip()
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or host != _APPLICATION_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        return None
    path_match = re.fullmatch(rf"/building/(?P<uuid>{_UUID_PATTERN})/?", parts.path, re.IGNORECASE)
    if path_match is None:
        return None
    return path_match.group("uuid").lower()


def discover_elise_application_uuid(source_html: str) -> str | None:
    """Return one UUID from an exact authored Elise application URL.

    A chat-widget ``building`` value or a free-floating URL-shaped script
    string is insufficient.  Only an anchor or Jonah's explicit ``leaseUrl``
    setting is eligible, and multiple building UUIDs fail closed.
    """
    if not source_html:
        return None
    normalized = html_lib.unescape(source_html).replace(r"\/", "/")
    uuids: set[str] = set()
    for pattern in _AUTHORED_URL_PATTERNS:
        for match in pattern.finditer(normalized):
            uuid = _elise_uuid_from_url(match.group("url"))
            if uuid:
                uuids.add(uuid)
    return next(iter(uuids)) if len(uuids) == 1 else None


def _source_matches_context(source_html: str, ctx: Any) -> bool:
    """Verify that the authored URL came from the configured property page."""
    expected_name = str(getattr(ctx, "property_name", "") or "").strip()
    expected_address = str(getattr(ctx, "address", "") or "").strip()
    expected_state = str(getattr(ctx, "state", "") or "").strip()
    zip_match = re.search(r"\b([0-9]{5})\b", str(getattr(ctx, "zip_code", "") or ""))
    if not all((source_html, expected_name, expected_address, expected_state, zip_match)):
        return False

    body_tokens = _text_tokens(source_html, street_tokens=True)
    body_text = " ".join(body_tokens)
    name_text = " ".join(_name_tokens(expected_name))
    address_text = " ".join(_text_tokens(expected_address, street_tokens=True))
    state_zip = " ".join(
        (*_text_tokens(expected_state), zip_match.group(1))
    )
    return bool(
        name_text
        and address_text
        and name_text in body_text
        and address_text in body_text
        and state_zip in body_text
    )


def _configuration_matches_context(payload: object, uuid: str, ctx: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    details = payload.get("building_details")
    if not isinstance(details, dict):
        return False
    observed_uuid = str(details.get("slug") or "").strip().lower()
    observed_name = str(details.get("building_name") or "").strip()
    expected_name = str(getattr(ctx, "property_name", "") or "").strip()
    return bool(
        _UUID_RE.fullmatch(observed_uuid)
        and observed_uuid == uuid
        and _names_match(expected_name, observed_name)
    )


def _bounded_number(
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _number_label(value: object, *, maximum: float = 20) -> str | None:
    number = _bounded_number(value, minimum=0, maximum=maximum)
    if number is None:
        return None
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _explicit_date(value: object) -> str | None:
    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def parse_elise_units(
    payload: object,
    *,
    uuid: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse the strict public ``searchUnits`` list for one verified UUID."""
    if (
        not _UUID_RE.fullmatch(uuid)
        or source_url != _units_url(uuid)
        or not isinstance(payload, list)
    ):
        return []
    if not payload or len(payload) > _MAX_UNITS:
        return []

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_units: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            return []

        native_id = str(item.get("id") or "").strip()
        unit_number = str(item.get("unit_number") or "").strip()
        unit_label = " ".join(_text_tokens(unit_number))
        plan_name = str(item.get("floorplan_name") or "").strip()
        beds = _number_label(item.get("number_of_bedrooms"))
        baths = _number_label(item.get("number_of_bathrooms"))
        sqft_number = _bounded_number(
            item.get("square_footage"),
            minimum=1,
            maximum=100_000,
        )
        rent_number = _bounded_number(item.get("rent"), minimum=1, maximum=1_000_000)
        available_date = _explicit_date(item.get("date_available"))

        if (
            not _UNIT_ID_RE.fullmatch(native_id)
            or not unit_number
            or len(unit_number) > 80
            or is_junk_unit_number(unit_number)
            or unit_label in _JUNK_UNIT_LABELS
            or not plan_name
            or len(plan_name) > 200
            or beds is None
            or baths is None
            or sqft_number is None
            or rent_number is None
            or available_date is None
        ):
            return []

        unit_key = unit_number.casefold()
        if native_id in seen_ids or unit_key in seen_units:
            return []
        seen_ids.add(native_id)
        seen_units.add(unit_key)

        floor = str(item.get("floor") or "").strip()
        building = str(item.get("sub_building_name") or "").strip()
        row = make_unit_dict(
            floor_plan_name=plan_name,
            bedrooms=beds,
            bathrooms=baths,
            sqft=str(int(round(sqft_number))),
            unit_number=unit_number,
            unit_name=unit_number,
            floor=floor,
            building=building,
            rent_low=int(round(rent_number)),
            rent_high=int(round(rent_number)),
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=available_date,
            source_api_url=source_url,
            extraction_tier=_TIER,
            source_ids={"elise_applications_unit_id": native_id},
        )
        row["_floor_plan_name_provenance"] = "elise.floorplan_name"
        row["_availability_date_provenance"] = "explicit_provider_date"
        stage = str(item.get("availability_stage") or "").strip()
        if stage:
            row["_elise_availability_stage"] = stage
        rows.append(row)
    return rows


def _configuration_url(uuid: str) -> str:
    return f"https://{_APPLICATION_HOST}/api/configuration/{uuid}"


def _units_url(uuid: str) -> str:
    return f"https://{_APPLICATION_HOST}/api/searchUnits?building_slug={uuid}"


async def _fetch_elise_json(url: str) -> object | None:
    """Fetch one bounded exact Elise JSON endpoint via direct HTTP only."""
    import httpx

    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or (parts.hostname or "").casefold().rstrip(".") != _APPLICATION_HOST
        or parts.username is not None
        or parts.password is not None
    ):
        return None

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(20.0),
        trust_env=False,
        headers={"Accept": "application/json"},
    ) as client:
        try:
            response = await client.get(url)
        except (httpx.HTTPError, ValueError):
            return None
    if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
        return None
    if "json" not in response.headers.get("content-type", "").casefold():
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def recover_elise_applications(ctx: Any) -> list[dict[str, Any]]:
    """Recover units after source-page and Elise configuration corroboration."""
    source_html = _ctx_body(ctx)
    uuid = discover_elise_application_uuid(source_html)
    if uuid is None or not _source_matches_context(source_html, ctx):
        return []

    configuration = await _fetch_elise_json(_configuration_url(uuid))
    if not _configuration_matches_context(configuration, uuid, ctx):
        return []

    source_url = _units_url(uuid)
    payload = await _fetch_elise_json(source_url)
    return parse_elise_units(payload, uuid=uuid, source_url=source_url)
