#!/usr/bin/env python3
"""Consolidate both read-only halves of the 35-property unknown audit."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "unknown_residual_lane"
LOCAL = LANE / "local_b/evidence_unknown_residual_local18_current_strict.json"
AGENT = LANE / "agent_a/evidence_unknown_residual17_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
OUT = LANE / "evidence_unknown_residual35_current_strict_consolidated.json"
NET_NEW_OUT = LANE / "strict_unknown_residual35_net_new_ids.json"
REJECTIONS_OUT = LANE / "strict_unknown_residual35_rejections.json"
FULL_DENOMINATOR = 344
TARGET_RATE = 0.60


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ledger_sha_start = _sha(LEDGER)
    remaining_sha_start = _sha(REMAINING)
    with LEDGER.open(encoding="utf-8", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    with REMAINING.open(encoding="utf-8-sig", newline="") as handle:
        remaining_rows = list(csv.DictReader(handle))
    ledger_ids = {row["property_id"] for row in ledger_rows}
    current_unknown_ids = {
        row["property_id"]
        for row in remaining_rows
        if row.get("current_detected_adapter") == "unknown"
    }

    local = _load(LOCAL)
    agent = _load(AGENT)
    local_ids = set(local["scope"]["target_ids"])
    agent_ids = {str(pid) for pid in agent["target_ids"]}
    assert len(local_ids) == 18 and len(agent_ids) == 17
    assert local_ids.isdisjoint(agent_ids)
    target_ids = local_ids | agent_ids
    assert len(target_ids) == 35

    local_accepted = list(local["accepted"])
    assert {row["property_id"] for row in local_accepted} == {"19245"}
    agent_accepted_results = [
        row for row in agent["results"] if row.get("outcome") == "UNIT_QUALIFIED"
    ]
    assert {str(row["property_id"]) for row in agent_accepted_results} == {"71962"}
    towne = agent_accepted_results[0]
    gate = towne["full_current_scraper_e2e"]["units_gate"]
    assert gate["all_rows_native_priced"] is True
    assert gate["native_anchors_unique"] is True
    assert gate["provider_boundary_single_property"] is True
    assert gate["rows_with_native_identity_and_positive_rent"] == 2
    accepted = local_accepted + [{
        "property_id": "71962",
        "property_name": towne["property_name"],
        "website": towne["canonical_website"],
        "strict_qualifies": True,
        "property_identity_match": True,
        "page_identity": towne["page_identity"],
        "provider_identity": towne["provider_identity"],
        "contamination_verdict": towne["contamination_verdict"],
        "native_identity_rows": gate["rows_with_provider_native_id_or_number"],
        "native_positive_rent_rows": gate["rows_with_native_identity_and_positive_rent"],
        "source_urls": gate["source_urls"],
        "source_property_ids": gate["source_property_ids"],
        "native_samples": gate["samples"],
        "current_full_scraper_e2e": towne["full_current_scraper_e2e"],
        "artifact": str(AGENT),
        "net_new_vs_lane_capture_ledger": 71962 in agent["net_new_ids"],
    }]
    assert {row["property_id"] for row in accepted} == {"19245", "71962"}
    assert sum(int(row["native_positive_rent_rows"]) for row in accepted) == 4

    local_rejections = list(local["rejections"])
    agent_rejections = []
    for row in agent["results"]:
        if row.get("outcome") != "REJECTED":
            continue
        agent_rejections.append({
            "property_id": str(row["property_id"]),
            "property_name": row["property_name"],
            "website": row["canonical_website"],
            "strict_qualifies": False,
            "selected_exact_route": row["selected_exact_route"],
            "route_normalization": row["route_normalization"],
            "current_page": row["selected_route_fetch"],
            "page_identity": row["page_identity"],
            "provider_identity": row["provider_identity"],
            "current_full_scraper_e2e": row["full_current_scraper_e2e"],
            "rejection_reasons": row["explicit_rejection_reasons"],
            "yotta_near_miss": row.get("yotta_near_miss"),
        })
    rejections = local_rejections + agent_rejections
    assert len(rejections) == 33
    rejection_ids = {row["property_id"] for row in rejections}
    assert rejection_ids == target_ids - {"19245", "71962"}
    assert rejection_ids.isdisjoint({row["property_id"] for row in accepted})

    charter = next(row for row in local_rejections if row["property_id"] == "74523")
    assert charter["native_provider_near_miss"]["native_positive_rent_rows"] == 1
    pepper = next(row for row in agent_rejections if row["property_id"] == "34785")
    yotta = pepper["yotta_near_miss"]
    assert yotta["provider_identity"]["property_identity_match"] is True
    assert yotta["raw_rows_with_unit_id_number_positive_rent"] == 13
    assert yotta["current_generic_api_injection_e2e"]["units"] == 13
    assert yotta["current_normalized_rows_with_preserved_source_ids"] == 0
    near_misses = [
        {
            "property_id": "74523",
            "property_name": charter["property_name"],
            "current_native_positive_rent_rows": 1,
            "current_configured_route_e2e_units": 0,
            "why_rejected": charter["rejection_reason"],
            "minimal_code_lever": charter["minimal_code_lever"],
            "evidence": charter["native_provider_near_miss"],
        },
        {
            "property_id": "34785",
            "property_name": pepper["property_name"],
            "current_native_positive_rent_rows": 13,
            "current_configured_route_e2e_units": 0,
            "why_rejected": yotta["strict_rejection"],
            "minimal_code_lever": agent["minimal_code_levers"][0]["lever"],
            "evidence": {
                "provider_identity": yotta["provider_identity"],
                "details_url": yotta["details_url"],
                "units_url": yotta["units_url"],
                "raw_unit_rows": yotta["raw_unit_rows"],
                "raw_rows_with_unit_id_number_positive_rent": yotta[
                    "raw_rows_with_unit_id_number_positive_rent"
                ],
                "raw_samples": yotta["raw_samples"],
                "current_generic_api_injection_e2e": yotta[
                    "current_generic_api_injection_e2e"
                ],
                "preserved_source_id_rows": yotta[
                    "current_normalized_rows_with_preserved_source_ids"
                ],
            },
        },
    ]

    lane_capture_net_new_ids = sorted(
        {
            *local["summary"]["net_new_ids"],
            *(str(pid) for pid in agent["net_new_ids"]),
        },
        key=int,
    )
    assert lane_capture_net_new_ids == ["19245", "71962"]
    latest_ledger_overlap = sorted(
        {row["property_id"] for row in accepted} & ledger_ids,
        key=int,
    )
    net_new_latest = sorted(
        {row["property_id"] for row in accepted} - ledger_ids,
        key=int,
    )
    projected_rows = len(ledger_rows) + len(net_new_latest)
    target_rows = math.ceil(FULL_DENOMINATOR * TARGET_RATE)
    assert _sha(LEDGER) == ledger_sha_start
    assert _sha(REMAINING) == remaining_sha_start

    policy = {
        "current_live_property_scoped_sources": True,
        "llm_calls": 0,
        "captcha_solving": False,
        "web_unlocker_calls": 0,
        "paid_canary": False,
        "repository_production_edits": 0,
        "shared_ledger_or_builder_mutations_by_audit": 0,
        "all_35_properties_probed": True,
        "minimum_three_before_cluster_generalization": True,
    }
    payload = {
        "audit": "current strict unknown residual 35-property consolidated audit",
        "capture_date": "2026-08-01",
        "scope": {
            "original_target_count": 35,
            "original_target_ids": sorted(target_ids, key=int),
            "latest_remaining_unknown_count": len(current_unknown_ids),
            "accepted_ids_removed_from_latest_remaining": sorted(
                {"19245", "71962"} - current_unknown_ids,
                key=int,
            ),
        },
        "strict_gate": {
            "exact_property_identity": True,
            "provider_native_unit_id_or_number": True,
            "positive_rent": True,
            "current_full_configured_route_adapter_scraper_e2e": True,
            "contamination_checks": True,
        },
        "summary": {
            "targets": 35,
            "strict_recovered_properties": 2,
            "strict_recovery_rate": round(2 / 35, 6),
            "strict_native_positive_rent_rows": 4,
            "strict_rejected_properties": 33,
            "near_miss_properties": 2,
            "near_miss_native_positive_rent_rows": 14,
            "lane_capture_net_new_ids_vs_183_row_ledger": lane_capture_net_new_ids,
            "already_in_latest_current_ledger": latest_ledger_overlap,
            "net_new_ids_vs_latest_current_ledger": net_new_latest,
            "latest_ledger_rows": len(ledger_rows),
            "projected_ledger_rows_after_net_new": projected_rows,
            "projected_strict_recovery_rate_of_344": round(
                projected_rows / FULL_DENOMINATOR, 6
            ),
            "rows_needed_for_60_percent": target_rows,
            "remaining_property_gap_to_60_percent": max(0, target_rows - projected_rows),
        },
        "accepted": sorted(accepted, key=lambda row: int(row["property_id"])),
        "near_misses": near_misses,
        "rejections": sorted(rejections, key=lambda row: int(row["property_id"])),
        "policy": policy,
        "latest_ledger_snapshot": {
            "rows": len(ledger_rows),
            "sha256_start": ledger_sha_start,
            "sha256_end": _sha(LEDGER),
            "unchanged_during_consolidation": True,
        },
        "latest_remaining_snapshot": {
            "rows": len(remaining_rows),
            "unknown_rows": len(current_unknown_ids),
            "sha256_start": remaining_sha_start,
            "sha256_end": _sha(REMAINING),
            "unchanged_during_consolidation": True,
        },
        "supporting_artifacts": [str(LOCAL), str(AGENT)],
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    NET_NEW_OUT.write_text(json.dumps({
        "source_artifact": str(OUT),
        "lane_capture_net_new_ids_vs_183_row_ledger": lane_capture_net_new_ids,
        "already_in_latest_current_ledger": latest_ledger_overlap,
        "net_new_ids_vs_latest_current_ledger": net_new_latest,
        "latest_ledger_rows": len(ledger_rows),
        "latest_ledger_sha256": ledger_sha_start,
    }, indent=2, sort_keys=True) + "\n")
    REJECTIONS_OUT.write_text(json.dumps({
        "source_artifact": str(OUT),
        "targets": 35,
        "accepted": 2,
        "rejected": 33,
        "near_miss_ids": ["74523", "34785"],
        "rejections": sorted(rejections, key=lambda row: int(row["property_id"])),
    }, indent=2, sort_keys=True) + "\n")
    assert _sha(LEDGER) == ledger_sha_start
    assert _sha(REMAINING) == remaining_sha_start
    print(json.dumps({
        "targets": 35,
        "accepted_ids": ["19245", "71962"],
        "accepted_units": 4,
        "rejected": 33,
        "near_miss_ids": ["74523", "34785"],
        "net_new_ids_vs_latest_current_ledger": net_new_latest,
        "latest_ledger_rows": len(ledger_rows),
        "latest_unknown_remaining": len(current_unknown_ids),
        "output": str(OUT),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
