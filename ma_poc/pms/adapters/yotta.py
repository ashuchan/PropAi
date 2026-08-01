"""Yotta Real public leasing-portal adapter.

The configured Yotta portal URL carries an exact DBA id (for example
``HomePage.aspx?Id=55``).  Two public JSON endpoints then expose the property
identity and its native available-unit roster.  The identity call is a hard
property-boundary gate: inventory is fetched only when the provider name,
street, city, state, and ZIP all match the configured property.

The contract was live-probed on four distinct DBAs (55, 57, 58, and 59) on
2026-08-01.  Every valid DBA returned the same ``GetDBADetails`` and
``GetFloorPlans/{dba}/1`` shapes with native ``unitId``/``unitNumber`` rows.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from ma_poc.pms.adapters._parsing import bed_label_from, make_unit_dict
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

_TIER = "TIER_1_API_YOTTA"
_DETAILS_URL = "https://residentapis.yottareal.com/api/DBA/GetDBADetails/{dba_id}"
_UNITS_URL = "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/{dba_id}/1"
_API_DBA_PATH_RE = re.compile(
    r"^/api/DBA/(?:GetDBADetails|GetFloorPlans)/(\d+)(?:/1)?/?$",
    re.IGNORECASE,
)
_NAME_STOPWORDS = frozenset(
    {
        "apartment",
        "apartments",
        "community",
        "home",
        "homes",
        "residence",
        "residences",
        "the",
    }
)
_ADDRESS_STOPWORDS = frozenset(
    {
        "avenue",
        "ave",
        "boulevard",
        "blvd",
        "circle",
        "court",
        "ct",
        "drive",
        "dr",
        "east",
        "e",
        "highway",
        "hwy",
        "lane",
        "ln",
        "north",
        "n",
        "parkway",
        "pkwy",
        "place",
        "pl",
        "road",
        "rd",
        "south",
        "s",
        "street",
        "st",
        "west",
        "w",
    }
)


def _tokens(value: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def extract_yotta_dba_id(*urls: str) -> str:
    """Return one exact numeric DBA id from Yotta URLs, else ``""``."""
    found: set[str] = set()
    for url in urls:
        if not url:
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            if not (host == "yottareal.com" or host.endswith(".yottareal.com")):
                continue
            query = {
                key.casefold(): values
                for key, values in parse_qs(parsed.query).items()
            }
        except (TypeError, ValueError):
            continue
        for key in ("id", "dbaid"):
            values = query.get(key) or []
            if len(values) == 1 and values[0].isdigit() and int(values[0]) > 0:
                found.add(str(int(values[0])))
        path_match = _API_DBA_PATH_RE.fullmatch(parsed.path or "")
        if path_match:
            found.add(str(int(path_match.group(1))))
    return next(iter(found)) if len(found) == 1 else ""


def yotta_property_identity_matches(
    details: dict[str, Any],
    ctx: AdapterContext,
    dba_id: str,
) -> bool:
    """Fail-closed exact-property boundary for Yotta's multi-tenant API."""
    expected_name = [
        token for token in _tokens(ctx.property_name) if token not in _NAME_STOPWORDS
    ]
    observed_name = set(
        token
        for token in _tokens(details.get("dbaName"))
        if token not in _NAME_STOPWORDS
    )
    expected_address = _tokens(ctx.address)
    observed_address = set(
        _tokens(f"{details.get('address1') or ''} {details.get('address2') or ''}")
    )
    expected_street_number = expected_address[0] if expected_address else ""
    expected_street_words = [
        token
        for token in expected_address[1:]
        if token not in _ADDRESS_STOPWORDS and not token.isdigit()
    ]
    expected_city = " ".join(_tokens(ctx.city))
    observed_city = " ".join(_tokens(details.get("city")))
    expected_state = "".join(_tokens(ctx.state))
    observed_state = "".join(_tokens(details.get("stateCode")))
    expected_zip = "".join(_tokens(ctx.zip_code))
    observed_zip = "".join(_tokens(details.get("zip")))
    try:
        provider_dba_id = str(int(details.get("dbaId")))
    except (TypeError, ValueError):
        provider_dba_id = ""
    return bool(
        dba_id
        and provider_dba_id == dba_id
        and expected_name
        and all(token in observed_name for token in expected_name)
        and expected_street_number
        and expected_street_number in observed_address
        and expected_street_words
        and all(token in observed_address for token in expected_street_words)
        and expected_city
        and observed_city == expected_city
        and expected_state
        and observed_state == expected_state
        and expected_zip
        and observed_zip == expected_zip
    )


def _positive_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _availability_date(item: dict[str, Any]) -> str:
    raw = str(
        item.get("MoveInDateAvailable")
        or item.get("unitAvailableDate")
        or item.get("availableDate")
        or item.get("dateAvailable")
        or ""
    ).strip()
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return iso_match.group(1) if iso_match else raw


