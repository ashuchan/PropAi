#!/usr/bin/env python3
"""Run the current scraper against Clear Run's exact same-origin Gables route."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any


os.environ["COMPLIANCE_MODE"] = "1"
os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""

from ma_poc.fetch.contracts import (  # noqa: E402
    FetchOutcome,
    FetchResult,
    RenderMode,
)
from ma_poc.pms.adapters._probe import (  # noqa: E402
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.scraper import scrape  # noqa: E402


PROPERTY_ID = "4756"
EXACT_URL = "https://www.clearrunaptswilmington.com/community/2430886"


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


async def main() -> None:
    reset_web_unlocker_call_count()
    response = probe_get(
        EXACT_URL,
        timeout=35,
        unlocker=False,
        retries=2,
        proxies={},
        verify=True,
    )
    fetch_result = FetchResult(
        url=EXACT_URL,
        outcome=FetchOutcome.OK,
        status=int(response.status_code),
        body=response.content,
        headers={str(k).lower(): str(v) for k, v in response.headers.items()},
        render_mode=RenderMode.GET,
        final_url=str(response.url),
        attempts=1,
        elapsed_ms=0,
    )
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    result = await scrape(
        EXACT_URL,
        page=None,
        fetch_result=fetch_result,
        csv_row={
            "apartmentid": PROPERTY_ID,
            "name": "Clear Run",
            "address": "5300 New Centre Dr",
            "city": "Wilmington",
            "state": "NC",
            "zip": "28403",
            "website": "https://www.clearrunaptswilmington.com/",
        },
        property_id=PROPERTY_ID,
        shared_budget=budget,
    )
    rows = [row for row in result.get("units", []) if isinstance(row, dict)]
    strict = [row for row in rows if row.get("unit_number") and positive_rent(row)]
    print(
        json.dumps(
            {
                "adapter": result.get("_adapter_used"),
                "detected_pms": result.get("_detected_pms"),
                "tier": result.get("extraction_tier_used"),
                "winning_url": result.get("winning_url"),
                "errors": result.get("errors"),
                "units": len(rows),
                "strict_rows": len(strict),
                "distinct_native_ids": len(
                    {str(row.get("unit_number")).casefold() for row in strict}
                ),
                "source_urls": sorted(
                    {
                        str(row.get("source_api_url") or "")
                        for row in strict
                        if row.get("source_api_url")
                    }
                ),
                "sample": strict[:3],
                "web_unlocker_calls": web_unlocker_call_count(),
                "budget": budget,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
