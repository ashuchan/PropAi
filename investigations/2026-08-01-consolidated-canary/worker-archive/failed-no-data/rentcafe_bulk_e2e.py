from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe import (
    RentCafeAdapter,
    _find_all_securecafe_bases,
)
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import promote_verified_unit_rows


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
CONCURRENCY = 6
PROPERTY_TIMEOUT_SECONDS = 75
CANDIDATE_MODE = os.environ.get("RC_CANDIDATE_MODE", "securecafe")


def archived_body(property_id: str) -> bytes | None:
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return None
    return gzip.open(path, "rb").read()


def make_context(record: dict, body: bytes) -> AdapterContext:
    website = str(record.get("website") or "")
    html = body.decode("utf-8", "replace").replace("\\/", "/")
    ctx = AdapterContext(
        base_url=website,
        detected=detect_pms(website, page_html=html),
        profile=None,
        expected_total_units=None,
        property_id=str(record["property_id"]),
        fetch_result=SimpleNamespace(body=body, final_url=website),
        property_name=str(record.get("proj_name") or ""),
        address=str(record.get("address") or ""),
        city=str(record.get("city") or ""),
        state=str(record.get("state") or ""),
        zip_code=str(record.get("zip_code") or ""),
    )
    ctx._api_responses = []
    return ctx


def securecafe_candidates(records: list[dict]) -> list[dict]:
    candidates = []
    for record in records:
        if record.get("adapter") != "rentcafe":
            continue
        property_id = str(record["property_id"])
        body = archived_body(property_id)
        if body is None:
            continue
        ctx = make_context(record, body)
        html = body.decode("utf-8", "replace").replace("\\/", "/")
        bases = _find_all_securecafe_bases(html, ctx)
        if CANDIDATE_MODE == "securecafe" and not bases:
            continue
        if CANDIDATE_MODE == "no_securecafe" and bases:
            continue
        candidate = dict(record)
        candidate["_securecafe_bases"] = bases
        candidates.append(candidate)
    return candidates


async def run_one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        property_id = str(record["property_id"])
        body = archived_body(property_id)
        if body is None:
            return {
                "property_id": property_id,
                "name": record.get("proj_name"),
                "outcome": "NO_ARCHIVED_BODY",
            }
        ctx = make_context(record, body)
        try:
            result = await asyncio.wait_for(
                RentCafeAdapter().extract(None, ctx),
                timeout=PROPERTY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {
                "property_id": property_id,
                "name": record.get("proj_name"),
                "outcome": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                "bases": record.get("_securecafe_bases", [])[:3],
            }
        promote_verified_unit_rows(result, property_id=property_id)
        return {
            "property_id": property_id,
            "name": record.get("proj_name"),
            "outcome": "NONFAILED" if result.units or result.plan_summaries else "EMPTY",
            "tier": result.tier_used,
            "units": len(result.units),
            "plans": len(result.plan_summaries),
            "sample_plans": sorted(
                {
                    str(row.get("floor_plan_name") or "")
                    for row in [*result.units, *result.plan_summaries]
                    if row.get("floor_plan_name")
                }
            )[:4],
            "errors": result.errors[-3:],
            "bases": record.get("_securecafe_bases", [])[:3],
        }


async def main() -> None:
    records = json.loads((ROOT / "failed344.json").read_text())
    candidates = securecafe_candidates(records)
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "property_ids": [str(row["property_id"]) for row in candidates],
            }
        ),
        flush=True,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(run_one(row, semaphore)) for row in candidates]
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