def parse_yotta_units(
    payload: dict[str, Any],
    *,
    dba_id: str,
    source_url: str,
) -> list[dict[str, Any]]:
    """Map Yotta ``hotSheetUnitsModel`` rows to canonical native units."""
    raw_rows = payload.get("hotSheetUnitsModel")
    if not isinstance(raw_rows, list):
        return []
    units: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        native_id = str(item.get("unitId") or "").strip()
        unit_number = str(item.get("unitNumber") or "").strip()
        rent = _positive_finite_number(item.get("rent"))
        if not native_id or not unit_number or rent is None or native_id in seen_ids:
            continue
        seen_ids.add(native_id)
        plan_name = str(
            item.get("dbaUnitType") or item.get("dbaUnitTypeCode") or ""
        ).strip()
        bedrooms = str(item.get("bedRooms") or "").strip()
        try:
            bedrooms_number = int(float(bedrooms))
        except (TypeError, ValueError):
            bedrooms_number = None
        bathrooms = str(item.get("bathRooms") or "").strip()
        sqft_raw = _positive_finite_number(item.get("squareFeet"))
        sqft = str(int(sqft_raw)) if sqft_raw is not None else ""
        rounded_rent = int(round(rent))
        unit = make_unit_dict(
            floor_plan_name=plan_name,
            bed_label=bed_label_from(bedrooms_number, plan_name),
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            sqft=sqft,
            unit_number=unit_number,
            floor=str(item.get("floorLevel") or "").strip(),
            building=str(item.get("buildingNumber") or "").strip(),
            rent_low=rounded_rent,
            rent_high=rounded_rent,
            availability_status="AVAILABLE",
            available_units="1",
            availability_date=_availability_date(item),
            source_api_url=source_url,
            extraction_tier=_TIER,
            source_ids={"yotta_unit_id": native_id},
        )
        unit["source_property_id"] = dba_id
        units.append(unit)
    return units


async def _fetch_json(url: str) -> tuple[int, dict[str, Any]]:
    response = await asyncio.to_thread(
        probe_get,
        url,
        timeout=25,
        unlocker=False,
        retries=1,
        headers={"Accept": "application/json"},
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        return status, {}
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return status, {}
    return status, payload if isinstance(payload, dict) else {}


class YottaAdapter:
    """Property-scoped public Yotta unit-roster adapter."""

    pms_name: str = "yotta"
    _fingerprints = ["adaraportal.yottareal.com", "residentapis.yottareal.com"]

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)

    def matches_response_body(self, body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        return (
            isinstance(body.get("hotSheetUnitsModel"), list)
            or (body.get("dbaId") is not None and bool(body.get("dbaName")))
        )

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        result = AdapterResult(tier_used=_TIER)
        fetch_result = getattr(ctx, "fetch_result", None)
        final_url = str(getattr(fetch_result, "final_url", "") or "")
        dba_id = extract_yotta_dba_id(final_url, ctx.base_url)
        if not dba_id:
            result.tier_used = f"{_TIER}_NO_EXACT_DBA_ID"
            result.errors.append(
                "YOTTA_NO_EXACT_DBA_ID: configured/final Yotta URL has no sole numeric Id or dbaId"
            )
            return result

        details_url = _DETAILS_URL.format(dba_id=dba_id)
        units_url = _UNITS_URL.format(dba_id=dba_id)
        try:
            details_status, details = await _fetch_json(details_url)
        except Exception as exc:
            result.tier_used = f"{_TIER}_DETAILS_FETCH_ERROR"
            result.errors.append(
                f"YOTTA_DETAILS_FETCH_ERROR: {type(exc).__name__}: {str(exc)[:100]}"
            )
            return result
        result.api_responses.append(
            {
                "url": details_url,
                "status": details_status,
                "body": "<yotta-property-details>",
                "via": "yotta_public_api",
            }
        )
        if details_status != 200 or not yotta_property_identity_matches(
            details, ctx, dba_id
        ):
            result.tier_used = f"{_TIER}_PROPERTY_IDENTITY_REJECTED"
            result.errors.append(
                "YOTTA_PROPERTY_IDENTITY_REJECTED: provider details do not exactly match configured property"
            )
            return result

        try:
            units_status, payload = await _fetch_json(units_url)
        except Exception as exc:
            result.tier_used = f"{_TIER}_UNITS_FETCH_ERROR"
            result.errors.append(
                f"YOTTA_UNITS_FETCH_ERROR: {type(exc).__name__}: {str(exc)[:100]}"
            )
            return result
        result.api_responses.append(
            {
                "url": units_url,
                "status": units_status,
                "body": "<yotta-native-unit-roster>",
                "via": "yotta_public_api",
            }
        )
        if units_status != 200:
            result.tier_used = f"{_TIER}_UNITS_HTTP_{units_status}"
            return result

        raw_units = parse_yotta_units(payload, dba_id=dba_id, source_url=units_url)
        if not raw_units:
            result.tier_used = f"{_TIER}_EMPTY"
            result.winning_url = units_url
            return result

        from ma_poc.extraction.post_process import post_process

        processed = post_process(raw_units, property_id=ctx.property_id)
        if processed.n_admitted <= 0:
            result.tier_used = f"{_TIER}_VALIDITY_REJECTED"
            result.errors.append(
                f"YOTTA_VALIDITY_REJECTED: {len(raw_units)} parsed rows failed unit validity"
            )
            return result
        result.units = processed.admitted
        result.plan_summaries = processed.plan_summaries
        result.winning_url = units_url
        result.tier_used = _TIER
        result.confidence = min(0.94, 0.78 + 0.02 * processed.n_admitted)
        return result
