from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/riverwalk_wimmer_lane")
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
EVIDENCE = ROOT / "evidence_riverwalk_71534_stale_guard_e2e.json"
PID = "71534"
STALE_URL = (
    "https://www.wimmercommunities.com/apartments/menomonee-falls/"
    "riverwalk-on-the-falls/"
)
CURRENT_URL = (
    "https://www.wimmercommunities.com/apartments/wi/menomonee-falls/"
    "riverwalk-on-the-falls/floorplans"
)
EXPECTED_API = "https://sightmap.com/app/api/v1/y8px5ljmv19/sightmaps/100325"
SOURCE_FILES = (
    "ma_poc/core/identity.py",
    "ma_poc/pms/adapters/sightmap.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/scraper.py",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict[str, str]:
    return {path: sha((REPO / path).read_bytes()) for path in SOURCE_FILES}


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def direct_get(url: str):
    return requests.get(
        url,
        timeout=45,
        impersonate="chrome",
        allow_redirects=True,
    )


def fetch_result(url: str, response, outcome: FetchOutcome) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=outcome,
        status=response.status_code,
        body=response.content,
        headers=dict(response.headers),
        render_mode=RenderMode.GET,
        final_url=str(response.url),
        attempts=1,
        elapsed_ms=0,
    )


async def run_repeat(
    repeat: int,
    metadata: dict[str, str],
    initial: FetchResult,
) -> dict[str, Any]:
    candidate_requests: list[str] = []

    async def direct_l1(task) -> FetchResult:
        candidate_requests.append(task.url)
        if task.url != CURRENT_URL:
            raise AssertionError(f"fail-closed boundary violated: {task.url}")
        response = direct_get(task.url)
        outcome = (
            FetchOutcome.OK
            if response.status_code == 200
            else FetchOutcome.DEAD_URL
        )
        return fetch_result(task.url, response, outcome)

    task = CrawlTask(
        url=STALE_URL,
        property_id=PID,
        priority=0,
        budget_ms=180_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    with patch("ma_poc.fetch.fetch", direct_l1, create=True):
        result = await asyncio.wait_for(
            scrape_jugnu(task, initial, csv_row=metadata),
            timeout=180,
        )

    emitted = [
        row for row in (result.get("units") or []) if isinstance(row, dict)
    ]
    strict = [
        row
        for row in emitted
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    numbers = [str(row.get("unit_number") or "") for row in strict]
    source_urls = sorted(
        {
            str(row.get("source_api_url"))
            for row in strict
            if row.get("source_api_url")
        }
    )
    guard = result.get("_wimmer_stale_path_recovery")
    checks = {
        "soft_404_entered_pipeline": result.get("_soft_404_recovery") is True,
        "link_hop_succeeded": result.get("_link_hop_success") is True,
        "exact_guard_anchor": (
            result.get("_link_hop_anchor")
            == "rediscovery:wimmer_state_path"
        ),
        "guard_identity_match": isinstance(guard, dict)
        and guard.get("identity_match") is True,
        "guard_portfolio_fallbacks_disabled": isinstance(guard, dict)
        and guard.get("portfolio_fallbacks_disabled") is True,
        "only_exact_candidate_fetched": candidate_requests == [CURRENT_URL],
        "sightmap_adapter": result.get("_adapter_used") == "sightmap",
        "tier_1_sightmap": (
            result.get("extraction_tier_used")
            == "TIER_1_API_SIGHTMAP_IFRAME"
        ),
        "eight_distinct_strict_units": (
            len(emitted) == len(strict) == len(set(numbers)) == 8
        ),
        "exact_source_api": source_urls == [EXPECTED_API],
        "all_floorplan_names": all(
            str(row.get("floor_plan_name") or "").strip() for row in strict
        ),
        "all_availability_dates": all(
            re.fullmatch(
                r"20\d\d-\d\d-\d\d",
                str(row.get("availability_date") or ""),
            )
            for row in strict
        ),
    }
    if not all(checks.values()):
        raise AssertionError(
            f"repeat {repeat} failed: {checks!r}; errors={result.get('errors')!r}"
        )
    return {
        "repeat": repeat,
        "checks": checks,
        "candidate_requests": candidate_requests,
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "guard": guard,
        "emitted_rows": len(emitted),
        "strict_native_positive_rent_rows": len(strict),
        "unit_numbers": numbers,
        "source_urls": source_urls,
        "entry_page_diagnostics": result.get("errors") or [],
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_DC_PROXY_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
        "ENABLE_RESIDENTIAL_TIER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER5_VISION": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "PROBE_PROXY_URL": "",
        "WEB_UNLOCKER_KEY": "",
    }
    actual_env = {key: os.environ.get(key, "") for key in expected_env}
    if actual_env != expected_env:
        raise SystemExit(f"guardrail environment mismatch: {actual_env!r}")

    with (REPO / "ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        metadata = next(
            row for row in csv.DictReader(handle) if row["apartmentid"] == PID
        )

    before = source_hashes()
    stale = direct_get(STALE_URL)
    if stale.status_code != 404 or len(stale.content) < 20_000:
        raise SystemExit(
            f"configured route no longer has measured stale shape: "
            f"status={stale.status_code} bytes={len(stale.content)}"
        )
    initial = fetch_result(STALE_URL, stale, FetchOutcome.DEAD_URL)
    repeats = [
        await run_repeat(repeat, metadata, initial)
        for repeat in range(1, 4)
    ]
    number_sets = [row["unit_numbers"] for row in repeats]
    if not all(numbers == number_sets[0] for numbers in number_sets):
        raise SystemExit(f"unit set drift across repeats: {number_sets!r}")

    after = source_hashes()
    if before != after:
        raise SystemExit("production sources changed during evidence run")

    script = Path(__file__).resolve()
    evidence = {
        "lane": "wimmer_stale_missing_state_fail_closed_e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "exact_2026-07-31_FAILED_NO_DATA_344",
        "property_id": int(PID),
        "property_name": metadata["name"],
        "configured_url": STALE_URL,
        "expected_exact_floorplans_url": CURRENT_URL,
        "outcome": "UNIT_QUALIFIED",
        "ledger_mutation": "none",
        "commit": "none",
        "push": "none",
        "paid_canary": False,
        "guardrails": {
            "environment": actual_env,
            "direct_public_http_only": True,
            "hyperbrowser_calls": 0,
            "llm_calls": 0,
            "proxy_calls": 0,
            "web_unlocker_calls": 0,
            "flaresolverr_calls": 0,
            "captcha_solving": False,
            "fingerprint_rotation": False,
        },
        "configured_route": {
            "status": stale.status_code,
            "body_bytes": len(stale.content),
            "body_sha256": sha(stale.content),
            "final_url": str(stale.url),
        },
        "three_full_configured_url_repeats": repeats,
        "same_eight_units_each_repeat": True,
        "source_snapshot_before": before,
        "source_snapshot_after": after,
        "materializer": {
            "path": str(script),
            "sha256": sha(script.read_bytes()),
        },
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(EVIDENCE),
                "evidence_sha256": sha(EVIDENCE.read_bytes()),
                "materializer": str(script),
                "materializer_sha256": sha(script.read_bytes()),
                "source_hashes": after,
                "repeats": len(repeats),
                "strict_units_each_repeat": [
                    row["strict_native_positive_rent_rows"] for row in repeats
                ],
                "unit_numbers": number_sets[0],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
