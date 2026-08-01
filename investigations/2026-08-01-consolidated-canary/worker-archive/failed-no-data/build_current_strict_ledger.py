from __future__ import annotations

import csv
import gzip
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
COHORT = ROOT / "failed344.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
TARGET_PROPERTIES = 258  # ceil(75% of the exact 344-property cohort)
INTERMEDIATE_GATE_PROPERTIES = 207

# This manifest is intentionally explicit. An artifact does not enter the
# numerator merely because it exists in the directory.
ARTIFACTS = [
    ("strict99_rentcafe", ROOT / "evidence_unit_rentcafe31_tight.json"),
    ("strict99_generic", ROOT / "evidence_unit_generic1_tight.json"),
    ("strict99_other", ROOT / "evidence_unknown_other21_tight.json"),
    ("strict99_page_local", ROOT / "evidence_unknown_page_local9_tight.json"),
    ("entrata_batch1", ROOT / "hb_entrata_sweep_batch1.json"),
    ("entrata_batch2", ROOT / "hb_entrata_sweep_batch2.json"),
    ("entrata_batch3", ROOT / "hb_entrata_sweep_batch3.json"),
    ("entrata_prior4", ROOT / "hb_entrata_sweep_prior4_revalidate.json"),
    ("securecafe_priority", ROOT / "evidence_securecafe_priority11_direct_revalidate.json"),
    ("securecafe_remaining", ROOT / "evidence_securecafe_remaining12_direct.json"),
    ("securecafe_boundary", ROOT / "evidence_securecafe_boundary2_revalidate.json"),
    ("residual_unknown", ROOT / "evidence_residual_unknown48_direct.json"),
    ("appfolio_address_match", ROOT / "evidence_appfolio4_strict_address_revalidate.json"),
    ("onesite_boundary_batch", ROOT / "evidence_onesite8_property_boundary_revalidate.json"),
    ("onesite_boulevard45", ROOT / "evidence_onesite_blvd45_boundary_revalidate.json"),
    ("onesite_residual", ROOT / "evidence_onesite_residual7_property_boundary.json"),
    ("onesite_standard_leander", ROOT / "evidence_onesite_standard_leander_boundary_revalidate.json"),
    ("vendor_tail_strict", ROOT / "evidence_vendor_tail30_strict.json"),
    ("rentmanager_residual", ROOT / "evidence_residual_rentmanager17_strict_audit.json"),
    ("strict99_false_positive_revalidate", ROOT / "evidence_strict99_false_positive50_consolidated.json"),
    ("wix_3dplans_bellagio", ROOT / "evidence_3dplans_bellagio_262964.json"),
    ("entrata_hb_root_sample3", ROOT / "hb_entrata_sweep_root_sample3.json"),
    (
        "rentcafe_applicant_nestin_vanity",
        ROOT / "evidence_rentcafe_applicant_vanity_proof2.json",
    ),
    (
        "rentcafe_nestin_enclave",
        ROOT / "evidence_rentcafe_enclave_free_path.json",
    ),
    (
        "unknown49_exact_provider_live",
        ROOT / "evidence_unknown49_strict.json",
    ),
    (
        "rentcafe_published_applicant_theme",
        ROOT / "evidence_rentcafe_published_theme3.json",
    ),
    (
        "rentcafe_meetinghouse_applicant_route",
        ROOT / "evidence_rentcafe_meetinghouse_routing_e2e.json",
    ),
    (
        "appfolio_generic_current_strict",
        ROOT
        / "appfolio_generic_lane"
        / "evidence_appfolio_generic_current_strict_consolidated.json",
    ),
    (
        "rentcafe_residual_direct_only",
        ROOT / "evidence_rentcafe_residual24_direct_only.json",
    ),
    (
        "centennial_same_origin_sightmap",
        ROOT / "evidence_centennial_sightmap_strict.json",
    ),
    (
        "entrata_residual_current_strict",
        ROOT
        / "entrata_residual_lane"
        / "evidence_entrata_residual_current_strict_consolidated.json",
    ),
    (
        "knock_jonah_redirect3",
        ROOT / "evidence_knock_jonah_redirect3_strict.json",
    ),
    (
        "rentmanager_pondview_current",
        ROOT / "evidence_rentmanager_49096_current_revalidate.json",
    ),
    (
        "rentmanager_iloveleasing_current",
        ROOT / "evidence_rentmanager_77794_current_revalidate.json",
    ),
    (
        "rentmanager_spherexx_legacy_current",
        ROOT / "evidence_spherexx_281149_current_revalidate.json",
    ),
    (
        "rentmanager_legacy_oaks_current_hb",
        ROOT / "evidence_rentmanager_55165_current_hb_e2e.json",
    ),
    (
        "arthaus_yardi_proxy_current_e2e",
        ROOT / "evidence_arthaus_yardi_proxy_current_e2e.json",
    ),
    (
        "entrata_remaining29_current_strict",
        ROOT
        / "entrata_residual_lane"
        / "evidence_entrata_remaining29_current_strict_consolidated.json",
    ),
    (
        "rentcafe_remaining29_current_strict",
        Path(
            "/Users/ankur/PropAi-codex-availability-date/investigations/"
            "2026-08-01-failed-no-data/rentcafe_remaining29/"
            "strict_net_new_recoveries.json"
        ),
    ),
    (
        "doorloop_park_place_current_e2e",
        ROOT / "evidence_doorloop_park_place_current_e2e.json",
    ),
    (
        "onesite_ollr_current_source_full_pipeline",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_ollr_detector_current_source_full_pipeline9.json",
    ),
    (
        "knock_unknown_current_source_e2e",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_knock_unknown_two_current_e2e.json",
    ),
    (
        "pepper_tree_yotta_current_source_e2e",
        ROOT
        / "unknown_residual_lane"
        / "evidence_pepper_tree_yotta_current_e2e.json",
    ),
    (
        "charter_club_mri_current_source_e2e",
        ROOT
        / "unknown_residual_lane"
        / "evidence_charter_club_mri_current_e2e.json",
    ),
    (
        "rentcafe_rentmanager_three_current_source_e2e",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_rentcafe_rentmanager_three_current_e2e.json",
    ),
    (
        "avana_sightmap_current_source_e2e",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_avana_sightmap_current_e2e.json",
    ),
    (
        "knock_dynamic_dni_current_source_e2e",
        ROOT
        / "encore_knock_lane"
        / "knock_agent"
        / "evidence_knock_residual6_current_strict.json",
    ),
    (
        "stadium_appfolio_repeat3_current_source_e2e",
        ROOT
        / "appfolio_wix_residual_lane"
        / "stadium_current_full_pipeline_repeat3.json",
    ),
    (
        "edgewater_entrata_current_source_e2e",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_edgewater_entrata_current_strict_replay.json",
    ),
    (
        "apartments_bel_air_entrata_current_source_e2e",
        ROOT
        / "entrata_residual_lane"
        / "evidence_30101_current_full_scraper_hb.json",
    ),
    (
        "village_gate_lamphouse_current_source_e2e",
        ROOT
        / "entrata_residual_lane"
        / "evidence_hb_unknown_rentcafe_two_current_strict.json",
    ),
    (
        "entrata_snippet_cluster_current_source_e2e",
        ROOT
        / "encore_knock_lane"
        / "evidence_entrata_snippet_cluster_current_strict.json",
    ),
    (
        "copper_onesite_collision_current_source_e2e",
        ROOT
        / "realpage_onesite_residual_lane"
        / "evidence_copper_onesite_collision_current_e2e.json",
    ),
    (
        "westwood_betternoi_current_full_configured_pipeline",
        ROOT
        / "encore_knock_lane"
        / "evidence_westwood_betternoi_current_full.json",
    ),
    (
        "village_park_mri_current_full_configured_pipeline",
        ROOT
        / "entrata_residual_lane"
        / "evidence_village_park_mri_current_full_strict.json",
    ),
    (
        "millennium_appfolio_current_full_configured_pipeline",
        ROOT
        / "entrata_residual_lane"
        / "evidence_millennium_appfolio_current_strict.json",
    ),
    (
        "tribeca_leaseleads_current_full_configured_pipeline",
        ROOT
        / "entrata_residual_lane"
        / "evidence_tribeca_leaseleads_current_full_strict.json",
    ),
    (
        "enclave_golden_triangle_current_published_entrata_detail",
        ROOT
        / "entrata_residual_lane"
        / "evidence_enclave_35192_current_strict.json",
    ),
    (
        "quiet_waters_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_quiet_waters_20672_current_strict.json",
    ),
    (
        "aventura_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_aventura_5735_current_strict.json",
    ),
    (
        "park_creek_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_park_creek_9297_current_strict.json",
    ),
    (
        "harpers_mill_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_harpers_mill_234772_current_strict.json",
    ),
    (
        "strata_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_strata_239274_current_strict.json",
    ),
    (
        "boro_phipps_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_boro_phipps_26736_current_strict.json",
    ),
    (
        "plan_only_pair_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_plan_only_modal_pair_current_strict.json",
    ),
    (
        "lavina_kelly_current_published_entrata_modal",
        ROOT
        / "entrata_residual_lane"
        / "evidence_lavina_kelly_current_strict.json",
    ),
    (
        "barberton_current_same_origin_rentcafe_inventory",
        ROOT
        / "entrata_residual_lane"
        / "evidence_barberton_46915_current_strict.json",
    ),
    (
        "scully_property_owned_entrata_current_configured_e2e",
        ROOT
        / "hb_scully_residual3_detail_probe"
        / "evidence_scully_three_current_strict.json",
    ),
    (
        "grand_westchase_apts247_plan_only_resman_current_configured_e2e",
        ROOT
        / "resman_grand_lane"
        / "evidence_grand_westchase_current_strict.json",
    ),
    (
        "unknown_wix_oll_application_hop",
        ROOT
        / "unknown_wix_parallel"
        / "22964_current_configured_scrape_jugnu_e2e.json",
    ),
    (
        "unknown_wix_oll_application_hop",
        ROOT
        / "unknown_wix_parallel"
        / "27349_current_configured_scrape_jugnu_e2e.json",
    ),
    (
        "unknown_wix_oll_application_hop_negative_control",
        ROOT
        / "unknown_wix_parallel"
        / "272772_current_configured_scrape_jugnu_e2e.json",
    ),
    (
        "clear_run_exact_same_origin_gables_sightmap_current_e2e",
        ROOT
        / "clear_run_gables_lane"
        / "evidence_clear_run_4756_current_strict.json",
    ),
    (
        "riverplace_stale_domain_current_official_rentcafe_current_e2e",
        ROOT
        / "riverplace_migration_lane"
        / "evidence_riverplace_64068_current_strict.json",
    ),
    (
        "riverfalls_stale_domain_current_official_securecafe_current_e2e",
        ROOT
        / "riverfalls_migration_lane"
        / "evidence_riverfalls_8654_current_strict.json",
    ),
    (
        "current_official_migrations_marketapts_g5_knock",
        ROOT
        / "current_official_migrations_three"
        / "evidence_current_official_migrations_three_strict.json",
    ),
    (
        "rentcafe_hosted_tamarron_waitlist_and_shared_host_fail_closed",
        ROOT
        / "rentcafe_hosted_table_lane"
        / "evidence_tamarron_34362_current_strict.json",
    ),
    (
        "bradley_pointe_to_willow_run_same_address_betternoi",
        ROOT
        / "bradley_willow_rebrand_lane"
        / "evidence_bradley_willow_275898_current_strict.json",
    ),
    (
        "dermot_220_east_72nd_page_published_nestio_current_e2e",
        ROOT
        / "rentcafe_residual_parallel"
        / "262799_current_configured_scrape_jugnu_e2e.json",
    ),
    (
        "park_northside_showmojo_current_configured_e2e",
        ROOT
        / "park_northside_showmojo_lane"
        / "independent_replay_admission_38378.json",
    ),
    (
        "1515_park_place_static_residence_current_configured_e2e",
        ROOT
        / "static_residence_lane"
        / "evidence_1515_park_place_261580_current_strict.json",
    ),
    (
        "onesite_workflow_and_rpfp_cws_current_configured_e2e",
        ROOT
        / "onesite_residual6_parallel"
        / "evidence_onesite_39995_43520_current_e2e.json",
    ),
    (
        "tor_view_static_team_roster_current_configured_e2e",
        ROOT
        / "onesite_residual6_parallel"
        / "evidence_tor_view_38677_page_local_e2e.json",
    ),
    (
        "annaberg_nesthub_current_configured_e2e",
        ROOT
        / "annaberg_nesthub_lane"
        / "evidence_annaberg_1765_nesthub_implementation_e2e.json",
    ),
    (
        "edgefield_onesite_compliant_hb_current_configured_e2e",
        ROOT
        / "edgefield_48075_hb_lane"
        / "evidence_edgefield_48075_configured_e2e.json",
    ),
    (
        "riverwalk_wimmer_stale_path_guard_sightmap_e2e",
        ROOT
        / "riverwalk_wimmer_lane"
        / "evidence_riverwalk_71534_stale_guard_e2e.json",
    ),
    (
        "vintage_grove_current_official_rentcafe_e2e",
        ROOT
        / "vintage_grove_migration_lane"
        / "evidence_vintage_grove_42554_current_strict.json",
    ),
    (
        "aster_current_appfolio_detail_parser_e2e",
        ROOT
        / "aster_appfolio_detail_lane"
        / "evidence_aster_274886_current_appfolio_e2e.json",
    ),
    (
        "acorn_current_rentvision_full_configured_e2e",
        ROOT
        / "acorn_56182_current_rentvision"
        / "evidence_acorn_56182_full_scrape_jugnu_3x.json",
    ),
]


