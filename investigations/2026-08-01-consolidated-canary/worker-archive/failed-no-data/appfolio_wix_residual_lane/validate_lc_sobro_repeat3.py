from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import ma_poc.fetch as fetch_mod
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "appfolio_wix_residual_lane"
HARNESS = LANE / "run_current_full_e2e.py"
OUTPUT = LANE / "lc_sobro_current_full_pipeline_repeat3.json"
PROPERTY_ID = "251514"
EXPECTED_SOURCE_HOST = "www.lcsobro.com"
EXPECTED_PATH_PREFIX = "/floorplans/nashville-TN/lc-sobro/"


def load_harness():
    spec = importlib.util.spec_from_file_location("appfolio_wix_e2e", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def exact_row_boundary(row: dict) -> bool:
    source_url = str(row.get("source_api_url") or "")
    parsed = urlparse(source_url)
    source_ids = row.get("source_ids") or {}
    provider_native = bool(
        str(source_ids.get("entrata_uid") or "")
        and str(source_ids.get("entrata_fpid") or "")
        and str(row.get("unit_number") or "")
    )
    positive_rent = any(
        isinstance(row.get(key), (int, float)) and row.get(key) > 0
        for key in ("market_rent_low", "market_rent_high")
    )
    return bool(
        parsed.hostname == EXPECTED_SOURCE_HOST
        and parsed.path.startswith(EXPECTED_PATH_PREFIX)
        and provider_native
        and positive_rent
    )


async def main() -> None:
    harness = load_harness()
    env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_DC_PROXY_TIER": "false",
        "ENABLE_RESIDENTIAL_TIER": "false",
        "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "FETCH_BACKEND": "brightdata",
        "RENDER_BACKEND": "local",
        "PROBE_PROXY_URL": "",
        "PROXY_POOL_URLS": "",
        "ENABLE_RENDER_ON_EMPTY": "false",
        "ENABLE_PLAN_UNIT_RENDER": "false",
        "ENABLE_ENTRATA_PLAN_RENDER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for key, expected in env.items():
        if os.environ.get(key, "").casefold() != expected:
            raise RuntimeError(f"{key} must be {expected!r}")

    residual = next(
        row
        for row in read_csv(ROOT / "strict_recovery_remaining_current.csv")
        if row["property_id"] == PROPERTY_ID
    )
    metadata = next(
        row
        for row in read_csv(Path("ma_poc/config/properties.csv"))
        if row["apartmentid"] == PROPERTY_ID
    )

    fetch_mod.fetch = harness.direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    source_before = harness.source_snapshot()
    runs = []
    for index in range(1, 4):
        row = await harness.one(residual, metadata)
        strict_rows = row.get("strict_shape_rows") or []
        current_title = str((row.get("body_diagnostics") or {}).get("title") or "")
        configured_final = str(
            (row.get("configured_fetch") or {}).get("final_url") or ""
        )
        property_boundary = bool(
            strict_rows
            and len(strict_rows)
            == int(row.get("strict_native_positive_rent_rows") or 0)
            and all(exact_row_boundary(item) for item in strict_rows)
            and urlparse(configured_final).hostname == EXPECTED_SOURCE_HOST
            and re.search(r"\bLC\s+SoBro\b", current_title, re.IGNORECASE)
        )
        row["repeat_index"] = index
        row["all_strict_rows_exact_property_path_native_ids_positive_rent"] = (
            property_boundary
        )
        row["full_pipeline_strict_verdict"] = (
            "pass_exact_property_entrata_native_ids_positive_rents"
            if property_boundary
            else "reject_repeat_did_not_prove_exact_boundary"
        )
        runs.append(row)
        print(
            json.dumps(
                {
                    "repeat": index,
                    "adapter": row.get("adapter"),
                    "tier": row.get("tier"),
                    "strict": row.get("strict_native_positive_rent_rows"),
                    "exact": property_boundary,
                }
            ),
            flush=True,
        )

    source_after = harness.source_snapshot()
    # This validator exercises the configured fetch, detector, orchestrator,
    # and Entrata parser. Other recovery agents can legitimately edit an
    # unrelated adapter (for example AppFolio) while this live repeat runs;
    # do not invalidate LC SoBro evidence for an unrelated source change.
    relevant_sources = (
        "ma_poc/pms/adapters/entrata.py",
        "ma_poc/pms/detector.py",
        "ma_poc/pms/scraper.py",
    )
    stable_hashes = bool(
        source_before["git_head"] == source_after["git_head"]
        and all(
            source_before["critical_file_sha256"].get(path)
            == source_after["critical_file_sha256"].get(path)
            for path in relevant_sources
        )
    )
    repeat_pass = bool(
        stable_hashes
        and len(runs) == 3
        and all(
            row.get("full_pipeline_strict_verdict", "").startswith("pass_")
            and row.get("adapter") == "entrata"
            and row.get("tier") == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"
            and int(row.get("strict_native_positive_rent_rows") or 0) > 0
            for row in runs
        )
    )
    accepted_rows = runs[0].get("strict_shape_rows") or [] if repeat_pass else []
    source_urls = sorted(
        {str(row.get("source_api_url") or "") for row in accepted_rows}
    )
    native_ids = sorted(
        {
            str((row.get("source_ids") or {}).get("entrata_uid") or "")
            for row in accepted_rows
        }
    )
    floorplan_ids = sorted(
        {
            str((row.get("source_ids") or {}).get("entrata_fpid") or "")
            for row in accepted_rows
        }
    )
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "property_id": int(PROPERTY_ID),
        "property_name": metadata["name"],
        "canonical_address": metadata["address"],
        "canonical_zip": metadata["zip"],
        "configured_url": metadata["website"],
        "exact_property_host": EXPECTED_SOURCE_HOST,
        "exact_property_path_prefix": EXPECTED_PATH_PREFIX,
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "critical_source_stable_across_repeats": stable_hashes,
        "relevant_source_files": list(relevant_sources),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "web_unlocker_call_count": web_unlocker_call_count(),
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "proxy": False,
            "paid_canary": False,
            "hyperbrowser_calls": hyperbrowser_property_call_count(PROPERTY_ID),
            "environment": env,
        },
        "full_pipeline_repeats": runs,
        "strict_accept": repeat_pass,
        "strict_verdict": (
            "pass_exact_property_entrata_native_ids_positive_rents_no_sibling_contamination"
            if repeat_pass
            else "reject_repeat_or_boundary_incomplete"
        ),
        "native_positive_rent_rows": len(accepted_rows),
        "source_property_ids": [],
        "source_property_id_note": (
            "Entrata detail rows expose per-property floorplan IDs and unit IDs, "
            "but no separate property ID field; blank preserved."
        ),
        "source_native_unit_ids": native_ids,
        "source_floorplan_ids": floorplan_ids,
        "source_urls": source_urls,
        "accepted_rows": accepted_rows,
    }
    if web_unlocker_call_count() != 0 or hyperbrowser_property_call_count(PROPERTY_ID) != 0:
        raise RuntimeError("forbidden backend used")
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "strict_accept": payload["strict_accept"],
                "strict_verdict": payload["strict_verdict"],
                "native_positive_rent_rows": payload["native_positive_rent_rows"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
