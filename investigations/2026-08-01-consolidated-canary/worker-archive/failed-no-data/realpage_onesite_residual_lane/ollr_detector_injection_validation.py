from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import ma_poc.fetch as fetch_mod
import ma_poc.pms.detector as detector_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_ollr_detector_current_source_full_pipeline9.json"

MARKER = "/ollr/widgetloader.js?siteid="
SITE_ID_RE = re.compile(r"/ollr/widgetLoader\.js\?siteId=(\d+)", re.IGNORECASE)
SESSION_RE = re.compile(r"(ClientSessionID=)[^&\"'\s]+", re.IGNORECASE)

# Discovered by a direct-only scan of all 170 current residual rows. Every
# non-configured URL is the first high-scored same-origin inventory anchor
# published by the configured property page; no guessed paths are included.
CANDIDATES = {
    2948: "https://crystalwoodsapts.com/floorplans/",
    16172: "https://townecrest.com/floorplans/",
    18194: "https://www.forestproperties.net/property/availabilities/parke-place-village/",
    36530: "https://www.thepointatmonroeplace.com/floor-plans",
    54798: "https://www.thepointatabington.com/availability",
    74872: "https://www.heatherridgeapts.net/floor-plans",
    228341: "https://commonsatcowanboulevard.com/floorplans/",
    253326: "https://www.thepointatreston.com/floor-plans",
    265143: "https://www.thepointatkingston.com/availability",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_url(value: str) -> str:
    value = value.strip()
    return value if "://" in value else f"https://{value}"


def host_key(value: str) -> str:
    return (urlparse(value).hostname or "").lower().removeprefix("www.")


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return SESSION_RE.sub(r"\1<redacted>", value)
    return value


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def strict_units(result: dict) -> list[dict]:
    return [
        row
        for row in result.get("units") or []
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]


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
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers={},
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
            error_signature=type(exc).__name__,
        )


def task(url: str, property_id: int) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=str(property_id),
        priority=0,
        budget_ms=35_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )


async def fetch_inventory_page(pid: int, url: str) -> tuple[FetchResult, str, list[str]]:
    fetched = await direct_fetch(task(url, pid))
    body = (fetched.body or b"").decode("utf-8", "replace")
    return fetched, body, sorted(set(SITE_ID_RE.findall(body)))


async def baseline_inventory_page(
    pid: int,
    row: dict[str, str],
    inventory_fetch: FetchResult,
) -> dict[str, object]:
    result = await scraper_mod.scrape(
        inventory_fetch.url,
        page=None,
        fetch_result=inventory_fetch,
        csv_row=row,
        property_id=str(pid),
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )
    qualified = strict_units(result)
    return {
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "strict_native_priced_rows": len(qualified),
        "errors": result.get("errors") or [],
    }