def load_results(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
    else:
        nested_rows = payload.get("results") if isinstance(payload, dict) else None
        if (
            isinstance(payload, dict)
            and payload.get("lane")
            == "ollr_widgetloader_detector_current_source_validation"
            and isinstance(payload.get("results"), list)
        ):
            rows = []
            for property_row in payload["results"]:
                if not isinstance(property_row, dict):
                    continue
                current = property_row.get("current_source_full_pipeline") or {}
                samples = [
                    sample
                    for sample in (current.get("sample_units") or [])
                    if isinstance(sample, dict)
                ]
                sole_site_id = str(
                    property_row.get("sole_current_published_site_id") or ""
                )
                source_property_ids = [
                    str(value)
                    for value in (current.get("source_property_ids") or [])
                    if str(value).strip()
                ]
                strict_count = int(current.get("strict_native_priced_rows") or 0)
                distinct_count = int(current.get("distinct_unit_numbers") or 0)
                direct_inventory_page = (
                    str(current.get("configured_final_url") or "").rstrip("/")
                    == str(property_row.get("inventory_url") or "").rstrip("/")
                )
                strict_shape = (
                    property_row.get("outcome")
                    == "CURRENT_SOURCE_FULL_PIPELINE_UNIT_QUALIFIED"
                    and property_row.get("eligible_for_authoritative_ledger") is True
                    and property_row.get("same_origin_inventory_route") is True
                    and property_row.get("exact_source_property_binding") is True
                    and bool(sole_site_id)
                    and source_property_ids == [sole_site_id]
                    and current.get("adapter") == "onesite"
                    and current.get("tier") == "TIER_1_API_ONESITE_WORKFLOW"
                    and (current.get("link_hop_success") is True or direct_inventory_page)
                    and strict_count == distinct_count
                    and strict_count > 0
                    and len(samples) > 0
                    and all(
                        str(sample.get("unit_number") or "").strip()
                        and str(sample.get("source_property_id") or "")
                        == sole_site_id
                        and any(
                            isinstance(sample.get(key), (int, float))
                            and not isinstance(sample.get(key), bool)
                            and sample.get(key) > 0
                            for key in ("market_rent_low", "market_rent_high")
                        )
                        for sample in samples
                    )
                )
                source_urls = sorted(
                    {
                        str(sample.get("source_api_url") or "")
                        for sample in samples
                        if str(sample.get("source_api_url") or "").strip()
                    }
                )
                rows.append(
                    {
                        **property_row,
                        "outcome": (
                            "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED"
                        ),
                        "property_identity_match": strict_shape,
                        "contamination_verdict": (
                            "pass_exact_configured_property_same_origin_published_ollr_"
                            "sole_site_id_full_pipeline"
                            if strict_shape
                            else "reject_ollr_full_pipeline_strict_shape_incomplete"
                        ),
                        "units": strict_count,
                        "website": property_row.get("configured_url") or "",
                        "identity_evidence": {
                            "rows_with_native_identity": strict_count,
                            "rows_with_native_identity_and_positive_rent": strict_count,
                            "source_urls": source_urls,
                        },
                        "native_samples": [
                            {
                                "identity": {
                                    "unit_number": str(
                                        sample.get("unit_number") or ""
                                    )
                                },
                                "positive_rent_evidence": {
                                    key: sample.get(key)
                                    for key in (
                                        "market_rent_low",
                                        "market_rent_high",
                                    )
                                    if sample.get(key) not in (None, "", 0, 0.0)
                                },
                                "source_api_url": str(
                                    sample.get("source_api_url") or ""
                                ),
                            }
                            for sample in samples
                        ],
                    }
                )
        elif isinstance(payload, dict) and isinstance(payload.get("properties"), list):
            rows = []
            for property_row in payload["properties"]:
                if not isinstance(property_row, dict):
                    continue
                property_meta = property_row.get("property") or {}
                if not isinstance(property_meta, dict):
                    property_meta = {}
                native_rows = [
                    item
                    for item in (property_row.get("native_rows") or [])
                    if isinstance(item, dict)
                ]
                gates = property_row.get("strict_gates") or {}
                native_count = int(
                    gates.get("rows_with_real_native_anchor")
                    or gates.get("rows_with_native_identity")
                    or gates.get("rows_with_native_unit_number")
                    or 0
                )
                priced_count = int(gates.get("rows_with_positive_rent") or 0)
                strict_shape = (
                    property_row.get("property_identity_match") is True
                    and str(property_row.get("contamination_verdict") or "").startswith("pass_")
                    and len(native_rows) > 0
                    and native_count == priced_count == len(native_rows)
                )
                rows.append(
                    {
                        **property_row,
                        "property_id": (
                            property_row.get("property_id")
                            or property_meta.get("property_id")
                            or property_meta.get("apartmentid")
                        ),
                        "property_name": (
                            property_row.get("property_name")
                            or property_meta.get("property_name")
                            or property_meta.get("name")
                        ),
                        "website": (
                            property_row.get("website")
                            or property_meta.get("website")
                        ),
                        "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                        "units": len(native_rows),
                        "identity_evidence": {
                            "rows_with_native_identity": native_count,
                            "rows_with_native_identity_and_positive_rent": priced_count,
                            "source_urls": property_row.get("source_urls") or [],
                        },
                        "native_samples": [
                            {
                                "identity": {
                                    "unit_number": str(item.get("unit_number") or ""),
                                    "entrata_uid": str(
                                        (item.get("source_ids") or {}).get("entrata_uid")
                                        if isinstance(item.get("source_ids"), dict)
                                        else ""
                                    ),
                                },
                                "positive_rent_evidence": {
                                    key: item.get(key)
                                    for key in ("market_rent_low", "market_rent_high", "rent")
                                    if item.get(key) not in (None, "", 0, 0.0)
                                },
                                "source_api_url": str(item.get("source_api_url") or ""),
                            }
                            for item in native_rows[:5]
                        ],
                    }
                )
        elif isinstance(payload, dict) and isinstance(payload.get("qualifying_results"), list):
            rows = []
            for row in payload["qualifying_results"]:
                if not isinstance(row, dict):
                    continue
                priced_rows = [
                    item
                    for item in (row.get("native_priced_rows") or [])
                    if isinstance(item, dict)
                ]
                boundary = row.get("property_boundary_evidence") or {}
                native_count = int(
                    row.get("distinct_native_unit_count")
                    or boundary.get("native_identity_rows")
                    or 0
                )
                priced_count = int(
                    row.get("native_priced_unit_count")
                    or boundary.get("native_positive_rent_rows")
                    or 0
                )
                rows.append(
                    {
                        **row,
                        "property_identity_match": True,
                        "units": priced_count,
                        "identity_evidence": {
                            "rows_with_native_identity": native_count,
                            "rows_with_native_identity_and_positive_rent": priced_count,
                            "source_urls": row.get("source_urls") or [],
                        },
                        "native_samples": [
                            {
                                "identity": {
                                    "unit_number": str(item.get("unit_number") or "")
                                },
                                "positive_rent_evidence": {
                                    key: item.get(key)
                                    for key in ("market_rent_low", "market_rent_high", "rent")
                                    if item.get(key) not in (None, "", 0, 0.0)
                                },
                                "source_api_url": str(item.get("source_api_url") or ""),
                            }
                            for item in priced_rows
                        ],
                    }
                )
            rows.extend(
                row
                for row in (payload.get("excluded_results") or [])
                if isinstance(row, dict)
            )
        elif isinstance(payload, dict) and isinstance(payload.get("e2e_candidates"), list):
            rows = []
            for candidate in payload["e2e_candidates"]:
                if not isinstance(candidate, dict):
                    continue
                adapter_e2e = candidate.get("adapter_e2e") or {}
                unit_samples = [
                    item
                    for item in (candidate.get("unit_samples") or [])
                    if isinstance(item, dict)
                ]
                native_count = int(candidate.get("native_identity_rows") or 0)
                priced_count = int(candidate.get("native_positive_rent_rows") or 0)
                declared_units = int(candidate.get("units") or 0)
                strict_shape = (
                    candidate.get("strict_pass") is True
                    and candidate.get("property_identity_match") is True
                    and str(candidate.get("contamination_verdict") or "").startswith("pass_")
                    and adapter_e2e.get("passed") is True
                    and int(adapter_e2e.get("strict_native_positive_rent_rows") or 0)
                    == native_count
                    == priced_count
                    == declared_units
                    and declared_units > 0
                    and len(unit_samples) > 0
                )
                rows.append(
                    {
                        **candidate,
                        "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                        "identity_evidence": {
                            "rows_with_native_identity": native_count,
                            "rows_with_native_identity_and_positive_rent": priced_count,
                            "source_urls": candidate.get("source_urls") or [],
                        },
                        "native_samples": [
                            {
                                "identity": {
                                    "unit_number": str(item.get("unit_number") or ""),
                                    "securecafe_apartment_id": str(
                                        (item.get("source_ids") or {}).get(
                                            "securecafe_apartment_id"
                                        )
                                        or ""
                                    ),
                                },
                                "positive_rent_evidence": {
                                    key: item.get(key)
                                    for key in ("market_rent_low", "market_rent_high", "rent")
                                    if item.get(key) not in (None, "", 0, 0.0)
                                },
                                "source_api_url": str(item.get("source_api_url") or ""),
                            }
                            for item in unit_samples
                        ],
                    }
                )
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "unknown_wix_residual_current_configured_scrape_jugnu_e2e"
        ):
            configured = payload.get("configured_route") or {}
            control = payload.get("published_application_route_control") or {}
            conclusion = payload.get("conclusion") or {}
            guardrails = payload.get("guardrails") or {}
            identity = payload.get("canonical_identity") or {}
            official = configured.get("official_application_onesite") or {}
            roster_gate = official.get("roster_gate") or {}
            roster_checks = roster_gate.get("checks") or {}
            site_id = str(official.get("site_id") or "")
            unit_ids = [
                str(value).strip()
                for value in (configured.get("native_ids") or [])
                if str(value).strip()
            ]
            samples = [
                sample
                for sample in (configured.get("samples") or [])
                if isinstance(sample, dict)
            ]
            source_urls = [
                str(value)
                for value in (configured.get("source_api_urls") or [])
                if str(value).strip()
            ]
            units = int(configured.get("units") or 0)
            identity_checks = official.get("application_identity_checks") or {}
            portal_identity_checks = official.get("portal_identity_checks") or {}
            strict_shape = bool(
                conclusion.get("configured_route_strict_accept") is True
                and conclusion.get("published_application_route_strict_accept") is True
                and conclusion.get("navigation_gap") is False
                and configured.get("strict_accept") is True
                and control.get("strict_accept") is True
                and configured.get("adapter") == "onesite"
                and configured.get("current_detected_pms") == "onesite"
                and configured.get("tier") == "TIER_1_API_ONESITE_WORKFLOW"
                and configured.get("link_hop_success") is True
                and configured.get("official_application_onesite_success") is True
                and not str(configured.get("exception") or "")
                and (configured.get("configured_fetch") or {}).get("status") == 200
                and (configured.get("configured_fetch") or {}).get("outcome") == "OK"
                and official.get("accepted") is True
                and official.get("reason") == "exact_application_onesite_chain"
                and official.get("roster_accepted") is True
                and bool(site_id)
                and official.get("portal_site_ids") == [site_id]
                and configured.get("source_property_ids") == [site_id]
                and configured.get("source_api_site_ids") == [site_id]
                and all(identity_checks.values())
                and len(identity_checks) >= 5
                and all(portal_identity_checks.values())
                and len(portal_identity_checks) >= 5
                and all(roster_checks.values())
                and len(roster_checks) >= 6
                and units > 0
                and int(configured.get("strict_native_positive_rent_rows") or 0)
                == int(roster_gate.get("row_count") or 0)
                == int(roster_gate.get("distinct_native_unit_ids") or 0)
                == units
                and len(unit_ids) == len(set(unit_ids)) == units
                and roster_gate.get("source_property_ids") == [site_id]
                and roster_gate.get("source_api_site_ids") == [site_id]
                and source_urls
                and all(f"/v1/{site_id}/" in value for value in source_urls)
                and samples
                and all(
                    str(sample.get("unit_number") or "").strip()
                    and str(sample.get("source_property_id") or "") == site_id
                    and f"/v1/{site_id}/" in str(sample.get("source_api_url") or "")
                    and any(
                        isinstance(sample.get(key), (int, float))
                        and not isinstance(sample.get(key), bool)
                        and sample.get(key) > 0
                        for key in ("market_rent_low", "market_rent_high")
                    )
                    for sample in samples
                )
                and str(identity.get("apartmentid") or "")
                == str(payload.get("property_id") or "")
                and payload.get("cohort_guard", {}).get(
                    "not_already_in_strict_ledger"
                )
                is True
                and guardrails.get("compliance_mode") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("flaresolverr") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("web_unlocker") is False
                and guardrails.get("paid_canary") is False
                and guardrails.get("llm") is False
            )
            rows = [
                {
                    **payload,
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_official_application_onesite_chain_native_roster"
                        if strict_shape
                        else "reject_official_application_onesite_strict_shape_incomplete"
                    ),
                    "website": identity.get("website") or "",
                    "units": units if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": units if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            units if strict_shape else 0
                        ),
                        "source_urls": source_urls,
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(sample.get("unit_number") or "")
                            },
                            "positive_rent_evidence": {
                                key: sample.get(key)
                                for key in ("market_rent_low", "market_rent_high")
                                if sample.get(key) not in (None, "", 0, 0.0)
                            },
                            "source_api_url": str(sample.get("source_api_url") or ""),
                        }
                        for sample in samples
                    ],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "acorn_current_rentvision_full_configured_e2e"
        ):
            configured_url = "https://www.liveatacornacres.com/"
            floorplans_url = "https://www.liveatacornacres.com/floorplans"
            detail_prefix = f"{floorplans_url}/"
            expected_config_row = {
                "apartmentid": "56182",
                "name": "Acorn Acres",
                "address": "3605 Brandywine Ct",
                "city": "Lafayette",
                "state": "IN",
                "zip": "47905",
                "website": configured_url,
            }
            expected_identity = {
                key: expected_config_row[key]
                for key in ("address", "city", "state", "zip")
            }
            expected_environment = {
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
            expected_guardrails = {
                "direct_only": True,
                "captcha_solving": False,
                "web_unlocker": False,
                "flaresolverr": False,
                "fingerprint_rotation": False,
                "hyperbrowser": False,
                "llm": False,
                "paid_canary": False,
                "environment": expected_environment,
            }
            expected_source_hashes = {
                "ma_poc/pms/adapters/rentvision.py": (
                    "2e64e24088f4ed63bb25fbda19cd328a901eaa2e3f72dc197197a6d86540ad35"
                ),
                "ma_poc/pms/scraper.py": (
                    "53601150274be6cba79388ef46c380d972b2a0decb9af4702f5fc581e009164c"
                ),
            }
            expected_identity_checks = {
                "exact_property_name_visible": True,
                "street_number_and_name_visible": True,
                "city_visible": True,
                "zip_visible": True,
                "configured_host_preserved": True,
            }
            expected_units = {
                "3717D",
                "3864D",
                "3805",
                "2575F",
                "3601E",
                "3886",
            }
            expected_signature = (
                "09421528cb9e416abf0a24cd2ff60a644550197fe3e437c44b5ec97b58d18bf0"
            )
            expected_route = (
                "fresh configured GET -> scrape_jugnu(page=None) -> "
                "RentVisionAdapter -> /floorplans -> bounded detail-page drill"
            )
            repo_root = Path("/Users/ankur/PropAi-codex-failed-no-data")
            config_path = repo_root / "ma_poc/config/properties.csv"
            config_rows: list[dict[str, str]] = []
            if config_path.is_file():
                with config_path.open(newline="", encoding="utf-8-sig") as handle:
                    config_rows = [
                        item
                        for item in csv.DictReader(handle)
                        if item.get("apartmentid") == "56182"
                    ]

            source_snapshot = payload.get("source_snapshot") or {}
            source_hashes_match = bool(
                source_snapshot == expected_source_hashes
                and all(
                    (repo_root / relative).is_file()
                    and hashlib.sha256(
                        (repo_root / relative).read_bytes()
                    ).hexdigest()
                    == expected_hash
                    for relative, expected_hash in expected_source_hashes.items()
                )
            )
            materializer = payload.get("materializer") or {}
            materializer_path = Path(str(materializer.get("path") or ""))
            materializer_matches = bool(
                str(materializer_path)
                == (
                    "/private/tmp/propai-fnd-vBkmT9/"
                    "acorn_56182_current_rentvision/"
                    "materialize_acorn_56182_full_e2e.py"
                )
                and materializer_path.is_file()
                and hashlib.sha256(materializer_path.read_bytes()).hexdigest()
                == "a400ada2842e38b9b275521c55e36ba4a8a10399afa53b477b4e1fc066009acd"
                == str(materializer.get("sha256") or "")
            )
            artifact_matches = bool(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == "915ceca06b63dc2ac3753a73b47f7003732f36f718208c5bec14a624f7bc4506"
            )
            repeats = [
                item
                for item in (payload.get("repeats") or [])
                if isinstance(item, dict)
            ]

            def acorn_repeat_matches(item: dict[str, Any]) -> bool:
                configured_fetch = item.get("configured_fetch") or {}
                trace = item.get("trace") or {}
                adapter_trace = trace.get("adapter_extract") or {}
                floorplans_trace = trace.get("floorplans_fetch") or {}
                detail_trace = trace.get("detail_fetch") or {}
                detail_urls = detail_trace.get("urls") or []
                body_bytes = detail_trace.get("body_bytes") or []
                samples = [
                    sample
                    for sample in (item.get("samples") or [])
                    if isinstance(sample, dict)
                ]
                sample_numbers = [
                    str(sample.get("unit_number") or "") for sample in samples
                ]
                sample_dates: list[date] = []
                try:
                    sample_dates = [
                        date.fromisoformat(str(sample.get("availability_date") or ""))
                        for sample in samples
                    ]
                except ValueError:
                    return False
                return bool(
                    item.get("configured_identity_checks")
                    == expected_identity_checks
                    and configured_fetch.get("status") == 200
                    and configured_fetch.get("final_url") == configured_url
                    and int(configured_fetch.get("body_bytes") or 0) > 100_000
                    and len(str(configured_fetch.get("body_sha256") or "")) == 64
                    and item.get("detected_pms") == "rentvision"
                    and item.get("adapter") == "rentvision"
                    and item.get("fallback_chain") == ["rentvision"]
                    and item.get("tier") == "TIER_3_DOM_RENTVISION_UNIT_LEVEL"
                    and int(item.get("units") or 0) == 6
                    and int(item.get("strict_native_positive_rent_rows") or 0) == 6
                    and int(item.get("plans") or 0) == 0
                    and item.get("errors") == []
                    and item.get("unit_signature_sha256") == expected_signature
                    and set(trace)
                    == {"adapter_extract", "floorplans_fetch", "detail_fetch"}
                    and adapter_trace.get("tier")
                    == "TIER_3_DOM_RENTVISION_UNIT_LEVEL"
                    and int(adapter_trace.get("units") or 0) == 6
                    and int(adapter_trace.get("plans") or 0) == 0
                    and adapter_trace.get("errors") == []
                    and adapter_trace.get("winning_url") == floorplans_url
                    and floorplans_trace.get("url") == floorplans_url
                    and int(floorplans_trace.get("bytes") or 0) > 400_000
                    and len(str(floorplans_trace.get("sha256") or "")) == 64
                    and int(detail_trace.get("requested") or 0) == 8
                    and int(detail_trace.get("returned_nonempty") or 0) == 8
                    and int(detail_trace.get("max_concurrency") or 0) == 8
                    and len(detail_urls) == 8
                    and len(set(str(url) for url in detail_urls)) == 8
                    and all(str(url).startswith(detail_prefix) for url in detail_urls)
                    and len(body_bytes) == 8
                    and all(int(value or 0) > 100_000 for value in body_bytes)
                    and len(samples) == 6
                    and len(set(sample_numbers)) == 6
                    and set(sample_numbers) == expected_units
                    and all(
                        isinstance(sample.get("market_rent_low"), (int, float))
                        and not isinstance(sample.get("market_rent_low"), bool)
                        and float(sample["market_rent_low"]) > 0
                        and sample.get("market_rent_low")
                        == sample.get("market_rent_high")
                        and str(sample.get("floor_plan_name") or "").strip()
                        and str(sample.get("bedrooms") or "").strip()
                        and str(sample.get("sqft") or "").strip()
                        and sample.get("availability_date")
                        == sample.get("available_date")
                        and sample.get("availability_status") == "AVAILABLE"
                        and str(sample.get("source_api_url") or "").startswith(
                            detail_prefix
                        )
                        and sample.get("extraction_tier")
                        == "TIER_3_DOM_RENTVISION_UNIT_LEVEL"
                        for sample in samples
                    )
                    and len(sample_dates) == 6
                    and all(value >= date(2026, 8, 1) for value in sample_dates)
                )

            strict_shape = bool(
                artifact_matches
                and payload.get("cohort")
                == "exact_2026-07-31_FAILED_NO_DATA_344"
                and payload.get("property_id") == 56182
                and payload.get("property_name") == "Acorn Acres"
                and payload.get("configured_url") == configured_url
                and payload.get("configured_identity") == expected_identity
                and payload.get("route") == expected_route
                and payload.get("guardrails") == expected_guardrails
                and payload.get("stable") is True
                and config_rows == [expected_config_row]
                and source_hashes_match
                and materializer_matches
                and len(repeats) == 3
                and [int(item.get("repeat") or 0) for item in repeats] == [1, 2, 3]
                and all(acorn_repeat_matches(item) for item in repeats)
            )
            samples = [
                sample
                for sample in ((repeats[0].get("samples") or []) if repeats else [])
                if isinstance(sample, dict)
            ]
            source_urls = sorted(
                {str(sample.get("source_api_url") or "") for sample in samples}
            )
            rows = [
                {
                    "property_id": "56182",
                    "property_name": "Acorn Acres",
                    "website": configured_url,
                    "outcome": (
                        "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED"
                    ),
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_configured_exact_name_address_current_official_"
                        "rentvision_native_units_positive_rent_three_repeats"
                        if strict_shape
                        else "reject_acorn_identity_route_or_native_shape_incomplete"
                    ),
                    "units": 6 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 6 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            6 if strict_shape else 0
                        ),
                        "source_urls": source_urls if strict_shape else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(sample.get("unit_number") or "")
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": sample.get("market_rent_low"),
                                "market_rent_high": sample.get("market_rent_high"),
                            },
                            "availability_date": sample.get("availability_date"),
                            "source_api_url": sample.get("source_api_url"),
                        }
                        for sample in samples
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "aster_current_appfolio_detail_parser_e2e"
        ):
            configured_url = (
                "https://parkplace380.com/listings/aster-village-two/"
            )
            syndication_url = (
                "https://www.showmetherent.com/listing/details/"
                "1251-Aster-Drive-Tiffin-IA-52340/"
                "4de2d364-61bc-11eb-abab-0efd77e47219"
            )
            application_uid = "13c06338-c373-42ae-a236-8687d24c30ad"
            application_url = (
                "https://thedersgrp.appfolio.com/listings/"
                "rental_applications/new?listable_uid="
                f"{application_uid}&source=Show+Me+The+Rent"
            )
            detail_url = (
                "https://thedersgrp.appfolio.com/listings/detail/"
                f"{application_uid}"
            )
            expected_identity = {
                "apartmentid": "274886",
                "name": "Aster Village Two",
                "address": "1251 Aster Dr",
                "city": "Tiffin",
                "state": "IA",
                "zip": "52340",
                "website": configured_url,
            }
            expected_environment = {
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
            expected_source_hashes = {
                "ma_poc/core/identity.py": (
                    "7215ed6a3b74ad5c4741d32a20142dee64e43faee79740660f31ddf7f1487167"
                ),
                "ma_poc/pms/adapters/appfolio.py": (
                    "f8500b9c2047df55a887dc8ee8dcf8eb505571eb7ca30a3862a1244f986b9655"
                ),
                "ma_poc/pms/detector.py": (
                    "48ebf6f88454f0ef71d557d00d1d879dcac62a5aaea727d584a7b16520f8706a"
                ),
                "ma_poc/pms/scraper.py": (
                    "53601150274be6cba79388ef46c380d972b2a0decb9af4702f5fc581e009164c"
                ),
            }
            expected_identity_checks = {
                "configured_exact_manager": True,
                "configured_exact_property_name": True,
                "official_detail_exact_address": True,
                "syndication_exact_application_uid": True,
                "syndication_exact_name_address": True,
                "syndication_one_available_unit": True,
            }
            expected_repeat_checks = {
                "appfolio_adapter": True,
                "appfolio_detected": True,
                "detail_tier": True,
                "exact_address": True,
                "exact_source": True,
                "native_uid": True,
                "native_unit_108": True,
                "no_pipeline_errors": True,
                "operator_date": True,
                "physical_fields": True,
                "positive_rent": True,
            }
            expected_unit = {
                "availability_date": "2026-10-10",
                "availability_status": "AVAILABLE",
                "bathrooms": "1",
                "bedrooms": "1",
                "floor_plan_name": "",
                "market_rent_high": 1365,
                "market_rent_low": 1365,
                "source_api_url": detail_url,
                "source_ids": {"appfolio_listable_uid": application_uid},
                "sqft": "821",
                "unit_id": "1251-aster-drive-108-108-tiffin-ia-52340",
                "unit_name": (
                    "1251 Aster Drive - 108, 108, Tiffin, IA 52340"
                ),
                "unit_number": "108",
            }
            expected_verdict = (
                "pass_configured_exact_name_manager_to_appfolio_owned_"
                "syndication_exact_name_address_application_uid_to_official_"
                "detail_exact_address_native_unit_positive_rent_three_repeats"
            )

            repo_root = Path("/Users/ankur/PropAi-codex-failed-no-data")
            config_rows: list[dict[str, str]] = []
            config_path = repo_root / "ma_poc/config/properties.csv"
            if config_path.is_file():
                with config_path.open(newline="", encoding="utf-8-sig") as handle:
                    config_rows = [
                        item
                        for item in csv.DictReader(handle)
                        if item.get("apartmentid") == "274886"
                    ]
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}
            source_hashes_match = bool(
                source_before == source_after == expected_source_hashes
                and all(
                    (repo_root / relative).is_file()
                    and hashlib.sha256(
                        (repo_root / relative).read_bytes()
                    ).hexdigest()
                    == expected_hash
                    for relative, expected_hash in expected_source_hashes.items()
                )
            )
            materializer = payload.get("materializer") or {}
            materializer_path = Path(str(materializer.get("path") or ""))
            materializer_matches = bool(
                str(materializer_path)
                == (
                    "/private/tmp/propai-fnd-vBkmT9/aster_appfolio_detail_lane/"
                    "materialize_aster_274886_current_appfolio_e2e.py"
                )
                and materializer_path.is_file()
                and hashlib.sha256(materializer_path.read_bytes()).hexdigest()
                == "974c470abf60007493c6d862d58bb01a5c82bfd3b0f5475f6d42d0962c471fbb"
                == str(materializer.get("sha256") or "")
            )
            identity_chain = payload.get("identity_chain") or {}
            http_evidence = payload.get("http_evidence") or {}
            guardrails = payload.get("guardrails") or {}
            controls = [
                item
                for item in (payload.get("current_template_cluster_controls") or [])
                if isinstance(item, dict)
            ]
            repeats = [
                item
                for item in (payload.get("three_full_pipeline_repeats") or [])
                if isinstance(item, dict)
            ]
            result_rows = [
                item
                for item in (payload.get("results") or [])
                if isinstance(item, dict)
            ]
            first_result = result_rows[0] if len(result_rows) == 1 else {}
            first_evidence = first_result.get("identity_evidence") or {}
            first_samples = [
                item
                for item in (first_result.get("native_samples") or [])
                if isinstance(item, dict)
            ]

            expected_controls = {
                "cross": ("3", "2", "1180", 1375, "2026-08-10"),
                "terracemgmt": ("2", "2", "934", 1150, ""),
                "equilibrium": ("4", "1.5", "1400", 1695, ""),
            }
            controls_match = bool(
                len(controls) == 3
                and {str(item.get("label") or "") for item in controls}
                == set(expected_controls)
                and all(
                    item.get("status") == 200
                    and int(item.get("body_bytes") or 0) > 25_000
                    and str(item.get("url") or "").startswith("https://")
                    and ".appfolio.com/listings/detail/" in str(item.get("url") or "")
                    and tuple(item.get("observed") or ())
                    == expected_controls[str(item.get("label") or "")]
                    for item in controls
                )
            )
            http_match = bool(
                set(http_evidence)
                == {"configured", "appfolio_owned_syndication", "official_detail"}
                and http_evidence.get("configured", {}).get("status") == 200
                and int(http_evidence.get("configured", {}).get("body_bytes") or 0)
                > 400_000
                and http_evidence.get("appfolio_owned_syndication", {}).get("status")
                == 200
                and int(
                    http_evidence.get("appfolio_owned_syndication", {}).get(
                        "body_bytes"
                    )
                    or 0
                )
                > 40_000
                and http_evidence.get("official_detail", {}).get("status") == 200
                and int(
                    http_evidence.get("official_detail", {}).get("body_bytes") or 0
                )
                > 30_000
                and all(
                    len(str(item.get("body_sha256") or "")) == 64
                    for item in http_evidence.values()
                    if isinstance(item, dict)
                )
            )
            repeats_match = bool(
                len(repeats) == 3
                and [int(item.get("repeat") or 0) for item in repeats]
                == [1, 2, 3]
                and all(
                    item.get("checks") == expected_repeat_checks
                    and item.get("errors") == []
                    and item.get("unit") == expected_unit
                    for item in repeats
                )
            )
            strict_shape = bool(
                payload.get("cohort") == "exact_2026-07-31_FAILED_NO_DATA_344"
                and payload.get("ledger_mutation") == "none"
                and payload.get("commit") == "none"
                and payload.get("push") == "none"
                and payload.get("paid_canary") is False
                and payload.get("configured_identity") == expected_identity
                and config_rows == [expected_identity]
                and guardrails.get("environment") == expected_environment
                and guardrails.get("direct_public_http_only") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and int(guardrails.get("hyperbrowser_calls") or 0) == 0
                and int(guardrails.get("llm_calls") or 0) == 0
                and int(guardrails.get("proxy_calls") or 0) == 0
                and int(guardrails.get("web_unlocker_calls") or 0) == 0
                and int(guardrails.get("flaresolverr_calls") or 0) == 0
                and source_hashes_match
                and materializer_matches
                and controls_match
                and http_match
                and identity_chain.get("configured_url") == configured_url
                and identity_chain.get("appfolio_owned_syndication_url")
                == syndication_url
                and identity_chain.get("exact_application_url") == application_url
                and identity_chain.get("official_detail_url") == detail_url
                and identity_chain.get("official_detail_title")
                == "1251 Aster Drive - 108, 108, Tiffin, IA 52340 MAP"
                and identity_chain.get("published_listing_title")
                == "Village Two - One Bedroom Style A"
                and identity_chain.get("checks") == expected_identity_checks
                and repeats_match
                and first_result.get("property_id") == 274886
                and first_result.get("property_name") == "Aster Village Two"
                and first_result.get("website") == configured_url
                and first_result.get("outcome") == "UNIT_QUALIFIED"
                and first_result.get("property_identity_match") is True
                and first_result.get("contamination_verdict") == expected_verdict
                and first_result.get("adapter") == "appfolio"
                and first_result.get("tier") == "TIER_1_DOM_APPFOLIO_DETAIL"
                and int(first_result.get("units") or 0) == 1
                and first_evidence.get("checks") == expected_identity_checks
                and int(first_evidence.get("native_identity_rows") or 0) == 1
                and int(first_evidence.get("native_positive_rent_rows") or 0) == 1
                and first_evidence.get("source_urls") == [detail_url]
                and first_samples == [expected_unit]
            )
            rows = [
                {
                    "property_id": "274886",
                    "property_name": "Aster Village Two",
                    "website": configured_url,
                    "outcome": (
                        "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED"
                    ),
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        expected_verdict
                        if strict_shape
                        else "reject_aster_identity_or_native_shape_incomplete"
                    ),
                    "units": 1 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 1 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            1 if strict_shape else 0
                        ),
                        "source_urls": [detail_url] if strict_shape else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": "108",
                                "appfolio_listable_uid": application_uid,
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": 1365,
                                "market_rent_high": 1365,
                            },
                            "availability_date": "2026-10-10",
                            "source_api_url": detail_url,
                        }
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "wimmer_stale_missing_state_fail_closed_e2e"
        ):
            stale_url = (
                "https://www.wimmercommunities.com/apartments/"
                "menomonee-falls/riverwalk-on-the-falls/"
            )
            exact_floorplans_url = (
                "https://www.wimmercommunities.com/apartments/wi/"
                "menomonee-falls/riverwalk-on-the-falls/floorplans"
            )
            source_api_url = (
                "https://sightmap.com/app/api/v1/y8px5ljmv19/"
                "sightmaps/100325"
            )
            expected_units = {"103", "201", "305", "310", "324", "403", "417", "419"}
            expected_environment = {
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
            expected_source_hashes = {
                "ma_poc/core/identity.py": (
                    "7215ed6a3b74ad5c4741d32a20142dee64e43faee79740660f31ddf7f1487167"
                ),
                "ma_poc/pms/adapters/sightmap.py": (
                    "afcbed2637ac3aa44113fb973726c3afb780208c37de55c4bb984749b420e3fa"
                ),
                "ma_poc/pms/detector.py": (
                    "48ebf6f88454f0ef71d557d00d1d879dcac62a5aaea727d584a7b16520f8706a"
                ),
                "ma_poc/pms/scraper.py": (
                    "53601150274be6cba79388ef46c380d972b2a0decb9af4702f5fc581e009164c"
                ),
            }
            expected_checks = {
                "all_availability_dates": True,
                "all_floorplan_names": True,
                "eight_distinct_strict_units": True,
                "exact_guard_anchor": True,
                "exact_source_api": True,
                "guard_identity_match": True,
                "guard_portfolio_fallbacks_disabled": True,
                "link_hop_succeeded": True,
                "only_exact_candidate_fetched": True,
                "sightmap_adapter": True,
                "soft_404_entered_pipeline": True,
                "tier_1_sightmap": True,
            }
            allowed_entry_diagnostics = [
                "RENTCAFE_NO_RESPONSE: no network responses captured during page load"
            ]
            guardrails = payload.get("guardrails") or {}
            configured_route = payload.get("configured_route") or {}
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}
            materializer = payload.get("materializer") or {}
            repeats = [
                item
                for item in (
                    payload.get("three_full_configured_url_repeats") or []
                )
                if isinstance(item, dict)
            ]

            repo_root = Path("/Users/ankur/PropAi-codex-failed-no-data")
            materializer_path = Path(str(materializer.get("path") or ""))
            config_rows: list[dict[str, str]] = []
            config_path = repo_root / "ma_poc/config/properties.csv"
            if config_path.is_file():
                with config_path.open(newline="", encoding="utf-8-sig") as handle:
                    config_rows = [
                        item
                        for item in csv.DictReader(handle)
                        if item.get("apartmentid") == "71534"
                    ]
            exact_config_identity = bool(
                len(config_rows) == 1
                and config_rows[0]
                == {
                    "apartmentid": "71534",
                    "name": "Riverwalk on the Falls I",
                    "address": "W165 N8910 Grand Ave",
                    "city": "Menomonee Falls",
                    "state": "WI",
                    "zip": "53051",
                    "website": stale_url,
                }
            )
            source_hashes_match = bool(
                source_before == source_after == expected_source_hashes
                and all(
                    (repo_root / relative).is_file()
                    and hashlib.sha256(
                        (repo_root / relative).read_bytes()
                    ).hexdigest()
                    == expected_hash
                    for relative, expected_hash in expected_source_hashes.items()
                )
            )
            materializer_matches = bool(
                materializer_path.is_file()
                and str(materializer_path)
                == (
                    "/private/tmp/propai-fnd-vBkmT9/riverwalk_wimmer_lane/"
                    "materialize_riverwalk_71534_stale_guard_e2e.py"
                )
                and hashlib.sha256(materializer_path.read_bytes()).hexdigest()
                == "3c1489be27c44d6e620cd0a6b8c47ebd8a4fa749e3f03c9350a25492bba0362f"
                == str(materializer.get("sha256") or "")
            )

            def strict_wimmer_repeat(item: dict[str, Any]) -> bool:
                numbers = [str(value) for value in (item.get("unit_numbers") or [])]
                return bool(
                    item.get("checks") == expected_checks
                    and item.get("candidate_requests") == [exact_floorplans_url]
                    and item.get("adapter") == "sightmap"
                    and item.get("tier") == "TIER_1_API_SIGHTMAP_IFRAME"
                    and item.get("guard")
                    == {
                        "entry_url": stale_url,
                        "exact_floorplans_url": exact_floorplans_url,
                        "identity_match": True,
                        "portfolio_fallbacks_disabled": True,
                    }
                    and int(item.get("emitted_rows") or 0) == 8
                    and int(item.get("strict_native_positive_rent_rows") or 0) == 8
                    and len(numbers) == len(set(numbers)) == 8
                    and set(numbers) == expected_units
                    and item.get("source_urls") == [source_api_url]
                    and item.get("entry_page_diagnostics")
                    == allowed_entry_diagnostics
                )

            strict_shape = bool(
                payload.get("cohort") == "exact_2026-07-31_FAILED_NO_DATA_344"
                and payload.get("property_id") == 71534
                and payload.get("property_name") == "Riverwalk on the Falls I"
                and payload.get("configured_url") == stale_url
                and payload.get("expected_exact_floorplans_url")
                == exact_floorplans_url
                and payload.get("outcome") == "UNIT_QUALIFIED"
                and payload.get("ledger_mutation") == "none"
                and payload.get("commit") == "none"
                and payload.get("push") == "none"
                and payload.get("paid_canary") is False
                and exact_config_identity
                and guardrails.get("environment") == expected_environment
                and guardrails.get("direct_public_http_only") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and int(guardrails.get("hyperbrowser_calls") or 0) == 0
                and int(guardrails.get("llm_calls") or 0) == 0
                and int(guardrails.get("proxy_calls") or 0) == 0
                and int(guardrails.get("web_unlocker_calls") or 0) == 0
                and int(guardrails.get("flaresolverr_calls") or 0) == 0
                and configured_route.get("status") == 404
                and configured_route.get("final_url") == stale_url
                and int(configured_route.get("body_bytes") or 0) > 80_000
                and len(str(configured_route.get("body_sha256") or "")) == 64
                and source_hashes_match
                and materializer_matches
                and len(repeats) == 3
                and [int(item.get("repeat") or 0) for item in repeats]
                == [1, 2, 3]
                and all(strict_wimmer_repeat(item) for item in repeats)
                and all(
                    item.get("unit_numbers") == repeats[0].get("unit_numbers")
                    for item in repeats
                )
                and payload.get("same_eight_units_each_repeat") is True
            )
            rows = [
                {
                    "property_id": "71534",
                    "property_name": "Riverwalk on the Falls I",
                    "website": stale_url,
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_wimmer_missing_state_path_fail_closed_"
                        "single_candidate_sightmap_three_configured_repeats"
                        if strict_shape
                        else "reject_wimmer_stale_path_guard_or_native_shape_incomplete"
                    ),
                    "units": 8 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 8 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            8 if strict_shape else 0
                        ),
                        "source_urls": [source_api_url] if strict_shape else [],
                    },
                    "native_samples": [
                        {
                            "identity": {"unit_number": number},
                            "source_api_url": source_api_url,
                        }
                        for number in (
                            repeats[0].get("unit_numbers") if repeats else []
                        )
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "vintage_grove_stale_summerwind_current_official_rentcafe_e2e"
        ):
            configured_url = "http://summerwindaptsfl.com/"
            current_url = "https://vintagegroveapts.com/"
            portal_url = (
                "https://vintagegroveapts.securecafe.com/onlineleasing/"
                "summerwind-apartments0/floorplans.aspx"
            )
            source_api_url = (
                "https://vintagegroveapts.securecafeapplicant.com/"
                "onlineleasing/api/floorplan/getfloorplanandavailableunits?"
                "propertyId=1722315&RequestBeforeLogin=true&isPropertyList=false"
            )
            expected_configured_identity = {
                "apartmentid": "42554",
                "name": "Vintage Grove Apartments",
                "address": "5262 Timuquana Rd",
                "city": "Jacksonville",
                "state": "FL",
                "zip": "32210",
                "website": configured_url,
            }
            expected_current_identity = {
                "name": "Vintage Grove Apartments",
                "address": "5262 Timuquana Rd",
                "city": "Jacksonville",
                "state": "FL",
                "zip": "32210",
                "website": current_url,
            }
            expected_environment = {
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
            expected_source_hashes = {
                "ma_poc/core/identity.py": (
                    "7215ed6a3b74ad5c4741d32a20142dee64e43faee79740660f31ddf7f1487167"
                ),
                "ma_poc/pms/adapters/rentcafe.py": (
                    "78527dd59228dbf978c29674ac34cc9c77ce9844a13425a2defddf36af4cf13a"
                ),
                "ma_poc/pms/detector.py": (
                    "48ebf6f88454f0ef71d557d00d1d879dcac62a5aaea727d584a7b16520f8706a"
                ),
                "ma_poc/pms/scraper.py": (
                    "53601150274be6cba79388ef46c380d972b2a0decb9af4702f5fc581e009164c"
                ),
            }
            expected_root_checks = {
                "all_securecafe_links_same_property_slug": True,
                "city_visible": True,
                "property_name_visible": True,
                "sole_published_securecafe_floorplan_route": True,
                "state_visible": True,
                "street_visible": True,
                "zip_visible": True,
            }
            expected_provider_checks = {
                "all_source_addresses_exact_property": True,
                "all_source_property_ids_exact": True,
                "all_source_property_names_exact": True,
                "all_source_urls_exact": True,
            }
            expected_repeat_checks = {
                "all_applicant_direct_tier": True,
                "all_availability_dates": True,
                "all_floorplan_names": True,
                "all_no_errors": True,
                "all_rentcafe_adapter": True,
                "all_rows_strict_native_positive_rent": True,
                "native_ids_unique": True,
                "same_native_id_set": True,
                "same_unit_set": True,
                "three_full_pipeline_repeats": True,
            }
            expected_links = [
                portal_url,
                (
                    "https://vintagegroveapts.securecafe.com/onlineleasing/"
                    "summerwind-apartments0/guestlogin.aspx"
                ),
                (
                    "https://vintagegroveapts.securecafenet.com/"
                    "residentservices/summerwind-apartments0/userlogin"
                ),
            ]
            expected_native_pairs = {
                ("107", "38133268"),
                ("201", "38133274"),
                ("305", "38133283"),
                ("307", "38133284"),
                ("501", "38133235"),
                ("601", "38133240"),
                ("701", "38133246"),
            }

            guardrails = payload.get("guardrails") or {}
            configured_probe = payload.get("configured_route_probe") or {}
            current_root = payload.get("current_root") or {}
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}
            materializer = payload.get("materializer") or {}
            repeats = [
                item
                for item in (payload.get("full_pipeline_repeats") or [])
                if isinstance(item, dict)
            ]
            result_rows = [
                item
                for item in (payload.get("results") or [])
                if isinstance(item, dict)
            ]
            first_result = result_rows[0] if len(result_rows) == 1 else {}
            first_result_evidence = first_result.get("identity_evidence") or {}
            first_result_samples = [
                item
                for item in (first_result.get("native_samples") or [])
                if isinstance(item, dict)
            ]

            repo_root = Path("/Users/ankur/PropAi-codex-failed-no-data")
            materializer_path = Path(str(materializer.get("path") or ""))
            gzip_path = Path(str(current_root.get("gzip_artifact") or ""))
            source_hashes_match = bool(
                source_before == source_after == expected_source_hashes
                and all(
                    (repo_root / relative).is_file()
                    and hashlib.sha256(
                        (repo_root / relative).read_bytes()
                    ).hexdigest()
                    == expected_hash
                    for relative, expected_hash in expected_source_hashes.items()
                )
            )
            materializer_matches = bool(
                materializer_path.is_file()
                and str(materializer_path)
                == (
                    "/private/tmp/propai-fnd-vBkmT9/vintage_grove_migration_lane/"
                    "materialize_vintage_grove_42554_current_strict.py"
                )
                and hashlib.sha256(materializer_path.read_bytes()).hexdigest()
                == "1ff67485c4052a527de86e2e574bad0a62d08763f127b9113d729650b8c03a2e"
                == str(materializer.get("sha256") or "")
            )
            gzip_matches = False
            if gzip_path.is_file():
                gzip_bytes = gzip_path.read_bytes()
                try:
                    root_bytes = gzip.decompress(gzip_bytes)
                except (OSError, EOFError):
                    root_bytes = b""
                gzip_matches = bool(
                    str(gzip_path)
                    == (
                        "/private/tmp/propai-fnd-vBkmT9/"
                        "vintage_grove_migration_lane/"
                        "42554_vintage_grove_current_root.html.gz"
                    )
                    and hashlib.sha256(gzip_bytes).hexdigest()
                    == str(current_root.get("gzip_sha256") or "")
                    and len(root_bytes) == int(current_root.get("body_bytes") or 0)
                    and hashlib.sha256(root_bytes).hexdigest()
                    == str(current_root.get("body_sha256") or "")
                )

            def valid_vintage_date(value: Any) -> bool:
                parts = str(value or "").split("/")
                if len(parts) != 3 or not all(part.isdigit() for part in parts):
                    return False
                try:
                    parsed = date(int(parts[2]), int(parts[0]), int(parts[1]))
                except ValueError:
                    return False
                return parsed >= date(2026, 8, 1)

            def strict_vintage_units(units: list[dict[str, Any]]) -> bool:
                native_pairs = {
                    (
                        str(item.get("unit_number") or ""),
                        str(
                            (item.get("source_ids") or {}).get(
                                "securecafe_apartment_id"
                            )
                            or ""
                        ),
                    )
                    for item in units
                }
                return bool(
                    len(units) == 7
                    and native_pairs == expected_native_pairs
                    and all(
                        str(
                            (item.get("source_ids") or {}).get(
                                "securecafe_floorplan_id"
                            )
                            or ""
                        ).isdigit()
                        and str(item.get("floor_plan_name") or "").strip()
                        and isinstance(item.get("market_rent_low"), (int, float))
                        and not isinstance(item.get("market_rent_low"), bool)
                        and item.get("market_rent_low") > 0
                        and isinstance(item.get("market_rent_high"), (int, float))
                        and not isinstance(item.get("market_rent_high"), bool)
                        and item.get("market_rent_high") > 0
                        and item.get("source_api_url") == source_api_url
                        and item.get("source_property_id") == "1722315"
                        and item.get("source_property_name")
                        == "Vintage Grove Apartments"
                        and item.get("source_property_address")
                        == "5262 Timaquana Rd, Jacksonville, FL, 32210"
                        and valid_vintage_date(item.get("availability_date"))
                        for item in units
                    )
                )

            def strict_vintage_repeat(item: dict[str, Any]) -> bool:
                units = [
                    row for row in (item.get("units") or []) if isinstance(row, dict)
                ]
                return bool(
                    item.get("adapter") == "rentcafe"
                    and item.get("tier")
                    == "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT"
                    and item.get("fallback_chain") == ["rentcafe"]
                    and item.get("errors") == []
                    and int(item.get("emitted_rows") or 0) == 7
                    and int(item.get("strict_native_positive_rent_rows") or 0)
                    == 7
                    and int(item.get("plan_summaries") or 0) == 1
                    and strict_vintage_units(units)
                )

            result_sample_pairs = {
                (
                    str((item.get("identity") or {}).get("unit_number") or ""),
                    str(
                        (item.get("identity") or {}).get(
                            "securecafe_apartment_id"
                        )
                        or ""
                    ),
                )
                for item in first_result_samples
            }
            strict_shape = bool(
                payload.get("cohort") == "exact_2026-07-31_FAILED_NO_DATA_344"
                and payload.get("ledger_mutation") == "none"
                and payload.get("commit") == "none"
                and payload.get("push") == "none"
                and payload.get("paid_canary") is False
                and payload.get("configured_identity")
                == expected_configured_identity
                and payload.get("current_identity") == expected_current_identity
                and guardrails.get("environment") == expected_environment
                and guardrails.get("direct_public_http_only") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and int(guardrails.get("hyperbrowser_calls") or 0) == 0
                and int(guardrails.get("llm_calls") or 0) == 0
                and int(guardrails.get("proxy_calls") or 0) == 0
                and int(guardrails.get("web_unlocker_calls") or 0) == 0
                and int(guardrails.get("flaresolverr_calls") or 0) == 0
                and configured_probe.get("requested_url") == configured_url
                and configured_probe.get("final_url") == configured_url
                and configured_probe.get("status") == 0
                and configured_probe.get("body_bytes") == 0
                and bool(str(configured_probe.get("error") or "").strip())
                and current_root.get("requested_url") == current_url
                and current_root.get("final_url") == current_url
                and current_root.get("status") == 200
                and int(current_root.get("body_bytes") or 0) > 350_000
                and current_root.get("published_securecafe_floorplan_routes")
                == [portal_url]
                and current_root.get("published_securecafe_links")
                == expected_links
                and payload.get("root_identity_checks") == expected_root_checks
                and payload.get("provider_identity_checks")
                == expected_provider_checks
                and payload.get("repeat_checks") == expected_repeat_checks
                and source_hashes_match
                and materializer_matches
                and gzip_matches
                and len(repeats) == 3
                and [int(item.get("repeat") or 0) for item in repeats]
                == [1, 2, 3]
                and all(strict_vintage_repeat(item) for item in repeats)
                and all(item.get("units") == repeats[0].get("units") for item in repeats)
                and first_result.get("property_id") == 42554
                and first_result.get("property_name")
                == "Vintage Grove Apartments"
                and first_result.get("website") == configured_url
                and first_result.get("current_official_url") == current_url
                and first_result.get("outcome") == "UNIT_QUALIFIED"
                and first_result.get("property_identity_match") is True
                and first_result.get("contamination_verdict")
                == (
                    "pass_exact_same_address_current_official_vintage_grove_"
                    "published_securecafe_property_native_units_three_repeats"
                )
                and first_result.get("adapter") == "rentcafe"
                and first_result.get("tier")
                == "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT"
                and int(first_result.get("units") or 0) == 7
                and int(first_result_evidence.get("rows_with_native_identity") or 0)
                == 7
                and int(
                    first_result_evidence.get(
                        "rows_with_native_identity_and_positive_rent"
                    )
                    or 0
                )
                == 7
                and first_result_evidence.get("source_urls") == [source_api_url]
                and first_result_evidence.get("source_property_ids") == ["1722315"]
                and len(first_result_samples) == 7
                and result_sample_pairs == expected_native_pairs
                and all(
                    valid_vintage_date(item.get("availability_date"))
                    and item.get("source_api_url") == source_api_url
                    and all(
                        isinstance((item.get("positive_rent_evidence") or {}).get(key), (int, float))
                        and not isinstance(
                            (item.get("positive_rent_evidence") or {}).get(key),
                            bool,
                        )
                        and (item.get("positive_rent_evidence") or {}).get(key) > 0
                        for key in ("market_rent_low", "market_rent_high")
                    )
                    for item in first_result_samples
                )
            )
            first_units = (
                [
                    item
                    for item in (repeats[0].get("units") or [])
                    if isinstance(item, dict)
                ]
                if repeats
                else []
            )
            rows = [
                {
                    "property_id": "42554",
                    "property_name": "Vintage Grove Apartments",
                    "website": configured_url,
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_same_address_current_official_vintage_grove_"
                        "published_securecafe_property_native_units_three_repeats"
                        if strict_shape
                        else "reject_vintage_grove_identity_or_native_shape_incomplete"
                    ),
                    "units": 7 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 7 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            7 if strict_shape else 0
                        ),
                        "source_urls": [source_api_url] if strict_shape else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(item.get("unit_number") or ""),
                                "securecafe_apartment_id": str(
                                    (item.get("source_ids") or {}).get(
                                        "securecafe_apartment_id"
                                    )
                                    or ""
                                ),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low"),
                                "market_rent_high": item.get("market_rent_high"),
                            },
                            "availability_date": str(
                                item.get("availability_date") or ""
                            ),
                            "source_api_url": str(
                                item.get("source_api_url") or ""
                            ),
                        }
                        for item in first_units
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "riverwalk_wimmer_stale_canonical_current_sightmap_e2e"
        ):
            configured_identity = payload.get("configured_identity") or {}
            guardrails = payload.get("guardrails") or {}
            environment = guardrails.get("environment") or {}
            http_evidence = payload.get("http_evidence") or {}
            stale_http = http_evidence.get("stale_configured") or {}
            current_root_http = http_evidence.get("current_root") or {}
            floorplans_http = http_evidence.get("current_floorplans") or {}
            identity_checks = payload.get("identity_checks") or {}
            repeat_checks = payload.get("repeat_checks") or {}
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}
            materializer = payload.get("materializer") or {}
            repeats = [
                item
                for item in (payload.get("full_pipeline_repeats") or [])
                if isinstance(item, dict)
            ]
            negative_controls = [
                item
                for item in (
                    payload.get("stale_404_foreign_knock_negative_controls") or []
                )
                if isinstance(item, dict)
            ]
            result_rows = [
                item
                for item in (payload.get("results") or [])
                if isinstance(item, dict)
            ]

            stale_url = (
                "https://www.wimmercommunities.com/apartments/"
                "menomonee-falls/riverwalk-on-the-falls/"
            )
            current_root_url = (
                "https://www.wimmercommunities.com/apartments/wi/"
                "menomonee-falls/riverwalk-on-the-falls"
            )
            floorplans_url = f"{current_root_url}/floorplans"
            source_api_url = (
                "https://sightmap.com/app/api/v1/y8px5ljmv19/"
                "sightmaps/100325"
            )
            expected_units = {"103", "201", "305", "310", "324", "403", "417", "419"}
            expected_environment = {
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
            expected_identity_checks = {
                "canonical_link_visible": True,
                "city_visible": True,
                "property_name_visible": True,
                "state_visible": True,
                "street_visible": True,
                "zip_visible": True,
            }
            expected_repeat_checks = {
                "all_availability_dates": True,
                "all_eight_emitted_and_strict": True,
                "all_floorplan_names": True,
                "all_positive_row_level_rents": True,
                "all_sightmap_adapter": True,
                "all_sightmap_detection": True,
                "all_tier_1_sightmap": True,
                "native_sightmap_ids_unique": True,
                "natural_unit_numbers_unique": True,
                "no_pipeline_errors": True,
                "one_exact_source_each_repeat": True,
                "same_units_each_repeat": True,
                "three_repeats": True,
            }
            expected_source_hashes = {
                "ma_poc/core/identity.py": (
                    "7215ed6a3b74ad5c4741d32a20142dee64e43faee79740660f31ddf7f1487167"
                ),
                "ma_poc/pms/adapters/sightmap.py": (
                    "afcbed2637ac3aa44113fb973726c3afb780208c37de55c4bb984749b420e3fa"
                ),
                "ma_poc/pms/detector.py": (
                    "48ebf6f88454f0ef71d557d00d1d879dcac62a5aaea727d584a7b16520f8706a"
                ),
                "ma_poc/pms/scraper.py": (
                    "17f8c4398cd4c10f8d776970a564f10b6fa858532accbe85dc61d44a9dba32cd"
                ),
            }
            expected_negative_controls = {
                (
                    "2007994",
                    "Oakton Beach",
                    "W289 N2183 Louis Ave Apt. 1",
                    "Pewaukee",
                    "53072",
                ),
                (
                    "2007989",
                    "Parkside",
                    "5992 South Kurtz Road",
                    "Hales Corners",
                    "53130",
                ),
                (
                    "2008000",
                    "Forest Ridge",
                    "11077 W Forest Home Ave",
                    "Hales Corners",
                    "53130",
                ),
                (
                    "2007983",
                    "The Orchard",
                    "9010 West Forest Home Avenue",
                    "Greenfield",
                    "53228",
                ),
                (
                    "2007982",
                    "Foxwood Crossing",
                    "4500 South 124th Street",
                    "Greenfield",
                    "53228",
                ),
                (
                    "2007988",
                    "Whitnall Gardens",
                    "9571 West Forest Home Avenue",
                    "Hales Corners",
                    "53130",
                ),
            }

            def riverwalk_artifact_hashes_match() -> bool:
                materializer_path = Path(str(materializer.get("path") or ""))
                gzip_path = Path(
                    str(floorplans_http.get("html_gzip_artifact") or "")
                )
                if not materializer_path.is_file() or not gzip_path.is_file():
                    return False
                if (
                    hashlib.sha256(materializer_path.read_bytes()).hexdigest()
                    != str(materializer.get("sha256") or "")
                ):
                    return False
                gzip_bytes = gzip_path.read_bytes()
                if (
                    hashlib.sha256(gzip_bytes).hexdigest()
                    != str(floorplans_http.get("html_gzip_sha256") or "")
                ):
                    return False
                try:
                    html_bytes = gzip.decompress(gzip_bytes)
                except (OSError, EOFError):
                    return False
                return bool(
                    len(html_bytes) == int(floorplans_http.get("body_bytes") or 0)
                    and hashlib.sha256(html_bytes).hexdigest()
                    == str(floorplans_http.get("body_sha256") or "")
                )

            def riverwalk_source_hashes_match() -> bool:
                if (
                    source_before != source_after
                    or source_after != expected_source_hashes
                ):
                    return False
                repo_root = Path("/Users/ankur/PropAi-codex-failed-no-data")
                return all(
                    (repo_root / source_path).is_file()
                    and hashlib.sha256(
                        (repo_root / source_path).read_bytes()
                    ).hexdigest()
                    == expected_hash
                    for source_path, expected_hash in expected_source_hashes.items()
                )

            def valid_future_date(value: Any) -> bool:
                try:
                    parsed = date.fromisoformat(str(value))
                except (TypeError, ValueError):
                    return False
                return parsed.year >= 2000

            def strict_riverwalk_units(units: list[dict[str, Any]]) -> bool:
                unit_numbers = [str(item.get("unit_number") or "") for item in units]
                sightmap_ids = [
                    str((item.get("source_ids") or {}).get("sightmap_unit_id") or "")
                    for item in units
                ]
                return bool(
                    len(units) == 8
                    and set(unit_numbers) == expected_units
                    and len(unit_numbers) == len(set(unit_numbers))
                    and all(sightmap_ids)
                    and len(sightmap_ids) == len(set(sightmap_ids))
                    and all(
                        str(item.get("floor_plan_name") or "").strip()
                        and valid_future_date(item.get("availability_date"))
                        and isinstance(item.get("market_rent_low"), (int, float))
                        and not isinstance(item.get("market_rent_low"), bool)
                        and item.get("market_rent_low") > 0
                        and isinstance(item.get("market_rent_high"), (int, float))
                        and not isinstance(item.get("market_rent_high"), bool)
                        and item.get("market_rent_high") > 0
                        and item.get("source_api_url") == source_api_url
                        for item in units
                    )
                )

            def strict_riverwalk_repeat(item: dict[str, Any]) -> bool:
                units = [
                    row for row in (item.get("units") or []) if isinstance(row, dict)
                ]
                return bool(
                    item.get("adapter") == "sightmap"
                    and item.get("detected_pms") == "sightmap"
                    and item.get("tier") == "TIER_1_API_SIGHTMAP_IFRAME"
                    and item.get("fallback_chain") == ["sightmap"]
                    and item.get("errors") == []
                    and int(item.get("emitted_rows") or 0) == 8
                    and int(item.get("strict_native_positive_rent_rows") or 0) == 8
                    and int(item.get("distinct_unit_numbers") or 0) == 8
                    and int(item.get("plan_summaries") or 0) == 19
                    and item.get("source_urls") == [source_api_url]
                    and strict_riverwalk_units(units)
                )

            negative_control_shape = {
                (
                    str(item.get("property_id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("street") or ""),
                    str(item.get("city") or ""),
                    str(item.get("zip") or ""),
                )
                for item in negative_controls
                if item.get("state") == "WI"
                and item.get("is_exact_riverwalk") is False
                and str(item.get("name") or "") != "Riverwalk on the Falls I"
                and str(item.get("street") or "") != "W165 N8910 Grand Ave"
                and str(item.get("official_website") or "").startswith(
                    "https://www.wimmercommunities.com/"
                )
            }
            first_repeat_units = (
                [
                    item
                    for item in (repeats[0].get("units") or [])
                    if isinstance(item, dict)
                ]
                if repeats
                else []
            )
            first_result = result_rows[0] if len(result_rows) == 1 else {}
            result_evidence = first_result.get("identity_evidence") or {}
            result_samples = [
                item
                for item in (first_result.get("native_samples") or [])
                if isinstance(item, dict)
            ]
            result_unit_numbers = {
                str((item.get("identity") or {}).get("unit_number") or "")
                for item in result_samples
            }
            result_sightmap_ids = {
                str((item.get("identity") or {}).get("sightmap_unit_id") or "")
                for item in result_samples
            }

            strict_shape = bool(
                payload.get("cohort") == "exact_2026-07-31_FAILED_NO_DATA_344"
                and payload.get("ledger_mutation") == "none"
                and payload.get("commit") == "none"
                and payload.get("push") == "none"
                and payload.get("paid_canary") is False
                and configured_identity
                == {
                    "address": "W165 N8910 Grand Ave",
                    "apartmentid": "71534",
                    "city": "Menomonee Falls",
                    "name": "Riverwalk on the Falls I",
                    "state": "WI",
                    "website": stale_url,
                    "zip": "53051",
                }
                and guardrails.get("direct_public_http_only") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and int(guardrails.get("flaresolverr_calls") or 0) == 0
                and int(guardrails.get("hyperbrowser_calls") or 0) == 0
                and int(guardrails.get("llm_calls") or 0) == 0
                and int(guardrails.get("proxy_calls") or 0) == 0
                and int(guardrails.get("web_unlocker_calls") or 0) == 0
                and environment == expected_environment
                and stale_http.get("requested_url") == stale_url
                and stale_http.get("final_url") == stale_url
                and stale_http.get("status") == 404
                and int(stale_http.get("body_bytes") or 0) > 80_000
                and current_root_http.get("requested_url") == current_root_url
                and current_root_http.get("final_url") == current_root_url
                and current_root_http.get("status") == 200
                and int(current_root_http.get("body_bytes") or 0) > 200_000
                and floorplans_http.get("requested_url") == floorplans_url
                and floorplans_http.get("final_url") == floorplans_url
                and floorplans_http.get("status") == 200
                and int(floorplans_http.get("body_bytes") or 0) > 800_000
                and identity_checks == expected_identity_checks
                and repeat_checks == expected_repeat_checks
                and riverwalk_artifact_hashes_match()
                and riverwalk_source_hashes_match()
                and len(repeats) == 3
                and [int(item.get("repeat") or 0) for item in repeats] == [1, 2, 3]
                and all(strict_riverwalk_repeat(item) for item in repeats)
                and all(item.get("units") == repeats[0].get("units") for item in repeats)
                and negative_control_shape == expected_negative_controls
                and len(negative_controls) == 6
                and first_result.get("property_id") == 71534
                and first_result.get("property_name") == "Riverwalk on the Falls I"
                and first_result.get("website") == stale_url
                and first_result.get("current_official_url") == current_root_url
                and first_result.get("outcome") == "UNIT_QUALIFIED"
                and first_result.get("property_identity_match") is True
                and str(first_result.get("contamination_verdict") or "").startswith(
                    "pass_"
                )
                and first_result.get("adapter") == "sightmap"
                and first_result.get("tier") == "TIER_1_API_SIGHTMAP_IFRAME"
                and int(first_result.get("units") or 0) == 8
                and result_evidence.get("configured_url_is_stale_404") is True
                and result_evidence.get("current_canonical_is_live_200") is True
                and result_evidence.get("identity_checks") == expected_identity_checks
                and int(result_evidence.get("rows_with_native_identity") or 0) == 8
                and int(
                    result_evidence.get("rows_with_native_identity_and_positive_rent")
                    or 0
                )
                == 8
                and int(result_evidence.get("distinct_native_sightmap_unit_ids") or 0)
                == 8
                and int(result_evidence.get("distinct_native_unit_numbers") or 0) == 8
                and result_evidence.get("source_urls") == [source_api_url]
                and len(result_samples) == 8
                and result_unit_numbers == expected_units
                and len(result_sightmap_ids) == 8
                and "" not in result_sightmap_ids
            )
            rows = [
                {
                    "property_id": "71534",
                    "property_name": "Riverwalk on the Falls I",
                    "website": stale_url,
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_wimmer_current_canonical_name_street_city_state_zip_"
                        "single_sightmap_three_full_pipeline_repeats"
                        if strict_shape
                        else "reject_riverwalk_property_boundary_or_native_shape_incomplete"
                    ),
                    "units": 8 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 8 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            8 if strict_shape else 0
                        ),
                        "source_urls": [source_api_url] if strict_shape else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(item.get("unit_number") or ""),
                                "sightmap_unit_id": str(
                                    (item.get("source_ids") or {}).get(
                                        "sightmap_unit_id"
                                    )
                                    or ""
                                ),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low"),
                                "market_rent_high": item.get("market_rent_high"),
                            },
                            "source_api_url": str(item.get("source_api_url") or ""),
                        }
                        for item in first_repeat_units
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and (payload.get("scope") or {}).get("property_id") == 48075
            and (payload.get("scope") or {}).get("property") == "Edgefield"
        ):
            scope = payload.get("scope") or {}
            implementation = payload.get("implementation") or {}
            verification = payload.get("verification") or {}
            contamination = payload.get("contamination_controls") or {}
            guardrails = payload.get("guardrails") or {}
            source_files = [
                item
                for item in (implementation.get("source_files") or [])
                if isinstance(item, dict)
            ]
            repeat_refs = [
                item
                for item in (verification.get("repeat_artifacts") or [])
                if isinstance(item, dict)
            ]

            def edgefield_source_hashes_match() -> bool:
                expected_suffixes = {
                    "ma_poc/fetch/fetcher.py",
                    "ma_poc/fetch/hyperbrowser_backend.py",
                    "ma_poc/tests/fetch/test_fetcher_render_captcha_hb_rescue.py",
                    "ma_poc/tests/fetch/test_hyperbrowser_raw_get_redirect.py",
                }
                observed_suffixes: set[str] = set()
                for item in source_files:
                    path = Path(str(item.get("path") or ""))
                    recorded_hash = str(item.get("sha256") or "")
                    if not path.is_file() or not recorded_hash:
                        return False
                    if hashlib.sha256(path.read_bytes()).hexdigest() != recorded_hash:
                        return False
                    matched = next(
                        (
                            suffix
                            for suffix in expected_suffixes
                            if str(path).endswith(suffix)
                        ),
                        "",
                    )
                    if not matched:
                        return False
                    observed_suffixes.add(matched)
                return observed_suffixes == expected_suffixes

            repeat_payloads: list[dict[str, Any]] = []
            repeat_hashes_match = len(repeat_refs) == 3
            for repeat_ref in repeat_refs:
                repeat_path = Path(str(repeat_ref.get("path") or ""))
                if not repeat_path.is_file():
                    repeat_hashes_match = False
                    continue
                repeat_bytes = repeat_path.read_bytes()
                if (
                    hashlib.sha256(repeat_bytes).hexdigest()
                    != str(repeat_ref.get("sha256") or "")
                ):
                    repeat_hashes_match = False
                repeat_payload = json.loads(repeat_bytes)
                if not isinstance(repeat_payload, dict):
                    repeat_hashes_match = False
                    continue
                repeat_payloads.append(repeat_payload)

            expected_units = {"13", "38", "108"}

            def strict_edgefield_repeat(item: dict[str, Any]) -> bool:
                repeat_guardrails = item.get("guardrails") or {}
                session_options = repeat_guardrails.get("session_options") or {}
                fetch = item.get("fetch") or {}
                scrape = item.get("scrape") or {}
                detected = scrape.get("detected") or {}
                native_rows = [
                    row
                    for row in (scrape.get("rows") or [])
                    if isinstance(row, dict)
                ]
                source_urls = {
                    str(row.get("source_api_url") or "") for row in native_rows
                }
                return bool(
                    item.get("property_id") == 48075
                    and item.get("configured_url")
                    == "https://www.edgefieldaptsva.com/"
                    and repeat_guardrails.get("captcha_solving") is False
                    and repeat_guardrails.get("web_unlocker") is False
                    and repeat_guardrails.get("flaresolverr") is False
                    and repeat_guardrails.get("fingerprint_rotation") is False
                    and repeat_guardrails.get("hyperbrowser") is True
                    and repeat_guardrails.get("hyperbrowser_max_calls_per_property")
                    == 1
                    and repeat_guardrails.get("hyperbrowser_use_stealth") is False
                    and repeat_guardrails.get("hyperbrowser_use_proxy") is True
                    and repeat_guardrails.get("llm") is False
                    and repeat_guardrails.get("paid_canary") is False
                    and session_options.get("solveCaptchas") is False
                    and session_options.get("useStealth") is False
                    and session_options.get("useProxy") is True
                    and fetch.get("outcome") == "OK"
                    and fetch.get("status") in {200, 202}
                    and int(fetch.get("body_bytes") or 0) > 100_000
                    and fetch.get("captcha_detected") is False
                    and fetch.get("exact_name_visible") is True
                    and fetch.get("exact_street_visible") is True
                    and fetch.get("exact_city_state_zip_visible") is True
                    and fetch.get("published_site_ids") == ["1060300"]
                    and fetch.get("published_onlineleasing_hosts")
                    == ["6359.onlineleasing.realpage.com"]
                    and detected.get("pms") == "onesite"
                    and float(detected.get("confidence") or 0) >= 0.9
                    and scrape.get("adapter") == "onesite"
                    and scrape.get("tier") == "TIER_1_API_ONESITE_WORKFLOW"
                    and scrape.get("fallback_chain") == ["onesite"]
                    and scrape.get("errors") == []
                    and int(scrape.get("units") or 0) == 3
                    and int(scrape.get("strict_native_positive_rent_rows") or 0)
                    == 3
                    and int(scrape.get("plan_summaries") or 0) == 0
                    and scrape.get("all_rows_have_expected_source_property_id")
                    is True
                    and scrape.get("all_rows_have_distinct_native_anchor") is True
                    and len(native_rows) == 3
                    and {str(row.get("unit_number") or "") for row in native_rows}
                    == expected_units
                    and len(source_urls) == 1
                    and all(
                        row.get("floor_plan_name") == "Two Bedroom"
                        and str(row.get("bedrooms") or "") == "2"
                        and str(row.get("bathrooms") or "") == "1.5"
                        and str(row.get("sqft") or "") == "950"
                        and row.get("market_rent_low")
                        == row.get("market_rent_high")
                        == 1475
                        and row.get("source_property_id") == "1060300"
                        and row.get("source_property_provenance")
                        == "marketing_page_site_id"
                        and "/workflowstartup/v1/1060300/English"
                        in str(row.get("source_api_url") or "")
                        for row in native_rows
                    )
                )

            configured_identity = scope.get("configured_identity") or {}
            prior_remainder = scope.get("current_remainder_artifact") or {}
            focused_tests = (
                verification.get("focused_and_fetch_regression_tests") or {}
            )
            strict_shape = bool(
                scope.get("cohort")
                == "exact 2026-07-31 FAILED_NO_DATA remainder"
                and scope.get("configured_url")
                == "https://www.edgefieldaptsva.com/"
                and configured_identity
                == {
                    "address": "5699 Craneybrook Ln",
                    "city": "Portsmouth",
                    "state": "VA",
                    "zip": "23703",
                }
                and scope.get("paid_canary") is False
                and scope.get("ledger_or_builder_modified") is False
                and prior_remainder.get("sha256")
                == "442a03d16a91c1f95f1d87341174e406c269094969bd0ef1a58069fe03cb9ec9"
                and str(prior_remainder.get("candidate_row") or "").startswith(
                    "48075,,https://www.edgefieldaptsva.com/"
                )
                and implementation.get("branch") == "codex/failed-no-data-recovery"
                and implementation.get("head") == implementation.get("origin_main")
                == "02369d2827dd6bfe49e7abb8d32e028742ef8d6c"
                and edgefield_source_hashes_match()
                and verification.get("configured_e2e_gate") == "3/3 strict pass"
                and focused_tests.get("result") == "71 passed"
                and repeat_hashes_match
                and len(repeat_payloads) == 3
                and all(strict_edgefield_repeat(item) for item in repeat_payloads)
                and [
                    int(item.get("strict_rows") or 0) for item in repeat_refs
                ]
                == [3, 3, 3]
                and all(
                    set(str(value) for value in (item.get("unit_numbers") or []))
                    == expected_units
                    for item in repeat_refs
                )
                and contamination.get("exact_first_party_identity_in_all_repeats")
                is True
                and contamination.get("sole_published_site_id_in_all_repeats")
                == "1060300"
                and contamination.get(
                    "sole_published_onlineleasing_host_in_all_repeats"
                )
                == "6359.onlineleasing.realpage.com"
                and contamination.get("all_rows_bound_to_published_site_id") is True
                and contamination.get("all_rows_have_distinct_native_unit_numbers")
                is True
                and set(
                    str(value)
                    for value in (
                        contamination.get("stable_unit_set_across_three_repeats")
                        or []
                    )
                )
                == expected_units
                and contamination.get("all_rows_positive_rent") is True
                and contamination.get("plan_summaries_all_repeats") == 0
                and guardrails.get("compliance_mode") is True
                and guardrails.get("solve_captchas") is False
                and guardrails.get("hyperbrowser_stealth") is False
                and guardrails.get("hyperbrowser_proxy") is True
                and guardrails.get("hyperbrowser_max_calls_per_property") == 1
                and guardrails.get("web_unlocker") is False
                and guardrails.get("flaresolverr") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("llm") is False
                and guardrails.get("paid_canary") is False
            )
            first_rows = (
                (repeat_payloads[0].get("scrape") or {}).get("rows") or []
                if repeat_payloads
                else []
            )
            rows = [
                {
                    "property_id": "48075",
                    "property_name": "Edgefield",
                    "website": "www.edgefieldaptsva.com",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_first_party_identity_sole_published_onesite_"
                        "site_id_three_repeats_compliant_hyperbrowser"
                        if strict_shape
                        else "reject_edgefield_property_boundary_or_native_shape_incomplete"
                    ),
                    "units": 3 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 3 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            3 if strict_shape else 0
                        ),
                        "source_urls": sorted(
                            {
                                str(item.get("source_api_url") or "")
                                for item in first_rows
                                if isinstance(item, dict)
                            }
                        )
                        if strict_shape
                        else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(item.get("unit_number") or "")
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low")
                            },
                            "source_api_url": str(item.get("source_api_url") or ""),
                        }
                        for item in first_rows
                        if isinstance(item, dict)
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "annaberg_nesthub_exact_property_ssr_implementation_e2e"
        ):
            cohort = payload.get("cohort") or {}
            state_before = cohort.get("state_before") or {}
            state_after = cohort.get("state_after") or {}
            guardrails = payload.get("guardrails") or {}
            implementation = payload.get("implementation") or {}
            verification = payload.get("verification") or {}
            counts = payload.get("provider_direct_counts") or {}
            source_snapshot = payload.get("source_snapshot_after") or {}
            source_hashes = source_snapshot.get("sha256") or {}
            repeats = [
                item
                for item in (verification.get("configured_pipeline_repeats") or [])
                if isinstance(item, dict)
            ]

            def annaberg_source_hashes_match() -> bool:
                if not source_hashes:
                    return False
                for source_path, recorded_hash in source_hashes.items():
                    path = Path(str(source_path))
                    if (
                        not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest()
                        != recorded_hash
                    ):
                        return False
                return True

            expected_env = {
                "COMPLIANCE_MODE": "1",
                "ENABLE_BODY_RESOLVER": "false",
                "ENABLE_CRAWL_GET_GATE": "false",
                "ENABLE_DC_PROXY_TIER": "false",
                "ENABLE_ENTRATA_PLAN_RENDER": "false",
                "ENABLE_FLARESOLVERR_TIER": "false",
                "ENABLE_PLAN_UNIT_RENDER": "false",
                "ENABLE_RENDER_ON_EMPTY": "false",
                "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
                "ENABLE_RESIDENTIAL_TIER": "false",
                "ENABLE_TIER4_LLM": "false",
                "ENABLE_TIER_ESCALATION": "false",
                "ENABLE_UNLOCKER_TIER": "false",
                "FETCH_BACKEND": "brightdata",
                "PROBE_PROXY_URL": "",
                "PROXY_POOL_URLS": "",
                "RENDER_BACKEND": "local",
            }
            expected_source_files = {
                "ma_poc/pms/adapters/_nesthub_public.py",
                "ma_poc/pms/scraper.py",
                "ma_poc/core/source_ids.py",
            }
            expected_test_files = {
                "ma_poc/tests/pms/adapters/test_nesthub_public.py",
                "ma_poc/tests/pms/adapters/test_showmojo_public.py",
                "ma_poc/tests/pms/adapters/test_betternoi_public.py",
            }
            checks = [
                item
                for item in (verification.get("checks") or [])
                if isinstance(item, dict)
            ]
            required_checks_pass = bool(
                len(checks) == 3
                and all(item.get("exit_code") == 0 for item in checks)
                and any(
                    "All checks passed!" in str(item.get("stdout") or "")
                    for item in checks
                )
                and any(
                    "46 passed" in str(item.get("stdout") or "")
                    for item in checks
                )
                and any(
                    "55 passed" in str(item.get("stdout") or "")
                    for item in checks
                )
            )

            def strict_annaberg_repeat(item: dict[str, Any], repeat: int) -> bool:
                assertions = item.get("assertions") or {}
                fetch = item.get("configured_fetch") or {}
                unit = item.get("unit") or {}
                chain = item.get("official_chain") or {}
                rejected = {
                    str(row.get("provider_listing_id") or ""): set(
                        row.get("reasons") or []
                    )
                    for row in (chain.get("rejected_rows") or [])
                    if isinstance(row, dict)
                }
                return bool(
                    item.get("repeat") == repeat
                    and item.get("adapter") == "nesthub_public"
                    and item.get("tier")
                    == "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY"
                    and item.get("fallback_chain")
                    == ["generic_plan_text", "page_published_native:nesthub_public"]
                    and int(item.get("unit_rows") or 0) == 1
                    and int(item.get("native_rows") or 0) == 1
                    and int(item.get("native_positive_rent_rows") or 0) == 1
                    and int(item.get("plan_rows") or 0) == 0
                    and len(assertions) == 23
                    and all(value is True for value in assertions.values())
                    and fetch.get("status") == 200
                    and fetch.get("outcome") == "OK"
                    and fetch.get("final_url") == payload.get("configured_url")
                    and int(fetch.get("body_bytes") or 0) > 40_000
                    and unit.get("unit_number") == "E7"
                    and unit.get("unit_name") == "2905 Arrowhead Drive - E7"
                    and unit.get("provider_unit_address")
                    == "2905 Arrowhead Drive - E7"
                    and unit.get("floor_plan_name") == "Chesapeake"
                    and str(unit.get("bedrooms") or "") == "2"
                    and str(unit.get("bathrooms") or "") == "2.5"
                    and str(unit.get("sqft") or "") == "1268"
                    and unit.get("market_rent_low")
                    == unit.get("market_rent_high")
                    == 1160
                    and unit.get("availability_date")
                    == unit.get("available_date")
                    == "2026-08-19"
                    and unit.get("availability_text")
                    == "Available: 08-19-2026"
                    and unit.get("availability_date_provenance")
                    == "provider_roster_and_detail_exact_date_agree"
                    and unit.get("source_ids") == {"nesthub_listing_id": "602"}
                    and unit.get("source_property_name") == "Annaberg"
                    and unit.get("source_property_provenance")
                    == (
                        "exact_configured_nesthub_detail_same_host_community_"
                        "published_filter_roster_exact_address_detail_revalidation"
                    )
                    and unit.get("source_api_url")
                    == (
                        "https://www.augustarentalhomes.net/_system/listings/602/"
                        "2905-Arrowhead-Drive---E7-Augusta-GA-30909-US"
                    )
                    and unit.get("source_listing_url") == unit.get("source_api_url")
                    and unit.get("source_community_url")
                    == "https://www.augustarentalhomes.net/annabergs"
                    and unit.get("source_portal_url")
                    == "https://www.augustarentalhomes.net/augusta-homes-for-rent"
                    and chain.get("attempted") is True
                    and chain.get("configured_listing_id") == "56"
                    and chain.get("configured_status")
                    == "This Property Is Not Available"
                    and chain.get("configured_listing_must_not_emit") is True
                    and chain.get("published_property_filter") == "search=ANNBRG"
                    and int(chain.get("portfolio_rows") or 0) == 33
                    and int(chain.get("exact_address_candidates") or 0) == 1
                    and int(chain.get("accepted_rows") or 0) == 1
                    and chain.get("native_listing_ids") == ["602"]
                    and chain.get("failure_reason") == ""
                    and chain.get("pages")
                    == [
                        {
                            "page": 1,
                            "rows": 21,
                            "url": (
                                "https://www.augustarentalhomes.net/"
                                "augusta-homes-for-rent"
                            ),
                        },
                        {
                            "page": 2,
                            "rows": 12,
                            "url": (
                                "https://www.augustarentalhomes.net/"
                                "augusta-homes-for-rent?pg=2"
                            ),
                        },
                    ]
                    and "canonical_street_and_native_unit_suffix_mismatch"
                    in rejected.get("601", set())
                    and "canonical_street_and_native_unit_suffix_mismatch"
                    in rejected.get("606", set())
                    and "canonical_city_mismatch" in rejected.get("606", set())
                    and "canonical_zip_mismatch" in rejected.get("606", set())
                )

            strict_shape = bool(
                payload.get("property_id") == 1765
                and payload.get("property_name") == "Annaberg"
                and payload.get("configured_url")
                == (
                    "https://www.augustarentalhomes.net/_system/listings/56/"
                    "2905-Arrowhead-Drive---D3-Augusta-GA-30909-US"
                )
                and payload.get("provider_direct_strict_pass") is True
                and payload.get("commit") == "none"
                and payload.get("push") == "none"
                and payload.get("canary_mutation") == "none"
                and payload.get("ledger_mutation") == "none"
                and cohort.get("boundary") == "exact_2026-07-31_FAILED_NO_DATA_344"
                and cohort.get("confirmed_remaining_not_ledger") is True
                and cohort.get("source_adapter_0731") == "generic"
                and cohort.get("current_detected_adapter") == "unknown"
                and cohort.get("global_cohort_changed_during_run") is False
                and state_before == state_after
                and state_before.get("property_in_ledger") is False
                and state_before.get("property_in_remaining") is True
                and int(state_before.get("ledger_rows") or 0) == 238
                and int(state_before.get("remaining_rows") or 0) == 106
                and guardrails.get("environment") == expected_env
                and guardrails.get("compliance_mode") is True
                and guardrails.get("llm") is False
                and guardrails.get("proxy") is False
                and guardrails.get("hyperbrowser") is False
                and int(guardrails.get("hyperbrowser_call_count") or 0) == 0
                and guardrails.get("web_unlocker") is False
                and int(guardrails.get("web_unlocker_call_count") or 0) == 0
                and guardrails.get("flaresolverr") is False
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("paid_canary") is False
                and implementation.get("tier")
                == "TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY"
                and set(implementation.get("source_files") or [])
                == expected_source_files
                and set(implementation.get("test_files") or [])
                == expected_test_files
                and implementation.get("source_id_scope")
                == {"nesthub_listing_id": "UNIT_PENDING"}
                and counts.get("configured_repeats") == 3
                and counts.get("unit_rows_each") == [1, 1, 1]
                and counts.get("plan_rows_each") == [0, 0, 0]
                and counts.get("native_positive_rent_rows_each") == [1, 1, 1]
                and counts.get("native_listing_ids_each")
                == [["602"], ["602"], ["602"]]
                and verification.get("all_repeats_pass") is True
                and required_checks_pass
                and verification.get("source_id_coverage_external_concurrent_failure")
                is True
                and set(verification.get("source_id_coverage_external_keys") or [])
                == {
                    "funnel_listing_id",
                    "funnel_building_id",
                    "funnel_community_id",
                }
                and annaberg_source_hashes_match()
                and len(repeats) == 3
                and all(
                    strict_annaberg_repeat(item, repeat)
                    for repeat, item in enumerate(repeats, start=1)
                )
            )
            unit = (repeats[0].get("unit") or {}) if repeats else {}
            rows = [
                {
                    "property_id": "1765",
                    "property_name": "Annaberg",
                    "website": "www.augustarentalhomes.net",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_same_host_nesthub_community_filter_roster_detail_"
                        "three_repeats_two_negative_controls"
                        if strict_shape
                        else "reject_nesthub_property_boundary_or_native_shape_incomplete"
                    ),
                    "units": 1 if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": 1 if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            1 if strict_shape else 0
                        ),
                        "source_urls": [unit.get("source_api_url")]
                        if strict_shape
                        else [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": unit.get("unit_number"),
                                "source_native_unit_id": (
                                    unit.get("source_ids") or {}
                                ).get("nesthub_listing_id"),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": unit.get("market_rent_low")
                            },
                            "source_api_url": unit.get("source_api_url"),
                        }
                    ]
                    if strict_shape
                    else [],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "park_northside_showmojo_independent_admission"
        ):
            evidence_path = Path(str(payload.get("evidence_path") or ""))
            evidence_bytes = evidence_path.read_bytes() if evidence_path.is_file() else b""
            evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
            evidence = json.loads(evidence_bytes) if evidence_bytes else {}
            identity = evidence.get("canonical_identity") or {}
            verification = evidence.get("verification") or {}
            repeats = [
                repeat
                for repeat in (verification.get("configured_pipeline_repeats") or [])
                if isinstance(repeat, dict)
            ]
            guardrails = evidence.get("guardrails") or {}
            attestation_guardrails = payload.get("guardrails") or {}
            expected_uids = {
                "e7c39f1061",
                "f9a10da061",
                "02193fd097",
                "6cdb90d053",
                "61ac367085",
                "4cb3ea70bd",
                "32d531e0d8",
                "2b1030a0f4",
                "c338072019",
                "ac0583204b",
                "2fb50de04d",
                "5ac2969071",
                "0b5bfa5039",
            }

            def strict_showmojo_repeat(repeat: dict[str, Any]) -> bool:
                units = [
                    unit
                    for unit in (repeat.get("units") or [])
                    if isinstance(unit, dict)
                ]
                chain = repeat.get("official_chain") or {}
                ids = {
                    str((unit.get("source_ids") or {}).get("showmojo_listing_uid") or "")
                    for unit in units
                }
                controls = {
                    str(row.get("provider_listing_uid") or ""): set(row.get("reasons") or [])
                    for row in (chain.get("rejected_rows") or [])
                    if isinstance(row, dict)
                }
                assertions = repeat.get("assertions") or {}
                return bool(
                    repeat.get("adapter") == "showmojo_public"
                    and repeat.get("tier")
                    == "TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN"
                    and (repeat.get("configured_fetch") or {}).get("status") == 200
                    and (repeat.get("configured_fetch") or {}).get("outcome") == "OK"
                    and repeat.get("winning_page_url")
                    == "https://showmojo.com/fea92db007/listings/mapsearch"
                    and int(repeat.get("unit_rows") or 0)
                    == int(repeat.get("native_rows") or 0)
                    == int(repeat.get("native_positive_rent_rows") or 0)
                    == len(units)
                    == 13
                    and int(repeat.get("plan_rows") or 0) == 0
                    and chain.get("attempted") is True
                    and chain.get("failure_reason") == ""
                    and chain.get("showmojo_account") == "fea92db007"
                    and chain.get("application_site_id") == "44261A"
                    and int(chain.get("portfolio_rows") or 0) == 52
                    and int(chain.get("accepted_rows") or 0) == 13
                    and len(chain.get("rejected_rows") or []) == 39
                    and set(chain.get("native_listing_ids") or []) == expected_uids
                    and ids == expected_uids
                    and controls.get("2ae5ea2026", set())
                    >= {
                        "canonical_property_name_absent",
                        "canonical_city_state_zip_mismatch",
                    }
                    and controls.get("097b680090", set())
                    >= {
                        "canonical_property_name_absent",
                        "canonical_city_state_zip_mismatch",
                    }
                    and controls.get("e3afa4f0bf", set())
                    >= {"canonical_city_state_zip_mismatch"}
                    and assertions
                    and all(value is True for value in assertions.values())
                    and all(
                        str(unit.get("unit_number") or "").strip()
                        and str(unit.get("provider_unit_address") or "").strip()
                        and not str(unit.get("floor_plan_name") or "").strip()
                        and "floor_plan_name" in (unit.get("data_gaps") or [])
                        and unit.get("source_property_name") == "Park Northside"
                        and unit.get("source_property_provenance")
                        == "exact_configured_identity_managed_by_reciprocal_manager_showmojo_iframe_name_city_state_zip_filter"
                        and (unit.get("source_ids") or {}).get("showmojo_account")
                        == "fea92db007"
                        and (unit.get("source_ids") or {}).get("rhr_application_site_id")
                        == "44261A"
                        and any(
                            isinstance(unit.get(field), (int, float))
                            and not isinstance(unit.get(field), bool)
                            and float(unit[field]) > 0
                            for field in ("market_rent_low", "market_rent_high")
                        )
                        for unit in units
                    )
                )

            strict_shape = bool(
                str(payload.get("property_id") or "") == "38378"
                and payload.get("replayed_by") == "root_independent_verifier"
                and payload.get("evidence_sha256") == evidence_sha
                and payload.get("independent_replay_exit_code") == 0
                and payload.get("all_repeats_pass") is True
                and payload.get("strict_native_positive_rent_counts") == [13, 13, 13]
                and payload.get("verdict")
                == "pass_independent_configured_pipeline_replay_exact_property_native_positive_rent"
                and identity
                == {
                    "name": "Park Northside",
                    "address": "1601 Roane St",
                    "city": "Richmond",
                    "state": "VA",
                    "zip": "23222",
                    "configured_url": "https://www.parknorthsiderva.com/",
                }
                and verification.get("all_repeats_pass") is True
                and verification.get("strict_native_positive_rent_counts")
                == [13, 13, 13]
                and len(repeats) == 3
                and [int(repeat.get("repeat") or 0) for repeat in repeats]
                == [1, 2, 3]
                and all(strict_showmojo_repeat(repeat) for repeat in repeats)
                and guardrails.get("ordinary_direct_get_only") is True
                and guardrails.get("llm") is False
                and guardrails.get("hyperbrowser") is False
                and int(guardrails.get("hyperbrowser_call_count") or 0) == 0
                and guardrails.get("web_unlocker") is False
                and int(guardrails.get("web_unlocker_call_count") or 0) == 0
                and guardrails.get("proxy") is False
                and guardrails.get("captcha_solving") is False
                and guardrails.get("flaresolverr") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("paid_canary") is False
                and attestation_guardrails.get("compliance_mode") is True
                and all(
                    attestation_guardrails.get(key) is False
                    for key in (
                        "llm",
                        "hyperbrowser",
                        "web_unlocker",
                        "proxy",
                        "captcha_solving",
                        "flaresolverr",
                        "fingerprint_rotation",
                        "paid_canary",
                    )
                )
            )
            first_units = [
                unit
                for unit in ((repeats[0].get("units") or []) if repeats else [])
                if isinstance(unit, dict)
            ]
            source_urls = sorted(
                {
                    str(unit.get("source_api_url") or "")
                    for unit in first_units
                    if str(unit.get("source_api_url") or "").strip()
                }
            )
            rows = [
                {
                    **payload,
                    "property_id": "38378",
                    "property_name": "Park Northside",
                    "website": identity.get("configured_url") or "",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_official_manager_showmojo_chain_mixed_roster_filtered"
                        if strict_shape
                        else "reject_showmojo_independent_replay_or_boundary_incomplete"
                    ),
                    "units": len(first_units) if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": len(first_units) if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            len(first_units) if strict_shape else 0
                        ),
                        "source_urls": source_urls,
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": unit.get("unit_number"),
                                "showmojo_listing_uid": (
                                    unit.get("source_ids") or {}
                                ).get("showmojo_listing_uid"),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": unit.get("market_rent_low"),
                                "market_rent_high": unit.get("market_rent_high"),
                            },
                            "source_api_url": unit.get("source_api_url"),
                        }
                        for unit in first_units
                    ],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("run_type")
            == "current configured scrape_jugnu E2E; LLM off; direct-only; no canary"
            and isinstance(payload.get("results"), list)
            and {str(item.get("property_id") or "") for item in payload["results"]}
            == {"39995", "43520", "14295"}
        ):
            inputs = payload.get("inputs") or {}
            guardrails = payload.get("guardrails") or {}
            assertions = payload.get("assertions") or {}
            results = [
                item for item in payload["results"] if isinstance(item, dict)
            ]
            by_pid = {
                str(item.get("property_id") or ""): item for item in results
            }

            def input_hash_matches(path_key: str, hash_key: str) -> bool:
                source_path = Path(str(inputs.get(path_key) or ""))
                return bool(
                    source_path.is_file()
                    and hashlib.sha256(source_path.read_bytes()).hexdigest()
                    == inputs.get(hash_key)
                )

            p39995 = by_pid.get("39995") or {}
            p43520 = by_pid.get("43520") or {}
            p14295 = by_pid.get("14295") or {}
            rows39995 = [
                item
                for item in (p39995.get("strict_rows") or [])
                if isinstance(item, dict)
            ]
            rows43520 = [
                item
                for item in (p43520.get("strict_rows") or [])
                if isinstance(item, dict)
            ]
            rows14295 = [
                item
                for item in (p14295.get("strict_rows") or [])
                if isinstance(item, dict)
            ]
            unit_numbers43520 = [
                str(item.get("unit_number") or "").strip() for item in rows43520
            ]
            native_ids43520 = [
                str(item.get("source_native_unit_id") or "").strip()
                for item in rows43520
            ]
            common_shape = bool(
                len(results) == len(by_pid) == 3
                and payload.get("self_verified") is True
                and assertions
                and len(assertions) == 6
                and all(value is True for value in assertions.values())
                and (payload.get("focused_test_run") or {}).get("returncode") == 0
                and input_hash_matches("failed344", "failed344_sha256")
                and inputs.get("failed344_sha256")
                == "992b55ec4ca4605296f4996c46bf15a39ce3543b3fb54e1c1383e7f3a1f8137c"
                and input_hash_matches("source", "source_sha256")
                and input_hash_matches("test", "test_sha256")
                and input_hash_matches("properties", "properties_sha256")
                and guardrails.get("llm_enabled") is False
                and guardrails.get("web_unlocker_calls") == 0
                and guardrails.get("flaresolverr") is False
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("paid_canary") is False
                and guardrails.get("pid38677_in_scope") is False
                and guardrails.get("hyperbrowser_calls")
                == {"14295": 0, "39995": 0, "43520": 0}
            )
            strict39995 = bool(
                common_shape
                and p39995.get("property_name") == "South Pointe"
                and p39995.get("canonical_address") == "6220 N Murray Dr"
                and p39995.get("canonical_city") == "Hanahan"
                and p39995.get("canonical_state") == "SC"
                and str(p39995.get("canonical_zip") or "") == "29410"
                and p39995.get("configured_url")
                == "https://www.southpointehanahan.com/"
                and (p39995.get("configured_fetch") or {}).get("status") == 200
                and (p39995.get("configured_fetch") or {}).get("outcome") == "OK"
                and (p39995.get("configured_fetch") or {}).get("final_url")
                == "https://www.southpointehanahan.com/"
                and p39995.get("detected_pms") == "onesite"
                and p39995.get("adapter") == "onesite"
                and p39995.get("tier") == "TIER_1_API_ONESITE_WORKFLOW"
                and int(p39995.get("units") or 0)
                == int(p39995.get("strict_native_positive_rent_rows") or 0)
                == len(rows39995)
                == 1
                and p39995.get("all_emitted_rows_strict") is True
                and p39995.get("native_unit_numbers_nonblank_unique") is True
                and p39995.get("source_property_ids") == ["5272798"]
                and p39995.get("source_provenance") == ["published_portal_shell"]
                and rows39995[0].get("unit_number") == "52"
                and rows39995[0].get("floor_plan_name") == "B1"
                and rows39995[0].get("market_rent_low") == 1263
                and rows39995[0].get("source_property_id") == "5272798"
                and rows39995[0].get("source_property_provenance")
                == "published_portal_shell"
                and rows39995[0].get("source_portal_url")
                == "http://9067331.onlineleasing.realpage.com/"
                and str(rows39995[0].get("source_api_url") or "").startswith(
                    "https://leasing.realpage.com/RP.Leasing.AppService.WebHost/"
                    "workflowstartup/v1/5272798/English?"
                )
            )
            strict43520 = bool(
                common_shape
                and p43520.get("property_name") == "Park at Blanding"
                and p43520.get("canonical_address") == "222 Blairmore Blvd E"
                and p43520.get("canonical_city") == "Orange Park"
                and p43520.get("canonical_state") == "FL"
                and str(p43520.get("canonical_zip") or "") == "32073"
                and p43520.get("configured_url")
                == "http://www.parkatblanding.com/"
                and (p43520.get("configured_fetch") or {}).get("status") == 200
                and (p43520.get("configured_fetch") or {}).get("outcome") == "OK"
                and (p43520.get("configured_fetch") or {}).get("final_url")
                == "https://theparkatblanding.com/"
                and p43520.get("detected_pms") == "onesite"
                and p43520.get("adapter") == "onesite"
                and p43520.get("tier") == "TIER_1_API_ONESITE_RPFP_CWS"
                and int(p43520.get("units") or 0)
                == int(p43520.get("strict_native_positive_rent_rows") or 0)
                == len(rows43520)
                == 26
                and int(p43520.get("plan_summaries") or 0) == 0
                and p43520.get("all_emitted_rows_strict") is True
                and p43520.get("native_unit_numbers_nonblank_unique") is True
                and p43520.get("source_native_ids_nonblank_unique") is True
                and len(unit_numbers43520) == len(set(unit_numbers43520)) == 26
                and all(unit_numbers43520)
                and len(native_ids43520) == len(set(native_ids43520)) == 26
                and all(native_ids43520)
                and p43520.get("source_property_ids") == ["9259508"]
                and p43520.get("source_partner_property_ids") == ["5586626"]
                and p43520.get("source_provenance")
                == ["same_origin_rpfp_property_details"]
                and p43520.get("source_api_urls")
                == ["https://api.ws.realpage.com/v2/property/9259508/units"]
                and all(
                    item.get("source_property_id") == "9259508"
                    and item.get("source_partner_property_id") == "5586626"
                    and item.get("source_property_provenance")
                    == "same_origin_rpfp_property_details"
                    and item.get("source_api_url")
                    == "https://api.ws.realpage.com/v2/property/9259508/units"
                    and item.get("source_portal_url")
                    == "https://theparkatblanding.com/Floor-Plans.aspx"
                    and isinstance(item.get("market_rent_low"), (int, float))
                    and not isinstance(item.get("market_rent_low"), bool)
                    and float(item["market_rent_low"]) > 0
                    for item in rows43520
                )
            )
            strict14295_negative = bool(
                common_shape
                and p14295.get("property_name") == "Timber Ridge Apartment Homes"
                and p14295.get("canonical_address") == "1025 Adams Cir"
                and p14295.get("canonical_city") == "Boulder"
                and p14295.get("canonical_state") == "CO"
                and str(p14295.get("canonical_zip") or "") == "80303"
                and int(p14295.get("units") or 0) == 0
                and int(p14295.get("strict_native_positive_rent_rows") or 0) == 0
                and rows14295 == []
                and p14295.get("tier")
                in {
                    "TIER_1_API_ONESITE_WORKFLOW",
                    "TIER_1_API_ONESITE_EMPTY",
                    "TIER_1_API_ONESITE_NO_RESPONSE",
                }
            )
            strict_all = strict39995 and strict43520 and strict14295_negative
            positive_rows = (
                (p39995, rows39995, strict39995)
                ,
                (p43520, rows43520, strict43520),
            )
            rows = [
                {
                    **result,
                    "outcome": "UNIT_QUALIFIED" if strict_all and passed else "UNIT_UNVERIFIED",
                    "property_identity_match": bool(strict_all and passed),
                    "contamination_verdict": (
                        "pass_exact_published_onesite_property_native_roster"
                        if strict_all and passed
                        else "reject_onesite_boundary_or_native_shape_incomplete"
                    ),
                    "website": result.get("configured_url") or "",
                    "units": len(unit_rows) if strict_all and passed else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": (
                            len(unit_rows) if strict_all and passed else 0
                        ),
                        "rows_with_native_identity_and_positive_rent": (
                            len(unit_rows) if strict_all and passed else 0
                        ),
                        "source_urls": result.get("source_api_urls") or [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": item.get("unit_number"),
                                "source_native_unit_id": item.get(
                                    "source_native_unit_id"
                                ),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low")
                            },
                            "source_api_url": item.get("source_api_url"),
                        }
                        for item in unit_rows
                    ],
                }
                for result, unit_rows, passed in positive_rows
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("run_type")
            == "three live configured boundaries + full scrape_jugnu; direct-only; LLM off; no canary"
            and isinstance(payload.get("results"), list)
            and {str(item.get("property_id") or "") for item in payload["results"]}
            == {"38677", "14295", "291774"}
        ):
            inputs = payload.get("inputs") or {}
            guardrails = payload.get("guardrails") or {}
            assertions = payload.get("assertions") or {}
            focused = payload.get("focused_test_run") or {}
            results = [
                item for item in payload.get("results") or [] if isinstance(item, dict)
            ]
            by_pid = {
                str(item.get("property_id") or ""): item for item in results
            }

            def tor_input_hash_matches(path_key: str, hash_key: str) -> bool:
                source_path = Path(str(inputs.get(path_key) or ""))
                return bool(
                    source_path.is_file()
                    and hashlib.sha256(source_path.read_bytes()).hexdigest()
                    == inputs.get(hash_key)
                )

            tor = by_pid.get("38677") or {}
            timber = by_pid.get("14295") or {}
            gallatin = by_pid.get("291774") or {}
            tor_rows = [
                item for item in (tor.get("samples") or []) if isinstance(item, dict)
            ]
            tor_by_unit = {
                str(item.get("unit_number") or ""): item for item in tor_rows
            }
            expected_tor = {
                "21I": ("Hasbrouck Drive", "A Style", "1", "840", 2625),
                "11B": ("Hasbrouck Drive", "M Style", "2", "955", 2930),
                "20C": ("Kensington Circle", "M Style", "2", "955", 3030),
                "18B": ("Kensington Circle", "E Style", "2", "1080", 2875),
                "3A": ("Kensington Circle", "C Style", "2", "774", 2895),
            }
            strict_common = bool(
                len(results) == len(by_pid) == 3
                and [str(item.get("property_id") or "") for item in results]
                == ["38677", "14295", "291774"]
                and payload.get("self_verified") is True
                and len(assertions) == 9
                and all(value is True for value in assertions.values())
                and focused.get("returncode") == 0
                and "53 passed" in str(focused.get("stdout") or "")
                and tor_input_hash_matches("failed344", "failed344_sha256")
                and inputs.get("failed344_sha256")
                == "992b55ec4ca4605296f4996c46bf15a39ce3543b3fb54e1c1383e7f3a1f8137c"
                and tor_input_hash_matches("source", "source_sha256")
                and tor_input_hash_matches("test", "test_sha256")
                and guardrails.get("llm_enabled") is False
                and guardrails.get("hyperbrowser_calls")
                == {"38677": 0, "14295": 0, "291774": 0}
                and guardrails.get("web_unlocker_calls") == 0
                and guardrails.get("flaresolverr") is False
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("paid_canary") is False
                and guardrails.get("ledger_or_builder_modified") is False
            )
            strict_tor = bool(
                strict_common
                and tor.get("property_name") == "Tor View Village"
                and tor.get("configured_url") == "www.torviewvillageapts.com"
                and (tor.get("configured_fetch") or {}).get("status") == 200
                and (tor.get("configured_fetch") or {}).get("outcome") == "OK"
                and (tor.get("configured_fetch") or {}).get("final_url")
                == "https://www.torviewvillageapts.com/"
                and int((tor.get("configured_fetch") or {}).get("body_bytes") or 0)
                > 50_000
                and tor.get("team_roster_shape") is True
                and tor.get("detected_pms") == "generic_plan_text"
                and tor.get("adapter") == "generic_plan_text"
                and tor.get("tier") == "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
                and tor.get("fallback_chain")
                == ["onesite", "retry:generic_plan_text"]
                and int(tor.get("units") or 0)
                == int(tor.get("strict_native_positive_rent_rows") or 0)
                == len(tor_rows)
                == len(tor_by_unit)
                == 5
                and int(tor.get("plan_summaries") or 0) == 0
                and set(tor_by_unit) == set(expected_tor)
                and all(
                    str(tor_by_unit[unit].get("source_native_unit_id") or "")
                    == unit
                    and tor_by_unit[unit].get("source_street_label") == expected[0]
                    and tor_by_unit[unit].get("source_unit_address_label")
                    == f"{unit} {expected[0]}"
                    and tor_by_unit[unit].get("floor_plan_name") == expected[1]
                    and str(tor_by_unit[unit].get("bedrooms") or "") == expected[2]
                    and str(tor_by_unit[unit].get("sqft") or "") == expected[3]
                    and tor_by_unit[unit].get("market_rent_low") == expected[4]
                    and tor_by_unit[unit].get("source_api_url")
                    == "https://www.torviewvillageapts.com/"
                    and tor_by_unit[unit].get("source_property_provenance")
                    == "exact_configured_property_team_card_roster"
                    and str(tor_by_unit[unit].get("source_listing_url") or "").startswith(
                        "https://"
                    )
                    and "craigslist.org/" in str(
                        tor_by_unit[unit].get("source_listing_url") or ""
                    )
                    for unit, expected in expected_tor.items()
                )
            )
            strict_negatives = bool(
                strict_common
                and timber.get("property_name") == "Timber Ridge Apartment Homes"
                and timber.get("team_roster_shape") is False
                and int(timber.get("units") or 0) == 0
                and int(timber.get("strict_native_positive_rent_rows") or 0) == 0
                and timber.get("tier") != "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
                and gallatin.get("property_name") == "Gallatin Village"
                and gallatin.get("team_roster_shape") is False
                and int(gallatin.get("units") or 0) == 0
                and int(gallatin.get("strict_native_positive_rent_rows") or 0) == 0
                and gallatin.get("tier") != "TIER_1_DOM_STATIC_TEAM_UNIT_ROSTER"
            )
            strict_shape = strict_tor and strict_negatives
            rows = [
                {
                    **tor,
                    "property_id": "38677",
                    "property_name": "Tor View Village",
                    "website": "www.torviewvillageapts.com",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_first_party_team_card_native_roster_two_live_negatives"
                        if strict_shape
                        else "reject_static_team_roster_boundary_or_native_shape_incomplete"
                    ),
                    "units": len(tor_rows) if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": len(tor_rows) if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            len(tor_rows) if strict_shape else 0
                        ),
                        "source_urls": ["https://www.torviewvillageapts.com/"],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": item.get("unit_number"),
                                "source_native_unit_id": item.get(
                                    "source_native_unit_id"
                                ),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low")
                            },
                            "source_api_url": item.get("source_api_url"),
                        }
                        for item in tor_rows
                    ],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "static_residence_1515_park_place_current_configured_e2e"
        ):
            identity = payload.get("canonical_identity") or {}
            cohort = payload.get("cohort") or {}
            reconciliation = payload.get("rp_2026_07_31_reconciliation") or {}
            boundary = [
                item
                for item in (payload.get("four_member_exr_boundary") or [])
                if isinstance(item, dict)
            ]
            repeats = [
                item
                for item in (payload.get("configured_pipeline_repeats") or [])
                if isinstance(item, dict)
            ]
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}
            guardrails = payload.get("guardrails") or {}
            assertions = payload.get("strict_assertions") or {}
            expected_units = {
                "102": ("2", "2", 3000),
                "101": ("4", "2", 4500),
                "103": ("4", "2", 4300),
            }
            expected_ranges = {
                "201-801",
                "205-805",
                "206-806",
                "303-803",
                "307-807",
            }
            expected_boundary = {
                "1515 Park Place": (
                    "https://www.1515parkplace.com/availability.html",
                    3,
                ),
                "200 Montague": (
                    "https://200montaguebk.com/availability",
                    0,
                ),
                "Prosper Prospect Heights": (
                    "https://prosperbrooklyn.com/availability",
                    0,
                ),
                "Franklin Court": (
                    "https://franklincrt.com/availabilities",
                    0,
                ),
            }

            def strict_static_repeat(repeat: dict[str, Any]) -> bool:
                units = [
                    item
                    for item in (repeat.get("units") or [])
                    if isinstance(item, dict)
                ]
                by_number = {
                    str(item.get("unit_number") or ""): item for item in units
                }
                repeat_assertions = repeat.get("assertions") or {}
                return bool(
                    int(repeat.get("repeat") or 0) in {1, 2, 3}
                    and repeat.get("detected_pms") == "generic_plan_text"
                    and repeat.get("adapter") == "generic_plan_text"
                    and repeat.get("tier")
                    == "TIER_1_DOM_STATIC_RESIDENCE_TABLE"
                    and repeat.get("winning_page_url")
                    == "https://www.1515parkplace.com/availability.html"
                    and repeat.get("link_hop_success") is True
                    and repeat.get("link_hop_from")
                    == "https://www.1515parkplace.com/"
                    and int(repeat.get("unit_rows") or 0) == len(units) == 3
                    and int(repeat.get("plan_rows") or 0) == 0
                    and set(by_number) == set(expected_units)
                    and len(by_number) == len(units)
                    and repeat_assertions
                    and all(value is True for value in repeat_assertions.values())
                    and all(
                        str(by_number[number].get("bedrooms") or "") == dimensions[0]
                        and str(by_number[number].get("bathrooms") or "")
                        == dimensions[1]
                        and by_number[number].get("market_rent_low") == dimensions[2]
                        and by_number[number].get("market_rent_high") == dimensions[2]
                        and by_number[number].get("real_native_anchor") is True
                        and by_number[number].get("positive_rent") is True
                        and not str(
                            by_number[number].get("floor_plan_name") or ""
                        ).strip()
                        and by_number[number].get("floor_plan_name_provenance")
                        == "provider_table_does_not_publish_floor_plan_name"
                        and "floor_plan_name"
                        in (by_number[number].get("data_gaps") or [])
                        and "sqft" in (by_number[number].get("data_gaps") or [])
                        and "availability_date"
                        in (by_number[number].get("data_gaps") or [])
                        and not str(
                            by_number[number].get("availability_date") or ""
                        ).strip()
                        and by_number[number].get("availability_date_provenance")
                        == "current_availability_roster_no_explicit_date"
                        and by_number[number].get("source_api_url")
                        == "https://www.1515parkplace.com/availability.html"
                        and by_number[number].get("source_property_name")
                        == "1515 Park Place"
                        and by_number[number].get("source_property_address")
                        == "1515 Park Pl, Brooklyn, NY, 11213"
                        and by_number[number].get("source_property_provenance")
                        == "exact_property_identity_server_rendered_availability_table"
                        for number, dimensions in expected_units.items()
                    )
                )

            boundary_by_name = {
                str(item.get("name") or ""): item for item in boundary
            }
            strict_boundary = bool(
                len(boundary) == len(boundary_by_name) == 4
                and set(boundary_by_name) == set(expected_boundary)
                and all(
                    int(boundary_by_name[name].get("status") or 0) == 200
                    and boundary_by_name[name].get("url") == expected[0]
                    and int(boundary_by_name[name].get("expected_units") or 0)
                    == expected[1]
                    and int(boundary_by_name[name].get("emitted_units") or 0)
                    == expected[1]
                    and boundary_by_name[name].get("pass") is True
                    for name, expected in expected_boundary.items()
                )
                and boundary_by_name["1515 Park Place"].get("unit_numbers")
                == ["102", "101", "103"]
                and all(
                    boundary_by_name[name].get("unit_numbers") == []
                    for name in (
                        "200 Montague",
                        "Prosper Prospect Heights",
                        "Franklin Court",
                    )
                )
            )
            strict_shape = bool(
                str(payload.get("property_id") or "") == "261580"
                and identity
                == {
                    "apartmentid": "261580",
                    "name": "1515 Park Place",
                    "address": "1515 Park Pl",
                    "city": "Brooklyn",
                    "state": "NY",
                    "zip": "11213",
                    "website": "https://www.1515parkplace.com/",
                }
                and cohort.get("boundary")
                == "exact_2026-07-31_FAILED_NO_DATA_344"
                and int(cohort.get("ledger_rows_before") or 0) == 233
                and int(cohort.get("remaining_rows_before") or 0) == 111
                and cohort.get("property_in_ledger_before") is False
                and cohort.get("property_in_remaining_before") is True
                and reconciliation.get("source_sha256")
                == "c9fe58ec076a6ce8a37081ec174ab77fb52653cce374b1bea67160a1534e1c57"
                and int(reconciliation.get("rows") or 0) == 8
                and set(reconciliation.get("unit_ids") or [])
                == set(expected_units) | expected_ranges
                and set(reconciliation.get("accepted_physical_residences") or [])
                == set(expected_units)
                and set(reconciliation.get("excluded_numeric_stack_ranges") or [])
                == expected_ranges
                and strict_boundary
                and len(repeats) == 3
                and [int(repeat.get("repeat") or 0) for repeat in repeats]
                == [1, 2, 3]
                and all(strict_static_repeat(repeat) for repeat in repeats)
                and source_before == source_after
                and assertions
                and all(value is True for value in assertions.values())
                and guardrails.get("ordinary_direct_get_only") is True
                and guardrails.get("llm") is False
                and guardrails.get("hyperbrowser") is False
                and int(guardrails.get("hyperbrowser_call_count") or 0) == 0
                and guardrails.get("web_unlocker") is False
                and int(guardrails.get("web_unlocker_call_count") or 0) == 0
                and guardrails.get("proxy") is False
                and guardrails.get("captcha_solving") is False
                and guardrails.get("flaresolverr") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("paid_canary") is False
                and payload.get("verdict")
                == "pass_exact_identity_static_residence_table_three_native_units"
            )
            first_units = [
                item
                for item in ((repeats[0].get("units") or []) if repeats else [])
                if isinstance(item, dict)
            ]
            rows = [
                {
                    **payload,
                    "property_id": "261580",
                    "property_name": "1515 Park Place",
                    "website": "https://www.1515parkplace.com/",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        "pass_exact_property_static_residence_table_ranges_excluded"
                        if strict_shape
                        else "reject_static_residence_strict_shape_incomplete"
                    ),
                    "units": len(first_units) if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": len(first_units) if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            len(first_units) if strict_shape else 0
                        ),
                        "source_urls": [
                            "https://www.1515parkplace.com/availability.html"
                        ],
                    },
                    "native_samples": [
                        {
                            "identity": {"unit_number": item.get("unit_number")},
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low"),
                                "market_rent_high": item.get("market_rent_high"),
                            },
                            "source_api_url": item.get("source_api_url"),
                        }
                        for item in first_units
                    ],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("lane")
            == "rentcafe_residual_current_configured_funnel_nestio_recovery"
        ):
            property_row = payload.get("property") or {}
            pipeline = payload.get("pipeline") or {}
            guardrails = payload.get("guardrails") or {}
            assertions = payload.get("strict_assertions") or {}
            unit_rows = [
                item for item in (payload.get("units") or []) if isinstance(item, dict)
            ]
            raw_response = payload.get("raw_api_response") or {}
            raw_body = raw_response.get("body") or {}
            raw_items = raw_body.get("items") or []
            source_urls = {
                str(item.get("source_api_url") or "") for item in unit_rows
            }
            unit_numbers = [
                str(item.get("unit_number") or "").strip() for item in unit_rows
            ]
            listing_ids = [
                str((item.get("source_ids") or {}).get("funnel_listing_id") or "").strip()
                for item in unit_rows
            ]
            source_url = str(pipeline.get("winning_page_url") or "")
            strict_shape = bool(
                str(property_row.get("property_id") or "") == "262799"
                and property_row.get("name") == "220 East 72nd"
                and property_row.get("address") == "220 E 72nd St"
                and property_row.get("city") == "New York"
                and property_row.get("state") == "NY"
                and str(property_row.get("zip") or "") == "10021"
                and pipeline.get("adapter") == "funnel"
                and pipeline.get("detected_pms") == "funnel"
                and pipeline.get("tier") == "TIER_1_API_FUNNEL_PUBLISHED_LISTINGS"
                and pipeline.get("errors") == []
                and int(pipeline.get("plan_summaries") or 0) == 0
                and int(pipeline.get("emitted_units") or 0)
                == int(pipeline.get("strict_native_positive_rent_units") or 0)
                == len(unit_rows)
                == len(raw_items)
                == 3
                and source_url.startswith(
                    "https://nestiolistings.com/api/v2/listings/all/?"
                )
                and "property=3152" in source_url
                and source_urls == {source_url}
                and len(unit_numbers) == len(set(unit_numbers)) == 3
                and all(unit_numbers)
                and len(listing_ids) == len(set(listing_ids)) == 3
                and all(listing_ids)
                and all(
                    item.get("real_native_anchor") is True
                    and item.get("positive_rent") is True
                    and str(item.get("source_property_id") or "") == "3152"
                    and item.get("source_property_name") == "220 East 72nd Street"
                    and item.get("source_property_address")
                    == "220 East 72nd Street, New York, NY, 10021"
                    and item.get("source_property_provenance")
                    == "published_nestio_community"
                    for item in unit_rows
                )
                and assertions
                and len(assertions) >= 18
                and all(value is True for value in assertions.values())
                and payload.get("verdict")
                == "pass_exact_configured_page_published_nestio_property_native_units"
                and guardrails.get("direct_only") is True
                and guardrails.get("captcha_solving") is False
                and guardrails.get("fingerprint_rotation") is False
                and guardrails.get("flaresolverr") is False
                and guardrails.get("hyperbrowser") is False
                and int(guardrails.get("hyperbrowser_call_count") or 0) == 0
                and guardrails.get("llm_enabled") is False
                and guardrails.get("paid_canary") is False
                and guardrails.get("proxy") is False
                and guardrails.get("web_unlocker") is False
                and int(guardrails.get("web_unlocker_call_count") or 0) == 0
            )
            rows = [
                {
                    **payload,
                    "property_id": property_row.get("property_id"),
                    "property_name": property_row.get("name"),
                    "website": property_row.get("configured_url") or "",
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        str(payload.get("verdict") or "")
                        if strict_shape
                        else "reject_published_nestio_strict_shape_incomplete"
                    ),
                    "units": len(unit_rows) if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": len(unit_rows) if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            len(unit_rows) if strict_shape else 0
                        ),
                        "source_urls": sorted(source_urls),
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": item.get("unit_number"),
                                "funnel_listing_id": (
                                    item.get("source_ids") or {}
                                ).get("funnel_listing_id"),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low"),
                                "market_rent_high": item.get("market_rent_high"),
                            },
                            "source_api_url": item.get("source_api_url"),
                        }
                        for item in unit_rows
                    ],
                }
            ]
        elif isinstance(payload, dict) and payload.get("strict_outcome") == "UNIT_QUALIFIED":
            unit_rows = [
                item for item in (payload.get("units") or []) if isinstance(item, dict)
            ]
            boundary = payload.get("property_boundary") or {}
            rows = [
                {
                    **payload,
                    "outcome": "UNIT_QUALIFIED",
                    "units": len(unit_rows),
                    "property_identity_match": boundary.get("verdict") == "exact_property_match",
                    "contamination_verdict": "pass_exact_property_boundary_native_positive_rent",
                    "identity_evidence": {
                        "rows_with_native_identity": len(unit_rows),
                        "rows_with_native_identity_and_positive_rent": int(
                            payload.get("native_unit_rows_with_positive_rent") or 0
                        ),
                        "source_urls": [str(payload.get("source_endpoint") or "")],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(item.get("unit_number") or ""),
                                "native_unit_id": str(item.get("native_unit_id") or ""),
                            },
                            "positive_rent_evidence": {"rent": item.get("rent")},
                            "source_api_url": str(payload.get("source_endpoint") or ""),
                        }
                        for item in unit_rows
                    ],
                }
            ]
        elif (
            isinstance(payload, dict)
            and payload.get("strict_accept") is True
            and isinstance(payload.get("full_pipeline_repeats"), list)
        ):
            repeats = [
                row
                for row in payload["full_pipeline_repeats"]
                if isinstance(row, dict)
            ]
            expected_count = int(payload.get("native_positive_rent_rows") or 0)
            expected_ids = {
                str(value)
                for value in (payload.get("source_native_listing_ids") or [])
                if str(value).strip()
            }
            source_url = str(payload.get("source_url") or "")
            helper = payload.get("helper_roster_audit") or {}
            filter_telemetry = helper.get("filter_telemetry") or {}
            source_before = payload.get("source_snapshot_before") or {}
            source_after = payload.get("source_snapshot_after") or {}

            def repeat_native_ids(repeat: dict[str, Any]) -> set[str]:
                return {
                    str((sample.get("source_ids") or {}).get("appfolio_listing_id") or "")
                    for sample in (repeat.get("emitted_samples") or [])
                    if isinstance(sample, dict)
                }

            strict_shape = bool(
                len(repeats) == 3
                and sorted(int(row.get("repeat_index") or 0) for row in repeats)
                == [1, 2, 3]
                and expected_count > 0
                and len(expected_ids) == expected_count
                and payload.get("critical_source_stable_across_repeats") is True
                and str(payload.get("strict_verdict") or "").startswith("pass_")
                and source_before.get("critical_file_sha256")
                == source_after.get("critical_file_sha256")
                and filter_telemetry.get("filter_activated") is True
                and int(filter_telemetry.get("kept") or 0) == expected_count
                and int(filter_telemetry.get("dropped") or 0) > 0
                and int(helper.get("exact_address_rows") or 0) == expected_count
                and all(
                    repeat.get("adapter") == "appfolio"
                    and repeat.get("current_detected_pms") == "wix_nopms"
                    and (repeat.get("configured_fetch") or {}).get("status") == 200
                    and (repeat.get("configured_fetch") or {}).get("outcome") == "OK"
                    and repeat.get("link_hop_success") is True
                    and str(repeat.get("full_pipeline_strict_verdict") or "").startswith(
                        "pass_"
                    )
                    and repeat.get(
                        "all_strict_rows_exact_street_zip_native_id_positive_rent"
                    )
                    is True
                    and int(repeat.get("emitted_unit_rows") or 0)
                    == int(repeat.get("native_identity_rows") or 0)
                    == int(repeat.get("strict_native_positive_rent_rows") or 0)
                    == expected_count
                    and not str(repeat.get("exception") or "")
                    and repeat.get("source_urls") == [source_url]
                    and repeat_native_ids(repeat) == expected_ids
                    for repeat in repeats
                )
            )
            samples = [
                row
                for row in (repeats[0].get("emitted_samples") or [])
                if isinstance(row, dict)
            ] if repeats else []
            rows = [
                {
                    **payload,
                    "outcome": "UNIT_QUALIFIED" if strict_shape else "UNIT_UNVERIFIED",
                    "property_identity_match": strict_shape,
                    "contamination_verdict": (
                        str(payload.get("strict_verdict") or "")
                        if strict_shape
                        else "reject_repeat_or_property_boundary_gate_incomplete"
                    ),
                    "website": payload.get("configured_url") or "",
                    "units": expected_count if strict_shape else 0,
                    "identity_evidence": {
                        "rows_with_native_identity": expected_count if strict_shape else 0,
                        "rows_with_native_identity_and_positive_rent": (
                            expected_count if strict_shape else 0
                        ),
                        "source_urls": payload.get("source_urls") or [],
                    },
                    "native_samples": [
                        {
                            "identity": {
                                "unit_number": str(item.get("unit_number") or ""),
                                "appfolio_listing_id": str(
                                    (item.get("source_ids") or {}).get(
                                        "appfolio_listing_id"
                                    )
                                    or ""
                                ),
                            },
                            "positive_rent_evidence": {
                                "market_rent_low": item.get("market_rent_low"),
                                "market_rent_high": item.get("market_rent_high"),
                            },
                            "source_api_url": str(item.get("source_api_url") or ""),
                        }
                        for item in samples
                    ],
                }
            ]
        elif isinstance(payload, dict) and isinstance(payload.get("recoveries"), list):
            rows = []
            for recovery in payload["recoveries"]:
                if not isinstance(recovery, dict):
                    continue
                unit_rows = [
                    item
                    for item in (recovery.get("units") or [])
                    if isinstance(item, dict)
                ]
                strict_verdict = str(recovery.get("strict_verdict") or "")
                native_count = int(recovery.get("native_identity_rows") or 0)
                priced_count = int(recovery.get("native_positive_rent_rows") or 0)
                source_urls = [
                    str(value)
                    for value in (recovery.get("source_urls") or [])
                    if str(value).strip()
                ]
                rows.append(
                    {
                        **recovery,
                        "outcome": (
                            "UNIT_QUALIFIED"
                            if strict_verdict.startswith("pass_")
                            else "UNIT_UNVERIFIED"
                        ),
                        "units": len(unit_rows),
                        "property_identity_match": strict_verdict.startswith("pass_"),
                        "contamination_verdict": strict_verdict,
                        "identity_evidence": {
                            "rows_with_native_identity": native_count,
                            "rows_with_native_identity_and_positive_rent": priced_count,
                            "source_urls": source_urls,
                        },
                        "native_samples": [
                            {
                                "identity": {
                                    "unit_number": str(
                                        item.get("provider_unit_id")
                                        or item.get("unit")
                                        or item.get("unit_number")
                                        or ""
                                    )
                                },
                                "positive_rent_evidence": {
                                    "rent": item.get("rent")
                                },
                                "source_api_url": str(
                                    item.get("source_url")
                                    or (source_urls[0] if source_urls else "")
                                ),
                            }
                            for item in unit_rows
                        ],
                    }
                )
        else:
            rows = [row for row in (nested_rows or []) if isinstance(row, dict)]

    # The RentManager residual audit stores the actual current-E2E verdict in
    # ``current_local_e2e`` and an explicit gate bit at the wrapper level.
    # Flatten only rows the audit explicitly marks as counting; all other
    # clean-direct-but-not-pipeline-supported candidates remain excluded.
    normalized: list[dict[str, Any]] = []
    for row in rows:
        current_e2e = row.get("current_local_e2e")
        if row.get("counts_toward_strict_207_gate") is True and isinstance(current_e2e, dict):
            normalized.append(
                {
                    **row,
                    **current_e2e,
                    "property_id": row.get("property_id"),
                    "property_name": row.get("property_name"),
                    "website": row.get("website"),
                }
            )
        else:
            normalized.append(row)
    return normalized


