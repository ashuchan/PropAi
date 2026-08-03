from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
SCAN_ROOT = ROOT / "remaining113_direct_scan"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = Path(
    os.environ.get(
        "OUTPUT",
        str(ROOT / "full_pipeline_remaining_discovery.json"),
    )
)
DEFAULT_ADAPTERS = {
    "unknown",
    "wix_nopms",
    "entrata",
    "knock",
    "funnel",
    "rentvision",
    "resman",
    "sightmap",
    "cortland",
    "equity",
    "g5",
    "squarespace_nopms",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in (
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
            timeout=min(30, max(5, int(task.budget_ms / 1000))),
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


def cached_fetch(
    task: CrawlTask, scan: dict, raw_path: Path
) -> FetchResult | None:
    if scan.get("status") != 200 or not raw_path.is_file():
        return None
    body = gzip.open(raw_path, "rb").read()
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html"},
        render_mode=task.render_mode,
        final_url=str(scan.get("final_url") or task.url),
        attempts=1,
        elapsed_ms=0,
    )


def compact(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "unit_id",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_date",
            "available_date",
            "source_api_url",
            "source_portal_url",
            "source_property_id",
            "source_property_name",
            "source_property_address",
            "source_property_provenance",
            "source_ids",
        )
    }


async def run_one(
    metadata: dict[str, str],
    prior: dict[str, str],
    scan: dict,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        property_id = metadata["apartmentid"]
        url = metadata["website"]
        if "://" not in url:
            url = "https://" + url
        task = CrawlTask(
            url=url,
            property_id=property_id,
            priority=0,
            budget_ms=90_000,
            reason=TaskReason.MANUAL,
            render_mode=RenderMode.GET,
        )
        raw = str(scan.get("raw_path") or "")
        fetched = cached_fetch(task, scan, Path(raw) if raw else Path("/__missing__"))
        if fetched is None:
            fetched = await direct_fetch(task)
        if fetched.outcome != FetchOutcome.OK or not fetched.body:
            return {
                "property_id": int(property_id),
                "property_name": metadata.get("name") or "",
                "prior_adapter": prior.get("current_detected_adapter") or "",
                "outcome": "INITIAL_FETCH_FAILED",
                "status": fetched.status,
                "error": fetched.error_signature,
            }
        started = time.monotonic()
        try:
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
        except Exception as exc:  # noqa: BLE001
            return {
                "property_id": int(property_id),
                "property_name": metadata.get("name") or "",
                "prior_adapter": prior.get("current_detected_adapter") or "",
                "outcome": "PIPELINE_ERROR",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
        strict = [
            row for row in units if unit_has_real_anchor(row) and positive_rent(row)
        ]
        unit_numbers = [
            str(row.get("unit_number") or "").strip() for row in strict
        ]
        source_urls = sorted(
            {
                str(row.get("source_api_url") or "")
                for row in strict
                if str(row.get("source_api_url") or "")
            }
        )
        return {
            "property_id": int(property_id),
            "property_name": metadata.get("name") or "",
            "configured_url": metadata.get("website") or "",
            "configured_identity": {
                key: metadata.get(key) or ""
                for key in ("address", "city", "state", "zip")
            },
            "prior_adapter": prior.get("current_detected_adapter") or "",
            "initial_body_sha256": hashlib.sha256(fetched.body).hexdigest(),
            "initial_final_url": fetched.final_url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "outcome": (
                "STRICT_NATIVE_PRICED_CANDIDATE"
                if strict
                and len(strict) == len(units)
                and len(unit_numbers) == len(set(unit_numbers))
                and all(unit_numbers)
                else "UNIT_UNVERIFIED"
                if units
                else "PLAN_ONLY"
                if result.get("plan_summaries")
                else "EMPTY"
            ),
            "detected_pms": (result.get("_detected_pms") or {}).get("pms"),
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "winning_page_url": result.get("winning_page_url"),
            "units": len(units),
            "strict_native_positive_rent_rows": len(strict),
            "distinct_native_unit_numbers": len(set(unit_numbers)),
            "plans": len(result.get("plan_summaries") or []),
            "source_urls": source_urls,
            "source_property_ids": sorted(
                {
                    str(row.get("source_property_id") or "")
                    for row in strict
                    if str(row.get("source_property_id") or "")
                }
            ),
            "samples": [compact(row) for row in strict[:8]],
            "errors": list(result.get("errors") or [])[-15:],
            "fallback_chain": result.get("_fallback_chain") or [],
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
    requested = {
        value.strip()
        for value in os.environ.get("ADAPTERS", "").split(",")
        if value.strip()
    }
    adapters = requested or DEFAULT_ADAPTERS
    remaining = read_csv(REMAINING)
    selected = [
        row
        for row in remaining
        if (row.get("current_detected_adapter") or "") in adapters
    ]
    configured = {
        row["apartmentid"]: row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    scan_payload = json.loads((SCAN_ROOT / "manifest.json").read_text())
    scans = {
        str(row.get("property_id")): row
        for row in scan_payload["results"]
        if isinstance(row, dict)
    }
    fetch_mod.fetch = direct_fetch
    semaphore = asyncio.Semaphore(int(os.environ.get("CONCURRENCY", "4")))
    results = await asyncio.gather(
        *(
            run_one(
                configured[row["property_id"]],
                row,
                scans.get(row["property_id"], {}),
                semaphore,
            )
            for row in selected
        )
    )
    payload = {
        "selected_adapters": sorted(adapters),
        "selected_properties": len(selected),
        "guardrails": {
            "direct_only": True,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "llm": False,
            "paid_canary": False,
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps([
        {
            key: row.get(key)
            for key in (
                "property_id",
                "property_name",
                "prior_adapter",
                "outcome",
                "detected_pms",
                "adapter",
                "tier",
                "units",
                "strict_native_positive_rent_rows",
                "plans",
                "winning_page_url",
                "error",
            )
        }
        for row in results
    ], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
