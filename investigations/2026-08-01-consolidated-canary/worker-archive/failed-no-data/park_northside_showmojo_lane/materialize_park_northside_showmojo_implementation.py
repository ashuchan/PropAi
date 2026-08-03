from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)

ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "park_northside_showmojo_lane"
EVIDENCE = LANE / "evidence_park_northside_38378_showmojo_implementation_e2e.json"
DISCOVERY = LANE / "evidence_park_northside_38378_showmojo_discovery.json"
PROPOSAL = LANE / "park_northside_showmojo_implementation_proposal.md"
MATERIALIZER = LANE / "materialize_park_northside_showmojo_implementation.py"
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
PROPERTIES = Path("ma_poc/config/properties.csv")

PROPERTY_ID = "38378"
CONFIGURED_URL = "https://www.parknorthsiderva.com/"
MANAGER_URL = "https://dobrinpropertymanagement.com/"
MANAGER_LISTINGS_URL = (
    "https://dobrinpropertymanagement.com/richmond-va-property-listings/"
)
EMBED_URL = "https://showmojo.com/fea92db007/listings/mapsearch"
ACCOUNT = "fea92db007"
SITE_ID = "44261A"
EXPECTED_UIDS = [
    "e7c39f1061",
    "f9a10da061",
    "02193fd097",
    "6cdb90d053",
    "61ac367085",
    "4cb3ea70bd",
    "32d531e0d8",
    "2b1030a0f4",
    "c338072019",
    "ac0583204b",
    "2fb50de04d",
    "5ac2969071",
    "0b5bfa5039",
]
CONTROL_REASONS = {
    "2ae5ea2026": {
        "canonical_property_name_absent",
        "canonical_city_state_zip_mismatch",
    },
    "097b680090": {
        "canonical_property_name_absent",
        "canonical_city_state_zip_mismatch",
    },
    "e3afa4f0bf": {"canonical_city_state_zip_mismatch"},
}
SOURCE_FILES = (
    Path("ma_poc/pms/adapters/_showmojo_public.py"),
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/services/floorplan_snap.py"),
)
TEST_FILES = (
    Path("ma_poc/tests/pms/adapters/test_showmojo_public.py"),
    Path("ma_poc/tests/integration/test_floorplan_snap.py"),
    Path("ma_poc/tests/pms/adapters/test_betternoi_public.py"),
)
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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def cohort_snapshot() -> dict[str, object]:
    ledger = read_csv(LEDGER)
    remaining = read_csv(REMAINING)
    return {
        "ledger_sha256": sha256(LEDGER),
        "remaining_sha256": sha256(REMAINING),
        "summary_sha256": sha256(SUMMARY),
        "ledger_rows": len(ledger),
        "remaining_rows": len(remaining),
        "property_in_ledger": any(row.get("property_id") == PROPERTY_ID for row in ledger),
        "property_in_remaining": any(
            row.get("property_id") == PROPERTY_ID for row in remaining
        ),
    }


def source_snapshot() -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "git_head": head,
        "sha256": {
            str(path): sha256(path) for path in (*SOURCE_FILES, *TEST_FILES)
        },
    }


