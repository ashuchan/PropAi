from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "scan_remaining_current_pipeline.json"

# Other live workers own AppFolio and Knock. Unknown was already exhaustively
# split across two completed residual audits. The OneSite exact-publication
# cluster was already exhaustively audited in this lane.
EXCLUDED_ADAPTERS = {"appfolio", "knock", "unknown", "onesite"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_url(value: str) -> str:
    value = value.strip()
    return value if "://" in value else f"https://{value}"


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
            timeout=25,
            unlocker=False,
            retries=1,
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
            headers={},
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
            error_signature=f"{type(exc).__name__}: {str(exc)[:160]}",
        )


def make_task(url: str, property_id: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=40_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


async def one(residual: dict[str, str], metadata: dict[str, str]) -> dict:
    pid = residual["property_id"]
    url = normalize_url(metadata.get("website") or residual.get("website") or "")
    task = make_task(url, pid)
    fetched = await direct_fetch(task)
    started = time.monotonic()
    try:
        result = await scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "property_id": int(pid),
            "property_name": metadata.get("name") or residual.get("property_name") or "",
            "website": url,
            "residual_detected_adapter": residual.get("current_detected_adapter") or "",
            "configured_status": fetched.status,
            "configured_final_url": fetched.final_url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "exception": f"{type(exc).__name__}: {str(exc)[:500]}",
            "strict_native_priced_rows": 0,
        }
    units = [item for item in result.get("units") or [] if isinstance(item, dict)]
    strict = [
        item
        for item in units
        if unit_has_real_anchor(item) and positive_rent(item)
    ]
    return {
        "property_id": int(pid),
        "property_name": metadata.get("name") or residual.get("property_name") or "",
        "canonical_address": metadata.get("address") or "",
        "website": url,
        "residual_detected_adapter": residual.get("current_detected_adapter") or "",
        "configured_status": fetched.status,
        "configured_final_url": fetched.final_url,
        "configured_outcome": fetched.outcome.value,
        "current_detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "emitted_rows": len(units),
        "strict_native_priced_rows": len(strict),
        "source_property_ids": sorted(
            {
                str(item.get("source_property_id") or "")
                for item in strict
                if item.get("source_property_id") not in (None, "")
            }
        ),
        "source_urls": sorted(
            {
                str(item.get("source_api_url") or "")
                for item in strict
                if item.get("source_api_url")
            }
        )[:10],
        "sample_units": [
            {
                "unit_number": str(item.get("unit_number") or ""),
                "native_unit_id": str(item.get("native_unit_id") or ""),
                "unit_id": str(item.get("unit_id") or ""),
                "floor_plan_name": str(item.get("floor_plan_name") or ""),
                "market_rent_low": item.get("market_rent_low"),
                "market_rent_high": item.get("market_rent_high"),
                "source_property_id": str(item.get("source_property_id") or ""),
                "source_api_url": str(item.get("source_api_url") or ""),
            }
            for item in strict[:5]
        ],
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": result.get("_link_hop_from") or "",
        "winning_page_url": result.get("_winning_page_url") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "errors": result.get("errors") or [],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "exception": "",
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
        actual = os.environ.get(name, "").lower()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    residuals = [
        row
        for row in read_csv(REMAINING)
        if row.get("current_detected_adapter") not in EXCLUDED_ADAPTERS
    ]
    metadata_by_id = {
        row["apartmentid"]: row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    assert all(row["property_id"] in metadata_by_id for row in residuals)

    # Link-hop imports this public symbol at call time. Use a direct-only
    # implementation so this scan never invokes a paid fetch backend.
    fetch_mod.fetch = direct_fetch
    semaphore = asyncio.Semaphore(3)

    async def bounded(row: dict[str, str]) -> dict:
        async with semaphore:
            result = await one(row, metadata_by_id[row["property_id"]])
            print(
                json.dumps(
                    {
                        "property_id": result["property_id"],
                        "adapter": result.get("adapter") or "",
                        "strict": result["strict_native_priced_rows"],
                        "status": result.get("configured_status"),
                    }
                ),
                flush=True,
            )
            return result

    results = await asyncio.gather(*(bounded(row) for row in residuals))
    results.sort(key=lambda item: int(item["property_id"]))
    positives = [item for item in results if item["strict_native_priced_rows"] > 0]
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "direct-only current full pipeline residual scan",
        "guardrails": {
            "llm_enabled": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "shared_source_modified": False,
            "shared_ledger_modified": False,
        },
        "cohort": {
            "remaining_csv": str(REMAINING),
            "remaining_csv_sha256": sha256(REMAINING),
            "remaining_rows": len(read_csv(REMAINING)),
            "scanned_rows": len(results),
            "excluded_adapters": sorted(EXCLUDED_ADAPTERS),
        },
        "summary": {
            "strict_candidate_properties": len(positives),
            "strict_candidate_ids": [item["property_id"] for item in positives],
            "strict_native_priced_rows": sum(
                item["strict_native_priced_rows"] for item in positives
            ),
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"artifact": str(OUTPUT), **payload["summary"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
