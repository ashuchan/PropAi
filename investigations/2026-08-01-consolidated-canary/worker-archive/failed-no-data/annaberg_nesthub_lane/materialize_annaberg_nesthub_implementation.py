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

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.core.source_ids import SourceIdScope, scope_of
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
LANE = ROOT / "annaberg_nesthub_lane"
EVIDENCE = LANE / "evidence_annaberg_1765_nesthub_implementation_e2e.json"
DISCOVERY = LANE / "evidence_annaberg_1765_nesthub_discovery.json"
PROPOSAL = LANE / "annaberg_1765_nesthub_implementation_proposal.md"
MATERIALIZER = LANE / "materialize_annaberg_nesthub_implementation.py"
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
PROPERTIES = Path("ma_poc/config/properties.csv")

PROPERTY_ID = "1765"
CONFIGURED_URL = (
    "https://www.augustarentalhomes.net/_system/listings/56/"
    "2905-Arrowhead-Drive---D3-Augusta-GA-30909-US"
)
COMMUNITY_URL = "https://www.augustarentalhomes.net/annabergs"
ROSTER_URL = "https://www.augustarentalhomes.net/augusta-homes-for-rent"
ROSTER_PAGE_2_URL = f"{ROSTER_URL}?pg=2"
TARGET_URL = (
    "https://www.augustarentalhomes.net/_system/listings/602/"
    "2905-Arrowhead-Drive---E7-Augusta-GA-30909-US"
)

SOURCE_FILES = (
    Path("ma_poc/pms/adapters/_nesthub_public.py"),
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/core/source_ids.py"),
)
TEST_FILES = (
    Path("ma_poc/tests/pms/adapters/test_nesthub_public.py"),
    Path("ma_poc/tests/pms/adapters/test_showmojo_public.py"),
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
        "property_in_ledger": any(row["property_id"] == PROPERTY_ID for row in ledger),
        "property_in_remaining": any(
            row["property_id"] == PROPERTY_ID for row in remaining
        ),
    }


def source_snapshot() -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        "git_head": head,
        "dirty": bool(status),
        "git_status_short": status,
        "sha256": {
            str(path): sha256(path) for path in (*SOURCE_FILES, *TEST_FILES)
        },
    }


