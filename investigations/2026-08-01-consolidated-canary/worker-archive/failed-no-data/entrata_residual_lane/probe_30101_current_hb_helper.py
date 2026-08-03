from __future__ import annotations

import asyncio
import csv
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ma_poc.fetch.hyperbrowser_backend import hyperbrowser_property_call_count
from ma_poc.pms.adapters._entrata_hb_recovery import (
    recover_entrata_hb_conventional,
    strict_conventional_url,
)
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms

OUTPUT = Path(
    "/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane/"
    "probe_30101_current_hb_helper.json"
)


def read_property() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        newline="", encoding="utf-8-sig"
    ) as handle:
        return next(
            item for item in csv.DictReader(handle) if item["apartmentid"] == "30101"
        )


async def main() -> None:
    for name, expected in {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "hyperbrowser",
        "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
    }.items():
        if os.environ.get(name, "").casefold() != expected:
            raise RuntimeError(f"guardrail {name} must equal {expected!r}")
    row = read_property()
    response = await asyncio.to_thread(
        probe_get,
        row["website"],
        timeout=30,
        unlocker=False,
        retries=1,
    )
    body = str(response.text or "")
    final_url = str(response.url or row["website"])
    context = AdapterContext(
        base_url=final_url,
        detected=detect_pms(final_url, page_html=body),
        profile=None,
        expected_total_units=None,
        property_id="30101",
        fetch_result=SimpleNamespace(body=body.encode(), final_url=final_url),
        property_name=row["name"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip"],
    )
    context._api_responses = []
    matched_url = strict_conventional_url(body, final_url, row["name"])
    try:
        outcome = await asyncio.wait_for(
            recover_entrata_hb_conventional(context),
            timeout=90,
        )
        payload = {
            "property_id": 30101,
            "configured_url": row["website"],
            "configured_final_url": final_url,
            "matched_url": matched_url,
            "attempted": outcome.attempted,
            "complete": outcome.complete,
            "failure_reason": outcome.failure_reason,
            "units": len(outcome.units),
            "plans": len(outcome.plan_rows),
            "session_calls": hyperbrowser_property_call_count("30101"),
            "source_urls": sorted(
                {
                    str(unit.get("source_api_url") or "")
                    for unit in outcome.units
                    if str(unit.get("source_api_url") or "")
                }
            ),
            "sample_units": outcome.units[:3],
        }
    except asyncio.TimeoutError:
        payload = {
            "property_id": 30101,
            "configured_url": row["website"],
            "configured_final_url": final_url,
            "matched_url": matched_url,
            "outcome": "TIMEOUT",
            "session_calls": hyperbrowser_property_call_count("30101"),
        }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
