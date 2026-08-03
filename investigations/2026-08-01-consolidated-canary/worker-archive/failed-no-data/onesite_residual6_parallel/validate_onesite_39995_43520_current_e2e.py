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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/onesite_residual6_parallel")
FAILED = Path("/private/tmp/propai-fnd-vBkmT9/failed344.json")
OUTPUT = ROOT / "evidence_onesite_39995_43520_current_e2e.json"
SOURCE = Path("ma_poc/pms/adapters/onesite.py")
TEST = Path("ma_poc/tests/pms/adapters/test_onesite_rpfp_cws.py")
PROPERTIES = Path("ma_poc/config/properties.csv")
TARGETS = ("39995", "43520", "14295")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


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


def sample(row: dict) -> dict:
    return {
        "unit_number": str(row.get("unit_number") or ""),
        "source_native_unit_id": str(row.get("source_native_unit_id") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "market_rent_low": row.get("market_rent_low"),
        "source_property_id": str(row.get("source_property_id") or ""),
        "source_partner_property_id": str(row.get("source_partner_property_id") or ""),
        "source_property_provenance": str(row.get("source_property_provenance") or ""),
        "source_api_url": str(row.get("source_api_url") or ""),
        "source_portal_url": str(row.get("source_portal_url") or ""),
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
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(task, fetched, page=None, profile=None, csv_row=metadata),
        timeout=180,
    )
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [row for row in units if unit_has_real_anchor(row) and positive_rent(row)]
    unit_numbers = [str(row.get("unit_number") or "").strip() for row in strict]
    native_ids = [str(row.get("source_native_unit_id") or "").strip() for row in strict]
    return {
        "property_id": int(pid),
        "property_name": metadata.get("proj_name") or "",
        "canonical_address": metadata.get("address") or "",
        "canonical_city": metadata.get("city") or "",
        "canonical_state": metadata.get("state") or "",
        "canonical_zip": metadata.get("zip_code") or metadata.get("zip") or "",
        "configured_url": task.url,
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "body_sha256": hashlib.sha256(fetched.body or b"").hexdigest(),
        },
        "detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(units),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "strict_native_positive_rent_rows": len(strict),
        "all_emitted_rows_strict": bool(strict and len(strict) == len(units)),
        "native_unit_numbers_nonblank_unique": bool(
            strict and all(unit_numbers) and len(unit_numbers) == len(set(unit_numbers))
        ),
        "source_native_ids_nonblank_unique": bool(
            strict and all(native_ids) and len(native_ids) == len(set(native_ids))
        ),
        "source_property_ids": sorted({str(row.get("source_property_id") or "") for row in strict}),
        "source_partner_property_ids": sorted(
            {str(row.get("source_partner_property_id") or "") for row in strict if row.get("source_partner_property_id")}
        ),
        "source_provenance": sorted(
            {str(row.get("source_property_provenance") or "") for row in strict}
        ),
        "source_api_urls": sorted({str(row.get("source_api_url") or "") for row in strict}),
        "samples": [sample(row) for row in strict[:5]],
        "strict_rows": [sample(row) for row in strict],
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
    failed_metadata = {
        str(row["property_id"]): row
        for row in failed
        if str(row.get("property_id")) in TARGETS
    }
    configured = {
        str(row["apartmentid"]): row
        for row in read_csv(PROPERTIES)
        if str(row.get("apartmentid")) in TARGETS
    }
    metadata = {
        pid: {
            **failed_metadata[pid],
            "proj_name": configured[pid]["name"],
            "address": configured[pid]["address"],
            "city": configured[pid]["city"],
            "state": configured[pid]["state"],
            "zip_code": configured[pid]["zip"],
            "zip": configured[pid]["zip"],
            "website": configured[pid]["website"],
        }
        for pid in TARGETS
    }
    assert set(metadata) == set(TARGETS)
    assert set(configured) == set(TARGETS)
    assert "38677" not in metadata

    reset_hyperbrowser_property_counts()
    reset_web_unlocker_call_count()
    fetch_mod.fetch = direct_fetch
    results = []
    for pid in TARGETS:
        row = await run_one(metadata[pid])
        results.append(row)
        print(json.dumps({"property_id": pid, "tier": row["tier"], "strict": row["strict_native_positive_rent_rows"]}), flush=True)

    by_pid = {str(row["property_id"]): row for row in results}
    p39995 = by_pid["39995"]
    p43520 = by_pid["43520"]
    p14295 = by_pid["14295"]
    assertions = {
        "pid39995_exact_workflow_native_positive": bool(
            p39995["tier"] == "TIER_1_API_ONESITE_WORKFLOW"
            and p39995["property_name"] == "South Pointe"
            and p39995["canonical_address"] == "6220 N Murray Dr"
            and p39995["canonical_city"] == "Hanahan"
            and p39995["canonical_state"] == "SC"
            and p39995["canonical_zip"] == "29410"
            and p39995["strict_native_positive_rent_rows"] == 1
            and p39995["all_emitted_rows_strict"]
            and p39995["native_unit_numbers_nonblank_unique"]
            and p39995["source_property_ids"] == ["5272798"]
            and p39995["source_provenance"] == ["published_portal_shell"]
            and all("/workflowstartup/v1/5272798/English" in url for url in p39995["source_api_urls"])
            and len(p39995["strict_rows"]) == 1
            and p39995["strict_rows"][0]["unit_number"] == "52"
            and p39995["strict_rows"][0]["floor_plan_name"] == "B1"
            and p39995["strict_rows"][0]["market_rent_low"] == 1263
            and p39995["strict_rows"][0]["source_portal_url"]
            == "http://9067331.onlineleasing.realpage.com/"
        ),
        "pid43520_exact_rpfp_native_positive": bool(
            p43520["tier"] == "TIER_1_API_ONESITE_RPFP_CWS"
            and p43520["property_name"] == "Park at Blanding"
            and p43520["canonical_address"] == "222 Blairmore Blvd E"
            and p43520["canonical_city"] == "Orange Park"
            and p43520["canonical_state"] == "FL"
            and p43520["canonical_zip"] == "32073"
            and p43520["strict_native_positive_rent_rows"] == 26
            and p43520["all_emitted_rows_strict"]
            and p43520["native_unit_numbers_nonblank_unique"]
            and p43520["source_native_ids_nonblank_unique"]
            and len(p43520["strict_rows"]) == 26
            and p43520["source_property_ids"] == ["9259508"]
            and p43520["source_partner_property_ids"] == ["5586626"]
            and p43520["source_provenance"] == ["same_origin_rpfp_property_details"]
            and p43520["source_api_urls"] == ["https://api.ws.realpage.com/v2/property/9259508/units"]
        ),
        "pid14295_no_native_unitids_stays_nonunit": bool(
            p14295["units"] == 0
            and p14295["strict_native_positive_rent_rows"] == 0
            and p14295["tier"]
            in {
                "TIER_1_API_ONESITE_WORKFLOW",
                "TIER_1_API_ONESITE_EMPTY",
                "TIER_1_API_ONESITE_NO_RESPONSE",
            }
        ),
        "exact_three_member_boundary_run": [str(row["property_id"]) for row in results] == list(TARGETS),
        "no_paid_or_unlocker_calls": bool(
            web_unlocker_call_count() == 0
            and all(hyperbrowser_property_call_count(pid) == 0 for pid in TARGETS)
        ),
    }

    test_run = subprocess.run(
        [
            "pytest",
            "-q",
            "ma_poc/tests/pms/adapters/test_onesite_rpfp_cws.py",
            "ma_poc/tests/pms/adapters/test_onesite_workflow.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assertions["focused_tests_pass"] = test_run.returncode == 0
    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "run_type": "current configured scrape_jugnu E2E; LLM off; direct-only; no canary",
        "guardrails": {
            "llm_enabled": False,
            "hyperbrowser_calls": {pid: hyperbrowser_property_call_count(pid) for pid in TARGETS},
            "web_unlocker_calls": web_unlocker_call_count(),
            "flaresolverr": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "ledger_or_builder_modified": False,
            "pid38677_in_scope": False,
        },
        "inputs": {
            "failed344": str(FAILED),
            "failed344_sha256": sha256(FAILED),
            "source": str(SOURCE),
            "source_sha256": sha256(SOURCE),
            "test": str(TEST),
            "test_sha256": sha256(TEST),
            "properties": str(PROPERTIES),
            "properties_sha256": sha256(PROPERTIES),
        },
        "focused_test_run": {
            "returncode": test_run.returncode,
            "stdout": test_run.stdout.strip(),
            "stderr": test_run.stderr.strip(),
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