def run_command(command: list[str], *, required: bool = True) -> dict[str, object]:
    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, text=True)
    result = {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if required and proc.returncode:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


def load_harness():
    spec = importlib.util.spec_from_file_location("annaberg_implementation_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load configured-route E2E harness")
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
    return {
        "unit_number": row.get("unit_number") or "",
        "unit_name": row.get("unit_name") or "",
        "provider_unit_address": row.get("provider_unit_address") or "",
        "floor_plan_name": row.get("floor_plan_name") or "",
        "floor_plan_name_provenance": row.get("floor_plan_name_provenance") or "",
        "bedrooms": row.get("bedrooms") or "",
        "bathrooms": row.get("bathrooms") or "",
        "sqft": row.get("sqft") or "",
        "market_rent_low": row.get("market_rent_low"),
        "market_rent_high": row.get("market_rent_high"),
        "availability_status": row.get("availability_status") or "",
        "availability_text": row.get("availability_text") or "",
        "availability_date": row.get("availability_date") or "",
        "available_date": row.get("available_date") or "",
        "availability_date_provenance": row.get("availability_date_provenance") or "",
        "source_api_url": row.get("source_api_url") or "",
        "source_listing_url": row.get("source_listing_url") or "",
        "source_portal_url": row.get("source_portal_url") or "",
        "source_community_url": row.get("source_community_url") or "",
        "source_property_name": row.get("source_property_name") or "",
        "source_property_provenance": row.get("source_property_provenance") or "",
        "source_ids": dict(source_ids) if isinstance(source_ids, dict) else {},
    }


async def replay_once(
    harness,
    metadata: dict[str, str],
    repeat: int,
) -> dict[str, object]:
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
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    plans = [
        row for row in result.get("plan_summaries") or [] if isinstance(row, dict)
    ]
    native = [row for row in units if unit_has_real_anchor(row)]
    strict = [row for row in native if positive_rent(row)]
    chain = result.get("_nesthub_official_chain")
    if not isinstance(chain, dict):
        raise RuntimeError("configured E2E omitted NestHub official-chain telemetry")
    rejected = {
        str(row.get("provider_listing_id") or ""): set(row.get("reasons") or [])
        for row in chain.get("rejected_rows") or []
        if isinstance(row, dict)
    }
    unit = strict[0] if len(strict) == 1 else {}
    source_ids = unit.get("source_ids") if isinstance(unit, dict) else {}
    assertions = {
        "configured_fetch_200": fetched.status == 200 and fetched.outcome.value == "OK",
        "adapter_exact": result.get("_adapter_used") == "nesthub_public",
        "tier_exact": result.get("extraction_tier_used")
        == "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY",
        "exact_one_native_priced_row": len(units) == len(native) == len(strict) == 1,
        "stale_deposit_plan_discarded": len(plans) == 0,
        "natural_unit_anchor_e7": unit.get("unit_number") == "E7",
        "provider_address_exact": unit.get("provider_unit_address")
        == "2905 Arrowhead Drive - E7",
        "provider_floor_plan_exact": unit.get("floor_plan_name") == "Chesapeake",
        "provider_dimensions_exact": str(unit.get("bedrooms") or "") == "2"
        and str(unit.get("bathrooms") or "") == "2.5"
        and str(unit.get("sqft") or "") == "1268",
        "provider_rent_exact": unit.get("market_rent_low")
        == unit.get("market_rent_high")
        == 1160,
        "provider_future_date_exact": unit.get("availability_date")
        == unit.get("available_date")
        == "2026-08-19",
        "provider_raw_date_preserved": unit.get("availability_text")
        == "Available: 08-19-2026",
        "native_listing_id_exact": isinstance(source_ids, dict)
        and source_ids == {"nesthub_listing_id": "602"},
        "native_listing_id_pending_scope": scope_of("nesthub_listing_id")
        == SourceIdScope.UNIT_PENDING,
        "source_urls_exact": unit.get("source_api_url") == TARGET_URL
        and unit.get("source_listing_url") == TARGET_URL
        and unit.get("source_portal_url") == ROSTER_URL
        and unit.get("source_community_url") == COMMUNITY_URL,
        "configured_stale_id_never_emitted": chain.get("configured_listing_id") == "56"
        and chain.get("configured_status") == "This Property Is Not Available"
        and chain.get("configured_listing_must_not_emit") is True
        and "56" not in set(chain.get("native_listing_ids") or []),
        "published_property_filter_exact": chain.get("published_property_filter")
        == "search=ANNBRG",
        "full_roster_paginated": chain.get("pages")
        == [
            {"page": 1, "url": ROSTER_URL, "rows": 21},
            {"page": 2, "url": ROSTER_PAGE_2_URL, "rows": 12},
        ]
        and chain.get("portfolio_rows") == 33,
        "exact_candidate_and_acceptance_counts": chain.get("exact_address_candidates")
        == 1
        and chain.get("accepted_rows") == 1
        and chain.get("native_listing_ids") == ["602"],
        "same_zip_wrong_street_control_excluded": "601" in rejected
        and "canonical_street_and_native_unit_suffix_mismatch" in rejected["601"]
        and "canonical_city_mismatch" not in rejected["601"]
        and "canonical_zip_mismatch" not in rejected["601"],
        "wrong_property_city_zip_control_excluded": "606" in rejected
        and "canonical_street_and_native_unit_suffix_mismatch" in rejected["606"]
        and "canonical_city_mismatch" in rejected["606"]
        and "canonical_zip_mismatch" in rejected["606"],
        "fallback_provenance_present": "page_published_native:nesthub_public"
        in (result.get("_fallback_chain") or []),
        "llm_not_used": not (result.get("_llm_interactions") or []),
    }
    if not all(assertions.values()):
        failed = [name for name, passed in assertions.items() if not passed]
        raise RuntimeError(f"repeat {repeat} failed assertions: {failed}")
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
        "unit_rows": len(units),
        "plan_rows": len(plans),
        "native_rows": len(native),
        "native_positive_rent_rows": len(strict),
        "unit": unit_evidence(unit),
        "fallback_chain": result.get("_fallback_chain") or [],
        "llm_interactions": result.get("_llm_interactions") or [],
        "official_chain": chain,
        "assertions": assertions,
    }


