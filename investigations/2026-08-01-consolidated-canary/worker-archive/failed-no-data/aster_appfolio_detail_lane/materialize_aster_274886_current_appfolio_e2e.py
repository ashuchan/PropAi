from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bs4 import BeautifulSoup
from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.appfolio import parse_appfolio_detail_page
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/aster_appfolio_detail_lane")
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
EVIDENCE = ROOT / "evidence_aster_274886_current_appfolio_e2e.json"
PID = "274886"
CONFIGURED_URL = "https://parkplace380.com/listings/aster-village-two/"
SHOWME_URL = (
    "https://www.showmetherent.com/listing/details/"
    "1251-Aster-Drive-Tiffin-IA-52340/"
    "4de2d364-61bc-11eb-abab-0efd77e47219"
)
UID = "13c06338-c373-42ae-a236-8687d24c30ad"
DETAIL_URL = f"https://thedersgrp.appfolio.com/listings/detail/{UID}"
APPLICATION_URL_PREFIX = (
    "https://thedersgrp.appfolio.com/listings/rental_applications/new?"
    f"listable_uid={UID}"
)
SOURCE_FILES = (
    "ma_poc/core/identity.py",
    "ma_poc/pms/adapters/appfolio.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/scraper.py",
)
CLUSTER_CONTROLS = (
    (
        "cross",
        "https://cross.appfolio.com/listings/detail/"
        "501d3938-dce7-4938-b43b-55bed06c2dd2",
        ("3", "2", "1180", 1375, "2026-08-10"),
    ),
    (
        "terracemgmt",
        "https://terracemgmt.appfolio.com/listings/detail/"
        "4b0339fb-749f-4823-b602-d83fcf8b2adf",
        ("2", "2", "934", 1150, ""),
    ),
    (
        "equilibrium",
        "https://equilibriumprops.appfolio.com/listings/detail/"
        "9424e2e3-c0f9-46e4-82cd-6f284b64ea62",
        ("4", "1.5", "1400", 1695, ""),
    ),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict[str, str]:
    return {path: sha((REPO / path).read_bytes()) for path in SOURCE_FILES}


def get(url: str):
    return requests.get(
        url,
        timeout=45,
        impersonate="chrome",
        allow_redirects=True,
    )


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def fetch_result(response) -> FetchResult:
    return FetchResult(
        url=DETAIL_URL,
        outcome=FetchOutcome.OK,
        status=response.status_code,
        body=response.content,
        headers=dict(response.headers),
        render_mode=RenderMode.GET,
        final_url=str(response.url),
        attempts=1,
        elapsed_ms=0,
    )


async def pipeline_repeat(
    repeat: int,
    metadata: dict[str, str],
    fetched: FetchResult,
) -> dict[str, Any]:
    task = CrawlTask(
        url=DETAIL_URL,
        property_id=PID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )

    async def unexpected_link_hop(_task):
        raise AssertionError("exact AppFolio detail must not need a link hop")

    with patch("ma_poc.fetch.fetch", unexpected_link_hop, create=True):
        result = await asyncio.wait_for(
            scrape_jugnu(task, fetched, csv_row=metadata),
            timeout=120,
        )
    emitted = [
        row for row in (result.get("units") or []) if isinstance(row, dict)
    ]
    strict = [
        row
        for row in emitted
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    if len(emitted) != len(strict) or len(strict) != 1:
        raise AssertionError(
            f"repeat {repeat}: emitted={len(emitted)} strict={len(strict)}"
        )
    row = strict[0]
    checks = {
        "appfolio_detected": (
            (result.get("_detected_pms") or {}).get("pms") == "appfolio"
        ),
        "appfolio_adapter": result.get("_adapter_used") == "appfolio",
        "detail_tier": (
            result.get("extraction_tier_used")
            == "TIER_1_DOM_APPFOLIO_DETAIL"
        ),
        "native_unit_108": row.get("unit_number") == "108",
        "native_uid": (
            (row.get("source_ids") or {}).get("appfolio_listable_uid")
            == UID
        ),
        "exact_address": (
            "1251asterdrive108" in norm(str(row.get("unit_name") or ""))
            and "tiffin" in norm(str(row.get("unit_name") or ""))
            and "52340" in norm(str(row.get("unit_name") or ""))
        ),
        "physical_fields": (
            row.get("bedrooms") == "1"
            and row.get("bathrooms") == "1"
            and row.get("sqft") == "821"
        ),
        "positive_rent": row.get("market_rent_low") == 1365,
        "operator_date": row.get("availability_date") == "2026-10-10",
        "exact_source": row.get("source_api_url") == DETAIL_URL,
        "no_pipeline_errors": not (result.get("errors") or []),
    }
    if not all(checks.values()):
        raise AssertionError(f"repeat {repeat}: {checks!r}; row={row!r}")
    compact_keys = (
        "unit_number",
        "unit_id",
        "unit_name",
        "floor_plan_name",
        "bedrooms",
        "bathrooms",
        "sqft",
        "market_rent_low",
        "market_rent_high",
        "availability_status",
        "availability_date",
        "source_api_url",
        "source_ids",
    )
    return {
        "repeat": repeat,
        "checks": checks,
        "unit": {key: row.get(key) for key in compact_keys},
        "errors": result.get("errors") or [],
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_DC_PROXY_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
        "ENABLE_RESIDENTIAL_TIER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER5_VISION": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "PROBE_PROXY_URL": "",
        "WEB_UNLOCKER_KEY": "",
    }
    actual_env = {key: os.environ.get(key, "") for key in expected_env}
    if actual_env != expected_env:
        raise SystemExit(f"guardrail environment mismatch: {actual_env!r}")

    with (REPO / "ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        metadata = next(
            row for row in csv.DictReader(handle) if row["apartmentid"] == PID
        )

    before = source_hashes()
    configured = get(CONFIGURED_URL)
    showme = get(SHOWME_URL)
    detail = get(DETAIL_URL)
    if any(r.status_code != 200 for r in (configured, showme, detail)):
        raise SystemExit(
            "live route failure: "
            f"configured={configured.status_code} "
            f"showme={showme.status_code} detail={detail.status_code}"
        )

    configured_text = BeautifulSoup(
        configured.text, "html.parser"
    ).get_text(" ", strip=True)
    showme_soup = BeautifulSoup(showme.text, "html.parser")
    showme_text = showme_soup.get_text(" ", strip=True)
    application_links = sorted(
        {
            str(anchor.get("href") or "")
            for anchor in showme_soup.select("a[href]")
            if str(anchor.get("href") or "").startswith(
                APPLICATION_URL_PREFIX
            )
        }
    )
    if len(application_links) != 1:
        raise SystemExit(f"exact application link missing: {application_links!r}")

    detail_soup = BeautifulSoup(detail.text, "html.parser")
    detail_title = (
        detail_soup.select_one("h1.js-show-title").get_text(" ", strip=True)
        if detail_soup.select_one("h1.js-show-title")
        else ""
    )
    published_listing_title = (
        detail_soup.select_one("h2.listing-detail__title").get_text(
            " ", strip=True
        )
        if detail_soup.select_one("h2.listing-detail__title")
        else ""
    )
    identity_checks = {
        "configured_exact_property_name": (
            norm("Aster Village Two") in norm(configured_text)
        ),
        "configured_exact_manager": "thedersgroup" in norm(configured_text),
        "syndication_exact_name_address": (
            norm("Aster Village Two") in norm(showme_text)
            and norm("1251 Aster Drive") in norm(showme_text)
            and norm("Tiffin IA 52340") in norm(showme_text)
        ),
        "syndication_one_available_unit": (
            "has 1 unit available" in showme.text
        ),
        "syndication_exact_application_uid": (
            application_links[0].startswith(APPLICATION_URL_PREFIX)
        ),
        "official_detail_exact_address": (
            norm("1251 Aster Drive") in norm(detail_title)
            and norm("Tiffin IA 52340") in norm(detail_title)
        ),
    }
    if not all(identity_checks.values()):
        raise SystemExit(f"identity chain failed: {identity_checks!r}")

    fetched = fetch_result(detail)
    repeats = [
        await pipeline_repeat(repeat, metadata, fetched)
        for repeat in range(1, 4)
    ]
    if not all(row["unit"] == repeats[0]["unit"] for row in repeats):
        raise SystemExit("pipeline output drift across repeats")

    cluster_controls: list[dict[str, Any]] = []
    for label, url, expected in CLUSTER_CONTROLS:
        response = get(url)
        rows = parse_appfolio_detail_page(response.text, url)
        row = rows[0] if rows else {}
        observed = (
            row.get("bedrooms"),
            row.get("bathrooms"),
            row.get("sqft"),
            row.get("market_rent_low"),
            row.get("availability_date"),
        )
        if response.status_code != 200 or observed != expected:
            raise SystemExit(
                f"cluster control {label} failed: status={response.status_code} "
                f"observed={observed!r} expected={expected!r}"
            )
        cluster_controls.append(
            {
                "label": label,
                "url": url,
                "status": response.status_code,
                "body_bytes": len(response.content),
                "observed": list(observed),
            }
        )

    after = source_hashes()
    if before != after:
        raise SystemExit("production sources changed during evidence run")
    script = Path(__file__).resolve()
    strict_result = {
        "property_id": int(PID),
        "property_name": metadata["name"],
        "website": metadata["website"],
        "outcome": "UNIT_QUALIFIED",
        "adapter": "appfolio",
        "tier": "TIER_1_DOM_APPFOLIO_DETAIL",
        "units": 1,
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_configured_exact_name_manager_to_appfolio_owned_"
            "syndication_exact_name_address_application_uid_to_official_"
            "detail_exact_address_native_unit_positive_rent_three_repeats"
        ),
        "identity_evidence": {
            "checks": identity_checks,
            "native_identity_rows": 1,
            "native_positive_rent_rows": 1,
            "source_urls": [DETAIL_URL],
        },
        "native_samples": [repeats[0]["unit"]],
    }
    evidence = {
        "lane": "aster_current_appfolio_detail_parser_e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "exact_2026-07-31_FAILED_NO_DATA_344",
        "ledger_mutation": "none",
        "commit": "none",
        "push": "none",
        "paid_canary": False,
        "guardrails": {
            "environment": actual_env,
            "direct_public_http_only": True,
            "hyperbrowser_calls": 0,
            "llm_calls": 0,
            "proxy_calls": 0,
            "web_unlocker_calls": 0,
            "flaresolverr_calls": 0,
            "captcha_solving": False,
            "fingerprint_rotation": False,
        },
        "configured_identity": {
            key: metadata[key]
            for key in (
                "apartmentid",
                "name",
                "address",
                "city",
                "state",
                "zip",
                "website",
            )
        },
        "identity_chain": {
            "checks": identity_checks,
            "configured_url": CONFIGURED_URL,
            "appfolio_owned_syndication_url": SHOWME_URL,
            "exact_application_url": application_links[0],
            "official_detail_url": DETAIL_URL,
            "official_detail_title": detail_title,
            "published_listing_title": published_listing_title,
            "floor_plan_name_policy": (
                "blank: AppFolio h2 is marketing_title, not proven "
                "unit_template_name"
            ),
        },
        "http_evidence": {
            "configured": {
                "status": configured.status_code,
                "body_bytes": len(configured.content),
                "body_sha256": sha(configured.content),
            },
            "appfolio_owned_syndication": {
                "status": showme.status_code,
                "body_bytes": len(showme.content),
                "body_sha256": sha(showme.content),
            },
            "official_detail": {
                "status": detail.status_code,
                "body_bytes": len(detail.content),
                "body_sha256": sha(detail.content),
            },
        },
        "three_full_pipeline_repeats": repeats,
        "current_template_cluster_controls": cluster_controls,
        "source_snapshot_before": before,
        "source_snapshot_after": after,
        "materializer": {
            "path": str(script),
            "sha256": sha(script.read_bytes()),
        },
        "results": [strict_result],
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "evidence": str(EVIDENCE),
                "evidence_sha256": sha(EVIDENCE.read_bytes()),
                "materializer": str(script),
                "materializer_sha256": sha(script.read_bytes()),
                "source_hashes": after,
                "strict_units_each_repeat": [1, 1, 1],
                "unit_number": "108",
                "availability_date": "2026-10-10",
                "cluster_controls": len(cluster_controls),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
