from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.entrata import _beans_address_matches


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "entrata_residual_lane"
OUTPUT = LANE / "evidence_tuscany_22986_beans_map_configured_e2e_3x.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
FAILED = ROOT / "failed344.csv"
PROPERTY_ID = "22986"
EXPECTED_MAP_PROPERTY_ID = "1182838"
EXPECTED_MAP_URL = (
    "https://www.tuscanyhillsapartments.com/Apartments/module/property_info/"
    "action/view_beans_map/property%5Bid%5D/1182838/"
    "?occupancy_type=conventional&analytics=1"
)
SOURCE_FILES = (
    Path("ma_poc/pms/adapters/entrata.py"),
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/tests/pms/adapters/test_entrata_beans_map.py"),
    Path("ma_poc/tests/pms/test_entrata_redirect_identity_guard.py"),
)
EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "FETCH_BACKEND": "requests",
    "ENABLE_HYPERBROWSER": "false",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER5_VISION": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
    "PROBE_PROXY_URL": "",
    "WEB_UNLOCKER_KEY": "",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes() -> dict[str, str]:
    return {str(path): sha256(path) for path in SOURCE_FILES}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def metadata() -> dict[str, str]:
    matches = [
        row for row in read_rows(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one configured row, got {len(matches)}")
    row = matches[0]
    expected = {
        "name": "Tuscany Hills",
        "address": "715 Ash Ln",
        "city": "San Marcos",
        "state": "CA",
        "zip": "92069",
        "website": "http://www.tuscanyhillsapartments.com/",
    }
    if any(row.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"configured identity changed: {row!r}")
    return row


def assert_failed_cohort_member() -> None:
    matches = [
        row for row in read_rows(FAILED) if row.get("property_id") == PROPERTY_ID
    ]
    if len(matches) != 1 or matches[0].get("website") != (
        "http://www.tuscanyhillsapartments.com/"
    ):
        raise RuntimeError("Tuscany is not the exact configured FAILED_NO_DATA member")


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and float(row[field]) > 0
        for field in ("market_rent_low", "market_rent_high")
    )


def make_task(configured_url: str) -> CrawlTask:
    return CrawlTask(
        url=configured_url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


async def direct_fetch(
    task: CrawlTask,
    profile: object | None = None,
) -> FetchResult:
    del profile
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(
            probe_get,
            task.url,
            timeout=30,
            unlocker=False,
            retries=1,
            proxies={},
            verify=True,
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(getattr(response, "headers", {}) or {}),
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


def compact(row: dict[str, Any]) -> dict[str, Any]:
    source_ids = row.get("source_ids")
    return {
        "unit_number": str(row.get("unit_number") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "bedrooms": row.get("bedrooms"),
        "bathrooms": row.get("bathrooms"),
        "sqft": row.get("sqft"),
        "market_rent_low": row.get("market_rent_low"),
        "market_rent_high": row.get("market_rent_high"),
        "availability_status": row.get("availability_status"),
        "availability_date": str(row.get("availability_date") or ""),
        "source_api_url": str(row.get("source_api_url") or ""),
        "source_property_address": str(row.get("source_property_address") or ""),
        "source_ids": dict(source_ids) if isinstance(source_ids, dict) else {},
    }


async def replay_once(repeat: int, row: dict[str, str]) -> dict[str, Any]:
    task = make_task(row["website"])
    fetched = await direct_fetch(task)
    body = (fetched.body or b"").decode("utf-8", "replace")
    visible = re.sub(
        r"[^a-z0-9]+",
        " ",
        BeautifulSoup(body, "lxml").get_text(" ", strip=True).casefold(),
    ).strip()
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=row,
        ),
        timeout=90,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    units = [item for item in (result.get("units") or []) if isinstance(item, dict)]
    strict = [
        item
        for item in units
        if unit_has_real_anchor(item)
        and positive_rent(item)
        and str(item.get("unit_number") or "").strip()
    ]
    unit_numbers = [str(item.get("unit_number") or "") for item in strict]
    source_ids = [item.get("source_ids") or {} for item in strict]
    pipeline_errors = [str(value) for value in (result.get("errors") or [])]
    fatal_errors = [
        value
        for value in pipeline_errors
        if not value.startswith("ENTRATA_NO_RESPONSE:")
    ]
    checks = {
        "configured_fetch_200": fetched.status == 200,
        "configured_final_host": fetched.final_url
        == "https://www.tuscanyhillsapartments.com/",
        "configured_name_visible": "tuscany hills" in visible,
        "adapter_entrata": result.get("_adapter_used") == "entrata",
        "beans_tier": result.get("extraction_tier_used")
        == "TIER_1_DOM_ENTRATA_BEANS_MAP",
        "thirteen_emitted": len(units) == 13,
        "all_emitted_strict": len(strict) == len(units) == 13,
        "distinct_native_unit_numbers": len(set(unit_numbers)) == len(unit_numbers),
        "exact_published_map_source": all(
            item.get("source_api_url") == EXPECTED_MAP_URL for item in strict
        ),
        "configured_address_boundary": all(
            _beans_address_matches(
                str(item.get("source_property_address") or ""),
                row["address"],
                row["zip"],
            )
            for item in strict
        ),
        "native_ids_complete": all(
            all(
                str(ids.get(key) or "").isdigit()
                for key in (
                    "entrata_uid",
                    "entrata_fpid",
                    "entrata_property_id",
                    "entrata_beans_listing_id",
                )
            )
            and str(ids.get("entrata_property_id")) == EXPECTED_MAP_PROPERTY_ID
            for ids in source_ids
        ),
        "available_status": all(
            item.get("availability_status") == "AVAILABLE" for item in strict
        ),
        # Path-B can retain the first empty Entrata attempt's diagnostic after
        # the configured retry wins via the strict Beans tier.  Preserve it in
        # evidence, but do not treat that superseded diagnostic as fatal.
        "no_fatal_pipeline_errors": not fatal_errors,
        "no_plan_fallback": not (result.get("plan_summaries") or []),
    }
    if not all(checks.values()):
        raise RuntimeError(
            f"repeat {repeat} failed: {checks!r}; errors={result.get('errors')!r}"
        )
    return {
        "repeat": repeat,
        "configured_fetch": {
            "status": fetched.status,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "body_sha256": hashlib.sha256(fetched.body or b"").hexdigest(),
        },
        "elapsed_ms": elapsed_ms,
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "emitted_rows": len(units),
        "strict_native_positive_rent_rows": len(strict),
        "availability_date_present_rows": sum(
            bool(str(item.get("availability_date") or "").strip()) for item in strict
        ),
        "checks": checks,
        "units": [compact(item) for item in strict],
        "errors": pipeline_errors,
        "fatal_errors": fatal_errors,
    }


def run_tests() -> dict[str, Any]:
    entrata_tests = [
        str(path)
        for path in sorted(Path("ma_poc/tests/pms/adapters").glob("test_entrata*.py"))
    ]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *entrata_tests,
        "ma_poc/tests/pms/test_entrata_redirect_identity_guard.py",
        "ma_poc/tests/pms/test_livebh_portfolio_redirect_guard.py",
    ]
    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, text=True)
    result = {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if proc.returncode:
        raise RuntimeError(json.dumps(result, indent=2))
    return result


async def main() -> None:
    actual_env = {key: os.environ.get(key, "") for key in EXPECTED_ENV}
    if actual_env != EXPECTED_ENV:
        raise RuntimeError(
            f"guardrail environment mismatch: actual={actual_env!r}"
        )
    assert_failed_cohort_member()
    row = metadata()
    hashes_before = source_hashes()
    fetch_mod.fetch = direct_fetch
    repeats = [await replay_once(repeat, row) for repeat in range(1, 4)]
    tests = run_tests()
    hashes_after = source_hashes()
    if hashes_before != hashes_after:
        raise RuntimeError("source changed during configured replay")

    evidence = {
        "schema_version": "tuscany_beans_map_configured_e2e_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "property_id": int(PROPERTY_ID),
        "configured_property": row,
        "expected_source": {
            "map_url": EXPECTED_MAP_URL,
            "entrata_property_id": EXPECTED_MAP_PROPERTY_ID,
            "published_address": "715 Ash Lane, San Marcos, CA, , 92069, US",
        },
        "guardrails": {
            "direct_only": True,
            "hyperbrowser": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "llm": False,
            "paid_canary": False,
            "environment": actual_env,
        },
        "input_hashes": {
            str(PROPERTIES): sha256(PROPERTIES),
            str(FAILED): sha256(FAILED),
        },
        "source_hashes": hashes_after,
        "test_run": tests,
        "repeats": repeats,
        "verification": {
            "repeat_count": len(repeats),
            "all_repeats_strict": all(
                item["strict_native_positive_rent_rows"] == item["emitted_rows"] == 13
                and all(item["checks"].values())
                for item in repeats
            ),
            "stable_unit_number_set": len(
                {
                    tuple(sorted(unit["unit_number"] for unit in item["units"]))
                    for item in repeats
                }
            )
            == 1,
            "source_stable": hashes_before == hashes_after,
            "strict_admission_ready": True,
        },
    }
    OUTPUT.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "output_sha256": sha256(OUTPUT),
                "source_hashes": hashes_after,
                "strict_rows_each": [
                    item["strict_native_positive_rent_rows"] for item in repeats
                ],
                "tests": tests["stdout"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