def qualify(row: dict[str, Any]) -> tuple[bool, str]:
    if row.get("outcome") != "UNIT_QUALIFIED":
        return False, "outcome_not_unit_qualified"
    if row.get("property_identity_match") is not True:
        return False, "property_identity_not_proven"
    verdict = str(row.get("contamination_verdict") or "")
    if not verdict.startswith("pass_"):
        return False, "contamination_not_passed"
    try:
        unit_count = int(row.get("units") or 0)
    except (TypeError, ValueError):
        unit_count = 0
    if unit_count <= 0:
        return False, "no_units"

    evidence = row.get("identity_evidence") or {}
    if evidence:
        native_count = int(evidence.get("rows_with_native_identity") or 0)
        if native_count <= 0:
            return False, "no_native_identity_rows"
        positive_rent = evidence.get("rows_with_native_identity_and_positive_rent")
        if positive_rent is not None and int(positive_rent or 0) <= 0:
            return False, "no_positive_rent_rows"
    else:
        samples = row.get("native_samples") or row.get("identity_samples") or []
        if not any(
            str((sample.get("identity") or {}).get("unit_number") or "").strip()
            for sample in samples
            if isinstance(sample, dict)
        ):
            return False, "no_native_identity_sample"
    return True, "strict_pass"


def main() -> None:
    with COHORT.open(newline="", encoding="utf-8-sig") as handle:
        cohort_rows = list(csv.DictReader(handle))
    cohort_by_id = {str(row["property_id"]): row for row in cohort_rows}
    if len(cohort_rows) != 344 or len(cohort_by_id) != 344:
        raise SystemExit(
            f"Expected exact unique 344 cohort, got rows={len(cohort_rows)} "
            f"unique={len(cohort_by_id)}"
        )

    ledger_by_id: dict[str, dict[str, Any]] = {}
    artifact_counts: Counter[str] = Counter()
    rejected_counts: Counter[str] = Counter()
    duplicates: list[dict[str, str]] = []

    for lane, path in ARTIFACTS:
        if not path.exists():
            raise SystemExit(f"Missing manifested artifact: {path}")
        for row in load_results(path):
            passed, reason = qualify(row)
            if not passed:
                rejected_counts[reason] += 1
                continue
            property_id = str(row.get("property_id") or "").strip()
            if property_id not in cohort_by_id:
                raise SystemExit(
                    f"Artifact {path.name} contains out-of-cohort pass {property_id}"
                )
            if property_id in ledger_by_id:
                duplicates.append(
                    {
                        "property_id": property_id,
                        "first_lane": str(ledger_by_id[property_id]["evidence_lane"]),
                        "duplicate_lane": lane,
                    }
                )
                continue

            cohort_row = cohort_by_id[property_id]
            evidence = row.get("identity_evidence") or {}
            samples = row.get("native_samples") or row.get("identity_samples") or []
            source_urls = evidence.get("source_urls") or row.get("source_urls") or []
            sample_units = [
                str((sample.get("identity") or {}).get("unit_number") or "").strip()
                for sample in samples
                if isinstance(sample, dict)
            ]
            sample_units = [value for value in sample_units if value]
            ledger_by_id[property_id] = {
                "property_id": property_id,
                "property_name": row.get("property_name") or cohort_row.get("proj_name") or "",
                "website": row.get("website") or cohort_row.get("website") or "",
                "evidence_lane": lane,
                "artifact": str(path),
                "units": int(row.get("units") or 0),
                "property_identity_match": True,
                "contamination_verdict": row.get("contamination_verdict") or "",
                "native_identity_rows": evidence.get("rows_with_native_identity") or "",
                "native_positive_rent_rows": evidence.get(
                    "rows_with_native_identity_and_positive_rent"
                )
                or "",
                "source_urls": " | ".join(str(value) for value in source_urls[:5]),
                "sample_native_unit_ids": " | ".join(sample_units[:5]),
                "local_validation": "artifact_backed_no_paid_canary",
            }
            artifact_counts[lane] += 1

    rows = sorted(ledger_by_id.values(), key=lambda row: int(row["property_id"]))
    fieldnames = list(rows[0]) if rows else []
    with LEDGER.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    metadata_by_id: dict[str, dict[str, str]] = {}
    for metadata_path in (
        ROOT / "strict99_authoritative_ledger.csv",
        ROOT / "strict99_residual245_classification.csv",
    ):
        with metadata_path.open(newline="", encoding="utf-8-sig") as handle:
            for metadata in csv.DictReader(handle):
                property_id = str(metadata.get("property_id") or "").strip()
                if property_id:
                    metadata_by_id[property_id] = metadata

    remaining_rows: list[dict[str, Any]] = []
    for property_id, cohort_row in cohort_by_id.items():
        if property_id in ledger_by_id:
            continue
        metadata = metadata_by_id.get(property_id, {})
        remaining_rows.append(
            {
                "property_id": property_id,
                "property_name": cohort_row.get("proj_name") or metadata.get("property_name") or "",
                "website": cohort_row.get("website") or metadata.get("website") or "",
                "source_adapter_0731": metadata.get("source_adapter_0731")
                or cohort_row.get("adapter")
                or "",
                "current_detected_adapter": metadata.get("current_detected_adapter") or "",
                "rp_oracle_native_unit_rows": metadata.get("rp_oracle_native_unit_rows") or "",
                "rp_oracle_distinct_floorplans": metadata.get("rp_oracle_distinct_floorplans") or "",
                "prior_disposition": metadata.get("disposition")
                or "strict99_false_positive_reintroduced",
            }
        )
    remaining_rows.sort(key=lambda row: int(row["property_id"]))
    remaining_fields = list(remaining_rows[0]) if remaining_rows else []
    with REMAINING.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=remaining_fields)
        writer.writeheader()
        writer.writerows(remaining_rows)

    remaining_adapter_counts = Counter(
        row["current_detected_adapter"] or "<unknown>" for row in remaining_rows
    )

    numerator = len(rows)
    summary = {
        "cohort": str(COHORT),
        "cohort_rows": len(cohort_rows),
        "cohort_unique_properties": len(cohort_by_id),
        "strict_unique_recovered_properties": numerator,
        "strict_recovery_percent": round(numerator / 344 * 100, 4),
        "target_properties": TARGET_PROPERTIES,
        "target_percent": 75.0,
        "remaining_to_target": max(0, TARGET_PROPERTIES - numerator),
        "intermediate_gate_properties": INTERMEDIATE_GATE_PROPERTIES,
        "remaining_to_intermediate_gate": max(
            0, INTERMEDIATE_GATE_PROPERTIES - numerator
        ),
        "remaining_cohort_properties": len(remaining_rows),
        "remaining_current_adapter_counts": dict(
            sorted(remaining_adapter_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "canary_confirmed_properties": 0,
        "paid_canary_run": False,
        "artifact_counts": dict(sorted(artifact_counts.items())),
        "rejected_result_counts": dict(sorted(rejected_counts.items())),
        "deduplicated_artifact_overlaps": duplicates,
        "ledger": str(LEDGER),
        "remaining_ledger": str(REMAINING),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
