from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch import fetch
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import _session_options
from ma_poc.pms.scraper import scrape_jugnu


PROPERTY_ID = "48075"
CONFIGURED_URL = "https://www.edgefieldaptsva.com/"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/edgefield_48075_hb_lane")
REPEAT = os.environ.get("E2E_REPEAT", "single").strip()
if not re.fullmatch(r"[A-Za-z0-9_-]+", REPEAT):
    raise RuntimeError("invalid E2E_REPEAT")
OUTPUT = ROOT / f"configured_e2e_{REPEAT}.json"
SESSION_RE = re.compile(r"(ClientSessionID=)[^&\"'\\s]+", re.IGNORECASE)


def _sanitize(value):
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return SESSION_RE.sub(r"\1<redacted>", value)
    return value


def _positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def _metadata() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        return next(
            row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") == PROPERTY_ID
        )


async def main() -> None:
    expected = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "hyperbrowser",
        "HB_USE_STEALTH": "0",
        "HB_USE_PROXY": "1",
        "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_BODY_RESOLVER": "false",
    }
    for key, wanted in expected.items():
        if os.environ.get(key, "").casefold() != wanted.casefold():
            raise RuntimeError(f"{key} guardrail mismatch")
    options = _session_options("render")
    if options.get("solveCaptchas") is not False:
        raise RuntimeError("Hyperbrowser CAPTCHA solving must remain disabled")
    if options.get("useStealth") is not False:
        raise RuntimeError("Hyperbrowser stealth must remain disabled")
    if options.get("useProxy") is not True:
        raise RuntimeError("Hyperbrowser proxy must remain enabled")

    task = CrawlTask(
        url=CONFIGURED_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    fetched = await asyncio.wait_for(fetch(task, profile=None), timeout=120)
    result = await asyncio.wait_for(
        scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=_metadata(),
        ),
        timeout=120,
    )
    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [
        row
        for row in units
        if unit_has_real_anchor(row) and _positive_rent(row)
    ]
    body = fetched.body or b""
    body_text = body.decode("utf-8", "replace")
    published_site_ids = sorted(
        set(re.findall(r"(?:siteId=|siteid=)(\d+)", body_text, re.IGNORECASE))
    )
    published_onlineleasing_hosts = sorted(
        set(
            re.findall(
                r"https?://([a-z0-9-]+\.onlineleasing\.realpage\.com)",
                body_text,
                re.IGNORECASE,
            )
        )
    )
    payload = _sanitize(
        {
            "repeat": REPEAT,
            "guardrails": {
                "captcha_solving": False,
                "web_unlocker": False,
                "flaresolverr": False,
                "fingerprint_rotation": False,
                "hyperbrowser": True,
                "hyperbrowser_max_calls_per_property": 1,
                "hyperbrowser_use_stealth": False,
                "hyperbrowser_use_proxy": True,
                "llm": False,
                "paid_canary": False,
                "session_options": options,
            },
            "property_id": int(PROPERTY_ID),
            "configured_url": CONFIGURED_URL,
            "fetch": {
                "outcome": fetched.outcome.value,
                "status": fetched.status,
                "final_url": fetched.final_url,
                "body_bytes": len(fetched.body or b""),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "error_signature": fetched.error_signature,
                "captcha_detected": fetched.captcha_detected,
                "fetch_tier_used": fetched.fetch_tier_used,
                "fetch_tier_attempts": fetched.fetch_tier_attempts,
                "exact_name_visible": "edgefield" in body_text.casefold(),
                "exact_street_visible": "5699 craneybrook" in body_text.casefold(),
                "exact_city_state_zip_visible": (
                    "portsmouth" in body_text.casefold()
                    and "va" in body_text.casefold()
                    and "23703" in body_text
                ),
                "published_site_ids": published_site_ids,
                "published_onlineleasing_hosts": published_onlineleasing_hosts,
            },
            "scrape": {
                "detected": result.get("_detected_pms"),
                "adapter": result.get("_adapter_used"),
                "tier": result.get("extraction_tier_used"),
                "units": len(units),
                "strict_native_positive_rent_rows": len(strict),
                "plan_summaries": len(result.get("plan_summaries") or []),
                "fallback_chain": result.get("_fallback_chain") or [],
                "errors": result.get("errors") or [],
                "all_rows_have_expected_source_property_id": bool(strict)
                and all(str(row.get("source_property_id") or "") == "1060300" for row in strict),
                "all_rows_have_distinct_native_anchor": len(
                    {str(row.get("unit_number") or "") for row in strict}
                )
                == len(strict),
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
                        "availability_date": row.get("availability_date") or "",
                        "source_property_id": row.get("source_property_id") or "",
                        "source_property_provenance": row.get(
                            "source_property_provenance"
                        )
                        or "",
                        "source_portal_url": row.get("source_portal_url") or "",
                        "source_api_url": row.get("source_api_url") or "",
                    }
                    for row in strict
                ],
            },
        }
    )
    ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload))
    print(json.dumps({"output": str(OUTPUT)}))


if __name__ == "__main__":
    asyncio.run(main())
