from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "cortland_oakrow_lane"
EVIDENCE = LANE / "evidence_oakrow_25489_configured_e2e.json"
IDENTITY_EVIDENCE = LANE / "hb_oakrow_identity_probe.json"
HARNESS = ROOT / "full_pipeline_remaining_discovery.py"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
PROPERTIES = Path("ma_poc/config/properties.csv")

PROPERTY_ID = "25489"
CONFIGURED_URL = "https://cortland.com/apartments/cortland-north-dallas/"
CURRENT_ROOT = "https://www.oakrowdallas.com/"
ROOT_BODY = LANE / "25489_hb_page_1.html.gz"
PLAN_PATH_RE = re.compile(
    r"^/floorplans/dallas-TX/oak-row-north-dallas/"
    r"(?P<slug>[a-z0-9-]+)-(?P<fpid>\d+)-1/$"
)

SOURCE_FILES = (
    Path("ma_poc/pms/adapters/entrata.py"),
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/core/identity.py"),
)
TEST_FILES = (Path("ma_poc/tests/pms/adapters/test_entrata_pp_ssr.py"),)
EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "ENABLE_HYPERBROWSER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
    "PROBE_PROXY_URL": "",
    "PROXY_POOL_URLS": "",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def ledger_snapshot() -> dict[str, object]:
    ledger = read_csv(LEDGER)
    remaining = read_csv(REMAINING)
    return {
        "ledger_rows": len(ledger),
        "remaining_rows": len(remaining),
        "ledger_sha256": sha256(LEDGER),
        "remaining_sha256": sha256(REMAINING),
        "summary_sha256": sha256(SUMMARY),
        "property_in_ledger": any(
            row.get("property_id") == PROPERTY_ID for row in ledger
        ),
        "property_in_remaining": any(
            row.get("property_id") == PROPERTY_ID for row in remaining
        ),
    }


def source_snapshot() -> dict[str, object]:
    return {
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "sha256": {
            str(path): sha256(path) for path in (*SOURCE_FILES, *TEST_FILES)
        },
    }


