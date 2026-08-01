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
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get

ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "encore_knock_lane/evidence_westwood_betternoi_current_full.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = "42571"
EXPECTED_CLIENT = "01a0e491-f0fd-4d03-9529-00d881128a10"
EXPECTED_PORTAL = "https://westwoodvillageapthomes.com/en/floor-plans/"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
        )
    )


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
        body = str(response.text or "").encode()
        return FetchResult(
            url=task.url,
            outcome=(
                FetchOutcome.OK
                if 200 <= status < 300 and body
                else FetchOutcome.HARD_FAIL
            ),
            status=status,
            body=body,
            headers=dict(response.headers or {}),
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
            error_signature=f"{type(exc).__name__}: {str(exc)[:240]}",
        )


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "requests",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for key, expected in expected_env.items():
        actual = os.environ.get(key, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {key}={actual!r}; expected {expected!r}")

    with PROPERTIES.open(newline="", encoding="utf-8-sig") as handle:
        row = next(
            item for item in csv.DictReader(handle)
            if item["apartmentid"] == PROPERTY_ID
        )
    website = row["website"]
    task = CrawlTask(
        url=website,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    fetched = await direct_fetch(task)
    root_html = (fetched.body or b"").decode("utf-8", "replace")
    root_text = norm(BeautifulSoup(root_html, "lxml").get_text(" ", strip=True))
    fetch_mod.fetch = direct_fetch
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_mod.scrape_jugnu(
            task,
            fetched,
            page=None,
            profile=None,
            csv_row=row,
        ),
        timeout=180,
    )
    emitted = [item for item in result.get("units", []) if isinstance(item, dict)]
    strict = [
        item
        for item in emitted
        if unit_has_real_anchor(item)
        and positive_rent(item)
        and str(item.get("unit_number") or "").strip()
        and str((item.get("source_ids") or {}).get("betternoi_unit_uuid") or "").strip()
    ]
    unit_numbers = [str(item.get("unit_number") or "").strip() for item in strict]
    native_ids = [
        str((item.get("source_ids") or {}).get("betternoi_unit_uuid") or "").strip()
        for item in strict
    ]
    source_property_ids = {
        str(item.get("source_property_id") or "").strip() for item in strict
    }
    source_urls = {
        str(item.get("source_api_url") or "").strip() for item in strict
    }
    portal_urls = {
        str(item.get("source_portal_url") or "").strip() for item in strict
    }
    fallback_chain = [
        str(value)
        for value in (
            result.get("_fallback_chain")
            or result.get("fallback_chain")
            or []
        )
    ]
    gates = {
        "configured_root_http_200": fetched.status == 200,
        "configured_root_name_visible": all(
            token in set(root_text.split())
            for token in norm(row["name"]).split()
        ),
        "current_full_pipeline_betternoi_tier": result.get(
            "extraction_tier_used"
        )
        == "TIER_1_PUBLIC_BETTERNOI_API",
        "current_full_pipeline_link_hop_reached_inventory": (
            str(result.get("_winning_page_url") or "").rstrip("/")
            == EXPECTED_PORTAL.rstrip("/")
            and str(result.get("_link_hop_from") or "").rstrip("/")
            == website.rstrip("/")
        ),
        "native_recovery_winner_betternoi": any(
            value
            in {
                "page_published_native:betternoi_public",
                "universal_recovery:betternoi_public",
            }
            for value in fallback_chain
        ),
        "all_emitted_rows_native_and_positive_rent": bool(strict)
        and len(strict) == len(emitted),
        "unique_visible_unit_numbers": bool(unit_numbers)
        and len(unit_numbers) == len(set(unit_numbers)),
        "unique_native_unit_uuids": bool(native_ids)
        and len(native_ids) == len(set(native_ids)),
        "sole_exact_published_client": source_property_ids == {EXPECTED_CLIENT},
        "exact_property_page_provenance": bool(strict)
        and all(
            item.get("source_property_provenance")
            == "exact_property_page_published_betternoi_client"
            and item.get("source_property_name") == row["name"]
            for item in strict
        ),
        "exact_property_inventory_portal": portal_urls == {EXPECTED_PORTAL},
        "all_sources_exact_public_betternoi_unit_api": bool(source_urls)
        and all(
            (urlsplit(url).hostname or "").casefold() == "ares.betternoi.com"
            and urlsplit(url).path.rstrip("/")
            == "/api/pub/v1/client/building/unit"
            for url in source_urls
        ),
    }
    passed = all(gates.values())
    evidence_row = {
        "property_id": int(PROPERTY_ID),
        "property_name": row["name"],
        "website": website,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "property_identity_match": passed,
        "contamination_verdict": (
            "pass_exact_property_page_single_betternoi_client_native_units"
            if passed
            else "reject_full_pipeline_or_property_boundary_gate_failed"
        ),
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(strict),
        "strict_gates": gates,
        "identity_evidence": {
            "rows_with_native_identity": len(strict),
            "rows_with_native_identity_and_positive_rent": len(strict),
            "source_property_ids": sorted(source_property_ids),
            "source_urls": sorted(source_urls),
            "portal_urls": sorted(portal_urls),
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": item.get("unit_number") or "",
                    "betternoi_unit_uuid": (
                        item.get("source_ids") or {}
                    ).get("betternoi_unit_uuid") or "",
                },
                "positive_rent_evidence": {
                    "market_rent_low": item.get("market_rent_low")
                },
                "floor_plan_name": item.get("floor_plan_name") or "",
                "availability_date": item.get("availability_date")
                or item.get("available_date")
                or "",
                "source_property_id": item.get("source_property_id") or "",
                "source_api_url": item.get("source_api_url") or "",
            }
            for item in strict[:5]
        ],
        "configured_final_url": fetched.final_url,
        "winning_page_url": result.get("_winning_page_url") or "",
        "link_hop_from": result.get("_link_hop_from") or "",
        "fallback_chain": fallback_chain,
        "errors": result.get("errors") or [],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    payload = {
        "lane": "westwood_betternoi_current_full_configured_pipeline",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "hyperbrowser": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
        },
        "results": [evidence_row],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence_row, indent=2, sort_keys=True))
    print(json.dumps({"artifact": str(OUTPUT), "sha256": sha256(OUTPUT)}))


if __name__ == "__main__":
    asyncio.run(main())
