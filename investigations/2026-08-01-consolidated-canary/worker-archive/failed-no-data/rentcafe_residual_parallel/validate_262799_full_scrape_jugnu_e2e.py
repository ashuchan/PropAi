from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "rentcafe_residual_parallel"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "262799_current_configured_scrape_jugnu_e2e.json"
PROPERTY_ID = "262799"
NATIVE_PROPERTY_ID = "3152"
EXPECTED_API_URL = (
    "https://nestiolistings.com/api/v2/listings/all/"
    "?key=7536d35593414ef29a6696a9dc35b6fc&property=3152"
)
EXPECTED_TIER = "TIER_1_API_FUNNEL_PUBLISHED_LISTINGS"
EXPECTED_REMAINING_SHA256 = (
    "834306df5112fdf9246f0156ec9c5289aa7f0df85c950c15eb07e50a19a9087e"
)
EXPECTED_LEDGER_SHA256 = (
    "9a5431c456cc01faa09160d8063b949421a9f70a12ce394979f01069bf7e0bd0"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


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
            error_signature=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


def compact_unit(unit: dict) -> dict:
    return {
        "unit_number": unit.get("unit_number"),
        "floor_plan_name": unit.get("floor_plan_name"),
        "bedrooms": unit.get("bedrooms"),
        "bathrooms": unit.get("bathrooms"),
        "sqft": unit.get("sqft"),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "availability_date": unit.get("availability_date"),
        "source_api_url": unit.get("source_api_url"),
        "source_ids": unit.get("source_ids"),
        "source_property_id": unit.get("source_property_id"),
        "source_property_name": unit.get("source_property_name"),
        "source_property_address": unit.get("source_property_address"),
        "source_property_provenance": unit.get("source_property_provenance"),
        "real_native_anchor": unit_has_real_anchor(unit),
        "positive_rent": positive_rent(unit),
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "FETCH_BACKEND": "curl_cffi",
        "WEB_UNLOCKER_API_KEY": "",
        "BRIGHTDATA_API_KEY": "",
        "FLARESOLVERR_URL": "",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "")
        if actual.casefold() != expected.casefold():
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    before_hashes = {
        "remaining": sha256(REMAINING),
        "ledger": sha256(LEDGER),
    }
    if before_hashes["remaining"] != EXPECTED_REMAINING_SHA256:
        raise RuntimeError("remaining CSV changed before E2E")
    if before_hashes["ledger"] != EXPECTED_LEDGER_SHA256:
        raise RuntimeError("strict ledger changed before E2E")

    metadata = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    configured_url = metadata["website"]
    task = CrawlTask(
        url=configured_url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )

    fetch_mod.fetch = direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    fetched = await direct_fetch(task)
    if fetched.outcome != FetchOutcome.OK or not fetched.body:
        raise RuntimeError(
            f"configured direct fetch failed: {fetched.status} {fetched.error_signature}"
        )

    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=metadata,
        ),
        timeout=120,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [row for row in units if unit_has_real_anchor(row) and positive_rent(row)]
    unit_numbers = [str(row.get("unit_number") or "").strip() for row in strict]
    listing_ids = [
        str((row.get("source_ids") or {}).get("funnel_listing_id") or "").strip()
        for row in strict
    ]
    raw_response = next(
        (
            row
            for row in result.get("_raw_api_responses") or []
            if isinstance(row, dict) and row.get("url") == EXPECTED_API_URL
        ),
        {},
    )
    raw_body = raw_response.get("body") if isinstance(raw_response, dict) else None
    raw_items = raw_body.get("items") if isinstance(raw_body, dict) else None

    assertions = {
        "detected_funnel": (result.get("_detected_pms") or {}).get("pms") == "funnel",
        "adapter_funnel": result.get("_adapter_used") == "funnel",
        "exact_tier": result.get("extraction_tier_used") == EXPECTED_TIER,
        "three_emitted_units": len(units) == 3,
        "three_strict_native_positive_units": len(strict) == 3,
        "complete_vs_raw_payload": isinstance(raw_items, list)
        and len(raw_items) == len(strict) == 3,
        "unique_unit_numbers": len(unit_numbers) == len(set(unit_numbers)) == 3,
        "unique_native_listing_ids": len(listing_ids) == len(set(listing_ids)) == 3,
        "all_native_listing_ids_present": all(listing_ids),
        "exact_source_property_id": all(
            str(row.get("source_property_id") or "") == NATIVE_PROPERTY_ID
            for row in strict
        ),
        "exact_source_property_name": all(
            row.get("source_property_name") == "220 East 72nd Street"
            for row in strict
        ),
        "exact_source_property_address": all(
            row.get("source_property_address")
            == "220 East 72nd Street, New York, NY, 10021"
            for row in strict
        ),
        "exact_source_api_url": all(
            row.get("source_api_url") == EXPECTED_API_URL for row in strict
        ),
        "exact_raw_api_url": raw_response.get("url") == EXPECTED_API_URL,
        "exact_raw_via": raw_response.get("via")
        == "published_nestio_listings_direct",
        "no_pipeline_errors": not (result.get("errors") or []),
        "no_web_unlocker_calls": web_unlocker_call_count() == 0,
        "no_hyperbrowser_calls": hyperbrowser_property_call_count(PROPERTY_ID) == 0,
    }
    if not all(assertions.values()):
        failed = [name for name, passed in assertions.items() if not passed]
        raise RuntimeError(f"strict E2E assertion(s) failed: {failed}")

    after_hashes = {
        "remaining": sha256(REMAINING),
        "ledger": sha256(LEDGER),
    }
    if after_hashes != before_hashes:
        raise RuntimeError("remaining CSV or strict ledger changed during E2E")

    source_files = {
        "ma_poc/pms/adapters/funnel.py": sha256(
            Path("ma_poc/pms/adapters/funnel.py")
        ),
        "ma_poc/tests/pms/adapters/test_funnel_nestio_items.py": sha256(
            Path("ma_poc/tests/pms/adapters/test_funnel_nestio_items.py")
        ),
    }
    git_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "rentcafe_residual_current_configured_funnel_nestio_recovery",
        "property": {
            "property_id": int(PROPERTY_ID),
            "name": metadata.get("name"),
            "address": metadata.get("address"),
            "city": metadata.get("city"),
            "state": metadata.get("state"),
            "zip": metadata.get("zip"),
            "configured_url": configured_url,
        },
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body),
            "body_sha256": hashlib.sha256(fetched.body).hexdigest(),
        },
        "pipeline": {
            "detected_pms": (result.get("_detected_pms") or {}).get("pms"),
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "winning_page_url": result.get("_winning_page_url"),
            "emitted_units": len(units),
            "strict_native_positive_rent_units": len(strict),
            "plan_summaries": len(result.get("plan_summaries") or []),
            "errors": result.get("errors") or [],
            "elapsed_ms": elapsed_ms,
        },
        "strict_assertions": assertions,
        "units": [compact_unit(row) for row in strict],
        "raw_api_response": raw_response,
        "raw_api_body_canonical_sha256": json_sha256(raw_body),
        "guardrails": {
            "direct_only": True,
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "proxy": False,
            "paid_canary": False,
            "hyperbrowser": False,
            "web_unlocker_call_count": web_unlocker_call_count(),
            "hyperbrowser_call_count": hyperbrowser_property_call_count(PROPERTY_ID),
            "environment": expected_env,
        },
        "immutability": {
            "before": before_hashes,
            "after": after_hashes,
        },
        "source_snapshot": {
            "git_head": git_head,
            "critical_file_sha256": source_files,
        },
        "verdict": (
            "pass_exact_configured_page_published_nestio_property_native_units"
        ),
    }
    OUTPUT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "detected_pms": payload["pipeline"]["detected_pms"],
                "adapter": payload["pipeline"]["adapter"],
                "tier": payload["pipeline"]["tier"],
                "units": payload["pipeline"]["emitted_units"],
                "strict": payload["pipeline"]["strict_native_positive_rent_units"],
                "unit_numbers": unit_numbers,
                "listing_ids": listing_ids,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