def run_command(command: list[str]) -> dict[str, object]:
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
    spec = importlib.util.spec_from_file_location("oakrow_e2e_harness", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load configured pipeline harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configured_metadata() -> dict[str, str]:
    matches = [
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one configured row, found {len(matches)}")
    return matches[0]


def task() -> CrawlTask:
    return CrawlTask(
        url=CONFIGURED_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def archived_render_fetch() -> FetchResult:
    body = gzip.open(ROOT_BODY, "rb").read()
    return FetchResult(
        url=CONFIGURED_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html"},
        render_mode=RenderMode.GET,
        final_url=CURRENT_ROOT,
        attempts=1,
        elapsed_ms=0,
    )


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def compact_unit(row: dict[str, object]) -> dict[str, object]:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_status",
            "availability_date",
            "available_date",
            "availability_date_provenance",
            "source_api_url",
            "source_ids",
        )
    }


def unit_scope_pass(row: dict[str, object]) -> bool:
    unit_number = str(row.get("unit_number") or "").strip()
    source_ids = row.get("source_ids") or {}
    if not isinstance(source_ids, dict):
        return False
    uid = str(source_ids.get("entrata_uid") or "").strip()
    fpid = str(source_ids.get("entrata_fpid") or "").strip()
    url = str(row.get("source_api_url") or "")
    parsed = urlparse(url)
    match = PLAN_PATH_RE.fullmatch(parsed.path)
    return bool(
        unit_has_real_anchor(row)
        and positive_rent(row)
        and unit_number
        and not unit_number.startswith("ent-")
        and uid
        and fpid
        and parsed.scheme == "https"
        and parsed.netloc == "www.oakrowdallas.com"
        and not parsed.query
        and match
        and match.group("fpid") == fpid
        and str(row.get("floor_plan_name") or "").strip()
        and str(row.get("bedrooms") or "").strip()
        and str(row.get("bathrooms") or "").strip()
        and str(row.get("sqft") or "").strip()
    )


def verify_identity_evidence() -> dict[str, object]:
    payload = json.loads(IDENTITY_EVIDENCE.read_text())
    pages = [row for row in payload.get("pages") or [] if isinstance(row, dict)]
    by_index = {
        int(Path(str(row.get("artifact") or "")).stem.split("_")[-1].split(".")[0]): row
        for row in pages
    }
    artifact_checks: list[dict[str, object]] = []
    for index, row in sorted(by_index.items()):
        artifact = Path(str(row.get("artifact") or ""))
        body = gzip.open(artifact, "rb").read()
        artifact_checks.append(
            {
                "index": index,
                "artifact": str(artifact),
                "artifact_sha256": sha256(artifact),
                "artifact_hash_matches": sha256(artifact)
                == row.get("artifact_sha256"),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "body_hash_matches": hashlib.sha256(body).hexdigest()
                == row.get("body_sha256"),
            }
        )
    root = by_index.get(1) or {}
    plan_pages = [by_index.get(index) or {} for index in (3, 4, 5)]
    expected_plan_shapes = {
        "A2 | 1 Bed Apartment | Oak Row North Dallas": (8, 8),
        "A15 | 1 Bed Apartment | Oak Row North Dallas": (1, 1),
        "B3 | 2 Bed Apartment | Oak Row North Dallas": (1, 1),
    }
    root_identity = root.get("identity") or {}
    exact_root = bool(
        root.get("requested_url") == CURRENT_ROOT
        and root.get("final_url") == CURRENT_ROOT
        and root.get("title") == "Oak Row North Dallas | Apartments In Dallas, TX"
        and root_identity.get("oak_row_north_dallas_visible") is True
        and root_identity.get("street_17811_vail_visible") is True
        and root_identity.get("dallas_tx_visible") is True
        and root_identity.get("zip_75287_visible") is True
        and root_identity.get("legacy_cortland_north_dallas_visible") is False
    )
    exact_plan_pages = bool(
        len(plan_pages) == 3
        and all(
            page.get("title") in expected_plan_shapes
            and int(page.get("unit_card_count") or 0)
            == expected_plan_shapes[page["title"]][0]
            and len(page.get("distinct_native_unit_uids") or [])
            == expected_plan_shapes[page["title"]][1]
            and (page.get("identity") or {}).get("oak_row_north_dallas_visible")
            is True
            and (page.get("identity") or {}).get("street_17811_vail_visible")
            is True
            and (page.get("identity") or {}).get("dallas_tx_visible") is True
            and (page.get("identity") or {}).get("zip_75287_visible") is True
            and page.get("foreign_property_plan_links") == []
            and urlparse(str(page.get("final_url") or "")).netloc
            == "www.oakrowdallas.com"
            and PLAN_PATH_RE.fullmatch(
                urlparse(str(page.get("final_url") or "")).path
            )
            for page in plan_pages
        )
    )
    strict = bool(
        payload.get("property_id") == 25489
        and payload.get("configured_property_name") == "Cortland North Dallas"
        and payload.get("configured_address") == "17811 Vail St, Dallas, TX 75287"
        and payload.get("current_property_name_expected") == "Oak Row North Dallas"
        and payload.get("hyperbrowser_sessions") == 1
        and payload.get("session_options")
        == {
            "adblock": True,
            "solveCaptchas": False,
            "useProxy": True,
            "useStealth": False,
        }
        and payload.get("guardrails")
        == {
            "basic_stealth": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "flaresolverr": False,
            "web_unlocker": False,
        }
        and payload.get("error") is None
        and len(pages) == len(by_index) == 6
        and all(
            row.get("artifact_hash_matches") is True
            and row.get("body_hash_matches") is True
            for row in artifact_checks
        )
        and exact_root
        and exact_plan_pages
    )
    return {
        "artifact": str(IDENTITY_EVIDENCE),
        "artifact_sha256": sha256(IDENTITY_EVIDENCE),
        "strict_pass": strict,
        "exact_root_identity": exact_root,
        "three_diverse_plan_pages_exact": exact_plan_pages,
        "artifact_checks": artifact_checks,
    }


async def replay_once(harness, metadata: dict[str, str], repeat: int) -> dict[str, object]:
    result = await asyncio.wait_for(
        harness.scraper_mod.scrape_jugnu(
            task(),
            archived_render_fetch(),
            page=None,
            profile=None,
            csv_row=metadata,
        ),
        timeout=180,
    )
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [row for row in units if unit_scope_pass(row)]
    numbers = [str(row.get("unit_number") or "") for row in strict]
    uids = [str((row.get("source_ids") or {}).get("entrata_uid") or "") for row in strict]
    source_urls = sorted({str(row.get("source_api_url") or "") for row in strict})
    assertions = {
        "detected_entrata": (result.get("_detected_pms") or {}).get("pms")
        == "entrata",
        "adapter_entrata": result.get("_adapter_used") == "entrata",
        "unit_tier_exact": result.get("extraction_tier_used")
        == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
        "fallback_chain_exact": result.get("_fallback_chain") == ["entrata"],
        "native_positive_units_only": bool(units) and len(units) == len(strict),
        "natural_unit_numbers_unique": len(numbers) == len(set(numbers)),
        "entrata_uids_unique": len(uids) == len(set(uids)),
        "multiple_exact_plan_sources": len(source_urls) >= 3,
        "no_plan_level_rows": len(result.get("plan_summaries") or []) == 0,
        "no_pipeline_errors": result.get("errors") == [],
    }
    return {
        "repeat": repeat,
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "fallback_chain": result.get("_fallback_chain"),
        "units": len(units),
        "strict_native_positive_rent_rows": len(strict),
        "distinct_unit_numbers": len(set(numbers)),
        "distinct_entrata_uids": len(set(uids)),
        "plans": len(result.get("plan_summaries") or []),
        "source_urls": source_urls,
        "entrata_uids": sorted(uids),
        "assertions": assertions,
        "strict_pass": len(assertions) == 10 and all(assertions.values()),
        "units_compact": sorted(
            (compact_unit(row) for row in strict),
            key=lambda row: str(row.get("unit_number") or ""),
        ),
        "raw_api_metadata": [
            {
                "url": row.get("url"),
                "status": row.get("status"),
                "via": row.get("via"),
            }
            for row in (result.get("_raw_api_responses") or [])
            if isinstance(row, dict)
        ],
    }


async def main() -> None:
    for key, expected in EXPECTED_ENV.items():
        if os.environ.get(key, "") != expected:
            raise RuntimeError(f"guardrail env mismatch: {key}")
    if os.environ.get("WEB_UNLOCKER_KEY"):
        raise RuntimeError("WEB_UNLOCKER_KEY must be absent")

    before = ledger_snapshot()
    if before["property_in_ledger"] or not before["property_in_remaining"]:
        raise RuntimeError("PID25489 is not an unadmitted exact-cohort remainder")
    metadata = configured_metadata()
    expected_metadata = {
        "apartmentid": PROPERTY_ID,
        "name": "Cortland North Dallas",
        "address": "17811 Vail St",
        "city": "Dallas",
        "state": "TX",
        "zip": "75287",
        "website": CONFIGURED_URL,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise RuntimeError("configured identity drifted")

    identity = verify_identity_evidence()
    if identity["strict_pass"] is not True:
        raise RuntimeError("archived one-session HB identity evidence failed")

    checks = [
        run_command(
            [
                "ruff",
                "check",
                *(str(path) for path in (*SOURCE_FILES, *TEST_FILES)),
            ]
        ),
        run_command(["pytest", "-q", str(TEST_FILES[0])]),
    ]
    harness = load_harness()
    harness.fetch_mod.fetch = harness.direct_fetch
    reset_hyperbrowser_property_counts()
    reset_web_unlocker_call_count()

    source_before = source_snapshot()
    direct_probe = await harness.direct_fetch(task())
    repeats: list[dict[str, object]] = []
    for repeat in range(1, 4):
        row = await replay_once(harness, metadata, repeat)
        repeats.append(row)
        print(
            json.dumps(
                {
                    key: row[key]
                    for key in (
                        "repeat",
                        "adapter",
                        "tier",
                        "units",
                        "strict_native_positive_rent_rows",
                        "distinct_unit_numbers",
                        "distinct_entrata_uids",
                        "plans",
                        "strict_pass",
                    )
                }
            ),
            flush=True,
        )

    source_after = source_snapshot()
    after = ledger_snapshot()
    uid_sets = [set(row.get("entrata_uids") or []) for row in repeats]
    all_repeats_pass = bool(
        len(repeats) == 3
        and all(row.get("strict_pass") is True for row in repeats)
        and all(uid_sets[0] == item for item in uid_sets[1:])
        and source_before == source_after
        and before == after
        and hyperbrowser_property_call_count(PROPERTY_ID) == 0
        and web_unlocker_call_count() == 0
    )
    payload = {
        "generated_at_utc": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
        "lane": "oakrow_25489_rebrand_exact_property_configured_e2e",
        "property_id": 25489,
        "configured_identity": expected_metadata,
        "current_identity": {
            "name": "Oak Row North Dallas",
            "address": "17811 Vail St",
            "city": "Dallas",
            "state": "TX",
            "zip": "75287",
            "root_url": CURRENT_ROOT,
        },
        "cohort": {
            "boundary": "exact_2026-07-31_FAILED_NO_DATA_344",
            "state_before": before,
            "state_after": after,
        },
        "configured_redirect_probe": {
            "status": direct_probe.status,
            "outcome": direct_probe.outcome.value,
            "final_url": direct_probe.final_url,
            "body_sha256": hashlib.sha256(direct_probe.body or b"").hexdigest(),
        },
        "archived_hyperbrowser_identity": identity,
        "guardrails": {
            "environment": dict(EXPECTED_ENV),
            "llm": False,
            "hyperbrowser_live_replay_calls": hyperbrowser_property_call_count(
                PROPERTY_ID
            ),
            "archived_identity_hyperbrowser_sessions": 1,
            "archived_identity_solve_captchas": False,
            "archived_identity_use_stealth": False,
            "web_unlocker_calls": web_unlocker_call_count(),
            "flaresolverr": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
        },
        "verification": {
            "checks": checks,
            "configured_pipeline_repeats": repeats,
            "stable_uid_set_across_repeats": all(
                uid_sets[0] == item for item in uid_sets[1:]
            ),
            "all_repeats_pass": all_repeats_pass,
        },
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "ledger_mutation": "none",
        "canary_mutation": "none",
        "commit": "none",
        "push": "none",
        "authoritative_ledger_eligible": False,
        "authoritative_ledger_hold_reason": (
            "parent must independently inspect and admit this evidence"
        ),
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256(EVIDENCE),
                "counts": [row["units"] for row in repeats],
                "all_repeats_pass": all_repeats_pass,
            },
            indent=2,
        )
    )
    if not all_repeats_pass:
        raise RuntimeError("Oak Row configured E2E strict gate failed")


if __name__ == "__main__":
    asyncio.run(main())
