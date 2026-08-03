from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import (
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.adapters._static_team_unit_roster import (
    has_static_team_unit_roster_shape,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/onesite_residual6_parallel")
FAILED = Path("/private/tmp/propai-fnd-vBkmT9/failed344.json")
OUTPUT = ROOT / "evidence_tor_view_38677_page_local_e2e.json"
SOURCE = Path("ma_poc/pms/adapters/_static_team_unit_roster.py")
TEST = Path("ma_poc/tests/pms/adapters/test_static_team_unit_roster.py")
BOUNDARY_IDS = ("38677", "14295", "291774")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(row: dict) -> bool:
    for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high", "rent"):
        try:
            if float(row.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(re.search(r"\$\s*[1-9]", str(row.get("rent_range") or "")))


async def direct_fetch(task: CrawlTask, profile=None) -> FetchResult:
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
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = FetchOutcome.OK if 200 <= status < 300 and body else FetchOutcome.HARD_FAIL
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(getattr(response, "headers", {}) or {}),
            render_mode=RenderMode.GET,
            final_url=str(getattr(response, "url", "") or task.url),
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
            render_mode=RenderMode.GET,
            final_url=task.url,
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_signature=f"{type(exc).__name__}: {str(exc)[:200]}",
        )


def context_for(metadata: dict, fetched: FetchResult) -> AdapterContext:
    html = (fetched.body or b"").decode("utf-8", errors="replace")
    return AdapterContext(
        base_url=fetched.final_url or metadata["website"],
        detected=detect_pms(fetched.final_url or metadata["website"], page_html=html),
        profile=None,
        expected_total_units=None,
        property_id=str(metadata["property_id"]),
        fetch_result=fetched,
        property_name=metadata.get("proj_name") or "",
        address=metadata.get("address") or "",
        city=metadata.get("city") or "",
        state=metadata.get("state") or "",
        zip_code=metadata.get("zip_code") or "",
    )


def sample(row: dict) -> dict:
    return {
        "unit_number": str(row.get("unit_number") or ""),
        "source_native_unit_id": str(row.get("source_native_unit_id") or ""),
        "source_unit_address_label": str(row.get("source_unit_address_label") or ""),
        "source_street_label": str(row.get("source_street_label") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "bedrooms": row.get("bedrooms"),
        "sqft": row.get("sqft"),
        "market_rent_low": row.get("market_rent_low"),
        "source_listing_url": str(row.get("source_listing_url") or ""),
        "source_property_provenance": str(row.get("source_property_provenance") or ""),
        "source_api_url": str(row.get("source_api_url") or ""),
    }


async def run_one(metadata: dict) -> dict:
    pid = str(metadata["property_id"])
    task = CrawlTask(
        url=metadata["website"],
        property_id=pid,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    fetched = await direct_fetch(task)
    shape = has_static_team_unit_roster_shape(context_for(metadata, fetched))
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(task, fetched, page=None, profile=None, csv_row=metadata),
        timeout=180,
    )
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [row for row in units if unit_has_real_anchor(row) and positive_rent(row)]
    return {
        "property_id": int(pid),
        "property_name": metadata.get("proj_name") or "",
        "configured_url": task.url,
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "body_sha256": hashlib.sha256(fetched.body or b"").hexdigest(),
        },
        "team_roster_shape": shape,
        "detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "units": len(units),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "strict_native_positive_rent_rows": len(strict),
        "samples": [sample(row) for row in strict[:10]],
        "errors": result.get("errors") or [],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")
    if os.environ.get("WEB_UNLOCKER_KEY", "").strip():
        raise RuntimeError("WEB_UNLOCKER_KEY must be blank")

    failed = json.loads(FAILED.read_text())
    metadata = {
        str(row["property_id"]): row
        for row in failed
        if str(row.get("property_id")) in BOUNDARY_IDS
    }
    assert set(metadata) == set(BOUNDARY_IDS)
    reset_hyperbrowser_property_counts()
    reset_web_unlocker_call_count()
    fetch_mod.fetch = direct_fetch

    results = []
    for pid in BOUNDARY_IDS:
        row = await run_one(metadata[pid])
        results.append(row)
        print(
            json.dumps(
                {
                    "property_id": pid,
                    "team_shape": row["team_roster_shape"],
                    "tier": row["tier"],
                    "strict": row["strict_native_positive_rent_rows"],
                }
            ),
            flush=True,
        )

    by_pid = {str(row["property_id"]): row for row in results}
    tor = by_pid["38677"]
    tor_rows = tor["samples"]
    assertions = {
        "exact_three_live_configured_boundary_members": [
            str(row["property_id"]) for row in results
        ]
        == list(BOUNDARY_IDS),
        "team_shape_activates_only_for_tor_view": bool(
            tor["team_roster_shape"]
            and not by_pid["14295"]["team_roster_shape"]
            and not by_pid["291774"]["team_roster_shape"]
        ),
        "pid38677_full_scrape_jugnu_strict_success": bool(
            tor["tier"] == "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
            and tor["adapter"] == "generic_plan_text"
            and tor["units"] == 5
            and tor["plan_summaries"] == 0
            and tor["strict_native_positive_rent_rows"] == 5
            and len(tor_rows) == 5
        ),
        "all_tor_rows_native_unique_positive": bool(
            all(row["unit_number"] and row["source_native_unit_id"] == row["unit_number"] for row in tor_rows)
            and len({row["unit_number"] for row in tor_rows}) == len(tor_rows)
            and all(float(row["market_rent_low"] or 0) > 0 for row in tor_rows)
        ),
        "unit_street_plan_fields_never_conflated": bool(
            all(
                row["unit_number"] != row["source_street_label"]
                and row["unit_number"] != row["floor_plan_name"]
                and row["source_unit_address_label"].startswith(row["unit_number"] + " ")
                for row in tor_rows
            )
        ),
        "all_tor_rows_exact_first_party_provenance": bool(
            all(
                row["source_property_provenance"]
                == "exact_configured_property_team_card_roster"
                and row["source_api_url"] == "https://www.torviewvillageapts.com/"
                for row in tor_rows
            )
        ),
        "nonteam_boundary_members_never_use_new_tier": all(
            by_pid[pid]["tier"] != "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
            for pid in ("14295", "291774")
        ),
        "no_paid_or_unlocker_calls": bool(
            web_unlocker_call_count() == 0
            and all(hyperbrowser_property_call_count(pid) == 0 for pid in BOUNDARY_IDS)
        ),
    }

    tests = subprocess.run(
        [
            "pytest",
            "-q",
            "ma_poc/tests/pms/adapters/test_static_team_unit_roster.py",
            "ma_poc/tests/pms/test_page_local_static_recovery.py",
            "ma_poc/tests/pms/adapters/test_generic_plan_text.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assertions["focused_tests_pass"] = tests.returncode == 0
    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "run_type": "three live configured boundaries + full scrape_jugnu; direct-only; LLM off; no canary",
        "guardrails": {
            "llm_enabled": False,
            "hyperbrowser_calls": {
                pid: hyperbrowser_property_call_count(pid) for pid in BOUNDARY_IDS
            },
            "web_unlocker_calls": web_unlocker_call_count(),
            "flaresolverr": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "ledger_or_builder_modified": False,
        },
        "inputs": {
            "failed344": str(FAILED),
            "failed344_sha256": sha256(FAILED),
            "source": str(SOURCE),
            "source_sha256": sha256(SOURCE),
            "test": str(TEST),
            "test_sha256": sha256(TEST),
        },
        "focused_test_run": {
            "returncode": tests.returncode,
            "stdout": tests.stdout.strip(),
            "stderr": tests.stderr.strip(),
        },
        "assertions": assertions,
        "self_verified": all(assertions.values()),
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if not payload["self_verified"]:
        raise AssertionError(json.dumps(assertions, sort_keys=True))
    print(json.dumps({"artifact": str(OUTPUT), "sha256": sha256(OUTPUT), "self_verified": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
