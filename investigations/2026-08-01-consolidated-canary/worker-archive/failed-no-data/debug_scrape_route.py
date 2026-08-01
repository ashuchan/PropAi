from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms import scraper as scraper_mod

import evidence_rerun as er


async def main() -> None:
    metadata = er.canonical_metadata()
    records = {
        int(row["property_id"]): er.with_canonical_metadata(row, metadata)
        for row in json.loads((er.ROOT / "failed344.json").read_text())
    }
    for property_id in (42371, 218786):
        record = records[property_id]
        fetch_result = er.fetch_for(record)
        profile_path = er.ROOT / "profiles" / f"{property_id}.json"
        profile = (
            ScrapeProfile.model_validate_json(profile_path.read_text())
            if profile_path.exists()
            else None
        )
        csv_row = {
            "apartmentid": str(property_id),
            "name": record.get("proj_name") or "",
            "address": record.get("address") or "",
            "city": record.get("city") or "",
            "state": record.get("state") or "",
            "zip": record.get("zip_code") or "",
            "website": record.get("website") or "",
        }
        result = await scraper_mod.scrape(
            str(record.get("website") or ""),
            profile=profile,
            page=None,
            fetch_result=fetch_result,
            csv_row=csv_row,
            property_id=str(property_id),
            shared_budget={
                "llm_api_calls": 0,
                "llm_dom_calls": 0,
                "llm_monolithic": 0,
                "link_hop": 0,
                "_cost_cap_usd": 0,
            },
        )
        print(
            json.dumps(
                {
                    "property_id": property_id,
                    "detected": result.get("_detected_pms"),
                    "adapter": result.get("_adapter_used"),
                    "tier": result.get("extraction_tier_used"),
                    "units": len(result.get("units") or []),
                    "plans": len(result.get("plan_summaries") or []),
                    "fallback_chain": result.get("_fallback_chain"),
                    "errors": result.get("errors"),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