async def main() -> None:
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")
    if not DISCOVERY.exists() or not PROPOSAL.exists():
        raise RuntimeError("accepted discovery/proposal artifacts are missing")

    source_before = source_snapshot()
    cohort_before = cohort_snapshot()
    if cohort_before["property_in_ledger"] or not cohort_before["property_in_remaining"]:
        raise RuntimeError("PID 1765 is no longer an untouched exact remainder")
    residual = next(
        row for row in read_csv(REMAINING) if row["property_id"] == PROPERTY_ID
    )
    metadata = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    if metadata.get("website") != CONFIGURED_URL:
        raise RuntimeError("configured route changed")

    checks = [
        run_command(
            [
                "ruff",
                "check",
                *[str(path) for path in (*SOURCE_FILES, *TEST_FILES)],
            ]
        ),
        run_command(
            [
                "pytest",
                "-q",
                "ma_poc/tests/pms/adapters/test_nesthub_public.py",
                "ma_poc/tests/pms/adapters/test_showmojo_public.py",
                "ma_poc/tests/pms/adapters/test_betternoi_public.py",
                "ma_poc/tests/pms/test_page_local_static_recovery.py",
                "ma_poc/tests/pms/test_universal_recovery_plan_level_gate.py",
            ]
        ),
        run_command(
            [
                "pytest",
                "-q",
                "ma_poc/tests/pms/test_scraper.py",
                "ma_poc/tests/pms/test_scraper_jugnu.py",
                "ma_poc/tests/pms/test_empty_exit_plan_text.py",
                "ma_poc/tests/pms/test_no_availability_salvage_checkpoint.py",
                "ma_poc/tests/integration/test_floorplan_snap.py",
            ]
        ),
    ]
    coverage_diagnostic = run_command(
        [
            "pytest",
            "-q",
            "ma_poc/tests/core/test_source_id_registry_coverage.py::"
            "test_every_written_source_id_key_is_registered",
        ],
        required=False,
    )
    coverage_text = str(coverage_diagnostic["stdout"]) + str(
        coverage_diagnostic["stderr"]
    )
    if coverage_diagnostic["exit_code"] != 0:
        # Funnel is an explicitly separate concurrent lane. Do not mutate its
        # keys or conflate that work with this accepted NestHub recovery.
        expected_external = {
            "funnel_listing_id",
            "funnel_building_id",
            "funnel_community_id",
        }
        if (
            not all(key in coverage_text for key in expected_external)
            or "nesthub_listing_id" in coverage_text
            or "showmojo_listing_uid" in coverage_text
        ):
            raise RuntimeError(
                "source-id coverage has an unexpected failure outside Funnel"
            )

    harness = load_harness()
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    repeats = []
    for repeat in range(1, 4):
        row = await replay_once(harness, metadata, repeat)
        repeats.append(row)
        print(
            json.dumps(
                {
                    "repeat": repeat,
                    "adapter": row["adapter"],
                    "tier": row["tier"],
                    "units": row["unit_rows"],
                    "plans": row["plan_rows"],
                    "native_positive_rent_rows": row[
                        "native_positive_rent_rows"
                    ],
                }
            ),
            flush=True,
        )
    unlocker_calls = web_unlocker_call_count()
    hb_calls = hyperbrowser_property_call_count(PROPERTY_ID)
    if unlocker_calls or hb_calls:
        raise RuntimeError(
            f"restricted backend observed unlocker={unlocker_calls} hb={hb_calls}"
        )

    source_after = source_snapshot()
    cohort_after = cohort_snapshot()
    if source_before["git_head"] != source_after["git_head"]:
        raise RuntimeError("git HEAD changed during implementation replay")
    if source_before["sha256"] != source_after["sha256"]:
        raise RuntimeError("implementation source/test files changed during replay")
    if cohort_after["property_in_ledger"] or not cohort_after["property_in_remaining"]:
        raise RuntimeError("PID 1765 cohort status changed during replay")

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "annaberg_nesthub_exact_property_ssr_implementation_e2e",
        "property_id": int(PROPERTY_ID),
        "property_name": "Annaberg",
        "configured_url": CONFIGURED_URL,
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "source_adapter_0731": residual.get("source_adapter_0731") or "",
            "current_detected_adapter": residual.get("current_detected_adapter") or "",
            "state_before": cohort_before,
            "state_after": cohort_after,
            "global_cohort_changed_during_run": cohort_before != cohort_after,
            "confirmed_remaining_not_ledger": True,
        },
        "accepted_discovery": {
            "artifact": str(DISCOVERY),
            "artifact_sha256": sha256(DISCOVERY),
            "proposal": str(PROPOSAL),
            "proposal_sha256": sha256(PROPOSAL),
        },
        "implementation": {
            "source_files": [str(path) for path in SOURCE_FILES],
            "test_files": [str(path) for path in TEST_FILES],
            "tier": "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY",
            "source_id_scope": {
                "nesthub_listing_id": scope_of("nesthub_listing_id").value
                if scope_of("nesthub_listing_id") is not None
                else "",
            },
        },
        "verification": {
            "checks": checks,
            "source_id_coverage_diagnostic": coverage_diagnostic,
            "source_id_coverage_external_concurrent_failure": (
                coverage_diagnostic["exit_code"] != 0
            ),
            "source_id_coverage_external_keys": [
                "funnel_listing_id",
                "funnel_building_id",
                "funnel_community_id",
            ]
            if coverage_diagnostic["exit_code"] != 0
            else [],
            "configured_pipeline_repeats": repeats,
            "all_repeats_pass": all(
                all(row["assertions"].values()) for row in repeats
            ),
        },
        "provider_direct_counts": {
            "configured_repeats": len(repeats),
            "unit_rows_each": [row["unit_rows"] for row in repeats],
            "plan_rows_each": [row["plan_rows"] for row in repeats],
            "native_positive_rent_rows_each": [
                row["native_positive_rent_rows"] for row in repeats
            ],
            "native_listing_ids_each": [
                row["official_chain"]["native_listing_ids"] for row in repeats
            ],
        },
        "provider_direct_strict_pass": True,
        "authoritative_ledger_eligible": False,
        "authoritative_ledger_hold_reason": (
            "Parent must independently replay/admit; this lane was instructed not "
            "to edit the strict builder or ledger."
        ),
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "guardrails": {
            "compliance_mode": True,
            "llm": False,
            "paid_canary": False,
            "proxy": False,
            "web_unlocker": False,
            "web_unlocker_call_count": unlocker_calls,
            "hyperbrowser": False,
            "hyperbrowser_call_count": hb_calls,
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "environment": EXPECTED_ENV,
        },
        "repo_mutation": "accepted_implementation_files_only",
        "ledger_mutation": "none",
        "canary_mutation": "none",
        "commit": "none",
        "push": "none",
    }
    EVIDENCE.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256(EVIDENCE),
                "materializer": str(MATERIALIZER),
                "materializer_sha256": sha256(MATERIALIZER),
                "source_sha256": source_after["sha256"],
                "configured_repeats": len(repeats),
                "native_positive_rent_counts": [
                    row["native_positive_rent_rows"] for row in repeats
                ],
                "all_repeats_pass": payload["verification"]["all_repeats_pass"],
                "authoritative_ledger_eligible": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
