"""Phase Z tests — final wiring audit (H11 + H12 invariants)."""

from __future__ import annotations

import pathlib


# Resolve ma_poc/ relative to this file rather than CWD so the tests work
# regardless of where pytest is launched from.
_HERE = pathlib.Path(__file__).resolve().parent        # tests/integration/
_TESTS = _HERE.parent                                  # tests/
_MAPOC = _TESTS.parent                                 # ma_poc/


def test_z_h11_compute_budget_has_production_caller() -> None:
    """H11: compute_budget() must be called from scraper.py (not just tests)."""
    src = (_MAPOC / "pms" / "scraper.py").read_text()
    assert "compute_budget(" in src


def test_z_h11_plan_next_action_has_production_caller() -> None:
    """H11: plan_next_action() must be called from generic.py."""
    src = (_MAPOC / "pms" / "adapters" / "generic.py").read_text()
    assert "plan_next_action(" in src


def test_z_h11_record_source_observations_has_production_caller() -> None:
    """H11: record_source_observations must be called from profile_updater.py."""
    src = (_MAPOC / "services" / "profile_updater.py").read_text()
    assert "record_source_observations(" in src


def test_z_h11_find_cluster_mates_has_production_caller() -> None:
    """H11: find_cluster_mates must be callable from cluster_store.py (function exists)."""
    src = (_MAPOC / "services" / "cluster_store.py").read_text()
    assert "def find_cluster_mates(" in src


def test_z_h11_resolve_source_url_has_production_caller() -> None:
    """H11: _resolve_source_url must be called from runners/jugnu.py."""
    src = (_MAPOC / "scripts" / "runners" / "jugnu.py").read_text()
    assert "_resolve_source_url(" in src


def test_z_h11_merge_sources_has_production_caller() -> None:
    """H11: merge_sources must be called from generic.py._phase5_post_merge."""
    src = (_MAPOC / "pms" / "adapters" / "generic.py").read_text()
    assert "merge_sources(" in src


def test_z_h12_no_dead_branch_comments_in_scraper() -> None:
    """H12: dead-branch comments removed from scraper.py."""
    src = (_MAPOC / "pms" / "scraper.py").read_text()
    assert "future-proofing path" not in src
    assert "Today the link-hop only fires when main is empty" not in src


def test_z_h12_cross_page_merge_is_reachable() -> None:
    """H12: Phase 9 cross-page merge is reachable via should_hop guard."""
    src = (_MAPOC / "pms" / "scraper.py").read_text()
    assert "should_hop" in src
    assert "if main_units and sub_units:" in src


def test_z_gate_pr23_script_exists() -> None:
    """Phase Z gate script must exist at scripts/gates/pr23.py."""
    gate = _MAPOC / "scripts" / "gates" / "pr23.py"
    assert gate.exists(), f"gates/pr23.py must exist at {gate}"


def test_z_all_phase_test_files_exist() -> None:
    """Phase test files must exist at their current canonical locations."""
    expected = [
        "tests/services/test_phase_a_correctness.py",
        "tests/profile/test_phase_b_save_mapping_kwargs.py",
        "tests/integration/propagate/test_propagate_source_url_and_field_patches.py",
        "tests/profile/test_phase_d_observations.py",
        "tests/profile/test_phase_e_dom_hints.py",
        "tests/integration/extract/test_extract_cluster_key_from_detector.py",
        "tests/integration/extract/test_extract_link_hop_guard_and_budget_reachability.py",
        "tests/integration/extract/test_extract_planner_budget_wiring.py",
        "tests/integration/propagate/test_propagate_telemetry_emissions.py",
    ]
    missing = [p for p in expected if not (_MAPOC / p).exists()]
    assert missing == [], f"Missing phase test files: {missing}"
