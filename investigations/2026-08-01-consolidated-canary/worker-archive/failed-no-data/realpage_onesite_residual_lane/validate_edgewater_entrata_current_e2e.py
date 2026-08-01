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
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_edgewater_entrata_current_e2e.json"
PROPERTY_ID = "48092"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def name_key(value: object) -> str:
    ignored = {"apartment", "apartments", "community", "the", "at"}
    return "".join(token for token in normalize(value).split() if token not in ignored)


def street_matches(canonical: object, source: object) -> bool:
    ignored = {
        "n", "s", "e", "w", "ne", "nw", "se", "sw",
        "north", "south", "east", "west", "st", "street",
        "rd", "road", "ave", "avenue", "dr", "drive",
        "cir", "circle", "blvd", "boulevard", "ln", "lane",
    }
    canonical_tokens = normalize(canonical).split()
    source_tokens = set(normalize(source).split())
    if not canonical_tokens:
        return False
    number = canonical_tokens[0]
    core = {
        token
        for token in canonical_tokens[1:]
        if token not in ignored and len(token) > 1
    }
    return bool(number in source_tokens and core and core <= source_tokens)


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
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
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        return FetchResult(
            url=task.url,
            outcome=(
                FetchOutcome.OK
                if 200 <= status < 300 and body
                else FetchOutcome.HARD_FAIL
            ),
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
            error_signature=f"{type(exc).__name__}: {str(exc)[:160]}",
        )


