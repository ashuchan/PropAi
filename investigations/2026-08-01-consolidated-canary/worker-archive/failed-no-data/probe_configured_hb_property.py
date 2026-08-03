from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sys

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    HyperbrowserProvider,
    _session_options,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.scraper import scrape_jugnu


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def compact(row: dict[str, object]) -> dict[str, object]:
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


async def main() -> None:
    pid = sys.argv[1]
    assert os.environ.get("COMPLIANCE_MODE") == "1"
    assert os.environ.get("HB_USE_STEALTH") == "0"
    assert os.environ.get("HB_USE_PROXY") == "1"
    assert os.environ.get("HYPERBROWSER_MAX_CALLS_PER_PROPERTY") == "1"
    assert not os.environ.get("PROBE_PROXY_URL", "").strip()
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False
    assert options["useProxy"] is True
    with open("ma_poc/config/properties.csv", encoding="utf-8-sig", newline="") as handle:
        metadata = next(row for row in csv.DictReader(handle) if row["apartmentid"] == pid)
    url = metadata["website"]
    if "://" not in url:
        url = "https://" + url
    task = CrawlTask(
        url=url,
        property_id=pid,
        priority=0,
        budget_ms=180_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    reset_hyperbrowser_property_counts()
    fetched = await HyperbrowserProvider(mode="render").fetch(task, None)
    result = {}
    if fetched.body:
        result = await asyncio.wait_for(
            scrape_jugnu(
                task,
                fetched,
                page=None,
                profile=None,
                csv_row=metadata,
            ),
            timeout=180,
        )
    units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
    strict = [row for row in units if unit_has_real_anchor(row) and positive_rent(row)]
    unit_numbers = [str(row.get("unit_number") or "").strip() for row in strict]
    payload = {
        "guardrails": {
            "compliance_mode": True,
            "session_options": options,
            "hyperbrowser_max_calls_per_property": 1,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "llm": False,
            "paid_canary": False,
        },
        "property_id": int(pid),
        "configured_identity": {
            key: metadata[key] for key in ("name", "address", "city", "state", "zip")
        },
        "configured_url": url,
        "fetch": {
            "outcome": fetched.outcome.value,
            "status": fetched.status,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "body_sha256": hashlib.sha256(fetched.body or b"").hexdigest(),
            "network_responses": len(fetched.network_log),
        },
        "hb_calls": hyperbrowser_property_call_count(pid),
        "adapter": result.get("_adapter_used"),
        "detected": result.get("_detected_pms"),
        "tier": result.get("extraction_tier_used"),
        "winning_page_url": result.get("winning_page_url"),
        "emitted_units": len(units),
        "strict_native_positive_rows": len(strict),
        "distinct_native_unit_numbers": len(set(unit_numbers)),
        "all_units_strict": bool(units) and len(units) == len(strict),
        "native_unit_numbers_nonblank_unique": bool(unit_numbers)
        and all(unit_numbers)
        and len(unit_numbers) == len(set(unit_numbers)),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "rows": [compact(row) for row in strict],
        "errors": list(result.get("errors") or []),
        "fallback_chain": result.get("_fallback_chain") or [],
        "raw_api_metadata": [
            {key: item.get(key) for key in ("url", "status", "via")}
            for item in (result.get("_raw_api_responses") or [])
            if isinstance(item, dict)
        ],
    }
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
