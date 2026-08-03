#!/usr/bin/env python3
"""Materialize strict current Clear Run evidence from exact official sources."""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from bs4 import BeautifulSoup


os.environ["COMPLIANCE_MODE"] = "1"
os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""

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
from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    parse_securecafe_availableunits,
)
from ma_poc.pms.scraper import scrape  # noqa: E402


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "clear_run_gables_lane"
EVIDENCE = OUT / "evidence_clear_run_4756_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"

PROPERTY_ID = "4756"
PROPERTY_NAME = "Clear Run"
RENTCAFE_PROPERTY_ID = "2430886"
SIGHTMAP_ID = "123453"
CONFIGURED_URL = "https://www.clearrunaptswilmington.com/"
EXACT_SOURCE_URL = f"{CONFIGURED_URL}community/{RENTCAFE_PROPERTY_ID}"
SECURECAFE_URL = (
    "https://gables.securecafe.com/onlineleasing/clear-run1/"
    f"availableunits.aspx?myOlePropertyId={RENTCAFE_PROPERTY_ID}"
)
CANONICAL_ADDRESS = "5300 New Centre Dr"
PUBLISHED_ADDRESS = "5300 New Centre Drive"
CITY = "Wilmington"
STATE = "NC"
POSTAL_CODE = "28403"
PLAN_PROBE_IDS = ("6528295", "6528296", "6528293")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


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


def direct_fetch(url: str) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    response = probe_get(
        url,
        timeout=35,
        unlocker=False,
        retries=2,
        proxies={},
        verify=True,
    )
    body = bytes(response.content or b"")
    text = body.decode("utf-8", "replace")
    final_url = str(response.url or url)
    status = int(response.status_code or 0)
    challenge = bool(
        re.search(
            r"just a moment|verify you are human|checking your browser|cf-chl-",
            text,
            re.I,
        )
    )
    assert status == 200
    assert body
    assert challenge is False
    return (
        body,
        {
            "requested_url": url,
            "status": status,
            "final_url": final_url,
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
        },
        {str(key).lower(): str(value) for key, value in response.headers.items()},
    )


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


def fetch_result(url: str, body: bytes, meta: dict[str, Any], headers: dict[str, str]) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=int(meta["status"]),
        body=body,
        headers=headers,
        render_mode=RenderMode.GET,
        final_url=str(meta["final_url"]),
        attempts=1,
        elapsed_ms=0,
    )


async def full_pipeline(
    url: str,
    body: bytes,
    meta: dict[str, Any],
    headers: dict[str, str],
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
        fetch_result=fetch_result(url, body, meta, headers),
        csv_row={
            "apartmentid": PROPERTY_ID,
            "name": PROPERTY_NAME,
            "address": CANONICAL_ADDRESS,
            "city": CITY,
            "state": STATE,
            "zip": POSTAL_CODE,
            "website": CONFIGURED_URL,
        },
        property_id=PROPERTY_ID,
        shared_budget=budget,
    )
    result["_validation_budget"] = budget
    return result


def parse_action(onclick: str) -> dict[str, str]:
    match = re.search(r"rentaloptions\.aspx\?([^'\"]+)", onclick, re.I)
    assert match, onclick
    query = parse_qs(match.group(1).replace("&amp;", "&"))
    lowered = {key.casefold(): values for key, values in query.items()}
    required = ("unitid", "floorplanid", "myolepropertyid", "moveindate")
    assert all(lowered.get(key) and lowered[key][0] for key in required)
    return {key: str(lowered[key][0]) for key in required}


