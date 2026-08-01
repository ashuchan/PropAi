from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import ma_poc.fetch as fetch_mod
from ma_poc.pms.adapters._probe import (
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    _extract_zip,
    _normalize_street,
    _street_candidates,
    filter_listings_by_property_address,
    parse_appfolio_listings_ssr,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "appfolio_wix_residual_lane"
HARNESS = LANE / "run_current_full_e2e.py"
OUTPUT = LANE / "stadium_current_full_pipeline_repeat3.json"
PROPERTY_ID = "19538"
SOURCE_URL = "https://abbottpropertiesmultifamily.appfolio.com/listings"


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


def exact_row_boundary(sample: dict, address: str, zip_code: str) -> bool:
    unit_address = str(sample.get("unit_name") or sample.get("address") or "")
    target = _normalize_street(address)
    row_streets = {
        _normalize_street(value) for value in _street_candidates(unit_address)
    }
    exact_street = bool(target and target in row_streets)
    exact_zip = _extract_zip(unit_address, prefer_last=True) == _extract_zip(zip_code)
    source_ids = sample.get("source_ids") or {}
    listing_id = str(source_ids.get("appfolio_listing_id") or "")
    native_identity = bool(
        listing_id
        and str(sample.get("unit_number") or "") == listing_id
        and str(sample.get("unit_id") or "").endswith("-" + listing_id)
    )
    positive_rent = any(
        isinstance(sample.get(key), (int, float)) and sample.get(key) > 0
        for key in ("market_rent_low", "market_rent_high")
    )
    return exact_street and exact_zip and native_identity and positive_rent


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
        samples = row.get("strict_shape_samples") or []
        exact = bool(samples) and len(samples) == int(
            row.get("strict_native_positive_rent_rows") or 0
        ) and all(
            exact_row_boundary(sample, metadata["address"], metadata["zip"])
            for sample in samples
        )
        row["repeat_index"] = index
        row["all_strict_rows_exact_street_zip_native_id_positive_rent"] = exact
        row["full_pipeline_strict_verdict"] = (
            "pass_exact_property_appfolio_native_ids_positive_rents"
            if exact
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
                    "exact": exact,
                }
            ),
            flush=True,
        )

    # Helper-only roster audit. It never creates a recovery by itself; it
    # explains the contamination filter behind the repeated full-pipeline win.
    response = await asyncio.to_thread(
        probe_get,
        SOURCE_URL,
        unlocker=False,
        retries=1,
        timeout=35,
        proxies={},
    )
    roster = parse_appfolio_listings_ssr(response.text or "", SOURCE_URL)
    kept, telemetry = filter_listings_by_property_address(
        roster,
        metadata["address"],
        metadata["zip"],
        evidence=ScopeEvidence.PUBLISHED_INDEX,
    )
    helper_rows = [harness.sample_row(row) for row in kept]

    source_after = harness.source_snapshot()
    stable_hashes = (
        source_before["git_head"] == source_after["git_head"]
        and source_before["critical_file_sha256"]
        == source_after["critical_file_sha256"]
    )
    repeat_pass = bool(
        stable_hashes
        and len(runs) == 3
        and all(
            row.get("full_pipeline_strict_verdict", "").startswith("pass_")
            and row.get("adapter") == "appfolio"
            and row.get("tier") == "TIER_1_DOM_APPFOLIO_VANITY"
            and int(row.get("strict_native_positive_rent_rows") or 0) == 3
            and row.get("source_urls") == [SOURCE_URL]
            for row in runs
        )
        and len(roster) > len(kept) == 3
        and all(
            exact_row_boundary(row, metadata["address"], metadata["zip"])
            for row in helper_rows
        )
    )
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "property_id": int(PROPERTY_ID),
        "property_name": metadata["name"],
        "canonical_address": metadata["address"],
        "canonical_zip": metadata["zip"],
        "configured_url": metadata["website"],
        "source_url": SOURCE_URL,
        "source_snapshot_before": source_before,
        "source_snapshot_after": source_after,
        "critical_source_stable_across_repeats": stable_hashes,
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
        "helper_roster_audit": {
            "counts_toward_recovery": False,
            "source_status": int(response.status_code or 0),
            "source_final_url": str(response.url or SOURCE_URL),
            "source_body_bytes": len(response.text or ""),
            "account_roster_rows": len(roster),
            "exact_address_rows": len(kept),
            "filter_telemetry": telemetry,
            "exact_rows": helper_rows,
        },
        "strict_accept": repeat_pass,
        "strict_verdict": (
            "pass_exact_property_appfolio_native_ids_positive_rents_no_sibling_contamination"
            if repeat_pass
            else "reject_repeat_or_boundary_incomplete"
        ),
        "native_positive_rent_rows": 3 if repeat_pass else 0,
        "source_property_ids": [],
        "source_property_id_note": "AppFolio SSR exposes listing IDs but no separate property ID; blank preserved.",
        "source_native_listing_ids": [
            str((row.get("source_ids") or {}).get("appfolio_listing_id") or "")
            for row in helper_rows
        ],
        "source_urls": [SOURCE_URL] if repeat_pass else [],
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
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