def run_check(command: list[str]) -> dict[str, object]:
    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, text=True)
    result = {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if proc.returncode:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


def load_harness():
    spec = importlib.util.spec_from_file_location("park_northside_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load configured-route harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task() -> CrawlTask:
    return CrawlTask(
        url=CONFIGURED_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and float(row[field]) > 0
        for field in ("market_rent_low", "market_rent_high")
    )


def unit_evidence(row: dict[str, object]) -> dict[str, object]:
    source_ids = row.get("source_ids")
    ids = dict(source_ids) if isinstance(source_ids, dict) else {}
    return {
        "unit_number": row.get("unit_number") or "",
        "unit_name": row.get("unit_name") or "",
        "provider_unit_address": row.get("provider_unit_address") or "",
        "floor_plan_name": row.get("floor_plan_name") or "",
        "floor_plan_name_catalog": row.get("floor_plan_name_catalog") or "",
        "floor_plan_name_provenance": row.get("floor_plan_name_provenance") or "",
        "floor_plan_snap_name_suppressed": bool(
            row.get("floor_plan_snap_name_suppressed")
        ),
        "bedrooms": row.get("bedrooms") or "",
        "bathrooms": row.get("bathrooms") or "",
        "sqft": row.get("sqft") or "",
        "market_rent_low": row.get("market_rent_low"),
        "market_rent_high": row.get("market_rent_high"),
        "availability_status": row.get("availability_status") or "",
        "availability_text": row.get("availability_text") or "",
        "availability_date": row.get("availability_date") or "",
        "available_date": row.get("available_date") or "",
        "availability_date_provenance": (
            row.get("availability_date_provenance") or ""
        ),
        "source_api_url": row.get("source_api_url") or "",
        "source_listing_url": row.get("source_listing_url") or "",
        "source_portal_url": row.get("source_portal_url") or "",
        "source_manager_url": row.get("source_manager_url") or "",
        "source_manager_listings_url": (
            row.get("source_manager_listings_url") or ""
        ),
        "source_property_name": row.get("source_property_name") or "",
        "source_property_provenance": (
            row.get("source_property_provenance") or ""
        ),
        "source_ids": ids,
        "data_gaps": row.get("data_gaps") or [],
        "data_quality_flag": row.get("data_quality_flag") or "",
    }


async def replay_once(harness, metadata: dict[str, str], repeat: int) -> dict[str, object]:
    configured_task = task()
    fetched = await harness.direct_fetch(configured_task)
    result = await asyncio.wait_for(
        harness.scraper_mod.scrape_jugnu(
            configured_task,
            fetched,
            page=None,
            profile=None,
            csv_row=metadata,
        ),
        timeout=120,
    )
    units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
    native = [row for row in units if unit_has_real_anchor(row)]
    strict = [row for row in native if positive_rent(row)]
    chain = result.get("_showmojo_official_chain")
    if not isinstance(chain, dict):
        raise RuntimeError("configured E2E omitted ShowMojo official-chain telemetry")
    rejected = {
        str(row.get("provider_listing_uid") or ""): set(row.get("reasons") or [])
        for row in chain.get("rejected_rows") or []
        if isinstance(row, dict)
    }
    uids = [
        str((row.get("source_ids") or {}).get("showmojo_listing_uid") or "")
        for row in strict
    ]

    assertions = {
        "configured_fetch_200": fetched.status == 200 and bool(fetched.body),
        "adapter_exact": result.get("_adapter_used") == "showmojo_public",
        "tier_exact": result.get("extraction_tier_used")
        == "TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN",
        "exact_13_native_priced_rows": len(units) == len(native) == len(strict) == 13,
        "native_listing_ids_exact": uids == EXPECTED_UIDS,
        "native_listing_ids_unique": len(uids) == len(set(uids)),
        "unit_addresses_unique": len(
            {str(row.get("unit_number") or "").casefold() for row in strict}
        )
        == 13,
        "floor_plan_names_remain_blank": all(
            not str(row.get("floor_plan_name") or "").strip() for row in strict
        ),
        "provider_name_gap_documented": all(
            "floor_plan_name" in (row.get("data_gaps") or []) for row in strict
        ),
        "availability_text_preserved": all(
            str(row.get("availability_text") or "").casefold().startswith("available")
            for row in strict
        ),
        "availability_dates_not_invented": all(
            not row.get("availability_date") and not row.get("available_date")
            for row in strict
        ),
        "source_uid_detail_binding": all(
            urlparse(str(row.get("source_listing_url") or "")).path.startswith(
                f"/l/{(row.get('source_ids') or {}).get('showmojo_listing_uid')}/"
            )
            for row in strict
        ),
        "official_chain_urls_exact": chain.get("configured_url") == CONFIGURED_URL
        and chain.get("manager_url") == MANAGER_URL
        and chain.get("manager_listings_url") == MANAGER_LISTINGS_URL
        and chain.get("showmojo_embed_url") == EMBED_URL,
        "account_and_application_exact": chain.get("showmojo_account") == ACCOUNT
        and chain.get("application_site_id") == SITE_ID,
        "mixed_roster_explicitly_filtered": chain.get("portfolio_rows") == 52
        and chain.get("accepted_rows") == 13,
        "three_same_roster_controls_excluded": all(
            expected <= rejected.get(uid, set())
            for uid, expected in CONTROL_REASONS.items()
        ),
        "fallback_provenance_present": (
            "page_published_native:showmojo_public"
            in (result.get("_fallback_chain") or [])
        ),
        "llm_not_used": not (result.get("_llm_interactions") or []),
    }
    if not all(assertions.values()):
        raise RuntimeError(f"configured E2E assertion failure: {assertions}")
    return {
        "repeat": repeat,
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
        },
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "winning_page_url": result.get("_winning_page_url") or "",
        "unit_rows": len(units),
        "native_rows": len(native),
        "native_positive_rent_rows": len(strict),
        "plan_rows": len(result.get("plan_summaries") or []),
        "assertions": assertions,
        "official_chain": chain,
        "units": [unit_evidence(row) for row in strict],
        "llm_interactions": result.get("_llm_interactions") or [],
    }


async def main() -> None:
    for key, expected in EXPECTED_ENV.items():
        actual = os.environ.get(key, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{key}={actual!r}; expected {expected!r}")

    cohort_before = cohort_snapshot()
    if cohort_before["property_in_ledger"] or not cohort_before["property_in_remaining"]:
        raise RuntimeError("PID 38378 is not an untouched remaining FAILED_NO_DATA row")
    source_before = source_snapshot()
    metadata = next(
        row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid") == PROPERTY_ID
    )
    if {
        "name": metadata.get("name"),
        "address": metadata.get("address"),
        "city": metadata.get("city"),
        "state": metadata.get("state"),
        "zip": metadata.get("zip"),
        "website": metadata.get("website"),
    } != {
        "name": "Park Northside",
        "address": "1601 Roane St",
        "city": "Richmond",
        "state": "VA",
        "zip": "23222",
        "website": CONFIGURED_URL,
    }:
        raise RuntimeError("canonical metadata changed")

    checks = [
        run_check(
            [
                "ruff",
                "check",
                *[str(path) for path in (*SOURCE_FILES, *TEST_FILES)],
            ]
        ),
        run_check(
            [
                "pytest",
                "-q",
                "ma_poc/tests/pms/adapters/test_showmojo_public.py",
                "ma_poc/tests/pms/adapters/test_betternoi_public.py",
                "ma_poc/tests/integration/test_floorplan_snap.py",
            ]
        ),
        run_check(
            [
                "pytest",
                "-q",
                "ma_poc/tests/pms/test_scraper.py",
                "ma_poc/tests/pms/test_universal_recovery_plan_level_gate.py",
                "ma_poc/tests/pms/test_page_local_static_recovery.py",
            ]
        ),
    ]

    harness = load_harness()
    harness.fetch_mod.fetch = harness.direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    repeats = []
    for repeat in range(1, 4):
        replay = await replay_once(harness, metadata, repeat)
        repeats.append(replay)
        print(
            json.dumps(
                {
                    "repeat": repeat,
                    "adapter": replay["adapter"],
                    "tier": replay["tier"],
                    "strict_rows": replay["native_positive_rent_rows"],
                    "floor_names_blank": replay["assertions"][
                        "floor_plan_names_remain_blank"
                    ],
                }
            ),
            flush=True,
        )

    unlocker_calls = web_unlocker_call_count()
    hyperbrowser_calls = hyperbrowser_property_call_count(PROPERTY_ID)
    if unlocker_calls or hyperbrowser_calls:
        raise RuntimeError(
            f"forbidden backend observed: unlocker={unlocker_calls} "
            f"hyperbrowser={hyperbrowser_calls}"
        )
    source_after = source_snapshot()
    cohort_after = cohort_snapshot()
    if source_before != source_after:
        raise RuntimeError("source changed during evidence replay")
    if cohort_before != cohort_after:
        raise RuntimeError("cohort ledger changed during evidence replay")

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "park_northside_showmojo_official_manager_chain_implementation_e2e",
        "property_id": int(PROPERTY_ID),
        "canonical_identity": {
            "name": "Park Northside",
            "address": "1601 Roane St",
            "city": "Richmond",
            "state": "VA",
            "zip": "23222",
            "configured_url": CONFIGURED_URL,
        },
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "before": cohort_before,
            "after": cohort_after,
            "ledger_mutation": "none",
        },
        "implementation": {
            "source_files": [str(path) for path in SOURCE_FILES],
            "test_files": [str(path) for path in TEST_FILES],
            "source_snapshot_before": source_before,
            "source_snapshot_after": source_after,
            "git_commit_created": False,
            "git_push": False,
            "canary_run": False,
        },
        "verification": {
            "checks": checks,
            "configured_pipeline_repeats": repeats,
            "repeat_count": len(repeats),
            "strict_native_positive_rent_counts": [
                row["native_positive_rent_rows"] for row in repeats
            ],
            "all_repeats_pass": all(
                all(row["assertions"].values()) for row in repeats
            ),
        },
        "guardrails": {
            "ordinary_direct_get_only": True,
            "llm": False,
            "paid_canary": False,
            "proxy": False,
            "web_unlocker": False,
            "web_unlocker_call_count": unlocker_calls,
            "hyperbrowser": False,
            "hyperbrowser_call_count": hyperbrowser_calls,
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "environment": EXPECTED_ENV,
        },
        "prior_discovery": {
            "evidence": str(DISCOVERY),
            "evidence_sha256": sha256(DISCOVERY),
            "proposal": str(PROPOSAL),
            "proposal_sha256": sha256(PROPOSAL),
        },
        "materializer": {
            "path": str(MATERIALIZER),
            "sha256": sha256(MATERIALIZER),
        },
        "authoritative_ledger_eligible": False,
        "authoritative_ledger_hold_reason": (
            "Implementation and local configured E2E pass; parent requested "
            "independent replay before any ledger admission."
        ),
    }
    EVIDENCE.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256(EVIDENCE),
                "materializer": str(MATERIALIZER),
                "materializer_sha256": sha256(MATERIALIZER),
                "strict_counts": payload["verification"][
                    "strict_native_positive_rent_counts"
                ],
                "all_repeats_pass": payload["verification"]["all_repeats_pass"],
                "authoritative_ledger_eligible": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
