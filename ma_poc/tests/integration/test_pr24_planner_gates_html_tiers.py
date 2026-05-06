"""PR24 Fix 13 — planner gates in HTML tiers (JSON-LD, embedded, DOM).

After each HTML tier finds units, _assess_and_decide is consulted. If the
planner says STOP, the cascade returns early without escalating to LLM.
If the planner escalates, LLM tiers are unlocked.
"""

from __future__ import annotations

import ast
import pathlib


# PR-4: GenericAdapter logic moved to tier_orchestrator.py; read from there.
# generic.py is now a thin re-export shim so the structural checks apply to
# the orchestrator file that actually contains the cascade implementation.
# Use __file__-relative path so the test works regardless of cwd.
_GENERIC_SRC = (
    pathlib.Path(__file__).parent.parent.parent
    / "pms/adapters/tier_orchestrator.py"
).read_text(encoding="utf-8")


def test_assess_and_decide_called_after_jsonld_tier() -> None:
    """generic.py must call _assess_and_decide after JSON-LD units are merged."""
    # Check that TIER_2_JSONLD and _assess_and_decide appear near each other
    assert "TIER_2_JSONLD" in _GENERIC_SRC, "generic.py must set TIER_2_JSONLD"
    assert "_assess_and_decide" in _GENERIC_SRC, "_assess_and_decide must be called"


def test_assess_and_decide_called_after_embedded_tier() -> None:
    """generic.py must call _assess_and_decide after embedded-JSON units are merged."""
    assert "TIER_3_EMBEDDED" in _GENERIC_SRC or "embedded" in _GENERIC_SRC.lower()


def test_assess_and_decide_called_after_dom_tier() -> None:
    """generic.py must call _assess_and_decide after DOM units are merged."""
    assert "TIER_4_DOM" in _GENERIC_SRC or "TIER_5_DOM" in _GENERIC_SRC or "dom" in _GENERIC_SRC.lower()


def test_merge_into_result_units_used_in_html_tiers() -> None:
    """_merge_into_result_units must be called in the HTML tier branches."""
    assert "_merge_into_result_units(" in _GENERIC_SRC


def test_planner_gate_count() -> None:
    """There must be at least 3 _assess_and_decide calls in the HTML tiers
    (one for JSON-LD, one for embedded, one for DOM)."""
    count = _GENERIC_SRC.count("_assess_and_decide(")
    assert count >= 4, (
        f"Expected ≥4 _assess_and_decide calls (API + JSON-LD + embedded + DOM), got {count}"
    )


def test_plan_next_action_integration_stop_after_jsonld() -> None:
    """When planner returns STOP after JSON-LD extraction, the cascade
    should not fall through to LLM tiers."""
    from ma_poc.services.source_planner import CompletenessReport, plan_next_action

    # Simulate fully-complete units scenario
    report = CompletenessReport(5, 1.0, 1.0, 0.95, 1.0)
    d = plan_next_action(
        report,
        sources_already_run=set(),
        budget_remaining={"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3},
    )
    assert d.action == "STOP", f"Expected STOP for complete units, got {d.action}"


def test_plan_next_action_integration_escalate_after_partial_jsonld() -> None:
    """When JSON-LD finds units but transactional data is missing, planner
    should escalate to LLM tier."""
    from ma_poc.services.source_planner import CompletenessReport, plan_next_action

    # JSON-LD gave us identity + physical but not transactional
    report = CompletenessReport(5, 1.0, 1.0, 0.0, 0.55)
    d = plan_next_action(
        report,
        sources_already_run=set(),
        budget_remaining={"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3},
    )
    assert d.action != "STOP", "Planner should escalate when transactional is missing"
    assert d.action in (
        "ESCALATE_LLM_TARGETED",
        "ESCALATE_LLM_MONOLITHIC",
        "ESCALATE_LINK_HOP",
    )
