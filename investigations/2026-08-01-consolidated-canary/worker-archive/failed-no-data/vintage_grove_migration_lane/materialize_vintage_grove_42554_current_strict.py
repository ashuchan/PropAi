from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "vintage_grove_migration_lane"
COHORT = ROOT / "failed344.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_vintage_grove_42554_current_strict.json"
ROOT_GZIP = LANE / "42554_vintage_grove_current_root.html.gz"
MATERIALIZER = Path(__file__)

PROPERTY_ID = "42554"
CONFIGURED_URL = "http://summerwindaptsfl.com/"
CURRENT_URL = "https://vintagegroveapts.com/"
PUBLISHED_PORTAL = (
    "https://vintagegroveapts.securecafe.com/onlineleasing/"
    "summerwind-apartments0/floorplans.aspx"
)
SOURCE_PROPERTY_ID = "1722315"
SOURCE_API_URL = (
    "https://vintagegroveapts.securecafeapplicant.com/onlineleasing/api/"
    "floorplan/getfloorplanandavailableunits?propertyId=1722315&"
    "RequestBeforeLogin=true&isPropertyList=false"
)
REPO_ROOT = Path("/Users/ankur/PropAi-codex-failed-no-data")
SOURCE_FILES = (
    "ma_poc/core/identity.py",
    "ma_poc/pms/adapters/rentcafe.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/scraper.py",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_snapshot() -> dict[str, str]:
    return {
        relative: sha256_bytes((REPO_ROOT / relative).read_bytes())
        for relative in SOURCE_FILES
    }


def read_property() -> dict[str, str]:
    with PROPERTIES.open(newline="", encoding="utf-8-sig") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") == PROPERTY_ID
        ]
    if len(rows) != 1:
        raise RuntimeError(f"expected one config row for {PROPERTY_ID}, got {len(rows)}")
    return rows[0]


def assert_exact_cohort_member() -> None:
    with COHORT.open(newline="", encoding="utf-8-sig") as handle:
        rows = [
            row for row in csv.DictReader(handle) if row.get("property_id") == PROPERTY_ID
        ]
    if len(rows) != 1 or rows[0].get("website") != CONFIGURED_URL:
        raise RuntimeError("PID 42554 is not the exact configured FAILED_NO_DATA member")


def positive_rent(unit: dict[str, Any]) -> bool:
    return all(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and float(unit[field]) > 0
        for field in ("market_rent_low", "market_rent_high")
    )


def normalized_visible(html: str) -> str:
    return " ".join(
        BeautifulSoup(html, "lxml").get_text(" ", strip=True).casefold().split()
    )


def canonical_csv_row(row: dict[str, str]) -> dict[str, str]:
    return {
        **row,
        "apartmentid": PROPERTY_ID,
        "name": "Vintage Grove Apartments",
        "address": "5262 Timuquana Rd",
        "city": "Jacksonville",
        "state": "FL",
        "zip": "32210",
        "website": CURRENT_URL,
    }


def make_fetch_result(body: bytes) -> FetchResult:
    return FetchResult(
        url=CONFIGURED_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.GET,
        final_url=CURRENT_URL,
        attempts=1,
        elapsed_ms=0,
    )