async def patched_full_pipeline(
    pid: int,
    row: dict[str, str],
) -> dict[str, object]:
    configured_url = normalize_url(row.get("website") or "")
    configured_task = task(configured_url, pid)
    configured_fetch = await direct_fetch(configured_task)
    result = await scraper_mod.scrape_jugnu(
        configured_task,
        configured_fetch,
        page=None,
        profile=None,
        csv_row=row,
    )
    qualified = strict_units(result)
    source_property_ids = sorted(
        {
            str(unit.get("source_property_id") or "")
            for unit in qualified
            if unit.get("source_property_id")
        }
    )
    return {
        "configured_status": configured_fetch.status,
        "configured_final_url": configured_fetch.final_url,
        "detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "strict_native_priced_rows": len(qualified),
        "source_property_ids": source_property_ids,
        "distinct_unit_numbers": len(
            {str(unit.get("unit_number") or "") for unit in qualified}
        ),
        "sample_units": [
            {
                "unit_number": str(unit.get("unit_number") or ""),
                "floor_plan_name": str(unit.get("floor_plan_name") or ""),
                "market_rent_low": unit.get("market_rent_low"),
                "market_rent_high": unit.get("market_rent_high"),
                "source_property_id": str(unit.get("source_property_id") or ""),
                "source_api_url": str(unit.get("source_api_url") or ""),
            }
            for unit in qualified[:5]
        ],
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": result.get("_link_hop_from") or "",
        "winning_page_url": result.get("_winning_page_url") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "errors": result.get("errors") or [],
    }


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
        actual = os.environ.get(name, "").lower()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    metadata = {
        int(row["apartmentid"]): row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    ledger_ids = {
        int(row["property_id"])
        for row in read_csv(LEDGER)
        if row.get("property_id")
    }

    inventory_capture: dict[int, tuple[FetchResult, str, list[str]]] = {}
    for pid, inventory_url in CANDIDATES.items():
        capture = await fetch_inventory_page(pid, inventory_url)
        if capture[0].status != 200 or MARKER not in capture[1].lower():
            raise RuntimeError(f"pid {pid}: OLLR marker absent from current inventory page")
        if len(capture[2]) != 1:
            raise RuntimeError(f"pid {pid}: expected one current SiteId, got {capture[2]}")
        inventory_capture[pid] = capture

    detector_source = Path(detector_mod.__file__).read_text().lower()
    if MARKER not in detector_source:
        raise RuntimeError("current detector source does not contain the OLLR marker delta")

    # Verify the current detector directly on each exact data-bearing page
    # before exercising the configured-page + link-hop orchestration.
    baseline = await asyncio.gather(
        *(
            baseline_inventory_page(pid, metadata[pid], inventory_capture[pid][0])
            for pid in CANDIDATES
        )
    )
    baseline_by_id = dict(zip(CANDIDATES, baseline, strict=True))

    # _try_link_hop imports ma_poc.fetch.fetch at call time. Replace only for
    # this local validation so the logical production orchestration runs over
    # direct public GETs without a browser, paid canary, or escalation backend.
    fetch_mod.fetch = direct_fetch

    semaphore = asyncio.Semaphore(3)

    async def bounded(pid: int) -> tuple[int, dict[str, object]]:
        async with semaphore:
            return pid, await patched_full_pipeline(pid, metadata[pid])

    patched_pairs = await asyncio.gather(*(bounded(pid) for pid in CANDIDATES))
    patched_by_id = dict(patched_pairs)

    results = []
    for pid, inventory_url in CANDIDATES.items():
        site_id = inventory_capture[pid][2][0]
        current = patched_by_id[pid]
        strict_count = int(current["strict_native_priced_rows"] or 0)
        source_ids = list(current["source_property_ids"] or [])
        bound = bool(strict_count > 0 and source_ids == [site_id])
        if not bound:
            raise RuntimeError(
                f"pid {pid}: strict binding failed count={strict_count} "
                f"source_ids={source_ids} site_id={site_id}"
            )
        results.append(
            sanitize(
                {
                    "property_id": pid,
                    "property_name": metadata[pid].get("name") or "",
                    "canonical_address": metadata[pid].get("address") or "",
                    "configured_url": normalize_url(metadata[pid].get("website") or ""),
                    "inventory_url": inventory_url,
                    "same_origin_inventory_route": host_key(inventory_url)
                    == host_key(normalize_url(metadata[pid].get("website") or "")),
                    "sole_current_published_site_id": site_id,
                    "current_detector_inventory_page": baseline_by_id[pid],
                    "current_source_full_pipeline": current,
                    "exact_source_property_binding": bound,
                    "outcome": "CURRENT_SOURCE_FULL_PIPELINE_UNIT_QUALIFIED",
                    "eligible_for_authoritative_ledger": True,
                }
            )
        )

    qualified_ids = [row["property_id"] for row in results]
    net_new_ids = sorted(set(qualified_ids) - ledger_ids)
    payload = sanitize(
        {
            "lane": "ollr_widgetloader_detector_current_source_validation",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "guardrails": {
                "production_source_modified_by_validation_lane": False,
                "shared_ledger_modified": False,
                "runtime_detector_monkeypatch": False,
                "runtime_fetch_monkeypatch": True,
                "llm_enabled": False,
                "captcha_solving": False,
                "web_unlocker": False,
                "flaresolverr": False,
                "hyperbrowser": False,
                "fingerprint_rotation": False,
                "paid_canary": False,
                "link_hop_test_fetch": "direct public curl_cffi GET, one fixed client",
            },
            "cohort": {
                "remaining_csv": str(REMAINING),
                "remaining_csv_sha256": sha256(REMAINING),
                "remaining_rows": len(read_csv(REMAINING)),
                "ledger_csv": str(LEDGER),
                "ledger_csv_sha256": sha256(LEDGER),
                "ledger_rows": len(ledger_ids),
                "fleet_marker_candidates": len(CANDIDATES),
            },
            "validated_detector_delta": {
                "marker": MARKER,
                "pms": "onesite",
                "confidence_without_knock": 0.94,
                "confidence_with_knock": 0.85,
                "reason": (
                    "The OLLR widgetLoader publishes the exact SiteId consumed by "
                    "OneSiteAdapter.workflowstartup; it must outrank generic "
                    "RealPage OLL and Entrata/Encore marketing signals."
                ),
            },
            "summary": {
                "current_source_full_pipeline_qualified_ids": sorted(qualified_ids),
                "current_source_full_pipeline_qualified_count": len(qualified_ids),
                "current_source_net_new_ids_vs_current_ledger": net_new_ids,
                "current_source_net_new_count_vs_current_ledger": len(net_new_ids),
                "total_strict_native_priced_rows": sum(
                    int(row["current_source_full_pipeline"]["strict_native_priced_rows"])
                    for row in results
                ),
                "authoritative_ledger_delta": 0,
            },
            "results": results,
        }
    )
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if re.search(r"ClientSessionID=(?!<redacted>)", serialized, re.IGNORECASE):
        raise RuntimeError("unsanitized ClientSessionID")
    if '"xyz"' in serialized.lower():
        raise RuntimeError("secret header name leaked")
    OUTPUT.write_text(serialized)
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "qualified_ids": sorted(qualified_ids),
                "net_new_ids": net_new_ids,
                "strict_rows": payload["summary"]["total_strict_native_priced_rows"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
