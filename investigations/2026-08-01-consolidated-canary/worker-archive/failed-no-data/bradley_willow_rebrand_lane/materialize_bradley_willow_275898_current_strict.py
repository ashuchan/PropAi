#!/usr/bin/env python3
"""Materialize strict rebrand evidence for Bradley Pointe / Willow Run."""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup


os.environ["COMPLIANCE_MODE"] = "1"
os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""
os.environ["ENABLE_HYPERBROWSER"] = "false"
os.environ["ENABLE_TIER4_LLM"] = "false"
os.environ["ENABLE_TIER_ESCALATION"] = "false"
os.environ["ENABLE_UNLOCKER_TIER"] = "false"
os.environ["ENABLE_FLARESOLVERR_TIER"] = "false"

from ma_poc.core.identity import unit_has_real_anchor  # noqa: E402
from ma_poc.fetch.contracts import (  # noqa: E402
    FetchOutcome,
    FetchResult,
    RenderMode,
)
from ma_poc.pms.adapters._probe import (  # noqa: E402
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.scraper import scrape  # noqa: E402


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "bradley_willow_rebrand_lane"
EVIDENCE = OUT / "evidence_bradley_willow_275898_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")

PROPERTY_ID = "275898"
CONFIGURED_NAME = "Bradley Pointe"
CURRENT_NAME = "Willow Run"
CONFIGURED_URL = "https://www.liveatbradleypointe.com/"
CURRENT_ROOT_URL = "https://liveatwillowrun.com/en/"
CURRENT_INVENTORY_URL = "https://liveatwillowrun.com/en/floor-plans/"
ADDRESS = "1355 Bradley Blvd"
CITY = "Savannah"
STATE = "GA"
POSTAL_CODE = "31419"
API_HOST = "ares.betternoi.com"
API_PATH = "/api/pub/v1/client/building/unit"
UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
PUBLISHED_PAIR_RE = re.compile(
    rf"data-property\s*=\s*[\"']+(?P<client>{UUID_PATTERN})[\"']+"
    rf".{{0,600}}?data-fpcode\s*=\s*[\"']+(?P<floorplan>{UUID_PATTERN})"
    r"[\"']+",
    re.IGNORECASE | re.DOTALL,
)
CRITICAL_SOURCE_FILES = (
    REPO / "ma_poc/core/identity.py",
    REPO / "ma_poc/pms/scraper.py",
    REPO / "ma_poc/pms/detector.py",
    REPO / "ma_poc/pms/adapters/_betternoi_public.py",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def snapshot(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): sha256_path(path) for path in paths}


