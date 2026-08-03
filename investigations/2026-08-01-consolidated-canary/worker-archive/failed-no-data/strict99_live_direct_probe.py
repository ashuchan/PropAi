from __future__ import annotations

import asyncio
import csv
import json
import os
import runpy
from pathlib import Path

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = Path(os.environ["OUTPUT_PATH"])
TARGETS = {int(value) for value in os.environ.get("PROPERTY_IDS", "").split(",") if value.strip()}
CONCURRENCY = max(1, min(6, int(os.environ.get("AUDIT_CONCURRENCY", "4"))))
os.environ.setdefault("OUTPUT_PATH", str(ROOT / "unused-helper-output.json"))
_HELPERS = runpy.run_path(str(ROOT / "evidence_rerun.py"), run_name="evidence_helpers")
_identity_verdict = _HELPERS["_identity_verdict"]
_identity_sample = _HELPERS["_identity_sample"]


def _metadata() -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out[int(row.get("apartmentid") or "")] = row
            except ValueError:
                continue
    return out


def _enrich(record: dict, canonical: dict[str, str]) -> dict:
    out = dict(record)
    for target, source in (("proj_name", "name"), ("address", "address"), ("city", "city"), ("state", "state"), ("zip_code", "zip")):
        out[target] = str(record.get(target) or canonical.get(source) or "")
    return out


async def _one(record: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        pid = int(record["property_id"])
        website = str(record.get("website") or "")
        request_url = website if "://" in website else "https://" + website
        try:
            response = await asyncio.to_thread(
                probe_get, request_url, timeout=35, unlocker=False, retries=1
            )
        except Exception as exc:
            return {"property_id": pid, "property_name": record.get("proj_name") or "", "website": website, "outcome": "ERROR", "units": 0, "property_identity_match": False, "contamination_verdict": "live_fetch_error", "identity_evidence": {"rows_with_native_identity": 0, "rows_with_native_identity_and_positive_rent": 0}, "identity_samples": [], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        status = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        final_url = str(getattr(response, "url", "") or request_url)
        if status != 200 or not body:
            return {"property_id": pid, "property_name": record.get("proj_name") or "", "website": website, "final_url": final_url, "http_status": status, "body_bytes": len(body.encode()), "outcome": "EMPTY", "units": 0, "property_identity_match": False, "contamination_verdict": "live_entry_unreachable", "identity_evidence": {"rows_with_native_identity": 0, "rows_with_native_identity_and_positive_rent": 0}, "identity_samples": []}
        fetch_result = FetchResult(
            url=request_url,
            outcome=FetchOutcome.OK,
            status=status,
            body=body.encode(),
            headers={},
            render_mode=RenderMode.GET,
            final_url=final_url,
            attempts=1,
            elapsed_ms=0,
        )
        csv_row = {"apartmentid": str(pid), "name": record.get("proj_name") or "", "proj_name": record.get("proj_name") or "", "address": record.get("address") or "", "city": record.get("city") or "", "state": record.get("state") or "", "zip": record.get("zip_code") or "", "website": website}
        try:
            result = await asyncio.wait_for(
                scraper_mod.scrape(
                    request_url,
                    page=None,
                    fetch_result=fetch_result,
                    csv_row=csv_row,
                    property_id=str(pid),
                    shared_budget={"llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0, "link_hop": 1, "_cost_cap_usd": 0},
                ),
                timeout=180,
            )
        except Exception as exc:
            return {"property_id": pid, "property_name": record.get("proj_name") or "", "website": website, "final_url": final_url, "http_status": status, "body_bytes": len(body.encode()), "outcome": "ERROR", "units": 0, "property_identity_match": False, "contamination_verdict": "current_pipeline_error", "identity_evidence": {"rows_with_native_identity": 0, "rows_with_native_identity_and_positive_rent": 0}, "identity_samples": [], "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        units = list(result.get("units") or [])
        plans = list(result.get("plan_summaries") or [])
        verdict, evidence = _identity_verdict(record, units, body)
        qualified = bool(units and verdict.startswith("pass_"))
        return {
            "property_id": pid,
            "property_name": record.get("proj_name") or "",
            "website": website,
            "final_url": final_url,
            "http_status": status,
            "body_bytes": len(body.encode()),
            "outcome": "UNIT_QUALIFIED" if qualified else "UNIT_UNVERIFIED" if units else "PLAN_ONLY" if plans else "EMPTY",
            "raw_extractor_outcome": "UNITS" if units else "PLANS" if plans else "EMPTY",
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "winning_url": result.get("_winning_page_url") or result.get("winning_url") or "",
            "units": len(units),
            "plans": len(plans),
            "property_identity_match": verdict.startswith("pass_"),
            "contamination_verdict": verdict,
            "identity_evidence": evidence,
            "identity_samples": [_identity_sample(row) for row in units[:3]],
            "sample_plan_names": sorted({str(row.get("floor_plan_name") or "") for row in [*units, *plans] if row.get("floor_plan_name")})[:8],
            "errors": list(result.get("errors") or [])[-5:],
        }


async def main() -> None:
    metadata = _metadata()
    records = [_enrich(row, metadata.get(int(row["property_id"]), {})) for row in json.loads((ROOT / "failed344.json").read_text()) if not TARGETS or int(row["property_id"]) in TARGETS]
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [asyncio.create_task(_one(row, semaphore)) for row in records]
    results = []
    for task in asyncio.as_completed(tasks):
        row = await task
        results.append(row)
        print(json.dumps({key: row.get(key) for key in ("property_id", "outcome", "adapter", "units", "plans", "contamination_verdict")}), flush=True)
    results.sort(key=lambda row: row["property_id"])
    OUTPUT.write_text(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"summary": {"targets": len(results), "strict_passes": sum(row["outcome"] == "UNIT_QUALIFIED" for row in results), "unit_unverified": sum(row["outcome"] == "UNIT_UNVERIFIED" for row in results), "plan_only": sum(row["outcome"] == "PLAN_ONLY" for row in results), "empty_or_error": sum(row["outcome"] in {"EMPTY", "ERROR"} for row in results), "strict_ids": [row["property_id"] for row in results if row["outcome"] == "UNIT_QUALIFIED"]}, "output": str(OUTPUT)}))


if __name__ == "__main__":
    asyncio.run(main())
