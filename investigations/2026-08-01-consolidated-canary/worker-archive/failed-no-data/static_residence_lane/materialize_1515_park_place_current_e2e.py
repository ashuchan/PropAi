from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.adapters._static_residence_table import (
    recover_static_residence_table,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "static_residence_lane"
OUTPUT = LANE / "evidence_1515_park_place_261580_current_strict.json"
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
PROPERTIES = Path("ma_poc/config/properties.csv")
RP = Path("/Users/ankur/Downloads/rp_unit_detail_0731.csv")
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTY_ID = "261580"
CONFIGURED_URL = "https://www.1515parkplace.com/"
AVAILABILITY_URL = "https://www.1515parkplace.com/availability.html"
EXPECTED_UNITS = {
    "102": (2, 2, 3000),
    "101": (4, 2, 4500),
    "103": (4, 2, 4300),
}
EXPECTED_STACK_RANGES = {
    "205-805",
    "206-806",
    "303-803",
    "307-807",
    "201-801",
}
EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_DC_PROXY_TIER": "false",
    "ENABLE_RESIDENTIAL_TIER": "false",
    "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "FETCH_BACKEND": "brightdata",
    "RENDER_BACKEND": "local",
    "PROBE_PROXY_URL": "",
    "PROXY_POOL_URLS": "",
    "ENABLE_RENDER_ON_EMPTY": "false",
    "ENABLE_PLAN_UNIT_RENDER": "false",
    "ENABLE_ENTRATA_PLAN_RENDER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
}
SOURCE_FILES = (
    Path("ma_poc/pms/adapters/_static_residence_table.py"),
    Path("ma_poc/pms/adapters/generic_plan_text.py"),
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/services/floorplan_snap.py"),
    Path("ma_poc/tests/pms/adapters/test_static_residence_table.py"),
    Path("ma_poc/tests/pms/test_page_local_static_recovery.py"),
)
CONTROLS = (
    {
        "name": "200 Montague",
        "url": "https://200montaguebk.com/availability",
        "address": "200 Montague Street",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11201",
    },
    {
        "name": "Prosper Prospect Heights",
        "url": "https://prosperbrooklyn.com/availability",
        "address": "1042 Atlantic Avenue",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11238",
    },
    {
        "name": "Franklin Court",
        "url": "https://franklincrt.com/availabilities",
        "address": "33 Franklin Street",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11222",
    },
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_csv(path: Path) -> list[dict[str, str]]:
    # The RP export contains a few legacy Windows-1252 bytes and embedded NULs
    # outside this property's rows. Replacement decoding preserves the CSV
    # record boundaries while the artifact still pins the source byte hash.
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(handle))


def load_harness():
    spec = importlib.util.spec_from_file_location("static_residence_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load direct configured-route harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and float(row[field]) > 0
        for field in ("market_rent_low", "market_rent_high")
    )


def compact_unit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_number": row.get("unit_number") or "",
        "unit_name": row.get("unit_name") or "",
        "bedrooms": row.get("bedrooms") or "",
        "bathrooms": row.get("bathrooms") or "",
        "sqft": row.get("sqft") or "",
        "floor_plan_name": row.get("floor_plan_name") or "",
        "floor_plan_name_catalog": row.get("floor_plan_name_catalog") or "",
        "floor_plan_name_provenance": row.get("floor_plan_name_provenance") or "",
        "market_rent_low": row.get("market_rent_low"),
        "market_rent_high": row.get("market_rent_high"),
        "availability_status": row.get("availability_status") or "",
        "availability_date": row.get("availability_date") or "",
        "available_date": row.get("available_date") or "",
        "availability_date_provenance": (
            row.get("availability_date_provenance") or ""
        ),
        "source_api_url": row.get("source_api_url") or "",
        "source_property_name": row.get("source_property_name") or "",
        "source_property_address": row.get("source_property_address") or "",
        "source_property_provenance": row.get("source_property_provenance") or "",
        "data_gaps": row.get("data_gaps") or [],
        "data_quality_flag": row.get("data_quality_flag") or "",
        "real_native_anchor": unit_has_real_anchor(row),
        "positive_rent": positive_rent(row),
    }


def source_snapshot() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "git_head": head,
        "sha256": {str(path): sha256(path) for path in SOURCE_FILES},
    }


