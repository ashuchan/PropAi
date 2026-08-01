from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import promote_verified_unit_rows


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_IDS = (
    "2948",
    "4554",
    "12398",
    "14295",
    "15358",
    "16172",
    "18194",
    "18736",
    "38677",
    "39995",
    "43520",
    "224888",
    "251908",
    "251974",
    "253326",
    "253646",
    "261116",
    "265143",
    "270367",
    "284199",
    "291774",
)


async def run_one(record: dict) -> dict:
    property_id = str(record["property_id"])
    raw_path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not raw_path.exists():
        return {"property_id": property_id, "outcome": "NO_ARCHIVED_BODY"}
    body = gzip.open(raw_path, "rb").read()
    website = str(record.get("website") or "")
    ctx = AdapterContext(
        base_url=website,
        detected=detect_pms(website, page_html=body.decode("utf-8", "replace")),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(body=body, final_url=website),
        property_name=str(record.get("proj_name") or ""),
        address=str(record.get("address") or ""),
        city=str(record.get("city") or ""),
        state=str(record.get("state") or ""),
        zip_code=str(record.get("zip_code") or ""),
    )
    ctx._api_responses = []
    try:
        result = await asyncio.wait_for(
            OneSiteAdapter().extract(None, ctx), timeout=45
        )
    except Exception as exc:
        return {
            "property_id": property_id,
            "name": record.get("proj_name"),
            "outcome": "ERROR",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
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
        "errors": result.errors[-2:],
    }


async def main() -> None:
    records = {
        str(row["property_id"]): row
        for row in json.loads((ROOT / "failed344.json").read_text())
    }
    results = []
    for property_id in PROPERTY_IDS:
        row = await run_one(records[property_id])
        results.append(row)
        print(json.dumps(row), flush=True)
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
                }
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