def normalized(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


ADDRESS_ALIASES = {
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


def normalized_address(value: Any) -> str:
    return " ".join(
        ADDRESS_ALIASES.get(token, token) for token in normalized(value).split()
    )


def page_identity(html: str) -> dict[str, bool]:
    soup = BeautifulSoup(html, "lxml")
    metadata = " ".join(
        str(node.get("content") or "") for node in soup.select("meta[content]")
    )
    text = normalized(f"{soup.get_text(' ', strip=True)} {metadata}")
    address_text = normalized_address(f"{soup.get_text(' ', strip=True)} {metadata}")
    words = set(text.split())
    return {
        "configured_name_visible": all(
            token in words for token in normalized(CONFIGURED_NAME).split()
        ),
        "current_name_visible": all(
            token in words for token in normalized(CURRENT_NAME).split()
        ),
        "exact_address_visible": (
            f" {normalized_address(ADDRESS)} " in f" {address_text} "
        ),
        "city_visible": CITY.casefold() in words,
        "state_visible": STATE.casefold() in words,
        "zip_visible": POSTAL_CODE in words,
    }


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and math.isfinite(float(row[key]))
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


def rent_value(row: dict[str, Any]) -> int:
    for key in (
        "market_rent_low",
        "rent_low",
        "rent",
        "market_rent_high",
        "rent_high",
    ):
        value = row.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return int(value)
    raise AssertionError(f"row has no positive rent: {row!r}")


def raw_rent(row: dict[str, Any]) -> int:
    for key in ("min_rent", "min_effective_rent", "display_rent", "market_rent"):
        value = row.get(key)
        try:
            parsed = int(float(str(value or "0").replace(",", "")))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    raise AssertionError(f"raw row has no positive rent: {row!r}")


def strict_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in (result.get("units") or [])
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]


def compact_e2e(result: dict[str, Any]) -> dict[str, Any]:
    rows = strict_rows(result)
    return {
        "adapter": result.get("_adapter_used"),
        "detected_pms": result.get("_detected_pms"),
        "tier": result.get("extraction_tier_used"),
        "errors": result.get("errors") or [],
        "fallback_chain": result.get("_fallback_chain") or [],
        "winning_page_url": result.get("_winning_page_url"),
        "strict_native_positive_rent_rows": len(rows),
        "distinct_native_unit_numbers": len(
            {str(row.get("unit_number") or "").casefold() for row in rows}
        ),
        "source_urls": sorted(
            {
                str(row.get("source_api_url") or "")
                for row in rows
                if str(row.get("source_api_url") or "")
            }
        ),
        "validation_budget": result.get("_validation_budget"),
    }


def direct_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    kwargs: dict[str, Any] = {
        "timeout": 35,
        "unlocker": False,
        "retries": 2,
        "proxies": {},
        "verify": True,
    }
    if headers:
        kwargs["headers"] = headers
    response = probe_get(url, **kwargs)
    body = bytes(response.content or b"")
    text = body.decode("utf-8", "replace")
    status = int(response.status_code or 0)
    challenge = bool(
        re.search(
            r"just a moment|verify you are human|checking your browser|cf-chl-",
            text,
            re.I,
        )
    )
    assert status == 200, (url, status, response.url)
    assert body
    assert challenge is False
    metadata = {
        "requested_url": url,
        "status": status,
        "final_url": str(response.url or url),
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
        "challenge_detected": challenge,
        "transport": {
            "backend": "direct_curl_cffi_probe_get",
            "unlocker": False,
            "proxies": {},
            "captcha_solving": False,
            "fingerprint_rotation": False,
        },
    }
    response_headers = {
        str(key).lower(): str(value) for key, value in response.headers.items()
    }
    return body, metadata, response_headers


def archive(name: str, body: bytes) -> dict[str, Any]:
    path = OUT / name
    with gzip.open(path, "wb") as handle:
        handle.write(body)
    return {
        "path": str(path),
        "compressed_sha256": sha256_path(path),
        "raw_sha256": sha256_bytes(body),
        "raw_bytes": len(body),
    }


def archive_json(name: str, payload: Any) -> dict[str, Any]:
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return archive(name, body)


def to_fetch_result(
    url: str,
    body: bytes,
    metadata: dict[str, Any],
    headers: dict[str, str],
) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=int(metadata["status"]),
        body=body,
        headers=headers,
        render_mode=RenderMode.GET,
        final_url=str(metadata["final_url"]),
        attempts=1,
        elapsed_ms=0,
    )


async def full_pipeline(
    url: str,
    body: bytes,
    metadata: dict[str, Any],
    headers: dict[str, str],
    *,
    property_name: str,
) -> dict[str, Any]:
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    result = await scrape(
        url,
        page=None,
        fetch_result=to_fetch_result(url, body, metadata, headers),
        csv_row={
            "apartmentid": PROPERTY_ID,
            "name": property_name,
            "address": ADDRESS,
            "city": CITY,
            "state": STATE,
            "zip": POSTAL_CODE,
            "website": url,
        },
        property_id=PROPERTY_ID,
        shared_budget=budget,
    )
    result["_validation_budget"] = budget
    return result