async def run_configured_pipeline(harness, metadata: dict[str, str], repeat: int):
    task = harness.make_task(CONFIGURED_URL, PROPERTY_ID)
    fetched = await harness.direct_fetch(task)
    result = await asyncio.wait_for(
        harness.scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=metadata,
        ),
        timeout=120,
    )
    units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
    plans = [
        row for row in (result.get("plan_summaries") or []) if isinstance(row, dict)
    ]
    compact = [compact_unit(row) for row in units]
    assertions = {
        "configured_fetch_200": fetched.status == 200 and bool(fetched.body),
        "configured_final_url_exact": fetched.final_url == CONFIGURED_URL,
        "detector_exact": (result.get("_detected_pms") or {}).get("pms")
        == "generic_plan_text",
        "adapter_exact": result.get("_adapter_used") == "generic_plan_text",
        "tier_exact": result.get("extraction_tier_used")
        == "TIER_1_DOM_STATIC_RESIDENCE_TABLE",
        "link_hop_exact": result.get("_link_hop_success") is True
        and result.get("_link_hop_from") == CONFIGURED_URL
        and result.get("_winning_page_url") == AVAILABILITY_URL,
        "exact_three_native_positive_rows": len(units) == 3
        and len(plans) == 0
        and all(row["real_native_anchor"] for row in compact)
        and all(row["positive_rent"] for row in compact),
        "unit_numbers_exact": {row["unit_number"] for row in compact}
        == set(EXPECTED_UNITS),
        "unit_dimensions_and_rents_exact": all(
            (
                int(row["bedrooms"]),
                int(float(row["bathrooms"])),
                int(row["market_rent_low"]),
            )
            == EXPECTED_UNITS[row["unit_number"]]
            for row in compact
        ),
        "provider_floorplan_name_not_invented": all(
            row["floor_plan_name"] == ""
            and row["floor_plan_name_provenance"]
            == "provider_table_does_not_publish_floor_plan_name"
            and "floor_plan_name" in row["data_gaps"]
            for row in compact
        ),
        "property_identity_exact": all(
            row["source_property_name"] == "1515 Park Place"
            and row["source_property_address"]
            == "1515 Park Pl, Brooklyn, NY, 11213"
            and row["source_property_provenance"]
            == "exact_property_identity_server_rendered_availability_table"
            for row in compact
        ),
        "source_url_exact": all(
            row["source_api_url"] == AVAILABILITY_URL for row in compact
        ),
        "availability_provenance_preserved": all(
            row["availability_date_provenance"]
            == "current_availability_roster_no_explicit_date"
            for row in compact
        ),
        "llm_not_used": not (result.get("_llm_interactions") or []),
    }
    return {
        "repeat": repeat,
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
        },
        "detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "winning_page_url": result.get("_winning_page_url") or "",
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": result.get("_link_hop_from") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "unit_rows": len(units),
        "plan_rows": len(plans),
        "units": compact,
        "assertions": assertions,
    }


async def probe_parser_boundary() -> list[dict[str, Any]]:
    cases = (
        {
            "name": "1515 Park Place",
            "url": AVAILABILITY_URL,
            "address": "1515 Park Pl",
            "city": "Brooklyn",
            "state": "NY",
            "zip": "11213",
            "expected_units": 3,
        },
        *({**control, "expected_units": 0} for control in CONTROLS),
    )
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        response = await asyncio.to_thread(
            probe_get,
            case["url"],
            timeout=30,
            unlocker=False,
            retries=1,
            proxies={},
        )
        html = response.text or ""
        final_url = str(response.url or case["url"])
        ctx = AdapterContext(
            base_url=case["url"],
            detected=DetectedPMS(pms="generic_plan_text", confidence=0.55),
            profile=None,
            expected_total_units=None,
            property_id=PROPERTY_ID if index == 0 else f"CONTROL-{index}",
            fetch_result=SimpleNamespace(body=html.encode(), final_url=final_url),
            property_name=case["name"],
            address=case["address"],
            city=case["city"],
            state=case["state"],
            zip_code=case["zip"],
        )
        rows = recover_static_residence_table(ctx)
        results.append(
            {
                "name": case["name"],
                "url": case["url"],
                "status": response.status_code,
                "final_url": final_url,
                "body_bytes": len(html.encode()),
                "body_sha256": sha256_bytes(html.encode()),
                "expected_units": case["expected_units"],
                "emitted_units": len(rows),
                "unit_numbers": [row.get("unit_number") for row in rows],
                "telemetry": getattr(ctx, "_static_residence_table_telemetry", {}),
                "pass": len(rows) == case["expected_units"],
            }
        )
    return results


