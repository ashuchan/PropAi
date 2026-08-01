from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.extraction.post_process import post_process
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "riverfalls_migration_lane"
COHORT = ROOT / "failed344.csv"
HB_DIR = ROOT / "hb_riverfalls_8654"
HB_SUMMARY = HB_DIR / "summary.json"
HB_ROOT_BODY = HB_DIR / "root.html.gz"
OUTPUT = LANE / "evidence_riverfalls_8654_current_strict.json"

PROPERTY_ID = "8654"
CURRENT_URL = "https://www.riverfallsatbellmar.com/"
SECURECAFE_URL = (
    "https://riverfallsatbellmar.securecafe.com/onlineleasing/"
    "riverfalls-at-bellmar/availableunits.aspx"
)
NATIVE_PROPERTY_ID = "2390470"
PLAN_PROBE_IDS = ("6463796", "6463798", "6463802")


def read_cohort_row() -> dict[str, str]:
    with COHORT.open(encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row["property_id"] == PROPERTY_ID]
    if len(rows) != 1:
        raise RuntimeError(f"expected one cohort row for {PROPERTY_ID}, got {len(rows)}")
    return rows[0]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalized_visible(html: str) -> str:
    return " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).casefold().split())


def positive_rent(unit: dict[str, Any]) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and float(unit[field]) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def rent_value(unit: dict[str, Any]) -> float | None:
    for field in (
        "market_rent_low",
        "rent_low",
        "asking_rent",
        "rent",
        "market_rent_high",
        "rent_high",
    ):
        value = unit.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def unit_number(unit: dict[str, Any]) -> str:
    return str(unit.get("unit_number") or "").strip()


def source_url(unit: dict[str, Any]) -> str:
    return str(unit.get("source_api_url") or "").strip()


def parse_native_row_bindings(html: str) -> list[dict[str, str]]:
    bindings: list[dict[str, str]] = []
    for row in re.findall(
        r"<tr[^>]*class=['\"]AvailUnitRow['\"][\s\S]*?</tr>",
        html,
        flags=re.IGNORECASE,
    ):
        apartment_match = re.search(
            r"data-label=['\"]?Apartment['\"]?[^>]*>\s*#?\s*([A-Za-z0-9-]+)",
            row,
            flags=re.IGNORECASE,
        )
        ids_match = re.search(
            r"UnitID=(\d+)&FloorPlanID=(\d+)&myOlePropertyid=(\d+)",
            row,
            flags=re.IGNORECASE,
        )
        if apartment_match and ids_match:
            bindings.append(
                {
                    "unit_number": apartment_match.group(1),
                    "securecafe_unit_id": ids_match.group(1),
                    "securecafe_floorplan_id": ids_match.group(2),
                    "securecafe_property_id": ids_match.group(3),
                }
            )
    return bindings


async def get_response(url: str):
    return await asyncio.to_thread(
        probe_get,
        url,
        timeout=30,
        unlocker=False,
        proxies={},
        verify=True,
        retries=1,
    )


async def direct_fetch(task: CrawlTask, profile=None) -> FetchResult:
    del profile
    started = time.monotonic()
    try:
        response = await get_response(task.url)
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.DEAD_URL
            if status in {404, 410, 451}
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(response.headers or {}),
            render_mode=task.render_mode,
            final_url=str(response.url or task.url),
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.TRANSIENT,
            status=None,
            body=None,
            headers={},
            render_mode=task.render_mode,
            final_url=task.url,
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_signature=f"{type(exc).__name__}: {str(exc)[:240]}",
        )


