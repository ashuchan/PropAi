from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider
from ma_poc.pms.scraper import scrape


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_ID = "55165"
START_URL = "https://legacyoaksapts.com/"
OUTPUT = ROOT / "evidence_rentmanager_55165_current_hb_e2e.json"


def _canonical() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            if str(row.get("apartmentid") or "").strip() == PROPERTY_ID:
                return row
    raise RuntimeError("canonical property 55165 not found")


def _positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
        )
    )


async def main() -> None:
    canonical = _canonical()
    task = CrawlTask(
        url=START_URL,
        property_id=PROPERTY_ID,
        priority=0,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.RENDER,
        budget_ms=180_000,
    )
    fetch_result = await HyperbrowserProvider(mode="render").fetch(task, None)
    body = (fetch_result.body or b"").decode("utf-8", "replace")
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    result = await scrape(
        START_URL,
        page=None,
        fetch_result=fetch_result,
        csv_row={
            "apartmentid": PROPERTY_ID,
            "name": canonical.get("name") or "Legacy Oaks",
            "address": canonical.get("address") or "",
            "city": canonical.get("city") or "",
            "state": canonical.get("state") or "",
            "zip": canonical.get("zip") or "",
            "website": START_URL,
        },
        property_id=PROPERTY_ID,
        shared_budget=budget,
    )
    units = [row for row in (result.get("units") or []) if isinstance(row, dict)]
    native_rows = [row for row in units if unit_has_real_anchor(row)]
    strict_rows = [row for row in native_rows if _positive_rent(row)]
    source_urls = sorted(
        {
            str(row.get("source_api_url") or "").strip()
            for row in strict_rows
            if str(row.get("source_api_url") or "").strip()
        }
    )
    source_hosts = {
        (urlparse(url).hostname or "").casefold().removeprefix("www.")
        for url in source_urls
    }
    expected_host = "legacyoaksapts.com"
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = re.sub(r"<[^>]+>", " ", title_match.group(1)).strip() if title_match else ""
    property_boundary = bool(
        fetch_result.ok()
        and fetch_result.final_url == "https://legacyoaksapts.com/unit-availability/"
        and source_hosts == {expected_host}
        and len(units) == len(native_rows) == len(strict_rows) > 0
        and "legacy oaks apartments" in body.casefold()
        and "675 white oak circle" in body.casefold()
        and all(
            str((row.get("source_ids") or {}).get("rentmanager_uid") or "").strip()
            for row in strict_rows
        )
    )
    evidence_row = {
        "property_id": int(PROPERTY_ID),
        "property_name": canonical.get("name") or "Legacy Oaks",
        "website": START_URL,
        "outcome": "UNIT_QUALIFIED" if property_boundary else "UNIT_UNVERIFIED",
        "adapter": result.get("_adapter_used") or "rentmanager",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(strict_rows),
        "plans": len(result.get("plan_summaries") or []),
        "property_identity_match": property_boundary,
        "contamination_verdict": (
            "pass_exact_same_origin_current_hb_native_identity"
            if property_boundary
            else "unverified_property_boundary"
        ),
        "identity_evidence": {
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(strict_rows),
            "distinct_unit_numbers": len(
                {str(row.get("unit_number") or "") for row in strict_rows}
            ),
            "source_urls": source_urls,
            "source_hosts": sorted(source_hosts),
            "published_inventory_url": fetch_result.final_url,
            "title": title,
            "canonical_address_in_current_body": "675 white oak circle"
            in body.casefold(),
        },
        "identity_samples": [
            {
                "identity": {"unit_number": str(row.get("unit_number") or "")},
                "source_ids": dict(row.get("source_ids") or {}),
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "availability_date": row.get("availability_date"),
                "source_api_url": row.get("source_api_url"),
            }
            for row in strict_rows
        ],
        "fetch": {
            "outcome": fetch_result.outcome.value,
            "status": fetch_result.status,
            "final_url": fetch_result.final_url,
            "body_bytes": len(fetch_result.body or b""),
            "hyperbrowser_session_count": 1,
            "captcha_solving": False,
        },
        "validation_scope": "current_live_hyperbrowser_provider_plus_actual_scraper",
        "llm_enabled": False,
        "paid_canary_run": False,
    }
    payload = {
        "batch_label": "rentmanager-legacy-oaks-current-hb-e2e",
        "filters": {"property_ids": [55165]},
        "results": [evidence_row],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence_row, indent=2))


asyncio.run(main())