def published_pairs(html: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in PUBLISHED_PAIR_RE.finditer(html):
        pair = (
            match.group("client").casefold(),
            match.group("floorplan").casefold(),
        )
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def api_url(client_id: str, floorplan_id: str | None = None) -> str:
    query = {"client_uuid": client_id}
    if floorplan_id:
        query["floorplan_uuid"] = floorplan_id
    query["is_available"] = "true"
    return f"https://{API_HOST}{API_PATH}?{urlencode(query)}"


def validate_raw_row(
    row: dict[str, Any],
    client_id: str,
    floorplan_ids: set[str],
) -> None:
    floorplan = row.get("floor_plan") or {}
    native_id = str(row.get("uuid") or "").casefold()
    unit_number = str(row.get("unit_number") or row.get("unit_identifier") or "")
    floorplan_id = str(floorplan.get("uuid") or "").casefold()
    assert re.fullmatch(UUID_PATTERN, native_id)
    assert unit_number
    assert str(row.get("client_uuid") or "").casefold() == client_id
    assert floorplan_id in floorplan_ids
    assert normalized_address(row.get("building_address")) == normalized_address(ADDRESS)
    assert normalized(row.get("building_city")) == normalized(CITY)
    assert normalized(row.get("building_state")) == normalized(STATE)
    assert str(row.get("building_postal_code") or "") == POSTAL_CODE
    assert raw_rent(row) > 0
    assert str(floorplan.get("name") or "").strip()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("adjusted_available_date") or ""))
    application = urlsplit(str(row.get("application_uri") or ""))
    application_query = parse_qs(application.query)
    assert (application.hostname or "").casefold() == API_HOST
    assert application.path.rstrip("/") == "/screening/application/create"
    assert application_query.get("key") == [client_id]
    assert application_query.get("uk") == [native_id]


def raw_key(row: dict[str, Any]) -> tuple[str, str, int, str, str]:
    floorplan = row.get("floor_plan") or {}
    return (
        str(row.get("uuid") or "").casefold(),
        str(row.get("unit_number") or row.get("unit_identifier") or ""),
        raw_rent(row),
        normalized(floorplan.get("name")),
        str(row.get("adjusted_available_date") or ""),
    )


def e2e_key(row: dict[str, Any]) -> tuple[str, str, int, str, str]:
    source_ids = row.get("source_ids") or {}
    return (
        str(source_ids.get("betternoi_unit_uuid") or "").casefold(),
        str(row.get("unit_number") or ""),
        rent_value(row),
        normalized(row.get("floor_plan_name")),
        str(row.get("availability_date") or ""),
    )