def parse_securecafe_rows(html: str, source_url: str) -> list[dict[str, Any]]:
    parsed = parse_securecafe_availableunits(html, source_url)
    dom_rows = BeautifulSoup(html, "lxml").select("tr.AvailUnitRow")
    assert len(parsed) == len(dom_rows) > 0
    native_rows: list[dict[str, Any]] = []
    for row, dom_row in zip(parsed, dom_rows, strict=True):
        apartment = dom_row.select_one('[data-label="Apartment"]')
        action = dom_row.select_one('input[onclick*="rentaloptions.aspx"]')
        assert apartment is not None and action is not None
        unit_number = " ".join(apartment.stripped_strings).lstrip("#").strip()
        assert unit_number == str(row.get("unit_number") or "")
        action_data = parse_action(str(action.get("onclick") or ""))
        source_ids = dict(row.get("source_ids") or {})
        assert action_data["floorplanid"] == str(
            source_ids.get("securecafe_floorplan_id") or ""
        )
        assert action_data["myolepropertyid"] == RENTCAFE_PROPERTY_ID
        source_ids.update(
            {
                "rentcafe_property_id": RENTCAFE_PROPERTY_ID,
                "rentcafe_unit_id": action_data["unitid"],
            }
        )
        materialized = dict(row)
        materialized["source_ids"] = source_ids
        materialized["securecafe_selection_move_in_date"] = datetime.strptime(
            action_data["moveindate"], "%m/%d/%Y"
        ).date().isoformat()
        assert unit_has_real_anchor(materialized)
        assert positive_rent(materialized)
        native_rows.append(materialized)
    assert len({row["unit_number"].casefold() for row in native_rows}) == len(native_rows)
    assert len(
        {str(row["source_ids"]["rentcafe_unit_id"]) for row in native_rows}
    ) == len(native_rows)
    return native_rows


def rent_counter(rows: list[dict[str, Any]]) -> Counter[tuple[int, int]]:
    return Counter(
        (int(row["market_rent_low"]), int(row["market_rent_high"]))
        for row in rows
    )


def ssr_rent_counter(plan: dict[str, Any]) -> Counter[tuple[int, int]]:
    return Counter(
        (int(row["minimumRent"]), int(row["maximumRent"]))
        for row in plan.get("units") or []
    )


