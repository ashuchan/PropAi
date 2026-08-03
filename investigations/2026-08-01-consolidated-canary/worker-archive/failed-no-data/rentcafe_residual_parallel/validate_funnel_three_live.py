from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.funnel import FunnelAdapter
from ma_poc.pms.detector import detect_pms


OUT = Path("/private/tmp/propai-fnd-vBkmT9/rentcafe_residual_parallel")
TARGETS = [
    {
        "label": "262799",
        "canonical_id": "262799",
        "name": "220 East 72nd",
        "address": "220 E 72nd St",
        "city": "New York",
        "state": "NY",
        "zip": "10021",
        "url": "https://www.dermotcompany.com/building/220-east-72nd-street#availability",
        "html_gz": OUT / "262799_0_www_dermotcompany_com.html.gz",
    },
    {
        "label": "control_3388",
        "canonical_id": "control-3388",
        "name": "21 West End Ave",
        "address": "21 West End Avenue",
        "city": "New York",
        "state": "NY",
        "zip": "10023",
        "url": "https://www.dermotcompany.com/building/21-west-end-ave",
        "html": OUT / "control_21westend.html",
    },
    {
        "label": "control_3226",
        "canonical_id": "control-3226",
        "name": "535 W 43rd Street",
        "address": "535 West 43rd Street",
        "city": "New York",
        "state": "NY",
        "zip": "10036",
        "url": "https://www.dermotcompany.com/building/535-w-43rd-street",
        "html": OUT / "control_535west43.html",
    },
]


def body_for(target: dict) -> bytes:
    if target.get("html_gz"):
        return gzip.open(target["html_gz"], "rb").read()
    return Path(target["html"]).read_bytes()


def positive_rent(row: dict) -> bool:
    rent = row.get("market_rent_low")
    return isinstance(rent, (int, float)) and not isinstance(rent, bool) and rent > 0


def sample(row: dict) -> dict:
    return {
        key: row.get(key)
        for key in (
            "unit_number", "floor_plan_name", "bedrooms", "bathrooms", "sqft",
            "market_rent_low", "market_rent_high", "availability_date",
            "source_api_url", "source_property_id", "source_property_name",
            "source_property_address", "source_property_provenance", "source_ids",
        )
    }


async def run(target: dict) -> dict:
    body = body_for(target)
    html = body.decode("utf-8", "replace")
    ctx = AdapterContext(
        base_url=target["url"],
        detected=detect_pms(target["url"], page_html=html),
        profile=None,
        expected_total_units=None,
        property_id=target["canonical_id"],
        fetch_result=SimpleNamespace(body=body, final_url=target["url"]),
        property_name=target["name"],
        address=target["address"],
        city=target["city"],
        state=target["state"],
        zip_code=target["zip"],
    )
    ctx._api_responses = []
    result = await FunnelAdapter().extract(None, ctx)
    rows = [row for row in result.units if isinstance(row, dict)]
    strict = [row for row in rows if unit_has_real_anchor(row) and positive_rent(row)]
    return {
        "label": target["label"],
        "configured_url": target["url"],
        "configured_body_sha256": hashlib.sha256(body).hexdigest(),
        "detected": ctx.detected.pms,
        "detected_evidence": ctx.detected.evidence,
        "tier": result.tier_used,
        "winning_url": result.winning_url,
        "units": len(rows),
        "strict_native_positive_rent_rows": len(strict),
        "distinct_unit_numbers": len({str(row.get("unit_number") or "") for row in strict}),
        "distinct_native_listing_ids": len({str((row.get("source_ids") or {}).get("funnel_listing_id") or "") for row in strict}),
        "source_property_ids": sorted({str(row.get("source_property_id") or "") for row in strict}),
        "source_property_names": sorted({str(row.get("source_property_name") or "") for row in strict}),
        "all_rows_strict": bool(rows and len(rows) == len(strict)),
        "samples": [sample(row) for row in strict[:5]],
        "errors": result.errors,
    }


async def main() -> None:
    rows = []
    for target in TARGETS:
        row = await run(target)
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
    payload = {
        "lane": "rentcafe_residual_current_funnel_published_listings_three",
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
        "results": rows,
    }
    output = OUT / "funnel_published_listings_three_live.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"artifact": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
