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

from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "riverwalk_wimmer_lane"
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
EVIDENCE = LANE / "evidence_riverwalk_71534_current_strict.json"
HTML_GZ = LANE / "71534_current_floorplans.html.gz"
PID = "71534"
STALE_URL = "https://www.wimmercommunities.com/apartments/menomonee-falls/riverwalk-on-the-falls/"
CURRENT_ROOT = "https://www.wimmercommunities.com/apartments/wi/menomonee-falls/riverwalk-on-the-falls"
CURRENT_FLOORPLANS = CURRENT_ROOT + "/floorplans"
EXPECTED_SIGHTMAP = "https://sightmap.com/app/api/v1/y8px5ljmv19/sightmaps/100325"
FOREIGN_KNOCK_IDS = ("2007994", "2007989", "2008000", "2007983", "2007982", "2007988")
SOURCE_FILES = (
    "ma_poc/core/identity.py",
    "ma_poc/pms/adapters/sightmap.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/scraper.py",
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_hashes() -> dict[str, str]:
    return {path: sha((REPO / path).read_bytes()) for path in SOURCE_FILES}


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def positive(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def compact(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "unit_number", "floor_plan_name", "bedrooms", "bathrooms", "sqft",
        "market_rent_low", "market_rent_high", "availability_date",
        "source_api_url", "source_property_id", "source_property_name",
        "source_property_address", "source_ids",
    )
    return {key: row.get(key) for key in keys}


def get(url: str):
    return requests.get(url, timeout=45, impersonate="chrome", allow_redirects=True)


async def repeat_pipeline(metadata: dict[str, str], body: bytes, headers: dict[str, str], repeat: int) -> dict[str, Any]:
    task = CrawlTask(
        url=CURRENT_FLOORPLANS, property_id=PID, priority=0, budget_ms=180_000,
        reason=TaskReason.MANUAL, render_mode=RenderMode.GET,
    )
    fetched = FetchResult(
        url=CURRENT_FLOORPLANS, outcome=FetchOutcome.OK, status=200, body=body,
        headers=headers, render_mode=RenderMode.GET, final_url=CURRENT_FLOORPLANS,
        attempts=1, elapsed_ms=0,
    )
    result = await asyncio.wait_for(
        scrape_jugnu(task, fetched, page=None, profile=None, csv_row=metadata), timeout=180
    )
    emitted = [row for row in (result.get("units") or []) if isinstance(row, dict)]
    strict = [row for row in emitted if unit_has_real_anchor(row) and positive(row)]
    units = [compact(row) for row in strict]
    return {
        "repeat": repeat,
        "adapter": result.get("_adapter_used"),
        "detected_pms": (result.get("_detected_pms") or {}).get("pms"),
        "tier": result.get("extraction_tier_used"),
        "fallback_chain": result.get("_fallback_chain") or [],
        "errors": result.get("errors") or [],
        "emitted_rows": len(emitted),
        "strict_native_positive_rent_rows": len(strict),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "distinct_unit_numbers": len({str(row.get("unit_number") or "").strip() for row in strict}),
        "source_urls": sorted({str(row.get("source_api_url") or "") for row in strict if row.get("source_api_url")}),
        "units": units,
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

    with (REPO / "ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as handle:
        metadata = next(row for row in csv.DictReader(handle) if row["apartmentid"] == PID)
    before = source_hashes()
    stale, current_root, floorplans = get(STALE_URL), get(CURRENT_ROOT), get(CURRENT_FLOORPLANS)
    if stale.status_code != 404 or current_root.status_code != 200 or floorplans.status_code != 200:
        raise SystemExit(
            f"route statuses stale={stale.status_code} root={current_root.status_code} floorplans={floorplans.status_code}"
        )

    body_norm = norm(floorplans.text)
    identity_checks = {
        "property_name_visible": norm("RiverWalk on the Falls") in body_norm,
        "street_visible": norm("W165N8910 Grand Avenue") in body_norm,
        "city_visible": norm("Menomonee Falls") in body_norm,
        "state_visible": norm("WI") in body_norm,
        "zip_visible": norm("53051") in body_norm,
        "canonical_link_visible": norm(CURRENT_ROOT) in body_norm,
    }
    if not all(identity_checks.values()):
        raise SystemExit(f"identity failed: {identity_checks!r}")
    HTML_GZ.write_bytes(gzip.compress(floorplans.content, compresslevel=9, mtime=0))

    repeats = [
        await repeat_pipeline(
            metadata, floorplans.content,
            {key.lower(): value for key, value in floorplans.headers.items()}, repeat
        )
        for repeat in range(1, 4)
    ]
    first = repeats[0]
    numbers = [str(row["unit_number"]) for row in first["units"]]
    native_ids = [str((row.get("source_ids") or {}).get("sightmap_unit_id") or "") for row in first["units"]]
    repeat_checks = {
        "three_repeats": len(repeats) == 3,
        "all_sightmap_adapter": all(row["adapter"] == "sightmap" for row in repeats),
        "all_sightmap_detection": all(row["detected_pms"] == "sightmap" for row in repeats),
        "all_tier_1_sightmap": all(row["tier"] == "TIER_1_API_SIGHTMAP_IFRAME" for row in repeats),
        "all_eight_emitted_and_strict": all(
            row["emitted_rows"] == row["strict_native_positive_rent_rows"] == row["distinct_unit_numbers"] == 8
            for row in repeats
        ),
        "same_units_each_repeat": all([str(unit["unit_number"]) for unit in row["units"]] == numbers for row in repeats),
        "one_exact_source_each_repeat": all(row["source_urls"] == [EXPECTED_SIGHTMAP] for row in repeats),
        "natural_unit_numbers_unique": len(numbers) == len(set(numbers)) == 8,
        "native_sightmap_ids_unique": len(native_ids) == len(set(native_ids)) == 8 and all(native_ids),
        "all_floorplan_names": all(str(row.get("floor_plan_name") or "").strip() for row in first["units"]),
        "all_availability_dates": all(re.fullmatch(r"20\d\d-\d\d-\d\d", str(row.get("availability_date") or "")) for row in first["units"]),
        "all_positive_row_level_rents": all(positive(row) for row in first["units"]),
        "no_pipeline_errors": all(not row["errors"] for row in repeats),
    }
    if not all(repeat_checks.values()):
        raise SystemExit(f"pipeline checks failed: {repeat_checks!r}")

    foreign_knock = []
    for property_id in FOREIGN_KNOCK_IDS:
        response = requests.get(
            f"https://doorway-api.knockrentals.com/v1/property/{property_id}", timeout=30,
            headers={"Origin": "https://doorway.knck.io", "Accept": "application/json"},
        )
        data = response.json()["property"]["data"]
        location, address = data["location"], data["location"]["address"]
        foreign_knock.append({
            "property_id": property_id, "name": location.get("name"),
            "street": address.get("street"), "city": address.get("city"),
            "state": address.get("state"), "zip": address.get("zip"),
            "official_website": (data.get("social") or {}).get("website"),
            "is_exact_riverwalk": norm(str(location.get("name") or "")) == norm("RiverWalk on the Falls")
            and norm(str(address.get("street") or "")) == norm("W165N8910 Grand Avenue"),
        })
    if any(row["is_exact_riverwalk"] for row in foreign_knock):
        raise SystemExit("foreign Knock negative control matched Riverwalk")
    after = source_hashes()
    if before != after:
        raise SystemExit("source changed during evidence run")

    native_samples = [
        {
            "identity": {
                "unit_number": str(row.get("unit_number") or ""),
                "sightmap_unit_id": str((row.get("source_ids") or {}).get("sightmap_unit_id") or ""),
            },
            "positive_rent_evidence": {
                "market_rent_low": row.get("market_rent_low"),
                "market_rent_high": row.get("market_rent_high"),
            },
            "floor_plan_name": row.get("floor_plan_name"),
            "availability_date": row.get("availability_date"),
            "source_api_url": row.get("source_api_url"),
        }
        for row in first["units"]
    ]
    strict_result = {
        "property_id": int(PID), "property_name": "Riverwalk on the Falls I",
        "website": STALE_URL, "current_official_url": CURRENT_ROOT,
        "outcome": "UNIT_QUALIFIED", "adapter": "sightmap",
        "tier": "TIER_1_API_SIGHTMAP_IFRAME", "units": 8,
        "property_identity_match": True,
        "contamination_verdict": "pass_exact_wimmer_current_canonical_name_street_city_state_zip_single_sightmap_three_full_pipeline_repeats",
        "identity_evidence": {
            "configured_url_is_stale_404": True, "current_canonical_is_live_200": True,
            "identity_checks": identity_checks, "rows_with_native_identity": 8,
            "rows_with_native_identity_and_positive_rent": 8,
            "distinct_native_unit_numbers": 8, "distinct_native_sightmap_unit_ids": 8,
            "source_urls": [EXPECTED_SIGHTMAP],
        },
        "native_samples": native_samples,
    }
    script = Path(__file__).resolve()
    evidence = {
        "lane": "riverwalk_wimmer_stale_canonical_current_sightmap_e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": "exact_2026-07-31_FAILED_NO_DATA_344", "ledger_mutation": "none",
        "commit": "none", "push": "none", "paid_canary": False,
        "guardrails": {
            "environment": actual_env, "direct_public_http_only": True,
            "hyperbrowser_calls": 0, "llm_calls": 0, "proxy_calls": 0,
            "web_unlocker_calls": 0, "flaresolverr_calls": 0,
            "captcha_solving": False, "fingerprint_rotation": False,
        },
        "configured_identity": {key: metadata[key] for key in ("apartmentid", "name", "address", "city", "state", "zip", "website")},
        "http_evidence": {
            "stale_configured": {
                "requested_url": STALE_URL, "status": stale.status_code,
                "final_url": str(stale.url), "body_bytes": len(stale.content),
                "body_sha256": sha(stale.content),
            },
            "current_root": {
                "requested_url": CURRENT_ROOT, "status": current_root.status_code,
                "final_url": str(current_root.url), "body_bytes": len(current_root.content),
                "body_sha256": sha(current_root.content),
            },
            "current_floorplans": {
                "requested_url": CURRENT_FLOORPLANS, "status": floorplans.status_code,
                "final_url": str(floorplans.url), "body_bytes": len(floorplans.content),
                "body_sha256": sha(floorplans.content), "html_gzip_artifact": str(HTML_GZ),
                "html_gzip_sha256": sha(HTML_GZ.read_bytes()),
            },
        },
        "identity_checks": identity_checks, "full_pipeline_repeats": repeats,
        "repeat_checks": repeat_checks,
        "stale_404_foreign_knock_negative_controls": foreign_knock,
        "source_snapshot_before": before, "source_snapshot_after": after,
        "materializer": {"path": str(script), "sha256": sha(script.read_bytes())},
        "results": [strict_result],
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "evidence": str(EVIDENCE), "evidence_sha256": sha(EVIDENCE.read_bytes()),
        "materializer": str(script), "materializer_sha256": sha(script.read_bytes()),
        "html_gzip": str(HTML_GZ), "html_gzip_sha256": sha(HTML_GZ.read_bytes()),
        "source_hashes": after, "strict_units": 8, "unit_numbers": numbers,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
