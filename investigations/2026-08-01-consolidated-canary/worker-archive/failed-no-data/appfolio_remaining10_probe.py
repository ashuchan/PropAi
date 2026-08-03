from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import promote_verified_unit_rows


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
SCAN_ROOT = ROOT / "remaining113_direct_scan"
OUTPUT = ROOT / "appfolio_remaining10_direct_adapter.json"
IDS = {
    "3788",
    "25443",
    "44955",
    "47845",
    "56567",
    "69282",
    "237787",
    "241145",
    "251514",
    "260761",
}


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


def compact(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_date",
            "source_api_url",
            "source_property_id",
            "source_property_name",
            "source_property_address",
            "source_property_provenance",
            "source_ids",
        )
    }


def load_inputs() -> tuple[dict[str, dict[str, str]], dict[str, dict]]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        properties = {
            row["apartmentid"]: row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") in IDS
        }
    manifest = json.loads((SCAN_ROOT / "manifest.json").read_text())
    scans = {
        str(row.get("property_id")): row
        for row in manifest["results"]
        if str(row.get("property_id")) in IDS
    }
    return properties, scans


async def run_one(
    metadata: dict[str, str], scan: dict, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        property_id = metadata["apartmentid"]
        raw_path = Path(str(scan.get("raw_path") or ""))
        if scan.get("status") != 200 or not raw_path.exists():
            return {
                "property_id": int(property_id),
                "property_name": metadata.get("name") or "",
                "outcome": "NO_CURRENT_BODY",
                "status": scan.get("status"),
            }
        body = gzip.open(raw_path, "rb").read()
        html = body.decode("utf-8", errors="replace")
        final_url = str(scan.get("final_url") or metadata["website"])
        context = AdapterContext(
            base_url=final_url,
            detected=detect_pms(final_url, page_html=html),
            profile=None,
            expected_total_units=None,
            property_id=property_id,
            fetch_result=SimpleNamespace(body=body, final_url=final_url),
            property_name=metadata.get("name") or "",
            address=metadata.get("address") or "",
            city=metadata.get("city") or "",
            state=metadata.get("state") or "",
            zip_code=metadata.get("zip") or "",
        )
        context._api_responses = []
        try:
            result = await asyncio.wait_for(
                AppFolioAdapter().extract(None, context), timeout=90
            )
            promote_verified_unit_rows(result, property_id=property_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "property_id": int(property_id),
                "property_name": metadata.get("name") or "",
                "outcome": "ERROR",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        rows = [row for row in result.units if isinstance(row, dict)]
        strict = [
            row for row in rows if unit_has_real_anchor(row) and positive_rent(row)
        ]
        unit_numbers = [
            str(row.get("unit_number") or "").strip() for row in strict
        ]
        return {
            "property_id": int(property_id),
            "property_name": metadata.get("name") or "",
            "configured_url": metadata.get("website") or "",
            "configured_identity": {
                key: metadata.get(key) or ""
                for key in ("address", "city", "state", "zip")
            },
            "status": scan.get("status"),
            "final_url": final_url,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "detected_pms": context.detected.pms,
            "tier": result.tier_used,
            "outcome": (
                "STRICT_NATIVE_PRICED"
                if strict
                and len(strict) == len(rows)
                and len(unit_numbers) == len(set(unit_numbers))
                else "UNIT_UNVERIFIED"
                if rows
                else "PLAN_ONLY"
                if result.plan_summaries
                else "EMPTY"
            ),
            "units": len(rows),
            "strict_native_positive_rent_rows": len(strict),
            "distinct_native_unit_numbers": len(set(unit_numbers)),
            "plans": len(result.plan_summaries),
            "winning_url": result.winning_url,
            "source_urls": sorted(
                {
                    str(row.get("source_api_url") or "")
                    for row in strict
                    if str(row.get("source_api_url") or "")
                }
            ),
            "samples": [compact(row) for row in strict[:8]],
            "errors": list(result.errors or [])[-12:],
        }


async def main() -> None:
    properties, scans = load_inputs()
    semaphore = asyncio.Semaphore(4)
    results = await asyncio.gather(
        *(
            run_one(properties[property_id], scans[property_id], semaphore)
            for property_id in sorted(IDS, key=int)
        )
    )
    payload = {
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
                "outcome",
                "tier",
                "units",
                "strict_native_positive_rent_rows",
                "plans",
                "winning_url",
                "errors",
            )
        }
        for row in results
    ], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
