from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "entrata_residual_lane"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "rentcafe_floorplans_current_full_direct.json"
TIMEOUT_SECONDS = 150
CONCURRENCY = 3
ROUTES = {
    "5782": "https://www.springfieldrenton.com/floorplans",
    "46915": "https://www.barbertonapt.com/floorplans",
    "58390": "https://www.live30lancaster.com/floorplans",
    "241538": "https://www.block88apts.com/floorplans",
    "225886": "https://www.casabaywoodapts.com/floorplans",
    "266766": "https://www.101oxford.com/floorplans",
    "289338": "https://www.201walnut.com/floorplans",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(unit: dict[str, object]) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and float(unit[field]) > 0
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
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.DEAD_URL
            if status in {404, 410, 451}
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(response.headers or {}),
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


def make_task(url: str, property_id: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def sample(unit: dict[str, object]) -> dict[str, object]:
    source_ids = unit.get("source_ids")
    return {
        "unit_number": str(unit.get("unit_number") or ""),
        "floor_plan_name": str(unit.get("floor_plan_name") or ""),
        "availability_date": str(
            unit.get("availability_date") or unit.get("available_date") or ""
        ),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "source_api_url": str(unit.get("source_api_url") or ""),
        "source_ids": dict(source_ids) if isinstance(source_ids, dict) else {},
    }


async def run_one(
    property_id: str,
    canonical: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, object]:
    async with semaphore:
        route = ROUTES[property_id]
        task = make_task(route, property_id)
        fetched = await direct_fetch(task)
        body = (fetched.body or b"").decode("utf-8", "replace")
        visible = " ".join(
            BeautifulSoup(body, "lxml").get_text(" ", strip=True).split()
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                scraper_mod.scrape_jugnu(
                    task,
                    fetched,
                    page=None,
                    profile=None,
                    csv_row=canonical,
                ),
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "property_id": int(property_id),
                "property_name": canonical.get("name") or "",
                "route": route,
                "fetch_status": fetched.status,
                "fetch_final_url": fetched.final_url,
                "outcome": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }

        units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
        plans = [
            row for row in (result.get("plan_summaries") or []) if isinstance(row, dict)
        ]
        strict = [
            unit
            for unit in units
            if unit_has_real_anchor(unit) and positive_rent(unit)
        ]
        return {
            "property_id": int(property_id),
            "property_name": canonical.get("name") or "",
            "canonical_address": canonical.get("address") or "",
            "canonical_city": canonical.get("city") or "",
            "canonical_state": canonical.get("state") or "",
            "canonical_zip": canonical.get("zip_code") or "",
            "route": route,
            "fetch_status": fetched.status,
            "fetch_final_url": fetched.final_url,
            "configured_identity_visible": bool(
                str(canonical.get("address") or "").split()[0] in visible
                and str(canonical.get("zip_code") or "").split(".")[0] in visible
            ),
            "outcome": "UNIT_CANDIDATE" if strict else "PLAN_ONLY" if plans else "EMPTY",
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
            "emitted_units": len(units),
            "strict_native_positive_rent_rows": len(strict),
            "plans": len(plans),
            "all_emitted_rows_strict": bool(strict and len(strict) == len(units)),
            "native_samples": [sample(unit) for unit in strict[:8]],
            "errors": result.get("errors") or [],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "requests",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {name}={actual!r}; expected {expected!r}")

    metadata = {row["apartmentid"]: row for row in read_csv(PROPERTIES)}
    fetch_mod.fetch = direct_fetch
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        asyncio.create_task(run_one(property_id, metadata[property_id], semaphore))
        for property_id in ROUTES
    ]
    results: list[dict[str, object]] = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    results.sort(key=lambda row: int(row["property_id"]))
    payload = {
        "lane": "rentcafe_exact_floorplans_current_full_pipeline_direct",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "authoritative_recoveries": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "llm_enabled": False,
            "paid_canary": False,
            "web_unlocker": False,
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "unit_candidate_ids": [
                    row["property_id"]
                    for row in results
                    if row.get("outcome") == "UNIT_CANDIDATE"
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