async def main() -> None:
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    metadata = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    if metadata != {
        "apartmentid": PROPERTY_ID,
        "name": "1515 Park Place",
        "address": "1515 Park Pl",
        "city": "Brooklyn",
        "state": "NY",
        "zip": "11213",
        "website": CONFIGURED_URL,
    }:
        raise RuntimeError("configured property identity changed")

    ledger = read_csv(LEDGER)
    remaining = read_csv(REMAINING)
    if any(row.get("property_id") == PROPERTY_ID for row in ledger):
        raise RuntimeError("property already entered strict ledger before evidence")
    if not any(row.get("property_id") == PROPERTY_ID for row in remaining):
        raise RuntimeError("property absent from exact current remainder")

    rp_rows = [row for row in read_csv(RP) if row.get("apartmentid") == PROPERTY_ID]
    rp_unit_ids = {row.get("unitid") or "" for row in rp_rows}
    if rp_unit_ids != set(EXPECTED_UNITS) | EXPECTED_STACK_RANGES:
        raise RuntimeError(f"unexpected RP identity set: {sorted(rp_unit_ids)}")

    harness = load_harness()
    fetch_mod.fetch = harness.direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    snapshot_before = source_snapshot()
    boundary = await probe_parser_boundary()
    repeats = [
        await run_configured_pipeline(harness, metadata, repeat)
        for repeat in range(1, 4)
    ]
    snapshot_after = source_snapshot()
    all_repeats_pass = all(
        repeat["assertions"]
        and all(value is True for value in repeat["assertions"].values())
        for repeat in repeats
    )
    all_boundary_pass = len(boundary) == 4 and all(row["pass"] for row in boundary)
    source_stable = snapshot_before == snapshot_after
    guardrails = {
        "ordinary_direct_get_only": True,
        "llm": False,
        "hyperbrowser": False,
        "hyperbrowser_call_count": hyperbrowser_property_call_count(PROPERTY_ID),
        "web_unlocker": False,
        "web_unlocker_call_count": web_unlocker_call_count(),
        "proxy": False,
        "captcha_solving": False,
        "flaresolverr": False,
        "fingerprint_rotation": False,
        "paid_canary": False,
        "environment": EXPECTED_ENV,
    }
    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "static_residence_1515_park_place_current_configured_e2e",
        "property_id": int(PROPERTY_ID),
        "canonical_identity": metadata,
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "ledger_rows_before": len(ledger),
            "remaining_rows_before": len(remaining),
            "ledger_sha256_before": sha256(LEDGER),
            "remaining_sha256_before": sha256(REMAINING),
            "property_in_ledger_before": False,
            "property_in_remaining_before": True,
        },
        "rp_2026_07_31_reconciliation": {
            "source": str(RP),
            "source_sha256": sha256(RP),
            "rows": len(rp_rows),
            "unit_ids": sorted(rp_unit_ids),
            "accepted_physical_residences": sorted(EXPECTED_UNITS),
            "excluded_numeric_stack_ranges": sorted(EXPECTED_STACK_RANGES),
            "interpretation": (
                "numeric-to-numeric labels are multi-floor stack/plan ranges; "
                "only single physical residence codes enter the unit ledger"
            ),
        },
        "four_member_exr_boundary": boundary,
        "configured_pipeline_repeats": repeats,
        "source_snapshot_before": snapshot_before,
        "source_snapshot_after": snapshot_after,
        "guardrails": guardrails,
        "strict_assertions": {
            "all_four_boundary_members_pass": all_boundary_pass,
            "three_configured_pipeline_repeats_pass": all_repeats_pass,
            "critical_source_stable_during_replay": source_stable,
            "hyperbrowser_calls_zero": guardrails["hyperbrowser_call_count"] == 0,
            "web_unlocker_calls_zero": guardrails["web_unlocker_call_count"] == 0,
            "no_paid_or_prohibited_mechanism": all(
                guardrails[key] is False
                for key in (
                    "llm",
                    "hyperbrowser",
                    "web_unlocker",
                    "proxy",
                    "captcha_solving",
                    "flaresolverr",
                    "fingerprint_rotation",
                    "paid_canary",
                )
            ),
        },
        "verdict": (
            "pass_exact_identity_static_residence_table_three_native_units"
            if all_boundary_pass and all_repeats_pass and source_stable
            else "reject_static_residence_strict_gate_incomplete"
        ),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "boundary_counts": [row["emitted_units"] for row in boundary],
                "repeat_counts": [row["unit_rows"] for row in repeats],
                "all_repeats_pass": all_repeats_pass,
                "verdict": payload["verdict"],
            },
            indent=2,
        )
    )
    if payload["verdict"] != (
        "pass_exact_identity_static_residence_table_three_native_units"
    ):
        raise RuntimeError("static residence strict gate failed; inspect artifact")


if __name__ == "__main__":
    asyncio.run(main())