def make_task() -> CrawlTask:
    return CrawlTask(
        url=CONFIGURED_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def summarize_unit(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_number": str(unit.get("unit_number") or "").strip(),
        "floor_plan_name": str(unit.get("floor_plan_name") or "").strip(),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "availability_date": str(unit.get("availability_date") or "").strip(),
        "source_api_url": str(unit.get("source_api_url") or "").strip(),
        "source_property_id": str(unit.get("source_property_id") or "").strip(),
        "source_property_name": str(unit.get("source_property_name") or "").strip(),
        "source_property_address": str(
            unit.get("source_property_address") or ""
        ).strip(),
        "source_ids": unit.get("source_ids") or {},
    }


async def full_pipeline_repeat(
    repeat: int,
    root_body: bytes,
    row: dict[str, str],
) -> dict[str, Any]:
    result = await asyncio.wait_for(
        scrape_jugnu(
            make_task(),
            make_fetch_result(root_body),
            page=None,
            profile=None,
            csv_row=canonical_csv_row(row),
        ),
        timeout=150,
    )
    units = [item for item in (result.get("units") or []) if isinstance(item, dict)]
    strict = [
        item
        for item in units
        if unit_has_real_anchor(item)
        and positive_rent(item)
        and str(item.get("unit_number") or "").strip()
        and str((item.get("source_ids") or {}).get("securecafe_apartment_id") or "").strip()
    ]
    return {
        "repeat": repeat,
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "errors": result.get("errors") or [],
        "emitted_rows": len(units),
        "strict_native_positive_rent_rows": len(strict),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "units": [summarize_unit(item) for item in strict],
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "requests",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_DC_PROXY_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
        "ENABLE_RESIDENTIAL_TIER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER5_VISION": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {name}={actual!r}; expected {expected!r}")

    assert_exact_cohort_member()
    row = read_property()
    if row != {
        "apartmentid": PROPERTY_ID,
        "name": "Vintage Grove Apartments",
        "address": "5262 Timuquana Rd",
        "city": "Jacksonville",
        "state": "FL",
        "zip": "32210",
        "website": CONFIGURED_URL,
    }:
        raise RuntimeError(f"unexpected configured identity: {row!r}")

    before = source_snapshot()
    configured_probe: dict[str, Any]
    try:
        configured_response = await asyncio.to_thread(
            probe_get,
            CONFIGURED_URL,
            timeout=20,
            unlocker=False,
            retries=0,
        )
        configured_text = str(configured_response.text or "")
        configured_probe = {
            "requested_url": CONFIGURED_URL,
            "final_url": str(configured_response.url or CONFIGURED_URL),
            "status": int(configured_response.status_code or 0),
            "body_bytes": len(configured_text.encode()),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - evidence records the exact failure
        configured_probe = {
            "requested_url": CONFIGURED_URL,
            "final_url": CONFIGURED_URL,
            "status": 0,
            "body_bytes": 0,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    current_response = await asyncio.to_thread(
        probe_get,
        CURRENT_URL,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    current_html = str(current_response.text or "")
    current_body = current_html.encode()
    visible = normalized_visible(current_html)
    published_links = sorted(
        {
            str(node.get("href") or "").strip()
            for node in BeautifulSoup(current_html, "lxml").find_all("a", href=True)
            if "securecafe" in str(node.get("href") or "").casefold()
        }
    )
    published_floorplan_routes = [
        link
        for link in published_links
        if "/onlineleasing/" in link.casefold()
        and "/floorplans.aspx" in link.casefold()
    ]
    ROOT_GZIP.write_bytes(gzip.compress(current_body, compresslevel=9))

    repeats = [
        await full_pipeline_repeat(index, current_body, row)
        for index in (1, 2, 3)
    ]
    after = source_snapshot()

    first_units = repeats[0]["units"] if repeats else []
    unit_sets = [
        sorted(str(item.get("unit_number") or "") for item in repeat["units"])
        for repeat in repeats
    ]
    native_id_sets = [
        sorted(
            str((item.get("source_ids") or {}).get("securecafe_apartment_id") or "")
            for item in repeat["units"]
        )
        for repeat in repeats
    ]
    provider_identity_checks = {
        "all_source_property_ids_exact": all(
            item.get("source_property_id") == SOURCE_PROPERTY_ID
            for repeat in repeats
            for item in repeat["units"]
        ),
        "all_source_property_names_exact": all(
            item.get("source_property_name") == "Vintage Grove Apartments"
            for repeat in repeats
            for item in repeat["units"]
        ),
        "all_source_addresses_exact_property": all(
            str(item.get("source_property_address") or "").casefold().startswith(
                "5262 timaquana rd, jacksonville, fl, 32210"
            )
            for repeat in repeats
            for item in repeat["units"]
        ),
        "all_source_urls_exact": all(
            item.get("source_api_url") == SOURCE_API_URL
            for repeat in repeats
            for item in repeat["units"]
        ),
    }
    repeat_checks = {
        "three_full_pipeline_repeats": len(repeats) == 3,
        "all_rentcafe_adapter": all(
            repeat["adapter"] == "rentcafe" for repeat in repeats
        ),
        "all_applicant_direct_tier": all(
            repeat["tier"]
            == "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT"
            for repeat in repeats
        ),
        "all_no_errors": all(repeat["errors"] == [] for repeat in repeats),
        "all_rows_strict_native_positive_rent": all(
            repeat["emitted_rows"]
            == repeat["strict_native_positive_rent_rows"]
            == len(repeat["units"])
            and len(repeat["units"]) > 0
            for repeat in repeats
        ),
        "same_unit_set": len(unit_sets) == 3 and unit_sets[0] == unit_sets[1] == unit_sets[2],
        "same_native_id_set": len(native_id_sets) == 3
        and native_id_sets[0] == native_id_sets[1] == native_id_sets[2],
        "native_ids_unique": all(
            values and len(values) == len(set(values)) for values in native_id_sets
        ),
        "all_floorplan_names": all(
            str(item.get("floor_plan_name") or "").strip()
            for repeat in repeats
            for item in repeat["units"]
        ),
        "all_availability_dates": all(
            bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", str(item.get("availability_date") or "")))
            for repeat in repeats
            for item in repeat["units"]
        ),
    }
    root_identity_checks = {
        "property_name_visible": "vintage grove apartments" in visible,
        "street_visible": "5262" in visible
        and "timuquana" in visible
        and (" rd" in visible or " road" in visible),
        "city_visible": "jacksonville" in visible,
        "state_visible": "fl" in visible or "florida" in visible,
        "zip_visible": "32210" in visible,
        "sole_published_securecafe_floorplan_route": published_floorplan_routes
        == [PUBLISHED_PORTAL],
        "all_securecafe_links_same_property_slug": bool(published_links)
        and all("summerwind-apartments0" in link for link in published_links),
    }
    strict = bool(
        int(current_response.status_code or 0) == 200
        and str(current_response.url or CURRENT_URL) == CURRENT_URL
        and len(current_body) > 350_000
        and all(root_identity_checks.values())
        and all(provider_identity_checks.values())
        and all(repeat_checks.values())
        and before == after
    )

    payload = {
        "lane": "vintage_grove_stale_summerwind_current_official_rentcafe_e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "exact_2026-07-31_FAILED_NO_DATA_344",
        "ledger_mutation": "none",
        "commit": "none",
        "push": "none",
        "paid_canary": False,
        "configured_identity": row,
        "current_identity": {
            "name": "Vintage Grove Apartments",
            "address": "5262 Timuquana Rd",
            "city": "Jacksonville",
            "state": "FL",
            "zip": "32210",
            "website": CURRENT_URL,
        },
        "guardrails": {
            "environment": expected_env,
            "direct_public_http_only": True,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "hyperbrowser_calls": 0,
            "llm_calls": 0,
            "proxy_calls": 0,
            "web_unlocker_calls": 0,
            "flaresolverr_calls": 0,
        },
        "configured_route_probe": configured_probe,
        "current_root": {
            "requested_url": CURRENT_URL,
            "final_url": str(current_response.url or CURRENT_URL),
            "status": int(current_response.status_code or 0),
            "body_bytes": len(current_body),
            "body_sha256": sha256_bytes(current_body),
            "gzip_artifact": str(ROOT_GZIP),
            "gzip_sha256": sha256_bytes(ROOT_GZIP.read_bytes()),
            "published_securecafe_links": published_links,
            "published_securecafe_floorplan_routes": published_floorplan_routes,
        },
        "root_identity_checks": root_identity_checks,
        "provider_identity_checks": provider_identity_checks,
        "repeat_checks": repeat_checks,
        "source_snapshot_before": before,
        "source_snapshot_after": after,
        "materializer": {
            "path": str(MATERIALIZER),
            "sha256": sha256_bytes(MATERIALIZER.read_bytes()),
        },
        "full_pipeline_repeats": repeats,
        "results": [
            {
                "property_id": int(PROPERTY_ID),
                "property_name": "Vintage Grove Apartments",
                "website": CONFIGURED_URL,
                "current_official_url": CURRENT_URL,
                "outcome": "UNIT_QUALIFIED" if strict else "UNIT_UNVERIFIED",
                "property_identity_match": strict,
                "contamination_verdict": (
                    "pass_exact_same_address_current_official_vintage_grove_"
                    "published_securecafe_property_native_units_three_repeats"
                    if strict
                    else "reject_vintage_grove_identity_or_native_shape_incomplete"
                ),
                "units": len(first_units) if strict else 0,
                "adapter": repeats[0]["adapter"] if repeats else "",
                "tier": repeats[0]["tier"] if repeats else "",
                "identity_evidence": {
                    "rows_with_native_identity": len(first_units) if strict else 0,
                    "rows_with_native_identity_and_positive_rent": (
                        len(first_units) if strict else 0
                    ),
                    "source_urls": [SOURCE_API_URL] if strict else [],
                    "source_property_ids": [SOURCE_PROPERTY_ID] if strict else [],
                },
                "native_samples": [
                    {
                        "identity": {
                            "unit_number": item["unit_number"],
                            "securecafe_apartment_id": str(
                                (item.get("source_ids") or {}).get(
                                    "securecafe_apartment_id"
                                )
                                or ""
                            ),
                        },
                        "positive_rent_evidence": {
                            "market_rent_low": item["market_rent_low"],
                            "market_rent_high": item["market_rent_high"],
                        },
                        "availability_date": item["availability_date"],
                        "source_api_url": item["source_api_url"],
                    }
                    for item in first_units
                ]
                if strict
                else [],
            }
        ],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256_bytes(OUTPUT.read_bytes()),
                "strict": strict,
                "units": len(first_units),
                "root_identity_checks": root_identity_checks,
                "provider_identity_checks": provider_identity_checks,
                "repeat_checks": repeat_checks,
                "source_stable": before == after,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not strict:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