def make_task(url: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=45_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


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

    canonical = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    ledger_ids = {row["property_id"] for row in read_csv(LEDGER)}
    if PROPERTY_ID in ledger_ids:
        raise RuntimeError(f"{PROPERTY_ID} already exists in captured ledger")

    task = make_task(canonical["website"])
    configured_fetch = await direct_fetch(task)
    body = (configured_fetch.body or b"").decode("utf-8", "replace")
    soup = BeautifulSoup(body, "lxml")
    visible = normalize(soup.get_text(" ", strip=True))
    form_routes = sorted(
        {
            urljoin(configured_fetch.final_url, str(tag.get(attribute) or ""))
            for tag in soup.find_all(["a", "form"], href=True)
            for attribute in ("href",)
            if "/greensboro/edgewater-village/conventional/"
            in str(tag.get(attribute) or "").lower()
        }
        | {
            urljoin(configured_fetch.final_url, str(tag.get("action") or ""))
            for tag in soup.find_all("form", action=True)
            if "/greensboro/edgewater-village/conventional/"
            in str(tag.get("action") or "").lower()
        }
    )

    fetch_mod.fetch = direct_fetch
    result = await scraper_mod.scrape_jugnu(
        task,
        configured_fetch,
        page=None,
        profile=None,
        csv_row=canonical,
    )
    emitted = [item for item in result.get("units") or [] if isinstance(item, dict)]
    qualified = [
        item for item in emitted if unit_has_real_anchor(item) and positive_rent(item)
    ]
    source_urls = sorted(
        {str(item.get("source_api_url") or "") for item in qualified}
    )
    source_fetches = await asyncio.gather(
        *(direct_fetch(make_task(url)) for url in source_urls)
    )
    source_bodies = {
        source_url: (capture.body or b"").decode("utf-8", "replace")
        for source_url, capture in zip(source_urls, source_fetches, strict=True)
    }
    output_native_ids = {
        str((item.get("source_ids") or {}).get("entrata_uid") or "")
        for item in qualified
    }
    output_floorplan_ids = {
        str((item.get("source_ids") or {}).get("entrata_fpid") or "")
        for item in qualified
    }
    every_row_replayed = all(
        str((item.get("source_ids") or {}).get("entrata_uid") or "")
        in source_bodies.get(str(item.get("source_api_url") or ""), "")
        and str((item.get("source_ids") or {}).get("entrata_fpid") or "")
        in source_bodies.get(str(item.get("source_api_url") or ""), "")
        and str(item.get("unit_number") or "")
        in source_bodies.get(str(item.get("source_api_url") or ""), "")
        for item in qualified
    )
    final_host = (urlparse(configured_fetch.final_url).hostname or "").lower()
    gates = {
        "configured_http_200": configured_fetch.status == 200,
        "configured_redirects_to_exact_edgewater_host": final_host
        == "www.edgewatervillage-apts.com",
        "configured_name_visible": name_key(canonical["name"]) in name_key(visible),
        "configured_street_exact_normalized": street_matches(
            canonical["address"], visible
        ),
        "configured_city_visible": normalize(canonical["city"]) in visible,
        "configured_state_visible": normalize(canonical["state"]) in visible,
        "configured_zip_visible": normalize(canonical["zip"]) in visible,
        "configured_publishes_exact_conventional_route": len(form_routes) == 1,
        "current_pipeline_adapter_entrata": result.get("_adapter_used") == "entrata",
        "current_pipeline_tier_unit_level": result.get("extraction_tier_used")
        == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
        "current_pipeline_no_errors": not (result.get("errors") or []),
        "all_emitted_rows_strict_native_positive": len(qualified) == len(emitted) > 0,
        "all_output_native_ids_present": bool(output_native_ids)
        and "" not in output_native_ids,
        "all_output_floorplan_ids_present": bool(output_floorplan_ids)
        and "" not in output_floorplan_ids,
        "all_output_unit_numbers_present": all(
            str(item.get("unit_number") or "").strip() for item in qualified
        ),
        "all_source_urls_exact_same_origin_property_slug": all(
            (urlparse(url).hostname or "").lower() == final_host
            and "/floorplans/greensboro-nc/edgewater-village/" in url.lower()
            for url in source_urls
        ),
        "all_source_pages_http_200": all(
            capture.status == 200 for capture in source_fetches
        ),
        "every_output_native_id_floorplan_id_and_unit_on_source_page": every_row_replayed,
    }
    if not all(gates.values()):
        raise RuntimeError(
            "Edgewater strict gates failed: "
            + json.dumps({key: value for key, value in gates.items() if not value})
        )

    recovery = {
        "property_id": int(PROPERTY_ID),
        "property_name": canonical["name"],
        "website": canonical["website"],
        "strict_verdict": "pass_exact_configured_redirect_entrata_native_unit_pages",
        "native_identity_rows": len(qualified),
        "native_positive_rent_rows": len(qualified),
        "source_urls": [canonical["website"], configured_fetch.final_url, *source_urls],
        "property_boundary_evidence": {
            "canonical_address": canonical["address"],
            "canonical_city": canonical["city"],
            "canonical_state": canonical["state"],
            "canonical_zip": canonical["zip"],
            "configured_final_url": configured_fetch.final_url,
            "published_conventional_routes": form_routes,
            "source_page_statuses": {
                url: capture.status
                for url, capture in zip(source_urls, source_fetches, strict=True)
            },
            "gates": gates,
        },
        "current_full_pipeline": {
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
            "winning_page_url": result.get("_winning_page_url") or "",
            "strict_native_positive_rent_rows": len(qualified),
            "errors": result.get("errors") or [],
        },
        "units": [
            {
                "unit_number": str(item.get("unit_number") or ""),
                "floor_plan_name": str(item.get("floor_plan_name") or ""),
                "rent": item.get("market_rent_low"),
                "market_rent_high": item.get("market_rent_high"),
                "availability_date": str(item.get("availability_date") or ""),
                "source_url": str(item.get("source_api_url") or ""),
                "source_ids": item.get("source_ids") or {},
            }
            for item in qualified
        ],
    }
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_label": "edgewater-entrata-current-source-configured-e2e",
        "ledger_snapshot": {
            "path": str(LEDGER),
            "sha256": sha256(LEDGER),
            "rows": len(read_csv(LEDGER)),
            "net_new_ids": [int(PROPERTY_ID)],
        },
        "guardrails": {
            "llm_enabled": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "production_source_modified_by_lane": False,
            "shared_builder_modified": False,
            "shared_ledger_modified": False,
        },
        "recoveries": [recovery],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "ledger_rows": payload["ledger_snapshot"]["rows"],
                "net_new_ids": payload["ledger_snapshot"]["net_new_ids"],
                "strict_native_positive_rent_rows": len(qualified),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