def compact_e2e(result: dict[str, Any], strict_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "adapter": result.get("_adapter_used"),
        "detected_pms": result.get("_detected_pms"),
        "tier": result.get("extraction_tier_used"),
        "winning_url": result.get("winning_url"),
        "errors": result.get("errors") or [],
        "raw_units": len([row for row in result.get("units") or [] if isinstance(row, dict)]),
        "strict_native_positive_rent_rows": len(strict_rows),
        "distinct_native_unit_numbers": len(
            {str(row.get("unit_number") or "").casefold() for row in strict_rows}
        ),
        "source_urls": sorted(
            {
                str(row.get("source_api_url") or "")
                for row in strict_rows
                if str(row.get("source_api_url") or "")
            }
        ),
        "validation_budget": result.get("_validation_budget"),
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ledger_before = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    with REMAINING.open(newline="", encoding="utf-8-sig") as handle:
        assert any(row.get("property_id") == PROPERTY_ID for row in csv.DictReader(handle))

    reset_web_unlocker_call_count()
    configured_body, configured_fetch, configured_headers = direct_fetch(CONFIGURED_URL)
    exact_body, exact_fetch, exact_headers = direct_fetch(EXACT_SOURCE_URL)
    inventory_body, inventory_fetch, _inventory_headers = direct_fetch(SECURECAFE_URL)

    configured_text = BeautifulSoup(configured_body, "lxml").get_text(" ", strip=True)
    assert normalized(PROPERTY_NAME) not in normalized(configured_text)
    assert "Gables" in configured_text

    exact_soup = BeautifulSoup(exact_body, "lxml")
    next_node = exact_soup.find("script", id="__NEXT_DATA__")
    assert next_node is not None and next_node.string
    next_data = json.loads(next_node.string)
    assert next_data.get("page") == "/community/[id]"
    assert str((next_data.get("query") or {}).get("id")) == RENTCAFE_PROPERTY_ID
    page_props = next_data["props"]["pageProps"]
    community = page_props["community"]
    entry = page_props["communityEntry"]
    third_party = entry["third_party"]
    assert str(community["id"]) == RENTCAFE_PROPERTY_ID
    assert community["name"] == PROPERTY_NAME
    assert community["address"] == PUBLISHED_ADDRESS
    assert community["city"] == CITY
    assert community["state"] == STATE
    assert community["zipCode"] == POSTAL_CODE
    assert entry["title"] == PROPERTY_NAME
    assert str(third_party["rentcafe_property_id"]) == RENTCAFE_PROPERTY_ID
    assert str(third_party["engrain_sightmap_id"]) == SIGHTMAP_ID
    assert CONFIGURED_URL in entry["custom_domains"]

    floorplans = page_props["floorPlanDetails"]
    floorplans_by_id = {str(plan["floorplanId"]): plan for plan in floorplans}
    assert len(floorplans) == len(floorplans_by_id) == 24
    for floorplan_id, plan in floorplans_by_id.items():
        assert str(plan["propertyId"]) == RENTCAFE_PROPERTY_ID
        expected_url = f"{SECURECAFE_URL}&floorPlans={floorplan_id}"
        assert plan["availabilityURL"] == expected_url

    inventory_html = inventory_body.decode("utf-8", "replace")
    inventory_soup = BeautifulSoup(inventory_html, "lxml")
    assert inventory_soup.title is not None
    assert "Clear Run" in inventory_soup.title.get_text(" ", strip=True)
    native_rows = parse_securecafe_rows(inventory_html, SECURECAFE_URL)
    assert len(native_rows) == 50
    securecafe_plan_ids = {
        str(row["source_ids"]["securecafe_floorplan_id"]) for row in native_rows
    }
    published_plan_ids = {
        floorplan_id
        for floorplan_id, plan in floorplans_by_id.items()
        if int(plan["availableUnitsCount"] or 0) > 0
    }
    assert securecafe_plan_ids == published_plan_ids
    assert sum(int(plan["availableUnitsCount"] or 0) for plan in floorplans) == 50

    exact_floorplan_join: list[dict[str, Any]] = []
    for floorplan_id in sorted(securecafe_plan_ids, key=int):
        plan = floorplans_by_id[floorplan_id]
        rows = [
            row
            for row in native_rows
            if str(row["source_ids"]["securecafe_floorplan_id"]) == floorplan_id
        ]
        count_match = len(rows) == int(plan["availableUnitsCount"])
        name_match = all(row["floor_plan_name"] == plan["floorplanName"] for row in rows)
        rent_multiset_match = rent_counter(rows) == ssr_rent_counter(plan)
        assert count_match and name_match and rent_multiset_match
        exact_floorplan_join.append(
            {
                "securecafe_floorplan_id": floorplan_id,
                "floorplan_name": plan["floorplanName"],
                "native_rows": len(rows),
                "gables_available_units_count": int(plan["availableUnitsCount"]),
                "count_match": count_match,
                "exact_name_match": name_match,
                "positive_rent_multiset_match": rent_multiset_match,
                "property_id": str(plan["propertyId"]),
                "availability_url": plan["availabilityURL"],
            }
        )

    plan_probe_results: list[dict[str, Any]] = []
    plan_captures: list[dict[str, Any]] = []
    for floorplan_id in PLAN_PROBE_IDS:
        plan_url = f"{SECURECAFE_URL}&floorPlans={floorplan_id}"
        plan_body, plan_fetch, _plan_headers = direct_fetch(plan_url)
        plan_rows = parse_securecafe_rows(plan_body.decode("utf-8", "replace"), plan_url)
        full_rows = [
            row
            for row in native_rows
            if str(row["source_ids"]["securecafe_floorplan_id"]) == floorplan_id
        ]
        assert {row["unit_number"] for row in plan_rows} == {
            row["unit_number"] for row in full_rows
        }
        assert rent_counter(plan_rows) == rent_counter(full_rows)
        assert all(
            str(row["source_ids"]["securecafe_floorplan_id"]) == floorplan_id
            and str(row["source_ids"]["rentcafe_property_id"])
            == RENTCAFE_PROPERTY_ID
            for row in plan_rows
        )
        plan_probe_results.append(
            {
                "securecafe_floorplan_id": floorplan_id,
                "floorplan_name": floorplans_by_id[floorplan_id]["floorplanName"],
                "url": plan_url,
                "status": plan_fetch["status"],
                "native_positive_rent_rows": len(plan_rows),
                "unit_numbers": [row["unit_number"] for row in plan_rows],
                "matches_full_inventory_exact_subset": True,
            }
        )
        plan_captures.append(
            archive(f"direct_securecafe_plan_{floorplan_id}_current.html.gz", plan_body)
        )

    configured_e2e = await full_pipeline(
        CONFIGURED_URL,
        configured_body,
        configured_fetch,
        configured_headers,
    )
    exact_e2e = await full_pipeline(
        EXACT_SOURCE_URL,
        exact_body,
        exact_fetch,
        exact_headers,
    )
    configured_rows = [
        row for row in configured_e2e.get("units") or [] if isinstance(row, dict)
    ]
    e2e_rows = [
        row
        for row in exact_e2e.get("units") or []
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]
    assert len(configured_rows) == 0
    assert exact_e2e.get("_adapter_used") == "sightmap"
    assert exact_e2e.get("extraction_tier_used") == "TIER_1_API_SIGHTMAP_DIRECT"
    assert len(e2e_rows) == 50
    assert len({str(row["unit_number"]).casefold() for row in e2e_rows}) == 50
    sightmap_source_urls = {
        str(row.get("source_api_url") or "") for row in e2e_rows
    }
    assert len(sightmap_source_urls) == 1
    assert all(f"/sightmaps/{SIGHTMAP_ID}" in url for url in sightmap_source_urls)
    assert len(
        {
            str((row.get("source_ids") or {}).get("sightmap_unit_id") or "")
            for row in e2e_rows
        }
    ) == 50

    securecafe_by_unit = {str(row["unit_number"]): row for row in native_rows}
    sightmap_by_unit = {str(row["unit_number"]): row for row in e2e_rows}
    assert set(securecafe_by_unit) == set(sightmap_by_unit)
    assert all(
        int(sightmap_by_unit[unit]["market_rent_low"])
        == int(securecafe_by_unit[unit]["market_rent_low"])
        for unit in securecafe_by_unit
    )

    sightmap_date_plans_exact = 0
    for floorplan_id in securecafe_plan_ids:
        plan = floorplans_by_id[floorplan_id]
        units = [
            str(row["unit_number"])
            for row in native_rows
            if str(row["source_ids"]["securecafe_floorplan_id"]) == floorplan_id
        ]
        sightmap_dates = Counter(
            (
                int(sightmap_by_unit[unit]["market_rent_low"]),
                str(sightmap_by_unit[unit].get("availability_date") or ""),
            )
            for unit in units
        )
        gables_dates = Counter(
            (int(row["minimumRent"]), str(row["availableDate"]))
            for row in plan.get("units") or []
        )
        sightmap_date_plans_exact += int(sightmap_dates == gables_dates)

    selection_floor = min(
        date.fromisoformat(str(row["securecafe_selection_move_in_date"]))
        for row in native_rows
    )
    clamped_selection_plans_exact = 0
    raw_selection_plans_exact = 0
    for floorplan_id in securecafe_plan_ids:
        plan = floorplans_by_id[floorplan_id]
        rows = [
            row
            for row in native_rows
            if str(row["source_ids"]["securecafe_floorplan_id"]) == floorplan_id
        ]
        observed = Counter(
            (
                int(row["market_rent_low"]),
                int(row["market_rent_high"]),
                str(row["securecafe_selection_move_in_date"]),
            )
            for row in rows
        )
        published = Counter(
            (
                int(row["minimumRent"]),
                int(row["maximumRent"]),
                str(row["availableDate"]),
            )
            for row in plan.get("units") or []
        )
        clamped = Counter(
            (
                int(row["minimumRent"]),
                int(row["maximumRent"]),
                max(date.fromisoformat(str(row["availableDate"])), selection_floor).isoformat(),
            )
            for row in plan.get("units") or []
        )
        raw_selection_plans_exact += int(observed == published)
        clamped_selection_plans_exact += int(observed == clamped)
    assert clamped_selection_plans_exact == len(securecafe_plan_ids)

    floorplan_name_exact_rows = sum(
        normalized(str(sightmap_by_unit[unit].get("floor_plan_name") or ""))
        == normalized(str(securecafe_by_unit[unit].get("floor_plan_name") or ""))
        for unit in securecafe_by_unit
    )
    sightmap_temp_name_rows = sum(
        normalized(str(row.get("floor_plan_name") or "")) == "temp"
        for row in e2e_rows
    )

    assert web_unlocker_call_count() == 0
    captures = {
        "configured_root": archive("direct_configured_root_current.html.gz", configured_body),
        "exact_same_origin_property": archive(
            "direct_exact_same_origin_2430886_current.html.gz", exact_body
        ),
        "securecafe_full_inventory": archive(
            "direct_securecafe_2430886_current.html.gz", inventory_body
        ),
        "securecafe_plan_probes": plan_captures,
    }
    ledger_after = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    assert ledger_before == ledger_after

    result = {
        "property_id": int(PROPERTY_ID),
        "property_name": PROPERTY_NAME,
        "website": CONFIGURED_URL,
        "outcome": "UNIT_QUALIFIED",
        "adapter": "sightmap",
        "tier": "TIER_1_API_SIGHTMAP_DIRECT",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_same_origin_gables_community_2430886_securecafe_"
            "floorplan_id_row_rent_full_sightmap_e2e_unit_set"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "published_address": PUBLISHED_ADDRESS,
            "city_state_zip": f"{CITY}, {STATE} {POSTAL_CODE}",
            "current_exact_same_origin_name_address_zip_match": True,
            "rentcafe_property_id": RENTCAFE_PROPERTY_ID,
            "sightmap_id": SIGHTMAP_ID,
            "custom_domain_exact_match": True,
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "distinct_unit_numbers": len(securecafe_by_unit),
            "distinct_rentcafe_unit_ids": len(
                {
                    str(row["source_ids"]["rentcafe_unit_id"])
                    for row in native_rows
                }
            ),
            "source_urls": [SECURECAFE_URL, *sorted(sightmap_source_urls)],
        },
        "strict_gates": {
            "exact_property_2430886": True,
            "same_origin_current_property_route": True,
            "securecafe_rows_all_exact_property_2430886": True,
            "securecafe_floorplan_ids_all_exact_gables_floorplan_id_joins": True,
            "all_17_available_floorplans_count_name_rent_multiset_match": True,
            "three_plan_direct_probes_match_full_inventory": True,
            "unique_native_unit_numbers": True,
            "unique_rentcafe_unit_ids": True,
            "positive_row_level_securecafe_rent": True,
            "full_scraper_exact_route_50_native_positive_rent_rows": True,
            "full_scraper_and_securecafe_unit_sets_identical": True,
            "full_scraper_and_securecafe_low_rents_identical_by_unit_number": True,
            "no_llm": True,
            "no_unlocker": True,
            "no_captcha_solver": True,
            "no_fingerprint_rotation": True,
        },
        "configured_source_validation": {
            "configured_root": compact_e2e(configured_e2e, []),
            "configured_root_limitation": (
                "corporate Gables homepage contains no Clear Run identity or provider link"
            ),
            "exact_same_origin_property_route": compact_e2e(exact_e2e, e2e_rows),
            "minimal_production_lever": (
                "update the canonical property URL to /community/2430886 or add a deterministic "
                "Gables custom-domain-to-community route resolution step"
            ),
        },
        "exact_floorplan_id_join_validation": exact_floorplan_join,
        "three_plan_direct_probes": plan_probe_results,
        "independent_source_crosscheck": {
            "securecafe_native_rows": len(native_rows),
            "sightmap_full_pipeline_native_rows": len(e2e_rows),
            "identical_native_unit_number_set": True,
            "identical_low_rent_by_native_unit_number": True,
            "securecafe_exact_floorplan_names_equal_gables_ssr_rows": len(native_rows),
            "sightmap_floorplan_names_exactly_equal_securecafe_rows": floorplan_name_exact_rows,
            "sightmap_temp_floorplan_name_rows": sightmap_temp_name_rows,
        },
        "availability_date_semantics": {
            "gables_ssr_field": "floorPlanDetails[].units[].availableDate",
            "sightmap_full_pipeline_field": "availability_date tied to native unit_number",
            "securecafe_action_field": "MoveInDate",
            "securecafe_action_semantics": (
                "earliest selectable application move-in date, not raw historical availability date"
            ),
            "securecafe_selection_floor_observed": selection_floor.isoformat(),
            "available_floorplans": len(securecafe_plan_ids),
            "securecafe_raw_move_in_multiset_equal_gables_available_date_plans": (
                raw_selection_plans_exact
            ),
            "securecafe_move_in_equal_gables_date_after_selection_floor_clamp_plans": (
                clamped_selection_plans_exact
            ),
            "sightmap_native_date_multiset_equal_gables_ssr_plans": sightmap_date_plans_exact,
            "materialized_securecafe_rows_leave_availability_date_unassigned": True,
        },
        "contamination_negative_checks": {
            "page_query_id_exact": True,
            "page_community_id_exact": True,
            "all_floorplan_property_ids": [RENTCAFE_PROPERTY_ID],
            "all_securecafe_action_property_ids": [RENTCAFE_PROPERTY_ID],
            "all_sightmap_source_ids": [SIGHTMAP_ID],
            "discarded_sibling_search_card_names": sorted(
                {
                    str(row.get("name") or "")
                    for row in page_props.get("communities") or []
                    if str(row.get("name") or "") != PROPERTY_NAME
                }
            ),
            "sibling_rows_admitted": 0,
            "securecafe_and_sightmap_unit_set_difference": [],
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": str(row["unit_number"]),
                    "rentcafe_unit_id": str(row["source_ids"]["rentcafe_unit_id"]),
                    "securecafe_floorplan_id": str(
                        row["source_ids"]["securecafe_floorplan_id"]
                    ),
                    "rentcafe_property_id": RENTCAFE_PROPERTY_ID,
                },
                "floor_plan_name": row["floor_plan_name"],
                "positive_rent_evidence": {
                    "market_rent_low": row["market_rent_low"],
                    "market_rent_high": row["market_rent_high"],
                },
                "securecafe_selection_move_in_date": row[
                    "securecafe_selection_move_in_date"
                ],
                "source_api_url": row["source_api_url"],
            }
            for row in native_rows[:10]
        ],
        "native_rows": native_rows,
        "current_capture": {
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "configured_fetch": configured_fetch,
            "exact_same_origin_fetch": exact_fetch,
            "securecafe_inventory_fetch": inventory_fetch,
            "captures": captures,
            "transport_policy": {
                "compliance_mode": True,
                "llm_calls": 0,
                "web_unlocker_calls": web_unlocker_call_count(),
                "captcha_interactions": 0,
                "fingerprint_rotation": False,
                "paid_canary_run": False,
            },
        },
        "ledger_snapshot": {
            "before": ledger_before,
            "after": ledger_after,
            "unchanged_during_materialization": True,
        },
    }
    payload = {
        "summary": {
            "result_type": "clear_run_exact_same_origin_gables_securecafe_sightmap_current_strict",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [int(PROPERTY_ID)],
            "native_positive_rent_rows": len(native_rows),
            "exact_floorplan_id_joins": len(exact_floorplan_join),
            "plan_direct_probes": len(plan_probe_results),
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "unlocker": False,
            "proxies": {},
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
                "native_positive_rent_rows": len(native_rows),
                "exact_floorplan_id_joins": len(exact_floorplan_join),
                "plan_direct_probes": len(plan_probe_results),
                "full_e2e_rows": len(e2e_rows),
                "floorplan_name_exact_rows_sightmap_vs_securecafe": floorplan_name_exact_rows,
                "sightmap_temp_floorplan_name_rows": sightmap_temp_name_rows,
                "web_unlocker_calls": web_unlocker_call_count(),
                "ledger_unchanged": ledger_before == ledger_after,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
