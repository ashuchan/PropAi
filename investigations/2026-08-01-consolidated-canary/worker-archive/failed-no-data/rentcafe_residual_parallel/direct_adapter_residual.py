from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter, _find_all_securecafe_bases
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import promote_verified_unit_rows


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "rentcafe_residual_parallel"
IDS = {
    "594", "5782", "17186", "17674", "24337", "27080", "39710",
    "58390", "58546", "71534", "72743", "74519", "223248", "225886",
    "231543", "241538", "244756", "262799", "266766", "289338",
}


def read_properties() -> dict[str, dict[str, str]]:
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["apartmentid"]: row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") in IDS
        }


def archived_body(pid: str) -> bytes:
    path = ROOT / "raw_all" / f"{pid}.html.gz"
    return gzip.open(path, "rb").read() if path.exists() else b""


def current_body(pid: str) -> tuple[bytes, str, str]:
    manifest = json.loads((OUT / "direct_probe_manifest.json").read_text())
    for row in manifest["results"]:
        if str(row.get("property_id")) != pid or row.get("status") != 200:
            continue
        path = Path(str(row.get("raw_path") or ""))
        if path.exists():
            return gzip.open(path, "rb").read(), str(row.get("final_url") or ""), "live_direct"
    return b"", "", ""


def positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


def sample(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "unit_number", "floor_plan_name", "availability_date", "available_date",
            "market_rent_low", "market_rent_high", "source_api_url",
            "source_portal_url", "source_property_id", "source_property_name",
            "source_property_address", "source_property_provenance", "source_ids",
        )
    }


async def run_one(row: dict[str, str], semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        pid = row["apartmentid"]
        live_body, final_url, body_source = current_body(pid)
        body = live_body or archived_body(pid)
        if not body:
            return {"property_id": int(pid), "outcome": "NO_BODY"}
        base_url = final_url or row["website"]
        if "://" not in base_url:
            base_url = "https://" + base_url
        html = body.decode("utf-8", "replace").replace("\\/", "/")
        ctx = AdapterContext(
            base_url=base_url,
            detected=detect_pms(base_url, page_html=html),
            profile=None,
            expected_total_units=None,
            property_id=pid,
            fetch_result=SimpleNamespace(body=body, final_url=base_url),
            property_name=row.get("name") or "",
            address=row.get("address") or "",
            city=row.get("city") or "",
            state=row.get("state") or "",
            zip_code=row.get("zip") or "",
        )
        ctx._api_responses = []
        bases = _find_all_securecafe_bases(html, ctx)
        try:
            result = await asyncio.wait_for(RentCafeAdapter().extract(None, ctx), timeout=90)
            promote_verified_unit_rows(result, property_id=pid)
        except Exception as exc:
            return {
                "property_id": int(pid), "property_name": row.get("name") or "",
                "outcome": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                "body_source": body_source or "archived_0731", "bases": bases[:8],
            }
        units = [item for item in result.units if isinstance(item, dict)]
        strict = [item for item in units if unit_has_real_anchor(item) and positive_rent(item)]
        unit_codes = [str(item.get("unit_number") or "").strip() for item in strict]
        source_urls = sorted({str(item.get("source_api_url") or "") for item in strict if item.get("source_api_url")})
        source_property_ids = sorted({str(item.get("source_property_id") or "") for item in strict if item.get("source_property_id")})
        source_property_names = sorted({str(item.get("source_property_name") or "") for item in strict if item.get("source_property_name")})
        return {
            "property_id": int(pid),
            "property_name": row.get("name") or "",
            "website": row.get("website") or "",
            "body_source": body_source or "archived_0731",
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "detected": ctx.detected.pms,
            "outcome": "STRICT_UNIT" if strict and len(strict) == len(units) and len(unit_codes) == len(set(unit_codes)) else "UNIT_UNVERIFIED" if units else "PLAN_ONLY" if result.plan_summaries else "EMPTY",
            "tier": result.tier_used,
            "units": len(units),
            "strict_native_positive_rent_rows": len(strict),
            "distinct_unit_codes": len(set(unit_codes)),
            "plans": len(result.plan_summaries),
            "bases": bases[:8],
            "source_urls": source_urls,
            "source_hosts": sorted({urlparse(url).hostname or "" for url in source_urls}),
            "source_property_ids": source_property_ids,
            "source_property_names": source_property_names,
            "samples": [sample(item) for item in strict[:6]],
            "errors": result.errors[-8:],
            "api_responses": [
                {"url": str(api.get("url") or ""), "status": api.get("status"), "via": api.get("via")}
                for api in result.api_responses
                if isinstance(api, dict)
            ],
        }


async def main() -> None:
    props = read_properties()
    semaphore = asyncio.Semaphore(4)
    rows = await asyncio.gather(*(run_one(props[pid], semaphore) for pid in sorted(IDS, key=int)))
    payload = {
        "guardrails": {
            "direct_only": True, "captcha_solving": False, "web_unlocker": False,
            "flaresolverr": False, "fingerprint_rotation": False,
            "hyperbrowser": False, "llm": False, "paid_canary": False,
        },
        "results": rows,
    }
    path = OUT / "direct_adapter_residual.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(path)
    for row in rows:
        print(json.dumps({key: row.get(key) for key in ("property_id", "outcome", "tier", "units", "strict_native_positive_rent_rows", "plans", "bases", "source_property_ids", "source_property_names", "errors")}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