def native_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": {
            "unit_number": str(row.get("unit_number") or ""),
            **(
                row.get("source_ids")
                if isinstance(row.get("source_ids"), dict)
                else {}
            ),
        },
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "availability_date": str(row.get("availability_date") or ""),
        "availability_status": str(row.get("availability_status") or ""),
        "positive_rent_evidence": {
            "market_rent_low": row.get("market_rent_low"),
            "market_rent_high": row.get("market_rent_high"),
        },
        "source_api_url": str(row.get("source_api_url") or ""),
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ledger_before = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    source_before = snapshot(CRITICAL_SOURCE_FILES)
    with REMAINING.open(newline="", encoding="utf-8-sig") as handle:
        remaining_ids = {
            str(row.get("property_id") or "") for row in csv.DictReader(handle)
        }
    assert PROPERTY_ID in remaining_ids

    reset_web_unlocker_call_count()
    configured_body, configured_fetch, configured_headers = direct_fetch(CONFIGURED_URL)
    root_body, root_fetch, _root_headers = direct_fetch(CURRENT_ROOT_URL)
    inventory_body, inventory_fetch, inventory_headers = direct_fetch(
        CURRENT_INVENTORY_URL
    )
    configured_html = configured_body.decode("utf-8", "replace")
    root_html = root_body.decode("utf-8", "replace")
    inventory_html = inventory_body.decode("utf-8", "replace")
    configured_fetch["identity_checks"] = page_identity(configured_html)
    root_fetch["identity_checks"] = page_identity(root_html)
    inventory_fetch["identity_checks"] = page_identity(inventory_html)
    assert configured_fetch["final_url"].rstrip("/") == CURRENT_ROOT_URL.rstrip("/")
    for metadata in (configured_fetch, root_fetch, inventory_fetch):
        checks = metadata["identity_checks"]
        assert checks["current_name_visible"] is True
        assert checks["exact_address_visible"] is True
        assert checks["city_visible"] is True
        assert checks["state_visible"] is True
        assert checks["zip_visible"] is True
        assert checks["configured_name_visible"] is False
    assert inventory_html.count("Bradley_Pointe") >= 7

    pairs = published_pairs(inventory_html)
    clients = {client for client, _floorplan in pairs}
    floorplan_ids = {floorplan for _client, floorplan in pairs}
    assert len(pairs) == 7
    assert len(clients) == 1
    assert len(floorplan_ids) == 7
    client_id = next(iter(clients))

    api_headers = {
        "Origin": "https://liveatwillowrun.com",
        "Referer": CURRENT_INVENTORY_URL,
        "Accept": "application/json",
    }
    all_url = api_url(client_id)
    all_body, all_fetch, _all_headers = direct_fetch(all_url, headers=api_headers)
    all_payload = json.loads(all_body)
    all_rows = [
        row for row in (all_payload.get("results") or []) if isinstance(row, dict)
    ]
    assert int(all_payload.get("count") or 0) == 20
    assert all_payload.get("next") in (None, "")
    assert len(all_rows) == 20
    for row in all_rows:
        validate_raw_row(row, client_id, floorplan_ids)
    assert len({str(row.get("uuid") or "").casefold() for row in all_rows}) == 20
    assert len(
        {
            str(row.get("unit_number") or row.get("unit_identifier") or "").casefold()
            for row in all_rows
        }
    ) == 20

    per_plan_probes: list[dict[str, Any]] = []
    per_plan_captures: list[dict[str, Any]] = []
    per_plan_rows: list[dict[str, Any]] = []
    for floorplan_id in sorted(floorplan_ids):
        plan_url = api_url(client_id, floorplan_id)
        plan_body, plan_fetch, _plan_headers = direct_fetch(
            plan_url, headers=api_headers
        )
        plan_payload = json.loads(plan_body)
        plan_rows = [
            row
            for row in (plan_payload.get("results") or [])
            if isinstance(row, dict)
        ]
        assert plan_rows
        assert int(plan_payload.get("count") or 0) == len(plan_rows)
        assert all(
            str((row.get("floor_plan") or {}).get("uuid") or "").casefold()
            == floorplan_id
            for row in plan_rows
        )
        for row in plan_rows:
            validate_raw_row(row, client_id, floorplan_ids)
        per_plan_rows.extend(plan_rows)
        per_plan_probes.append(
            {
                "floorplan_uuid": floorplan_id,
                "floor_plan_name": str(
                    (plan_rows[0].get("floor_plan") or {}).get("name") or ""
                ),
                "url": plan_url,
                "status": plan_fetch["status"],
                "native_positive_rent_rows": len(plan_rows),
                "unit_numbers": [
                    str(row.get("unit_number") or row.get("unit_identifier") or "")
                    for row in plan_rows
                ],
                "all_rows_exact_address_client_and_floorplan": True,
            }
        )
        capture = archive_json(
            f"betternoi_floorplan_{floorplan_id}.json.gz", plan_payload
        )
        capture["url"] = plan_url
        per_plan_captures.append(capture)
    assert len(per_plan_rows) == 20
    assert {raw_key(row) for row in per_plan_rows} == {
        raw_key(row) for row in all_rows
    }

    configured_e2e = await full_pipeline(
        CONFIGURED_URL,
        configured_body,
        configured_fetch,
        configured_headers,
        property_name=CONFIGURED_NAME,
    )
    stale_name_inventory_e2e = await full_pipeline(
        CURRENT_INVENTORY_URL,
        inventory_body,
        inventory_fetch,
        inventory_headers,
        property_name=CONFIGURED_NAME,
    )
    current_e2e = await full_pipeline(
        CURRENT_INVENTORY_URL,
        inventory_body,
        inventory_fetch,
        inventory_headers,
        property_name=CURRENT_NAME,
    )
    assert strict_rows(configured_e2e) == []
    assert strict_rows(stale_name_inventory_e2e) == []
    current_rows = strict_rows(current_e2e)
    assert current_e2e.get("_adapter_used") == "betternoi_public"
    assert current_e2e.get("extraction_tier_used") == "TIER_1_PUBLIC_BETTERNOI_API"
    assert len(current_rows) == 20
    assert {e2e_key(row) for row in current_rows} == {raw_key(row) for row in all_rows}
    assert all(
        str(row.get("source_property_id") or "") == client_id
        and str((row.get("source_ids") or {}).get("property_id") or "") == client_id
        and str((row.get("source_ids") or {}).get("floor_plan_id") or "")
        in floorplan_ids
        for row in current_rows
    )
    assert web_unlocker_call_count() == 0

    captures = {
        "configured_old_brand_url_redirected_current_root": archive(
            "configured_liveatbradleypointe_redirected_current_root.html.gz",
            configured_body,
        ),
        "current_official_root": archive("current_willow_run_root.html.gz", root_body),
        "current_official_inventory": archive(
            "current_willow_run_floorplans.html.gz", inventory_body
        ),
        "betternoi_unfiltered_client_payload": archive_json(
            "betternoi_unfiltered_client_units.json.gz", all_payload
        ),
        "betternoi_seven_floorplan_payloads": per_plan_captures,
    }
    source_after = snapshot(CRITICAL_SOURCE_FILES)
    ledger_after = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    assert source_before == source_after
    assert ledger_before == ledger_after

    floorplan_names = sorted(
        {str((row.get("floor_plan") or {}).get("name") or "") for row in all_rows}
    )
    statuses = sorted({str(row.get("availability_status") or "") for row in all_rows})
    result = {
        "property_id": int(PROPERTY_ID),
        "property_name": CONFIGURED_NAME,
        "published_current_name": CURRENT_NAME,
        "website": CONFIGURED_URL,
        "current_official_url": CURRENT_INVENTORY_URL,
        "outcome": "UNIT_QUALIFIED",
        "adapter": "betternoi_public",
        "tier": "TIER_1_PUBLIC_BETTERNOI_API",
        "units": len(current_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_old_brand_domain_redirect_current_rebrand_same_address_"
            "single_betternoi_client_seven_published_floorplans_full_pipeline"
        ),
        "identity_evidence": {
            "configured_name": CONFIGURED_NAME,
            "published_current_name": CURRENT_NAME,
            "exact_address": ADDRESS,
            "city_state_zip": f"{CITY}, {STATE} {POSTAL_CODE}",
            "configured_old_brand_url_redirects_to_current_official_root": True,
            "configured_redirect_final_url": configured_fetch["final_url"],
            "configured_redirect_page_identity_checks": configured_fetch[
                "identity_checks"
            ],
            "current_inventory_page_identity_checks": inventory_fetch[
                "identity_checks"
            ],
            "legacy_bradley_pointe_asset_path_occurrences_on_current_page": (
                inventory_html.count("Bradley_Pointe")
            ),
            "betternoi_client_uuid": client_id,
            "published_floorplan_uuids": sorted(floorplan_ids),
            "rows_with_native_identity": len(current_rows),
            "rows_with_native_identity_and_positive_rent": len(current_rows),
            "distinct_unit_numbers": len(
                {str(row.get("unit_number") or "").casefold() for row in current_rows}
            ),
            "distinct_native_betternoi_unit_uuids": len(
                {
                    str((row.get("source_ids") or {}).get("betternoi_unit_uuid") or "")
                    for row in current_rows
                }
            ),
            "source_urls": [all_url],
        },
        "strict_gates": {
            "old_brand_domain_redirects_to_exact_current_official_property": True,
            "exact_same_street_city_state_zip_before_and_after_rebrand": True,
            "current_page_publishes_legacy_bradley_pointe_assets": True,
            "one_and_only_one_page_published_betternoi_client": True,
            "seven_unique_page_published_floorplan_uuids": True,
            "all_provider_rows_exact_client_address_and_published_floorplan": True,
            "all_rows_unique_native_betternoi_uuid": True,
            "all_rows_unique_native_unit_number": True,
            "all_rows_positive_row_level_rent": True,
            "unfiltered_and_seven_per_plan_api_row_sets_match": True,
            "current_name_full_pipeline_20_native_positive_rent_rows": True,
            "direct_api_and_full_pipeline_unit_uuid_rent_plan_date_match": True,
            "stale_name_current_inventory_full_pipeline_zero_native_rows": True,
            "no_llm": True,
            "no_unlocker": True,
            "no_captcha_solver": True,
            "no_fingerprint_rotation": True,
        },
        "configured_source_validation": {
            "configured_fetch": configured_fetch,
            "configured_name_full_pipeline": compact_e2e(configured_e2e),
            "current_inventory_with_stale_name_full_pipeline": compact_e2e(
                stale_name_inventory_e2e
            ),
            "current_inventory_with_current_name_full_pipeline": compact_e2e(
                current_e2e
            ),
            "minimal_production_lever": (
                "update canonical URL to the current Willow Run inventory page and "
                "record Willow Run as the current published name/alias for the same "
                "1355 Bradley Blvd physical property"
            ),
        },
        "seven_floorplan_direct_probes": per_plan_probes,
        "floorplan_name_and_date_semantics": {
            "exact_native_floorplan_names": floorplan_names,
            "exact_native_floorplan_uuids": sorted(floorplan_ids),
            "explicit_adjusted_available_date_rows": sum(
                bool(row.get("adjusted_available_date")) for row in all_rows
            ),
            "native_availability_statuses": statuses,
            "floorplan_names_are_provider_floor_plan_name_values": True,
            "availability_dates_are_provider_adjusted_available_date_values": True,
            "dates_are_not_derived_from_application_selection": True,
        },
        "contamination_negative_checks": {
            "provider_client_uuids": [client_id],
            "provider_addresses": [ADDRESS],
            "provider_cities": [CITY],
            "provider_states": [STATE],
            "provider_postal_codes": [POSTAL_CODE],
            "provider_floorplan_uuids_minus_page_published_floorplan_uuids": [],
            "sibling_rows_admitted": 0,
            "duplicate_native_ids_admitted": 0,
            "direct_vs_full_pipeline_row_set_difference": [],
        },
        "native_samples": [native_sample(row) for row in current_rows[:10]],
        "native_rows": current_rows,
        "current_capture": {
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "configured_fetch": configured_fetch,
            "current_root_fetch": root_fetch,
            "current_inventory_fetch": inventory_fetch,
            "unfiltered_api_fetch": all_fetch,
            "captures": captures,
            "transport_policy": {
                "compliance_mode": True,
                "llm_calls": 0,
                "web_unlocker_calls": web_unlocker_call_count(),
                "captcha_interactions": 0,
                "flaresolverr": False,
                "fingerprint_rotation": False,
                "paid_canary_run": False,
            },
        },
        "ledger_snapshot": {
            "before": ledger_before,
            "after": ledger_after,
            "unchanged_during_materialization": True,
        },
        "source_snapshot": {
            "before": source_before,
            "after": source_after,
            "unchanged_during_materialization": True,
        },
    }
    payload = {
        "summary": {
            "result_type": "old_brand_to_current_rebrand_betternoi_strict",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [int(PROPERTY_ID)],
            "native_positive_rent_rows": len(current_rows),
            "floorplan_direct_probes": len(per_plan_probes),
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "unlocker": False,
            "proxies": {},
            "hyperbrowser": False,
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": [result],
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256_path(EVIDENCE),
                "property_id": int(PROPERTY_ID),
                "native_positive_rent_rows": len(current_rows),
                "floorplan_direct_probes": len(per_plan_probes),
                "web_unlocker_calls": web_unlocker_call_count(),
                "ledger_unchanged": ledger_before == ledger_after,
                "source_unchanged": source_before == source_after,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
