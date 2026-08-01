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
OUTPUT = ROOT / "encore_knock_lane/entrata_snippet_cluster_current_full.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_IDS = ("59649", "252116", "258789")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: object) -> str:
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
            "rent",
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
        body = str(response.text or "").encode()
        status = int(response.status_code or 0)
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


def expected_child_host(website: str) -> str:
    host = (urlsplit(website).hostname or "").casefold().removeprefix("www.")
    return f"entratasnipit.{host}"


async def run_one(row: dict[str, str]) -> dict[str, object]:
    property_id = row["apartmentid"]
    website = row["website"]
    task = CrawlTask(
        url=website,
        property_id=property_id,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    fetched = await direct_fetch(task)
    root_html = (fetched.body or b"").decode("utf-8", "replace")
    root_text = normalize(BeautifulSoup(root_html, "lxml").get_text(" ", strip=True))
    started = time.monotonic()
    try:
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
    except Exception as exc:  # noqa: BLE001
        return {
            "property_id": int(property_id),
            "property_name": row["name"],
            "website": website,
            "outcome": "ERROR",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    emitted = [item for item in result.get("units", []) if isinstance(item, dict)]
    strict = [
        item
        for item in emitted
        if unit_has_real_anchor(item)
        and positive_rent(item)
        and str(item.get("unit_number") or "").strip()
        and not str(item.get("unit_number") or "").startswith("ent-")
        and str((item.get("source_ids") or {}).get("entrata_uid") or "").strip()
    ]
    unit_numbers = [str(item["unit_number"]).strip() for item in strict]
    source_property_ids = {
        str(item.get("source_property_id") or "").strip() for item in strict
    }
    portal_urls = {
        str(item.get("source_portal_url") or "").strip() for item in strict
    }
    source_urls = {
        str(item.get("source_api_url") or "").strip() for item in strict
    }
    child_host = expected_child_host(website)

    def host_is_expected(url: str) -> bool:
        return (urlsplit(url).hostname or "").casefold() == child_host

    gates = {
        "configured_root_http_200": fetched.status == 200,
        "configured_root_name_visible": normalize(row["name"]) in root_text,
        "current_full_scraper_selected_entrata": result.get("_adapter_used")
        == "entrata",
        "current_full_scraper_selected_snippet_unit_tier": result.get(
            "extraction_tier_used"
        )
        == "TIER_1_DOM_ENTRATA_SNIPPET_UNIT_LEVEL",
        "all_emitted_rows_native_positive_and_priced": bool(strict)
        and len(strict) == len(emitted),
        "visible_unit_numbers_globally_unique": bool(unit_numbers)
        and len(unit_numbers) == len(set(unit_numbers)),
        "one_nonempty_source_property_id": len(source_property_ids) == 1
        and "" not in source_property_ids,
        "canonical_property_name_stamped_on_every_row": bool(strict)
        and all(item.get("source_property_name") == row["name"] for item in strict),
        "strict_property_owned_iframe_provenance_on_every_row": bool(strict)
        and all(
            item.get("source_property_provenance")
            == "exact_property_owned_entratasnippet_iframe"
            for item in strict
        ),
        "one_expected_property_owned_portal": len(portal_urls) == 1
        and all(host_is_expected(url) for url in portal_urls),
        "all_detail_sources_on_expected_property_child": bool(source_urls)
        and all(host_is_expected(url) for url in source_urls),
    }
    passed = all(gates.values())
    return {
        "property_id": int(property_id),
        "property_name": row["name"],
        "website": website,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "configured_status": fetched.status,
        "configured_final_url": fetched.final_url,
        "strict_native_positive_rows": len(strict),
        "all_emitted_rows": len(emitted),
        "distinct_visible_unit_numbers": len(set(unit_numbers)),
        "source_property_ids": sorted(source_property_ids),
        "portal_urls": sorted(portal_urls),
        "source_urls": sorted(source_urls),
        "strict_gates": gates,
        "contamination_verdict": (
            "pass_exact_property_owned_iframe_single_property_id_native_units"
            if passed
            else "reject_one_or_more_property_boundary_gates_failed"
        ),
        "native_samples": [
            {
                "unit_number": item.get("unit_number"),
                "entrata_uid": (item.get("source_ids") or {}).get("entrata_uid"),
                "floor_plan_name": item.get("floor_plan_name"),
                "rent_low": item.get("market_rent_low"),
                "availability_date": item.get("availability_date")
                or item.get("available_date"),
                "source_property_id": item.get("source_property_id"),
            }
            for item in strict[:5]
        ],
        "errors": result.get("errors") or [],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


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
        by_id = {row["apartmentid"]: row for row in csv.DictReader(handle)}
    rows = [by_id[property_id] for property_id in PROPERTY_IDS]
    fetch_mod.fetch = direct_fetch
    # Run serially: every property fans out across 8-12 detail pages on the
    # same Entrata infrastructure, so serial property probes are kinder and
    # make the evidence reproducible.
    results = []
    for row in rows:
        result = await run_one(row)
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)

    payload = {
        "lane": "entrata_snippet_three_property_current_full_configured_pipeline",
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
        "property_count": len(results),
        "qualified_property_ids": [
            result["property_id"]
            for result in results
            if result["outcome"] == "UNIT_QUALIFIED"
        ],
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "qualified_property_ids": payload["qualified_property_ids"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
