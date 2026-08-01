from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path

import ma_poc.pms.adapters  # noqa: F401
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
CONCURRENCY = 6
PROPERTY_TIMEOUT_SECONDS = 90
SOURCE_ADAPTER = os.environ.get("SOURCE_ADAPTER", "__none__")
DETECTED_ADAPTERS = {
    item.strip()
    for item in os.environ.get("DETECTED_ADAPTERS", "").split(",")
    if item.strip()
}
EXCLUDE_IDS = {
    int(item)
    for item in os.environ.get("EXCLUDE_IDS", "").split(",")
    if item.strip()
}
PROPERTY_IDS = {
    int(item)
    for item in os.environ.get("PROPERTY_IDS", "").split(",")
    if item.strip()
}


def fetch_for(record: dict) -> FetchResult | None:
    property_id = str(record["property_id"])
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return None
    body = gzip.open(path, "rb").read()
    url = str(record.get("website") or "")
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
    )


async def run_one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        property_id = str(record["property_id"])
        fetch_result = fetch_for(record)
        if fetch_result is None:
            return {
                "property_id": property_id,
                "outcome": "NO_ARCHIVED_BODY",
            }
        profile_path = ROOT / "profiles" / f"{property_id}.json"
        try:
            profile = (
                ScrapeProfile.model_validate_json(profile_path.read_text())
                if profile_path.exists()
                else None
            )
        except Exception:
            profile = None
        csv_row = {
            "apartmentid": property_id,
            "name": record.get("proj_name") or "",
            "address": record.get("address") or "",
            "city": record.get("city") or "",
            "state": record.get("state") or "",
            "zip": record.get("zip_code") or "",
            "website": record.get("website") or "",
        }
        budget = {
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        }
        try:
            result = await asyncio.wait_for(
                scraper_mod.scrape(
                    record.get("website") or "",
                    profile=profile,
                    page=None,
                    fetch_result=fetch_result,
                    csv_row=csv_row,
                    property_id=property_id,
                    shared_budget=budget,
                ),
                timeout=PROPERTY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {
                "property_id": property_id,
                "outcome": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            }
        units = result.get("units") or []
        plans = result.get("plan_summaries") or []
        return {
            "property_id": property_id,
            "outcome": "NONFAILED" if units or plans else "EMPTY",
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "units": len(units),
            "plans": len(plans),
            "sample_plans": sorted(
                {
                    str(row.get("floor_plan_name") or "")
                    for row in [*units, *plans]
                    if row.get("floor_plan_name")
                }
            )[:4],
            "errors": (result.get("errors") or [])[-3:],
        }


async def main() -> None:
    records = []
    for row in json.loads((ROOT / "failed344.json").read_text()):
        property_id = int(row["property_id"])
        if property_id in EXCLUDE_IDS:
            continue
        if PROPERTY_IDS and property_id not in PROPERTY_IDS:
            continue
        if SOURCE_ADAPTER == "__none__":
            source_matches = row.get("adapter") is None
        elif SOURCE_ADAPTER == "__all__":
            source_matches = True
        else:
            source_matches = row.get("adapter") == SOURCE_ADAPTER
        if not source_matches:
            continue
        if DETECTED_ADAPTERS:
            archived = fetch_for(row)
            html = (
                archived.body.decode("utf-8", "replace")
                if archived is not None
                else ""
            )
            detected = detect_pms(
                str(row.get("website") or ""),
                page_html=html,
            ).pms
            if detected not in DETECTED_ADAPTERS:
                continue
        records.append(row)
    print(
        json.dumps(
            {
                "candidate_count": len(records),
                "property_ids": [str(row["property_id"]) for row in records],
            }
        ),
        flush=True,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(run_one(row, semaphore)) for row in records]
    results = []
    for task in asyncio.as_completed(tasks):
        row = await task
        results.append(row)
        print(json.dumps(row), flush=True)
    results.sort(key=lambda row: int(row["property_id"]))
    converted = [row for row in results if row.get("outcome") == "NONFAILED"]
    print(
        json.dumps(
            {
                "summary": {
                    "denominator": len(results),
                    "converted": len(converted),
                    "rate": round(len(converted) / len(results), 4),
                    "unit_level": sum(bool(row.get("units")) for row in converted),
                    "plan_only": sum(
                        not bool(row.get("units")) and bool(row.get("plans"))
                        for row in converted
                    ),
                    "converted_ids": [row["property_id"] for row in converted],
                    "blocked_ids": [
                        row["property_id"]
                        for row in results
                        if row.get("outcome") != "NONFAILED"
                    ],
                },
                "results": results,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
