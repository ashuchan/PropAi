from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import time
from pathlib import Path

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = ROOT / "unknown_wix_parallel" / "remaining_unknown_wix_current_screen.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def positive_rent(unit: dict) -> bool:
    for key in (
        "market_rent_low",
        "market_rent_high",
        "rent_low",
        "rent_high",
        "asking_rent",
        "rent",
    ):
        try:
            if float(unit.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(re.search(r"\$\s*[1-9]", str(unit.get("rent_range") or "")))


async def direct_fetch(task: CrawlTask, profile=None) -> FetchResult:
    del profile
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(
            probe_get,
            task.url,
            timeout=30,
            unlocker=False,
            retries=1,
            proxies={},
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.DEAD_URL
            if status in {404, 410, 451}
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(getattr(response, "headers", {}) or {}),
            render_mode=task.render_mode,
            final_url=str(response.url or task.url),
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.TRANSIENT,
            status=None,
            body=None,
            headers={},
            render_mode=task.render_mode,
            final_url=task.url,
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_signature=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


def make_task(property_id: str, url: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


async def screen_one(residual: dict[str, str], metadata: dict[str, str]) -> dict:
    property_id = residual["property_id"]
    url = residual["website"]
    task = make_task(property_id, url)
    fetched = await direct_fetch(task)
    result: dict = {}
    exception = ""
    if fetched.body:
        try:
            result = await asyncio.wait_for(
                scraper_mod.scrape_jugnu(
                    task,
                    fetched,
                    page=None,
                    profile=None,
                    csv_row=metadata,
                ),
                timeout=150,
            )
        except Exception as exc:  # noqa: BLE001
            exception = f"{type(exc).__name__}: {str(exc)[:500]}"

    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [
        row for row in units if unit_has_real_anchor(row) and positive_rent(row)
    ]
    ids = [str(row.get("unit_number") or "").strip() for row in strict]
    source_property_ids = sorted(
        {
            str(row.get("source_property_id") or "")
            for row in strict
            if str(row.get("source_property_id") or "").strip()
        }
    )
    official = result.get("_official_application_onesite") or {}
    site_id = str(official.get("site_id") or "")
    application_url = str(official.get("application_url") or "")
    official_strict = bool(
        result.get("_official_application_onesite_success") is True
        and official.get("accepted") is True
        and official.get("reason") == "exact_application_onesite_chain"
        and official.get("roster_accepted") is True
        and site_id
        and source_property_ids == [site_id]
        and units
        and len(strict) == len(units) == len(ids) == len(set(ids))
        and not exception
    )
    row = {
        "property_id": property_id,
        "property_name": metadata.get("name") or residual.get("property_name") or "",
        "website": url,
        "assigned_adapter": residual.get("current_detected_adapter") or "",
        "fetch_status": fetched.status,
        "fetch_outcome": fetched.outcome.value,
        "final_url": fetched.final_url,
        "detected": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(units),
        "native_positive_rent_rows": len(strict),
        "official_application_success": bool(
            result.get("_official_application_onesite_success")
        ),
        "official_application_strict": official_strict,
        "application_url": application_url,
        "portal_url": str(official.get("portal_url") or ""),
        "site_id": site_id,
        "source_property_ids": source_property_ids,
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": str(result.get("_link_hop_from") or ""),
        "errors": result.get("errors") or [],
        "exception": exception,
    }
    print(json.dumps(row, sort_keys=True), flush=True)
    return row


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        if os.environ.get(name, "").casefold() != expected:
            raise RuntimeError(f"{name} must be {expected}")
    if os.environ.get("WEB_UNLOCKER_KEY", "").strip():
        raise RuntimeError("WEB_UNLOCKER_KEY must be blank")

    remaining = [
        row
        for row in read_csv(REMAINING)
        if row.get("current_detected_adapter") in {"unknown", "wix_nopms"}
        and row.get("website")
    ]
    metadata_by_id = {
        row["apartmentid"]: row for row in read_csv(PROPERTIES)
    }
    fetch_mod.fetch = direct_fetch
    results = []
    for residual in remaining:
        metadata = metadata_by_id.get(residual["property_id"])
        if metadata is None:
            raise RuntimeError(f"missing property metadata: {residual['property_id']}")
        results.append(await screen_one(residual, metadata))
    OUTPUT.write_text(
        json.dumps(
            {
                "guardrails": {
                    "compliance_mode": True,
                    "llm": False,
                    "web_unlocker": False,
                    "hyperbrowser": False,
                    "captcha_solving": False,
                    "flaresolverr": False,
                    "fingerprint_rotation": False,
                    "paid_canary": False,
                },
                "remaining_snapshot": str(REMAINING),
                "screened": len(results),
                "strict_candidates": [
                    row for row in results if row["official_application_strict"]
                ],
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
