from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._doorloop_listings import (
    build_doorloop_feed_url,
    extract_published_doorloop_listing_urls,
)
from ma_poc.pms.adapters._probe import probe_fetch_status
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter
from ma_poc.pms.detector import detect_pms


OUT = Path("/private/tmp/propai-fnd-vBkmT9/evidence_doorloop_park_place_current_e2e.json")
PROPERTY_ID = "254556"
PROPERTY_NAME = "Park Place"
WEBSITE = "https://www.parkplacegreenville.com/"
ADDRESS = "305 W Jack Finney Blvd"
CITY = "Greenville"
STATE = "TX"
ZIP = "75402"


async def main() -> None:
    status, body = await probe_fetch_status(WEBSITE)
    if status != 200 or not body:
        raise SystemExit(f"marketing fetch failed: status={status} bytes={len(body)}")
    published = extract_published_doorloop_listing_urls(body)
    if len(published) != 1:
        raise SystemExit(f"expected one exact published DoorLoop route, got {published}")
    feed_url = build_doorloop_feed_url(published[0])
    if not feed_url:
        raise SystemExit("published DoorLoop route did not derive a feed")

    ctx = AdapterContext(
        base_url=WEBSITE,
        detected=detect_pms(WEBSITE, page_html=body),
        profile=None,
        expected_total_units=None,
        property_id=PROPERTY_ID,
        fetch_result=SimpleNamespace(body=body.encode(), final_url=WEBSITE),
        property_name=PROPERTY_NAME,
        address=ADDRESS,
        city=CITY,
        state=STATE,
        zip_code=ZIP,
    )
    result = await WixNoPmsAdapter().extract(None, ctx)  # type: ignore[arg-type]
    strict_rows = [
        row
        for row in result.units
        if unit_has_real_anchor(row)
        and isinstance(row.get("market_rent_low"), (int, float))
        and not isinstance(row.get("market_rent_low"), bool)
        and row["market_rent_low"] > 0
        and row.get("source_property_provenance")
        == "published_doorloop_company_link_address_bound"
    ]
    listing_ids = [
        str((row.get("source_ids") or {}).get("doorloop_listing_id") or "")
        for row in strict_rows
    ]
    property_ids = [
        str((row.get("source_ids") or {}).get("doorloop_property_id") or "")
        for row in strict_rows
    ]
    unit_numbers = [str(row.get("unit_number") or "") for row in strict_rows]
    addresses = [str(row.get("source_property_address") or "") for row in strict_rows]
    source_urls = sorted({str(row.get("source_api_url") or "") for row in strict_rows})
    strict_pass = bool(
        result.tier_used == "TIER_1_API_DOORLOOP_MITS"
        and strict_rows
        and len(strict_rows) == len(result.units)
        and len(set(listing_ids)) == len(listing_ids)
        and len(set(unit_numbers)) == len(unit_numbers)
        and all(len(value) == 24 for value in listing_ids)
        and all(len(value) == 24 for value in property_ids)
        and all("305 W Jack Finney Blvd" in value for value in addresses)
        and source_urls == [feed_url]
    )
    payload = {
        "strict_outcome": "UNIT_QUALIFIED" if strict_pass else "UNIT_UNVERIFIED",
        "property_id": int(PROPERTY_ID),
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "rp_oracle_native_unit_rows": 5,
        "current_native_unit_rows": len(strict_rows),
        "native_unit_rows_with_positive_rent": len(strict_rows),
        "distinct_native_listing_ids": len(set(listing_ids)),
        "distinct_native_unit_numbers": len(set(unit_numbers)),
        "property_boundary": {
            "verdict": "exact_property_match" if strict_pass else "unverified",
            "configured_address": f"{ADDRESS}, {CITY}, {STATE} {ZIP}",
            "observed_addresses": sorted(set(addresses)),
            "published_portal_url": published[0],
            "source_endpoint": feed_url,
            "contamination_rows": sum(
                "305 W Jack Finney Blvd" not in value for value in addresses
            ),
        },
        "source_endpoint": feed_url,
        "source_portal_url": published[0],
        "adapter": "wix_nopms",
        "tier": result.tier_used,
        "local_adapter_e2e_passed": strict_pass,
        "llm_enabled": False,
        "captcha_solving": False,
        "paid_canary_run": False,
        "units": [
            {
                "unit_number": row.get("unit_number"),
                "native_unit_id": (row.get("source_ids") or {}).get(
                    "doorloop_listing_id"
                ),
                "native_property_id": (row.get("source_ids") or {}).get(
                    "doorloop_property_id"
                ),
                "rent": row.get("market_rent_low"),
                "bedrooms": row.get("bedrooms"),
                "bathrooms": row.get("bathrooms"),
                "sqft": row.get("sqft"),
                "availability_date": row.get("availability_date"),
                "source_property_address": row.get("source_property_address"),
            }
            for row in strict_rows
        ],
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({key: payload[key] for key in (
        "strict_outcome",
        "property_id",
        "current_native_unit_rows",
        "distinct_native_listing_ids",
        "distinct_native_unit_numbers",
        "tier",
        "local_adapter_e2e_passed",
    )}))
    if not strict_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
