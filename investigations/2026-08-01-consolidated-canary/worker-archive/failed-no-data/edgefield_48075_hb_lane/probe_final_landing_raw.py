from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    _HbSession,
    _INPAGE_FETCH_JS,
    _session_options,
)
from ma_poc.pms.scraper import scrape_jugnu


PID = "48075"
URL = "https://www.edgefieldaptsva.com/"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/edgefield_48075_hb_lane")
OUTPUT = ROOT / "final_landing_raw_discovery.json"
BODY = ROOT / "final_landing_raw.html.gz"
SESSION_RE = re.compile(r"(ClientSessionID=)[^&\"'\\s]+", re.IGNORECASE)


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return SESSION_RE.sub(r"\1<redacted>", value)
    return value


def positive_rent(row):
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def metadata():
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        return next(
            row for row in csv.DictReader(handle) if row["apartmentid"] == PID
        )


async def main() -> None:
    assert os.environ.get("COMPLIANCE_MODE") == "1"
    assert os.environ.get("HB_USE_STEALTH") == "0"
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False

    session = _HbSession(mode="render")
    try:
        page = await session.open()
        await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        final_url = str(page.url or URL)
        parts = urlsplit(final_url)
        final_relative = parts.path + (("?" + parts.query) if parts.query else "")
        response = await page.evaluate(_INPAGE_FETCH_JS, final_relative)
    finally:
        await session.close()

    status = int((response or {}).get("status") or 0)
    body_text = str((response or {}).get("body") or "")
    body = body_text.encode()
    with gzip.open(BODY, "wb") as handle:
        handle.write(body)

    task = CrawlTask(
        url=URL,
        property_id=PID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    fetched = FetchResult(
        url=URL,
        outcome=FetchOutcome.OK if status == 200 and body else FetchOutcome.HARD_FAIL,
        status=status,
        body=body or None,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=URL,
        attempts=1,
        elapsed_ms=0,
        error_signature="local_final_landing_path_probe",
    )
    result = await scrape_jugnu(
        task,
        fetched,
        page=None,
        profile=None,
        csv_row=metadata(),
    )
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [
        row
        for row in units
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    payload = sanitize(
        {
            "guardrails": {
                "hyperbrowser_sessions": 1,
                "session_options": options,
                "captcha_solving": False,
                "web_unlocker": False,
                "flaresolverr": False,
                "fingerprint_rotation": False,
                "llm": False,
                "paid_canary": False,
            },
            "navigation": {
                "configured_url": URL,
                "final_url": final_url,
                "final_relative": final_relative,
                "raw_status": status,
                "raw_body_bytes": len(body),
                "raw_body_sha256": hashlib.sha256(body).hexdigest(),
                "exact_name_visible": "edgefield" in body_text.casefold(),
                "exact_street_visible": "5699 craneybrook" in body_text.casefold(),
                "exact_zip_visible": "23703" in body_text,
                "published_site_ids": sorted(
                    set(
                        re.findall(
                            r"(?:siteId=|siteid=)(\\d+)", body_text, re.IGNORECASE
                        )
                    )
                ),
                "published_onlineleasing_hosts": sorted(
                    set(
                        re.findall(
                            r"https?://([a-z0-9-]+\\.onlineleasing\\.realpage\\.com)",
                            body_text,
                            re.IGNORECASE,
                        )
                    )
                ),
            },
            "scrape": {
                "detected": result.get("_detected_pms"),
                "adapter": result.get("_adapter_used"),
                "tier": result.get("extraction_tier_used"),
                "units": len(units),
                "strict_native_positive_rent_rows": len(strict),
                "plan_summaries": len(result.get("plan_summaries") or []),
                "errors": result.get("errors") or [],
                "rows": [
                    {
                        "unit_number": row.get("unit_number") or "",
                        "floor_plan_name": row.get("floor_plan_name") or "",
                        "bedrooms": row.get("bedrooms") or "",
                        "bathrooms": row.get("bathrooms") or "",
                        "sqft": row.get("sqft") or "",
                        "market_rent_low": row.get("market_rent_low"),
                        "market_rent_high": row.get("market_rent_high"),
                        "availability_status": row.get("availability_status") or "",
                        "source_property_id": row.get("source_property_id") or "",
                        "source_property_provenance": row.get(
                            "source_property_provenance"
                        )
                        or "",
                        "source_api_url": row.get("source_api_url") or "",
                    }
                    for row in strict
                ],
            },
        }
    )
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload))
    print(json.dumps({"output": str(OUTPUT), "body": str(BODY)}))


if __name__ == "__main__":
    asyncio.run(main())
