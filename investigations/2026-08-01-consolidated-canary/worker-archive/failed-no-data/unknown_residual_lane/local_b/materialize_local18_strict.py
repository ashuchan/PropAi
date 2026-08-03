#!/usr/bin/env python3
"""Consolidate the local 18-property half of the current unknown residual."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "unknown_residual_lane/local_b"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PAGE_PROBE = LANE / "current_page_probe.json"
E2E = LANE / "current_live_scraper_e2e.json"
TENZEN = LANE / "evidence_19245_tenzen_current_strict.json"
MRI = LANE / "evidence_mri_three_current_probe.json"
OUT = LANE / "evidence_unknown_residual_local18_current_strict.json"
REJECTIONS_OUT = LANE / "strict_unknown_residual_local18_rejections.json"
NET_NEW_OUT = LANE / "strict_unknown_residual_local18_net_new_ids.json"
TARGET_IDS = {
    "1765", "4756", "19245", "22964", "27349", "33993", "34708",
    "37071", "40733", "42977", "48389", "53567", "64068", "72732",
    "74523", "232583", "246962", "274886",
}
REASONS = {
    "1765": (
        "exact_native_listing_explicitly_states_property_is_not_available_and_current_scraper_emits_plan_only",
        "none_current_source_explicitly_withdraws_the_listing",
    ),
    "4756": (
        "configured_clear_run_domain_serves_gables_corporate_homepage_identity_mismatch",
        "none_without_a_current_exact_clear_run_inventory_route",
    ),
    "22964": (
        "exact_property_page_publishes_no_native_availability_or_positive_rent_rows",
        "none_without_a_current_property_scoped_inventory_source",
    ),
    "27349": (
        "current_exact_page_and_scraper_are_plan_level_only_no_native_unit_identity",
        "none_without_native_unit_inventory",
    ),
    "33993": (
        "configured_url_redirects_to_spherexx_nonpayment_hosting_suspension_page",
        "none_website_or_provider_route_must_be_restored",
    ),
    "34708": (
        "retired_property_route_redirects_to_camden_corporate_homepage_identity_mismatch",
        "none_without_a_current_exact_property_route",
    ),
    "37071": (
        "configured_domain_serves_unrelated_chinese_gambling_content_identity_mismatch",
        "none_domain_is_no_longer_a_property_source",
    ),
    "40733": (
        "current_exact_page_and_scraper_publish_plan_pricing_only_no_native_unit_identity",
        "none_without_native_unit_inventory",
    ),
    "42977": (
        "configured_route_redirects_to_realpage_call_support_page_no_property_inventory",
        "none_without_a_current_exact_realpage_property_route",
    ),
    "48389": (
        "current_route_returns_sgcaptcha_interstitial_no_captcha_interaction_or_source_data",
        "non_captcha_current_provider_route_or_allowed_hyperbrowser_fetch_then_full_e2e",
    ),
    "53567": (
        "current_exact_mri_search_publishes_plan_pricing_only_no_native_unit_rows",
        "none_without_new_native_inventory",
    ),
    "64068": (
        "configured_domain_is_a_godaddy_parking_lander_not_a_property_source",
        "none_domain_is_parked",
    ),
    "72732": (
        "current_exact_page_and_scraper_publish_bedroom_type_rent_ranges_only_no_native_units",
        "none_without_native_unit_inventory",
    ),
    "74523": (
        "exact_mri_search_has_one_native_positive_rent_unit_but_current_configured_route_scraper_emits_zero_units",
        "property_scoped_mri_prospectconnect_link_hop_with_session_csrf_search_post_and_data_unitid_parser",
    ),
    "232583": (
        "current_exact_mri_search_publishes_plan_pricing_only_no_native_unit_rows",
        "none_without_new_native_inventory",
    ),
    "246962": (
        "configured_domain_is_a_godaddy_parking_lander_not_a_property_source",
        "none_domain_is_parked",
    ),
    "274886": (
        "exact_property_page_has_floorplan_content_but_no_native_unit_identity_or_current_availability",
        "none_without_a_property_scoped_native_inventory_source",
    ),
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ledger_sha_before = _sha(LEDGER)
    with REMAINING.open(encoding="utf-8-sig", newline="") as handle:
        unknown = [
            row for row in csv.DictReader(handle)
            if row.get("current_detected_adapter") == "unknown"
        ]
    assert len(unknown) == 35
    target = {row["property_id"]: row for row in unknown if row["property_id"] in TARGET_IDS}
    assert set(target) == TARGET_IDS
    with LEDGER.open(encoding="utf-8", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger_ids = {row["property_id"] for row in ledger_rows}

    page_by_id = {
        str(row["property_id"]): row for row in _load(PAGE_PROBE)["results"]
    }
    e2e_by_id = {
        str(row["property_id"]): row for row in _load(E2E)["results"]
    }
    assert set(page_by_id) == TARGET_IDS
    assert set(e2e_by_id) == TARGET_IDS
    tenzen = _load(TENZEN)
    assert tenzen["property_id"] == "19245" and tenzen["strict_qualifies"] is True
    assert tenzen["native_positive_rent_rows"] == 2
    assert tenzen["current_full_scraper_e2e"]["strict_native_positive_rent_rows"] == 2
    mri_by_id = {
        str(row["property_id"]): row for row in _load(MRI)["results"]
    }
    assert set(mri_by_id) == {"53567", "74523", "232583"}
    assert mri_by_id["74523"]["native_positive_rent_rows"] == 1
    assert mri_by_id["74523"]["current_configured_route_scraper_e2e"]["units"] == 0

    accepted = [{
        "property_id": "19245",
        "property_name": tenzen["property_name"],
        "website": tenzen["website"],
        "strict_qualifies": True,
        "property_identity_match": True,
        "contamination_verdict": tenzen["contamination_verdict"],
        "native_identity_rows": tenzen["native_identity_rows"],
        "native_positive_rent_rows": tenzen["native_positive_rent_rows"],
        "source_urls": tenzen["source_urls"],
        "native_rows": tenzen["native_rows"],
        "current_full_scraper_e2e": tenzen["current_full_scraper_e2e"],
        "artifact": str(TENZEN),
        "net_new_vs_current_ledger": "19245" not in ledger_ids,
    }]
    rejections = []
    for pid in sorted(TARGET_IDS - {"19245"}, key=int):
        page = page_by_id[pid]
        e2e = e2e_by_id[pid]
        reason, lever = REASONS[pid]
        provider = mri_by_id.get(pid)
        rejection = {
            "property_id": pid,
            "property_name": page.get("property_name") or target[pid].get("property_name") or "",
            "website": target[pid]["website"],
            "strict_qualifies": False,
            "rejection_reason": reason,
            "minimal_code_lever": lever,
            "current_page": {
                "status": page.get("status"),
                "final_url": page.get("final_url"),
                "body_bytes": page.get("body_bytes"),
                "body_sha256": page.get("body_sha256"),
                "title": page.get("title"),
                "name_visible": page.get("name_visible", False),
                "address_visible": page.get("address_visible", False),
                "detected_adapter": page.get("detected_adapter"),
                "provider_links": page.get("provider_links") or [],
                "marker_counts": page.get("marker_counts") or {},
                "error": page.get("error") or "",
            },
            "current_full_scraper_e2e": {
                "outcome": e2e.get("outcome"),
                "adapter": e2e.get("adapter"),
                "tier": e2e.get("tier"),
                "units": int(e2e.get("units") or 0),
                "plans": int(e2e.get("plans") or 0),
                "contamination_verdict": e2e.get("contamination_verdict"),
                "errors": e2e.get("errors") or [],
            },
            "native_provider_near_miss": (
                {
                    "provider": "MRI ProspectConnect",
                    "provider_property_id": provider["provider_property_id"],
                    "native_identity_rows": provider["native_identity_rows"],
                    "native_positive_rent_rows": provider["native_positive_rent_rows"],
                    "native_rows": provider["native_rows"],
                    "property_identity_match": provider["property_identity_match"],
                    "contamination_verdict": provider["contamination_verdict"],
                    "source_urls": [provider["provider_index_url"], provider["provider_search_url"]],
                }
                if provider and provider["native_positive_rent_rows"]
                else None
            ),
        }
        assert rejection["current_full_scraper_e2e"]["units"] == 0
        rejections.append(rejection)
    assert len(accepted) == 1 and len(rejections) == 17
    assert {row["property_id"] for row in accepted + rejections} == TARGET_IDS
    assert sum(row["native_positive_rent_rows"] for row in accepted) == 2
    net_new = [row for row in accepted if row["property_id"] not in ledger_ids]
    assert _sha(LEDGER) == ledger_sha_before

    policy = {
        "llm_calls": 0,
        "web_unlocker_calls": 0,
        "captcha_interactions": 0,
        "hyperbrowser_sessions": 0,
        "paid_canary": False,
        "repository_production_edits": 0,
        "shared_ledger_mutations": 0,
    }
    payload = {
        "audit": "current unknown residual local 18 strict audit",
        "capture_date": "2026-08-01",
        "scope": {
            "filter": "current_detected_adapter == unknown",
            "full_unknown_denominator": len(unknown),
            "local_target_count": len(TARGET_IDS),
            "target_ids": sorted(TARGET_IDS, key=int),
        },
        "strict_gate": {
            "exact_property_identity": True,
            "provider_native_unit_identity": True,
            "positive_rent": True,
            "current_configured_route_full_scraper_e2e": True,
            "no_sibling_or_portfolio_contamination": True,
        },
        "summary": {
            "targets": 18,
            "strict_recovered_properties": 1,
            "strict_rejected_properties": 17,
            "strict_native_positive_rent_rows": 2,
            "net_new_properties_vs_current_ledger": len(net_new),
            "net_new_ids": [row["property_id"] for row in net_new],
            "near_miss_properties": 1,
            "near_miss_ids": ["74523"],
        },
        "accepted": accepted,
        "rejections": rejections,
        "policy": policy,
        "ledger_snapshot": {
            "rows": len(ledger_rows),
            "sha256": ledger_sha_before,
            "unchanged_after_materialization": True,
        },
        "supporting_artifacts": [str(PAGE_PROBE), str(E2E), str(TENZEN), str(MRI)],
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    REJECTIONS_OUT.write_text(json.dumps({
        "targets": 18,
        "rejected": len(rejections),
        "rejections": rejections,
        "policy": policy,
    }, indent=2, sort_keys=True) + "\n")
    NET_NEW_OUT.write_text(json.dumps({
        "ledger_sha256": ledger_sha_before,
        "ledger_rows": len(ledger_rows),
        "net_new_count": len(net_new),
        "net_new_ids": [row["property_id"] for row in net_new],
        "rows": net_new,
    }, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "targets": 18,
        "accepted": 1,
        "rejected": 17,
        "net_new_ids": [row["property_id"] for row in net_new],
        "near_miss_ids": ["74523"],
        "output": str(OUT),
    }))


if __name__ == "__main__":
    main()
