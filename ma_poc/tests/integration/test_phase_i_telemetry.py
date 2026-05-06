"""Phase I tests — telemetry emissions for IDENTITY_FUZZY_LINK and SOURCES_MERGED."""

from __future__ import annotations

import pathlib


def test_i1_identity_fuzzy_link_wired_in_phase5_post_merge() -> None:
    """IDENTITY_FUZZY_LINK must be emitted via fuzzy_link_callback in _phase5_post_merge."""
    src = pathlib.Path("ma_poc/pms/adapters/tier_orchestrator.py").read_text()
    assert "IDENTITY_FUZZY_LINK" in src, (
        "generic.py must emit IDENTITY_FUZZY_LINK via _phase5_post_merge callback"
    )
    assert "fuzzy_link_callback=_emit_fuzzy" in src or "fuzzy_link_callback=" in src, (
        "merge_sources call must pass a fuzzy_link_callback"
    )


def test_i1_sources_merged_emitted_after_merge() -> None:
    """SOURCES_MERGED must be emitted after merge_sources completes."""
    src = pathlib.Path("ma_poc/pms/adapters/tier_orchestrator.py").read_text()
    assert "SOURCES_MERGED" in src, (
        "generic.py must emit SOURCES_MERGED after _phase5_post_merge"
    )


def test_i1_dom_hints_miss_emitted_in_profile_updater() -> None:
    """DOM_HINTS_MISS must be emitted when consecutive_misses increments."""
    src = pathlib.Path("ma_poc/services/profile_updater.py").read_text()
    assert "DOM_HINTS_MISS" in src, (
        "profile_updater.py must emit DOM_HINTS_MISS on each DOM hints miss"
    )


def test_i1_dom_hints_evicted_emitted_in_profile_updater() -> None:
    """DOM_HINTS_EVICTED must be emitted when selectors are evicted."""
    src = pathlib.Path("ma_poc/services/profile_updater.py").read_text()
    assert "DOM_HINTS_EVICTED" in src, (
        "profile_updater.py must emit DOM_HINTS_EVICTED when field selectors are cleared"
    )


def test_i1_planner_decision_emitted_in_assess_and_decide() -> None:
    """PLANNER_DECISION must be emitted from _assess_and_decide."""
    src = pathlib.Path("ma_poc/pms/adapters/tier_orchestrator.py").read_text()
    assert "PLANNER_DECISION" in src, (
        "generic.py must emit PLANNER_DECISION from _assess_and_decide"
    )
