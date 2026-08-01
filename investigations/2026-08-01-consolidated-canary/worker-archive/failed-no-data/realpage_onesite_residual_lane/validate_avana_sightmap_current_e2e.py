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
from ma_poc.pms.adapters.sightmap import (
    extract_sightmap_api_url,
    find_sightmap_embed_codes,
    parse_sightmap_payload,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_avana_sightmap_current_e2e.json"
PROPERTY_ID = "3169"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def name_key(value: object) -> str:
    ignored = {"apartment", "apartments", "community", "the"}
    return "".join(token for token in normalize(value).split() if token not in ignored)


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
        raise RuntimeError(f"{PROPERTY_ID} is already in the captured ledger")

    configured_url = canonical["website"]
    configured_task = make_task(configured_url)
    configured_fetch = await direct_fetch(configured_task)
    configured_body = (configured_fetch.body or b"").decode("utf-8", "replace")
    configured_soup = BeautifulSoup(configured_body, "lxml")
    configured_text = normalize(configured_soup.get_text(" ", strip=True))
    configured_host = (urlparse(configured_fetch.final_url).hostname or "").removeprefix(
        "www."
    )
    floor_plan_links = sorted(
        {
            urljoin(configured_fetch.final_url, str(anchor.get("href") or ""))
            for anchor in configured_soup.find_all("a", href=True)
            if "/conventional/" in str(anchor.get("href") or "")
        }
    )
    same_origin_floor_plan_links = [
        url
        for url in floor_plan_links
        if (urlparse(url).hostname or "").removeprefix("www.") == configured_host
    ]
    if len(same_origin_floor_plan_links) != 1:
        raise RuntimeError(
            f"Expected one same-origin conventional link, got {same_origin_floor_plan_links}"
        )
    floor_plan_url = same_origin_floor_plan_links[0]
    floor_plan_fetch = await direct_fetch(make_task(floor_plan_url))
    floor_plan_body = (floor_plan_fetch.body or b"").decode("utf-8", "replace")
    embed_codes = find_sightmap_embed_codes(floor_plan_body)
    if len(embed_codes) != 1:
        raise RuntimeError(f"Expected one SightMap embed, got {embed_codes}")
    embed_url = f"https://sightmap.com/embed/{embed_codes[0]}"
    embed_fetch = await direct_fetch(make_task(embed_url))
    embed_body = (embed_fetch.body or b"").decode("utf-8", "replace")
    api_url = extract_sightmap_api_url(embed_body)
    if not api_url:
        raise RuntimeError("SightMap embed did not publish an API URL")
    api_fetch = await direct_fetch(make_task(api_url))
    api_payload = json.loads((api_fetch.body or b"").decode("utf-8", "replace"))
    api_data = api_payload.get("data") if isinstance(api_payload, dict) else {}
    api_data = api_data if isinstance(api_data, dict) else {}
    asset = api_data.get("asset") if isinstance(api_data.get("asset"), dict) else {}
    asset_name = str(asset.get("name") or "")
    raw_units, parser_drops = parse_sightmap_payload(api_payload, api_url)

    # scrape_jugnu imports this symbol for configured-page link-hop. This is
    # the only runtime substitution: a direct public GET avoids paid fetchers
    # while preserving current source detection, ranking, adapters, and gates.
    fetch_mod.fetch = direct_fetch
    result = await scraper_mod.scrape_jugnu(
        configured_task,
        configured_fetch,
        page=None,
        profile=None,
        csv_row=canonical,
    )
    emitted = [item for item in result.get("units") or [] if isinstance(item, dict)]
    qualified = [
        item
        for item in emitted
        if unit_has_real_anchor(item) and positive_rent(item)
    ]
    raw_native_ids = {
        str((item.get("source_ids") or {}).get("sightmap_unit_id") or "")
        for item in raw_units
        if isinstance(item, dict)
        and str((item.get("source_ids") or {}).get("sightmap_unit_id") or "")
    }
    output_native_ids = {
        str((item.get("source_ids") or {}).get("sightmap_unit_id") or "")
        for item in qualified
    }
    address_tokens = normalize(canonical["address"]).split()
    address_core = [
        token
        for token in address_tokens
        if token not in {"dr", "drive", "st", "street", "rd", "road", "ave", "avenue"}
    ]
    configured_tokens = set(configured_text.split())
    gates = {
        "configured_http_200": configured_fetch.status == 200,
        "configured_name_visible": normalize(canonical["name"]) in configured_text,
        "configured_address_visible": all(token in configured_tokens for token in address_core),
        "configured_city_visible": normalize(canonical["city"]) in configured_text,
        "configured_state_visible": normalize(canonical["state"]) in configured_text,
        "configured_zip_visible": normalize(canonical["zip"]) in configured_text,
        "single_same_origin_floor_plan_link": len(same_origin_floor_plan_links) == 1,
        "floor_plan_page_http_200": floor_plan_fetch.status == 200,
        "single_sightmap_embed": len(embed_codes) == 1,
        "embed_http_200": embed_fetch.status == 200,
        "embed_publishes_api_url": bool(api_url),
        "api_http_200": api_fetch.status == 200,
        "api_asset_name_exact": name_key(asset_name) == name_key(canonical["name"]),
        "api_sightmap_id_matches_url": str(api_data.get("id") or "") == "75290",
        "zero_parser_join_drops": parser_drops == 0,
        "current_pipeline_adapter_sightmap": result.get("_adapter_used") == "sightmap",
        "current_pipeline_tier_sightmap": result.get("extraction_tier_used")
        == "TIER_1_API_SIGHTMAP_IFRAME",
        "current_pipeline_link_hop_success": result.get("_link_hop_success") is True,
        "current_pipeline_winning_url_exact_api": str(
            result.get("_winning_page_url") or ""
        ).rstrip("/")
        == api_url.rstrip("/"),
        "all_emitted_rows_strict_native_positive": len(qualified) == len(emitted) > 0,
        "all_output_native_ids_present": bool(output_native_ids)
        and "" not in output_native_ids,
        "all_output_native_ids_in_api": output_native_ids <= raw_native_ids,
        "all_output_source_urls_exact_api": all(
            str(item.get("source_api_url") or "").rstrip("/") == api_url.rstrip("/")
            for item in qualified
        ),
        "all_output_unit_numbers_present": all(
            str(item.get("unit_number") or "").strip() for item in qualified
        ),
    }
    strict_pass = all(gates.values())
    if not strict_pass:
        raise RuntimeError(
            "Avana strict gates failed: "
            + json.dumps({key: value for key, value in gates.items() if not value})
        )

    recovery = {
        "property_id": int(PROPERTY_ID),
        "property_name": canonical["name"],
        "website": canonical["website"],
        "strict_verdict": "pass_exact_configured_same_origin_sightmap_current_e2e",
        "native_identity_rows": len(qualified),
        "native_positive_rent_rows": len(qualified),
        "source_urls": [configured_url, floor_plan_url, embed_url, api_url],
        "property_boundary_evidence": {
            "canonical_address": canonical["address"],
            "canonical_city": canonical["city"],
            "canonical_state": canonical["state"],
            "canonical_zip": canonical["zip"],
            "configured_final_url": configured_fetch.final_url,
            "sightmap_asset_name": asset_name,
            "sightmap_asset_id": str(asset.get("id") or ""),
            "sightmap_id": str(api_data.get("id") or ""),
            "embed_code": embed_codes[0],
            "raw_api_rows": len(raw_units),
            "current_pipeline_emitted_rows": len(emitted),
            "gates": gates,
        },
        "current_full_pipeline": {
            "detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
            "link_hop_success": bool(result.get("_link_hop_success")),
            "link_hop_from": result.get("_link_hop_from") or "",
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
        "batch_label": "avana-sightmap-current-source-configured-e2e",
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