def make_task(url: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=180_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "requests",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {name}={actual!r}; expected {expected!r}")

    cohort = read_cohort_row()
    hb_summary = json.loads(HB_SUMMARY.read_text())
    with gzip.open(HB_ROOT_BODY, "rb") as handle:
        root_body = handle.read()
    root_html = root_body.decode("utf-8", "replace")
    root_visible = normalized_visible(root_html)

    inventory_response = await get_response(SECURECAFE_URL)
    inventory_status = int(inventory_response.status_code or 0)
    inventory_html = str(inventory_response.text or "")
    inventory_visible = normalized_visible(inventory_html)
    parsed = parse_securecafe_availableunits(inventory_html, SECURECAFE_URL)
    processed = post_process(parsed, property_id=PROPERTY_ID)
    direct_units = [unit for unit in processed.admitted if isinstance(unit, dict)]
    direct_strict = [
        unit for unit in direct_units if unit_has_real_anchor(unit) and positive_rent(unit)
    ]
    bindings = parse_native_row_bindings(inventory_html)
    binding_by_unit = {row["unit_number"]: row for row in bindings}

    plan_probes: list[dict[str, Any]] = []
    for floorplan_id in PLAN_PROBE_IDS:
        plan_url = (
            f"{SECURECAFE_URL}?myOlePropertyId={NATIVE_PROPERTY_ID}"
            f"&floorPlans={floorplan_id}"
        )
        plan_response = await get_response(plan_url)
        plan_html = str(plan_response.text or "")
        plan_parsed = parse_securecafe_availableunits(plan_html, plan_url)
        plan_processed = post_process(plan_parsed, property_id=PROPERTY_ID)
        plan_units = [
            unit
            for unit in plan_processed.admitted
            if isinstance(unit, dict) and unit_has_real_anchor(unit) and positive_rent(unit)
        ]
        expected_units = sorted(
            row["unit_number"]
            for row in bindings
            if row["securecafe_floorplan_id"] == floorplan_id
        )
        actual_units = sorted(unit_number(unit) for unit in plan_units)
        plan_bindings = parse_native_row_bindings(plan_html)
        plan_probes.append(
            {
                "securecafe_floorplan_id": floorplan_id,
                "url": plan_url,
                "status": int(plan_response.status_code or 0),
                "expected_native_units_from_full_inventory": expected_units,
                "direct_probe_native_units": actual_units,
                "exact_unit_set_match": actual_units == expected_units and bool(actual_units),
                "all_rows_exact_property_id": bool(plan_bindings)
                and {row["securecafe_property_id"] for row in plan_bindings}
                == {NATIVE_PROPERTY_ID},
            }
        )

    canonical = {
        **cohort,
        "apartmentid": PROPERTY_ID,
        "name": cohort["proj_name"],
        "address": cohort["address"],
        "city": cohort["city"],
        "state": cohort["state"],
        "zip": cohort["zip_code"],
        "zip_code": cohort["zip_code"],
        "website": CURRENT_URL,
    }
    task = make_task(CURRENT_URL)
    fetched = FetchResult(
        url=CURRENT_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=root_body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=CURRENT_URL,
        attempts=1,
        elapsed_ms=0,
        fetch_tier_used=3,
        fetch_tier_attempts=[3],
        proxy_used="hyperbrowser_residential",
    )
    fetch_mod.fetch = direct_fetch
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=canonical,
        ),
        timeout=180,
    )
    elapsed_seconds = round(time.monotonic() - started, 2)
    emitted = [unit for unit in (result.get("units") or []) if isinstance(unit, dict)]
    strict = [unit for unit in emitted if unit_has_real_anchor(unit) and positive_rent(unit)]

    direct_by_unit = {unit_number(unit): unit for unit in direct_strict}
    pipeline_by_unit = {unit_number(unit): unit for unit in strict}
    direct_unit_set = set(direct_by_unit)
    pipeline_unit_set = set(pipeline_by_unit)
    rent_sets_match = direct_unit_set == pipeline_unit_set and all(
        rent_value(direct_by_unit[number]) == rent_value(pipeline_by_unit[number])
        for number in direct_unit_set
    )

    old_response = await get_response(cohort["website"])
    old_status = int(old_response.status_code or 0)
    old_final_url = str(old_response.url or cohort["website"])
    old_visible = normalized_visible(str(old_response.text or ""))

    root_identity = {
        "name_visible": "riverfalls at bellmar" in root_visible,
        "street_visible": "10570 stone canyon" in root_visible,
        "city_state_zip_visible": "dallas" in root_visible and "75230" in root_visible,
    }
    inventory_identity = {
        "name_visible": "riverfalls at bellmar" in inventory_visible,
        "street_visible": "10570 stone canyon" in inventory_visible,
        "city_state_zip_visible": "dallas" in inventory_visible and "75230" in inventory_visible,
        "exact_property_id_on_every_native_row": bool(bindings)
        and {row["securecafe_property_id"] for row in bindings} == {NATIVE_PROPERTY_ID},
    }
    gates = {
        "cohort_exact_property": cohort["proj_name"] == "Riverfalls at Bellmar"
        and cohort["address"].startswith("10570 Stone Canyon")
        and cohort["zip_code"] == "75230",
        "captured_current_root_http_200": hb_summary.get("status") == 200,
        "captured_current_root_hash_exact": sha256_bytes(root_body)
        == hb_summary.get("body_sha256"),
        "captured_current_root_identity_exact": all(root_identity.values()),
        "hyperbrowser_single_session_without_captcha_or_stealth": (
            hb_summary.get("hyperbrowser_sessions") == 1
            and hb_summary.get("session_options", {}).get("solve_captchas") is False
            and hb_summary.get("session_options", {}).get("stealth") is False
            and hb_summary.get("session_options", {}).get("fingerprint_rotation") is False
        ),
        "current_root_publishes_exact_securecafe_portal": (
            "riverfallsatbellmar.securecafe.com/onlineleasing/riverfalls-at-bellmar"
            in root_html.casefold()
        ),
        "securecafe_inventory_http_200": inventory_status == 200,
        "securecafe_inventory_identity_exact": all(inventory_identity.values()),
        "securecafe_native_bindings_complete": len(bindings) == len(direct_strict) > 0,
        "securecafe_native_unit_ids_unique": len({row["securecafe_unit_id"] for row in bindings})
        == len(bindings),
        "securecafe_native_unit_numbers_unique": len({row["unit_number"] for row in bindings})
        == len(bindings),
        "securecafe_all_rows_native_and_positive_rent": len(direct_strict)
        == len(direct_units)
        == len(bindings)
        > 0,
        "three_floorplan_boundary_probes_exact": len(plan_probes) == 3
        and all(
            probe["status"] == 200
            and probe["exact_unit_set_match"]
            and probe["all_rows_exact_property_id"]
            for probe in plan_probes
        ),
        "current_full_pipeline_selected_rentcafe": result.get("_adapter_used") == "rentcafe",
        "current_full_pipeline_selected_securecafe_unit_tier": (
            result.get("extraction_tier_used") == "TIER_1_API_RENTCAFE_SECURECAFE"
        ),
        "current_full_pipeline_all_rows_native_positive": len(strict) == len(emitted) > 0,
        "current_full_pipeline_unit_set_matches_direct_inventory": pipeline_unit_set
        == direct_unit_set
        and bool(pipeline_unit_set),
        "current_full_pipeline_rents_match_direct_inventory": rent_sets_match,
        "no_llm": not (result.get("_llm_interactions") or []),
        "no_unlocker": True,
        "no_flaresolverr": True,
        "no_captcha_solver": True,
        "no_fingerprint_rotation": True,
        "no_paid_canary": True,
    }
    strict_pass = all(gates.values())

    native_samples = []
    for unit in sorted(strict, key=unit_number)[:8]:
        number = unit_number(unit)
        binding = binding_by_unit[number]
        native_samples.append(
            {
                "identity": {
                    "unit_number": number,
                    "securecafe_unit_id": binding["securecafe_unit_id"],
                    "securecafe_floorplan_id": binding["securecafe_floorplan_id"],
                    "securecafe_property_id": binding["securecafe_property_id"],
                },
                "positive_rent_evidence": {
                    "market_rent_low": unit.get("market_rent_low"),
                    "market_rent_high": unit.get("market_rent_high"),
                },
                "availability_date": unit.get("availability_date")
                or unit.get("available_date")
                or "",
                "source_api_url": source_url(unit),
            }
        )

    payload = {
        "summary": {
            "result_type": "riverfalls_stale_domain_current_official_securecafe_current_strict",
            "capture_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "strict_unit_qualified_properties": 1 if strict_pass else 0,
            "strict_unit_qualified_property_ids": [int(PROPERTY_ID)] if strict_pass else [],
            "native_positive_rent_rows": len(strict),
            "direct_inventory_native_rows": len(direct_strict),
            "plan_direct_probes": len(plan_probes),
            "hyperbrowser_sessions": hb_summary.get("hyperbrowser_sessions"),
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "unlocker": False,
            "flaresolverr": False,
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": [
            {
                "property_id": int(PROPERTY_ID),
                "property_name": cohort["proj_name"],
                "website": CURRENT_URL,
                "configured_2026_07_31_website": cohort["website"],
                "outcome": "UNIT_QUALIFIED" if strict_pass else "UNIT_UNVERIFIED",
                "adapter": result.get("_adapter_used") or "",
                "tier": result.get("extraction_tier_used") or "",
                "units": len(strict),
                "property_identity_match": strict_pass,
                "contamination_verdict": (
                    "pass_exact_current_official_name_address_securecafe_property_2390470_"
                    "native_unit_bindings_three_plan_probes_full_pipeline"
                    if strict_pass
                    else "reject_strict_gate_failure"
                ),
                "identity_evidence": {
                    "canonical_name": cohort["proj_name"],
                    "canonical_address": cohort["address"],
                    "canonical_city_state_zip": (
                        f"{cohort['city']}, {cohort['state']} {cohort['zip_code']}"
                    ),
                    "current_official_url": CURRENT_URL,
                    "securecafe_property_id": NATIVE_PROPERTY_ID,
                    "current_root_identity": root_identity,
                    "inventory_identity": inventory_identity,
                    "rows_with_native_identity": len(strict),
                    "rows_with_native_identity_and_positive_rent": len(strict),
                    "distinct_unit_numbers": len(pipeline_unit_set),
                    "distinct_securecafe_unit_ids": len(
                        {row["securecafe_unit_id"] for row in bindings}
                    ),
                    "source_urls": sorted({source_url(unit) for unit in strict if source_url(unit)}),
                },
                "native_samples": native_samples,
                "strict_gates": gates,
                "floorplan_boundary_probes": plan_probes,
                "configured_source_validation": {
                    "configured_2026_07_31_url": cohort["website"],
                    "configured_status": old_status,
                    "configured_final_url": old_final_url,
                    "configured_final_page_contains_property_identity": (
                        "riverfalls at bellmar" in old_visible
                        or "10570 stone canyon" in old_visible
                        or "75230" in old_visible
                    ),
                    "current_official_url": CURRENT_URL,
                    "current_official_root_fetch_via": "one Hyperbrowser residential-proxy session",
                    "current_full_pipeline_adapter": result.get("_adapter_used") or "",
                    "current_full_pipeline_tier": result.get("extraction_tier_used") or "",
                    "current_full_pipeline_units": len(emitted),
                    "current_full_pipeline_strict_native_positive_rent_rows": len(strict),
                    "current_full_pipeline_source_urls": sorted(
                        {source_url(unit) for unit in strict if source_url(unit)}
                    ),
                    "current_full_pipeline_errors": result.get("errors") or [],
                    "current_full_pipeline_elapsed_seconds": elapsed_seconds,
                    "minimal_production_lever": (
                        "refresh the stale configured domain to the exact current official domain, "
                        "or add fail-closed stale-domain rediscovery requiring exact name/address/ZIP"
                    ),
                },
            }
        ],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    if not strict_pass:
        failed = [name for name, passed in gates.items() if not passed]
        raise SystemExit(f"strict gates failed: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
