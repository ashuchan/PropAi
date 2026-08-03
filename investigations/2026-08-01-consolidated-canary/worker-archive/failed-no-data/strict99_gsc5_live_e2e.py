from __future__ import annotations

import asyncio
import csv
import gzip
import json
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "evidence_strict99_false_positive_gsc5_live.json"
TARGETS = {6477, 34303, 74488, 78593, 221995}


def _metadata() -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out[int(row.get("apartmentid") or "")] = row
            except ValueError:
                continue
    return out


def _archived(pid: int) -> str:
    path = ROOT / "raw_all" / f"{pid}.html.gz"
    return gzip.open(path, "rb").read().decode("utf-8", "replace")


def _positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row.get(key) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def _sample(row: dict) -> dict:
    identity = {
        key: str(row.get(key))
        for key in (
            "unit_number",
            "unit_id",
            "native_unit_id",
            "source_unit_id",
            "floor_plan_id",
        )
        if row.get(key) not in (None, "")
    }
    return {
        "identity": identity,
        "source_ids": row.get("source_ids") if isinstance(row.get("source_ids"), dict) else {},
        "source_api_url": str(row.get("source_api_url") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "positive_rent_evidence": {
            key: row.get(key)
            for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
            if isinstance(row.get(key), (int, float)) and row.get(key) > 0
        },
    }


async def _one(record: dict, canonical: dict[str, str], semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        pid = int(record["property_id"])
        name = str(record.get("proj_name") or canonical.get("name") or "")
        entry_url = str(record.get("website") or "")
        csv_row = {
            "apartmentid": str(pid),
            "proj_name": name,
            "name": name,
            "address": str(record.get("address") or canonical.get("address") or ""),
            "city": str(record.get("city") or canonical.get("city") or ""),
            "state": str(record.get("state") or canonical.get("state") or ""),
            "zip": str(record.get("zip_code") or canonical.get("zip") or ""),
            "website": entry_url,
        }
        archived = _archived(pid)
        detected = detect_pms(entry_url, csv_row=csv_row, page_html=archived)
        recovered_url = await scraper_mod._rediscover_stale_gsc_property_url(
            entry_url, detected, str(pid), csv_row
        )
        if not recovered_url:
            return {
                "property_id": pid,
                "property_name": name,
                "website": entry_url,
                "outcome": "EMPTY",
                "units": 0,
                "property_identity_match": False,
                "contamination_verdict": "no_exact_rediscovery",
                "identity_evidence": {"rows_with_native_identity": 0, "rows_with_native_identity_and_positive_rent": 0},
                "native_samples": [],
            }
        try:
            response = await asyncio.to_thread(
                probe_get, recovered_url, timeout=30, unlocker=False, retries=1
            )
        except Exception as exc:
            return {
                "property_id": pid,
                "property_name": name,
                "website": entry_url,
                "rediscovered_url": recovered_url,
                "outcome": "ERROR",
                "units": 0,
                "property_identity_match": True,
                "contamination_verdict": "live_fetch_error",
                "identity_evidence": {"rows_with_native_identity": 0, "rows_with_native_identity_and_positive_rent": 0},
                "native_samples": [],
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        status = int(getattr(response, "status_code", 0) or 0)
        body = str(getattr(response, "text", "") or "")
        final_url = str(getattr(response, "url", "") or recovered_url)
        if status != 200 or not body:
            result = {}
        else:
            fetch_result = FetchResult(
                url=recovered_url,
                outcome=FetchOutcome.OK,
                status=status,
                body=body.encode(),
                headers={},
                render_mode=RenderMode.GET,
                final_url=final_url,
                attempts=1,
                elapsed_ms=0,
            )
            result = await scraper_mod.scrape(
                recovered_url,
                page=None,
                fetch_result=fetch_result,
                csv_row=csv_row,
                property_id=str(pid),
                shared_budget={
                    "llm_api_calls": 0,
                    "llm_dom_calls": 0,
                    "llm_monolithic": 0,
                    "link_hop": 0,
                    "_cost_cap_usd": 0,
                },
            )
        units = list(result.get("units") or [])
        native = [row for row in units if unit_has_real_anchor(row)]
        qualified = [row for row in native if _positive_rent(row)]
        old_host = (urlparse(entry_url if "://" in entry_url else "https://" + entry_url).hostname or "").removeprefix("www.")
        new_host = (urlparse(recovered_url).hostname or "").removeprefix("www.")
        boundary = old_host == new_host == "gscapts.com"
        passed = bool(qualified and boundary and len(qualified) == len(units))
        source_urls = sorted({str(row.get("source_api_url") or "") for row in qualified if row.get("source_api_url")})
        return {
            "property_id": pid,
            "property_name": name,
            "website": entry_url,
            "rediscovered_url": recovered_url,
            "final_url": final_url,
            "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED" if units else "EMPTY",
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "units": len(units),
            "plans": len(result.get("plan_summaries") or []),
            "property_identity_match": bool(boundary),
            "contamination_verdict": "pass_exact_gsc_name_rediscovery_and_native_positive_rent" if passed else "no_native_units",
            "identity_evidence": {
                "rows_with_native_identity": len(native),
                "rows_with_native_identity_and_positive_rent": len(qualified),
                "source_urls": source_urls[:5],
                "rediscovery_same_gsc_host": boundary,
                "rediscovery_method_gate": "current_code_mgmt_sitemap_or_homepage_confidence_ge_0.9",
            },
            "native_samples": [_sample(row) for row in qualified[:2]],
            "errors": list(result.get("errors") or [])[-5:],
        }


async def main() -> None:
    records = [row for row in json.loads((ROOT / "failed344.json").read_text()) if int(row["property_id"]) in TARGETS]
    metadata = _metadata()
    semaphore = asyncio.Semaphore(3)
    results = await asyncio.gather(*(_one(row, metadata.get(int(row["property_id"]), {}), semaphore) for row in records))
    results.sort(key=lambda row: row["property_id"])
    OUTPUT.write_text(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"summary": {"targets": len(results), "strict_passes": sum(row["outcome"] == "UNIT_QUALIFIED" for row in results), "ids": [row["property_id"] for row in results if row["outcome"] == "UNIT_QUALIFIED"]}, "results": results}))


if __name__ == "__main__":
    asyncio.run(main())
