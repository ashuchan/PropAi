from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import (
    hb_raw_get,
    hyperbrowser_property_call_count,
)
from ma_poc.pms.adapters.entrata import (
    _find_pp_conventional_index,
    parse_entrata_prospectportal_html,
)
from ma_poc.validation.unit_validity import is_valid_unit


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_IDS = {
    int(item)
    for item in os.environ.get("PROPERTY_IDS", "").split(",")
    if item.strip()
}
CONCURRENCY = int(os.environ.get("AUDIT_CONCURRENCY", "3"))
TIMEOUT_SECONDS = int(os.environ.get("AUDIT_TIMEOUT_SECONDS", "70"))


def archived_body(property_id: int) -> str:
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    return gzip.open(path, "rb").read().decode("utf-8", "replace")


async def run_one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        property_id = int(record["property_id"])
        website = str(record.get("website") or "")
        anchors = _find_pp_conventional_index(archived_body(property_id), website)
        if not anchors:
            return {"property_id": property_id, "outcome": "NO_EXACT_ANCHOR"}
        anchor = anchors[0]
        started = time.monotonic()
        try:
            status, body = await asyncio.wait_for(
                hb_raw_get(anchor, str(property_id)),
                timeout=TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            status, body = 0, ""
        rows = parse_entrata_prospectportal_html(body, anchor) if status == 200 else []
        valid = [row for row in rows if is_valid_unit(row)]
        plan_names = sorted(
            {
                str(row.get("floor_plan_name") or "").strip()
                for row in valid
                if str(row.get("floor_plan_name") or "").strip()
            }
        )
        return {
            "property_id": property_id,
            "name": str(record.get("proj_name") or ""),
            "outcome": "NONFAILED" if valid else "EMPTY",
            "http_status": status,
            "body_bytes": len(body),
            "valid_rows": len(valid),
            "unit_rows": sum(bool(str(row.get("unit_number") or "").strip()) for row in valid),
            "plan_names": plan_names[:8],
            "plan_name_count": len(plan_names),
            "rent_rows": sum(row.get("rent_low") not in (None, "", 0, "0") for row in valid),
            "availability_date_rows": sum(bool(str(row.get("availability_date") or "").strip()) for row in valid),
            "available_count_rows": sum(bool(str(row.get("available_units") or "").strip()) for row in valid),
            "exact_anchor": anchor,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "session_calls": hyperbrowser_property_call_count(str(property_id)),
        }


async def main() -> None:
    records = json.loads((ROOT / "failed344.json").read_text())
    candidates = []
    for row in records:
        property_id = int(row["property_id"])
        if PROPERTY_IDS and property_id not in PROPERTY_IDS:
            continue
        if _find_pp_conventional_index(
            archived_body(property_id), str(row.get("website") or "")
        ):
            candidates.append(row)
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "property_ids": [int(row["property_id"]) for row in candidates],
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
    converted = [row for row in results if row.get("outcome") == "NONFAILED"]
    print(
        json.dumps(
            {
                "summary": {
                    "denominator": len(results),
                    "converted": len(converted),
                    "converted_ids": [row["property_id"] for row in converted],
                    "session_calls": sum(int(row.get("session_calls") or 0) for row in results),
                    "elapsed_session_seconds": round(
                        sum(float(row.get("elapsed_seconds") or 0) for row in results), 2
                    ),
                }
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
