"""Phase 3 ActionDecider tests — spec §10, cases D1–D4 + budget invariants.

Validates ActionDecider.decide() rule priority and RC3 deferral logic.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.signal_engine.decider import (
    ActionDecider,
    ActionType,
    DecisionContext,
    DOMAnalysisResult,
    ExtractionAction,
)
from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal
from ma_poc.pms.signal_engine.ranker import RankedSignal


def _ranked(kind: SourceKind, url: str, score: int) -> RankedSignal:
    sig = SourceSignal(kind=kind, url=url)
    return RankedSignal(signal=sig, composite_score=score, reason=f"test:{kind}")


@pytest.fixture
def decider() -> ActionDecider:
    return ActionDecider()


# ── D1: RC3 — DOM nav hint + 0 units + budget + high-conf hop → HOP ───────────

def test_d1_rc3_defer_monolithic_to_hop(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.LLM_HINT, "https://example.com/floorplans", 10_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 1, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(
            unit_count=0,
            navigation_hint="https://example.com/floorplans",
        ),
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.HOP_TO_URL
    assert action.rationale == "dom_analysis_defer_monolithic_to_hop"
    # budget_after must NOT decrement llm_monolithic (deferred, not consumed)
    assert action.budget_after["llm_monolithic"] == 1
    assert action.target is not None
    assert action.target.signal.kind == SourceKind.LLM_HINT


# ── D2: RC3 condition not met when llm_monolithic budget = 0 ──────────────────

def test_d2_rc3_no_defer_when_monolithic_budget_zero(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.LLM_HINT, "https://example.com/floorplans", 10_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 0, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(
            unit_count=0,
            navigation_hint="https://example.com/floorplans",
        ),
    )
    action = decider.decide(ctx)
    # RC3 requires monolithic budget > 0; without it falls to Rule 4 (dispatch top signal)
    assert action.action_type == ActionType.HOP_TO_URL
    assert action.rationale != "dom_analysis_defer_monolithic_to_hop"
    assert action.rationale.startswith("top_signal:")


# ── D3: Rule 1 — units already found → STOP immediately ──────────────────────

def test_d3_units_found_stop(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.LLM_HINT, "https://example.com/fp", 10_000)],
        current_unit_count=10,
        budget={"llm_monolithic": 1},
        dom_analysis_result=DOMAnalysisResult(unit_count=0, navigation_hint="https://example.com/fp"),
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.STOP
    assert action.rationale == "units_found"


# ── D4: Rule 3 — no signals → STOP ───────────────────────────────────────────

def test_d4_no_signals_stop(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[],
        current_unit_count=0,
        budget={"llm_monolithic": 1},
        dom_analysis_result=None,
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.STOP
    assert action.rationale == "no_signals"


# ── Budget immutability invariant ─────────────────────────────────────────────

def test_budget_after_is_new_dict(decider: ActionDecider) -> None:
    original_budget = {"llm_monolithic": 1, "link_hop": 2}
    ctx = DecisionContext(
        ranked_signals=[],
        current_unit_count=0,
        budget=original_budget,
        dom_analysis_result=None,
    )
    action = decider.decide(ctx)
    assert action.budget_after is not original_budget, "budget_after must be a new dict"


# ── RC3 only triggers with HIGH_CONF_HOP_KINDS ────────────────────────────────

def test_rc3_pms_prior_not_high_conf_hop(decider: ActionDecider) -> None:
    # PMS_PRIOR has score 5_000 < 9_000 threshold — should NOT trigger RC3 deferral
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.PMS_PRIOR, "https://example.com/fp", 5_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 1, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(unit_count=0, navigation_hint="https://example.com/fp"),
    )
    action = decider.decide(ctx)
    # PMS_PRIOR score 5_000 < 9_000 threshold, so RC3 deferral does NOT fire
    assert action.rationale != "dom_analysis_defer_monolithic_to_hop"


def test_rc3_profile_winning_triggers_deferral(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[
            _ranked(SourceKind.PROFILE_WINNING, "https://example.com/winning", 10_001)
        ],
        current_unit_count=0,
        budget={"llm_monolithic": 1, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(unit_count=0, navigation_hint="https://example.com/fp"),
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.HOP_TO_URL
    assert action.rationale == "dom_analysis_defer_monolithic_to_hop"


def test_rc3_no_navigation_hint_does_not_defer(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.LLM_HINT, "https://example.com/fp", 10_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 1, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(unit_count=0, navigation_hint=None),
    )
    action = decider.decide(ctx)
    assert action.rationale != "dom_analysis_defer_monolithic_to_hop"


# ── Rule 4: dispatch top signal with correct action mapping ───────────────────

def test_rule4_api_response_maps_to_parse_api(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.API_RESPONSE, "https://api.example.com/units", 8_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 0, "link_hop": 0},
        dom_analysis_result=None,
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.PARSE_API


def test_rule4_internal_link_maps_to_hop(decider: ActionDecider) -> None:
    ctx = DecisionContext(
        ranked_signals=[_ranked(SourceKind.INTERNAL_LINK, "https://example.com/floor-plans", 4_000)],
        current_unit_count=0,
        budget={"llm_monolithic": 0, "link_hop": 2},
        dom_analysis_result=None,
    )
    action = decider.decide(ctx)
    assert action.action_type == ActionType.HOP_TO_URL
    assert int(action.budget_after.get("link_hop", 0)) == 1


# ── _decrement never mutates input ────────────────────────────────────────────

def test_decrement_returns_new_dict(decider: ActionDecider) -> None:
    budget = {"link_hop": 3}
    new_b = decider._decrement(budget, ActionType.HOP_TO_URL)
    assert new_b is not budget
    assert new_b["link_hop"] == 2
    assert budget["link_hop"] == 3  # original unchanged
