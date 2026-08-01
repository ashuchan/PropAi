from __future__ import annotations

import asyncio
import csv
import gzip
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from ma_poc.extraction.post_process import post_process
from ma_poc.fetch.hyperbrowser_backend import hyperbrowser_property_call_count
from ma_poc.pms.adapters._entrata_hb_recovery import (
    recover_entrata_hb_conventional,
    strict_conventional_url,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTIES_PATH = Path("ma_poc/config/properties.csv")
STRICT_PATH = ROOT / "strict99_authoritative_ledger.csv"
OUTPUT_PATH = Path(os.environ["OUTPUT_PATH"])
PROPERTY_IDS = {
    int(item)
    for item in os.environ.get("PROPERTY_IDS", "").split(",")
    if item.strip()
}
EXCLUDE_IDS = {
    int(item)
    for item in os.environ.get("EXCLUDE_IDS", "").split(",")
    if item.strip()
}
CONCURRENCY = min(3, max(1, int(os.environ.get("AUDIT_CONCURRENCY", "3"))))
TIMEOUT_SECONDS = int(os.environ.get("AUDIT_TIMEOUT_SECONDS", "180"))
INCLUDE_STRICT_IDS = os.environ.get("INCLUDE_STRICT_IDS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
try:
    PROPERTY_NAME_OVERRIDES = {
        int(key): str(value)
        for key, value in json.loads(
            os.environ.get("PROPERTY_NAME_OVERRIDE_JSON", "{}")
        ).items()
    }
except (json.JSONDecodeError, TypeError, ValueError):
    PROPERTY_NAME_OVERRIDES = {}


def archived_html(property_id: int) -> str:
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    return gzip.open(path, "rb").read().decode("utf-8", "replace")


def canonical_metadata() -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    with PROPERTIES_PATH.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                out[int(row.get("apartmentid") or "")] = row
            except ValueError:
                continue
    return out


def enrich(record: dict, metadata: dict[int, dict[str, str]]) -> dict:
    canonical = metadata.get(int(record["property_id"]), {})
    merged = dict(record)
    for target, source in (
        ("proj_name", "name"),
        ("address", "address"),
        ("city", "city"),
        ("state", "state"),
        ("zip_code", "zip"),
    ):
        merged[target] = str(record.get(target) or canonical.get(source) or "")
    merged["_canonical_property_name"] = merged.get("proj_name") or ""
    if int(record["property_id"]) in PROPERTY_NAME_OVERRIDES:
        merged["proj_name"] = PROPERTY_NAME_OVERRIDES[int(record["property_id"])]
    return merged


def context_for(record: dict, html: str) -> AdapterContext:
    website = str(record.get("website") or "")
    ctx = AdapterContext(
        base_url=website,
        detected=detect_pms(website, page_html=html),
        profile=None,
        expected_total_units=None,
        property_id=str(record["property_id"]),
        fetch_result=SimpleNamespace(body=html.encode(), final_url=website),
        property_name=str(record.get("proj_name") or ""),
        address=str(record.get("address") or ""),
        city=str(record.get("city") or ""),
        state=str(record.get("state") or ""),
        zip_code=str(record.get("zip_code") or ""),
    )
    ctx._api_responses = []
    return ctx


def identity_sample(row: dict) -> dict:
    identity = {}
    for key in (
        "unit_number",
        "unit_id",
        "native_unit_id",
        "source_unit_id",
        "_source_native_id",
        "source_id",
        "floor_plan_id",
    ):
        value = row.get(key)
        if value not in (None, ""):
            identity[key] = str(value)
    return {
        "identity": identity,
        "source_ids": (
            dict(row.get("source_ids") or {})
            if isinstance(row.get("source_ids"), dict)
            else {}
        ),
        "source_api_url": str(row.get("source_api_url") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "positive_rent_evidence": {
            key: row.get(key)
            for key in (
                "market_rent_low",
                "market_rent_high",
                "rent_low",
                "rent_high",
                "asking_rent",
                "rent",
            )
            if row.get(key) not in (None, "", 0, 0.0)
        },
        "availability_date": str(row.get("availability_date") or ""),
    }


async def run_one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        property_id = int(record["property_id"])
        html = archived_html(property_id)
        ctx = context_for(record, html)
        matched_url = strict_conventional_url(
            html,
            str(record.get("website") or ""),
            str(record.get("proj_name") or ""),
        )
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                recover_entrata_hb_conventional(ctx),
                timeout=TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {
                "property_id": property_id,
                "property_name": str(record.get("_canonical_property_name") or record.get("proj_name") or ""),
                "matched_url": matched_url,
                "outcome": "TIMEOUT",
                "units": 0,
                "plans": 0,
                "elapsed_session_seconds": round(time.monotonic() - started, 2),
                "session_calls": hyperbrowser_property_call_count(str(property_id)),
            }
        unit_pp = post_process(outcome.units, property_id=str(property_id))
        units = list(unit_pp.units)
        plan_pp = post_process(outcome.plan_rows, property_id=str(property_id))
        plans = list(plan_pp.plan_summaries)
        return {
            "property_id": property_id,
            "property_name": str(record.get("_canonical_property_name") or record.get("proj_name") or ""),
            "website": str(record.get("website") or ""),
            "matched_url": matched_url,
            "winning_url": outcome.winning_url,
            "outcome": "UNIT_QUALIFIED" if units else "PLAN_ONLY" if plans else "EMPTY",
            "attempted": outcome.attempted,
            "complete": outcome.complete,
            "failure_reason": outcome.failure_reason,
            "units": len(units),
            "plans": len(plans),
            "native_samples": [identity_sample(row) for row in units[:2]],
            "source_urls": sorted(
                {
                    str(row.get("source_api_url") or "")
                    for row in units
                    if str(row.get("source_api_url") or "")
                }
            )[:5],
            "property_identity_match": bool(matched_url),
            "contamination_verdict": (
                "pass_strict_property_boundary_and_native_positive_rent"
                if units and matched_url
                else "no_native_units"
            ),
            "elapsed_session_seconds": round(time.monotonic() - started, 2),
            "session_calls": hyperbrowser_property_call_count(str(property_id)),
        }


async def main() -> None:
    strict_ids = set()
    with STRICT_PATH.open(encoding="utf-8", newline="") as handle:
        strict_ids = {int(row["property_id"]) for row in csv.DictReader(handle)}
    metadata = canonical_metadata()
    records = [
        enrich(row, metadata)
        for row in json.loads((ROOT / "failed344.json").read_text())
        if (INCLUDE_STRICT_IDS or int(row["property_id"]) not in strict_ids)
        and int(row["property_id"]) not in EXCLUDE_IDS
        and (not PROPERTY_IDS or int(row["property_id"]) in PROPERTY_IDS)
    ]
    candidates = []
    for row in records:
        html = archived_html(int(row["property_id"]))
        if strict_conventional_url(
            html,
            str(row.get("website") or ""),
            str(row.get("proj_name") or ""),
        ):
            candidates.append(row)
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "property_ids": [int(row["property_id"]) for row in candidates],
                "concurrency": CONCURRENCY,
            }
        ),
        flush=True,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(run_one(row, semaphore)) for row in candidates]
    results = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print(json.dumps(result), flush=True)
    results.sort(key=lambda row: row["property_id"])
    OUTPUT_PATH.write_text(
        json.dumps({"results": results}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "candidate_count": len(results),
        "unit_qualified": sum(row.get("outcome") == "UNIT_QUALIFIED" for row in results),
        "plan_only": sum(row.get("outcome") == "PLAN_ONLY" for row in results),
        "empty": sum(row.get("outcome") == "EMPTY" for row in results),
        "challenge_unsolved": sum(row.get("failure_reason") == "CHALLENGE_UNSOLVED" for row in results),
        "session_calls": sum(int(row.get("session_calls") or 0) for row in results),
        "elapsed_session_seconds": round(
            sum(float(row.get("elapsed_session_seconds") or 0) for row in results), 2
        ),
        "unexpected_session_multiplication": any(int(row.get("session_calls") or 0) > 1 for row in results),
        "output_path": str(OUTPUT_PATH),
    }
    print(json.dumps({"summary": summary}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
