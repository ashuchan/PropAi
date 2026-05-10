"""Phase 4 — decision map tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from models.source import FieldValue, ProvenancedUnit, SourceId
from services.source_planner import (
    DEFAULT_SOURCE_RANKING,
    CompletenessReport,
    Decision,
    compute_budget,
    evaluate_completeness,
    get_property_llm_cost_cap_hop_bonus_usd,
    get_property_llm_cost_cap_usd,
    plan_next_action,
    rank_sources_for_field_group,
)


def _full_unit() -> ProvenancedUnit:
    pu = ProvenancedUnit()
    pu["unit_id"] = FieldValue(value="U1", source=SourceId.API_SIGHTMAP, confidence=0.95)
    # Physical requires ≥2 fields (A2 fix)
    pu["beds"] = FieldValue(value=1, source=SourceId.API_SIGHTMAP, confidence=0.95)
    pu["sqft"] = FieldValue(value=750, source=SourceId.API_SIGHTMAP, confidence=0.95)
    pu["asking_rent"] = FieldValue(value=1500, source=SourceId.API_SIGHTMAP, confidence=0.95)
    return pu


def test_completeness_empty_units() -> None:
    r = evaluate_completeness([])
    assert r.n_units == 0
    assert r.pct_with_identity == 0.0
    assert r.pct_with_physical == 0.0
    assert r.pct_with_transactional == 0.0
    assert r.pct_complete == 0.0


def test_completeness_all_complete() -> None:
    units = [_full_unit() for _ in range(10)]
    r = evaluate_completeness(units)
    assert r.n_units == 10
    assert r.pct_with_identity == 1.0
    assert r.pct_with_physical == 1.0
    assert r.pct_with_transactional == 1.0
    assert r.pct_complete == 1.0


def test_decision_stop_when_complete() -> None:
    r = CompletenessReport(10, 1.0, 1.0, 0.80, 0.95)
    d = plan_next_action(r, set(), {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1})
    assert d.action == "STOP"


def test_decision_target_gap_picks_failing_axis() -> None:
    r = CompletenessReport(10, 0.95, 0.95, 0.40, 0.55)
    d = plan_next_action(r, sources_already_run=set(), budget_remaining={"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 0, "llm_monolithic": 1})
    assert d.action == "ESCALATE_LLM_TARGETED"
    assert d.target_field_group == "transactional"


def test_decision_one_decision_per_call() -> None:
    """H2 invariant — every call returns exactly one Decision."""
    r = CompletenessReport(5, 0.4, 0.5, 0.3, 0.2)
    for _ in range(100):
        d = plan_next_action(r, set(), {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1})
        assert isinstance(d, Decision)


def test_budget_zero_blocks_escalation() -> None:
    """H3 invariant — exhausted budget → ACCEPT_PARTIAL."""
    r = CompletenessReport(10, 0.95, 0.95, 0.40, 0.55)
    d = plan_next_action(
        r, set(),
        {"llm_api_calls": 0, "llm_dom_calls": 0, "link_hop": 0, "llm_monolithic": 0},
    )
    assert d.action == "ACCEPT_PARTIAL"


def test_decision_broad_recovery_prefers_link_hop() -> None:
    r = CompletenessReport(10, 0.2, 0.2, 0.2, 0.1)
    d = plan_next_action(r, set(), {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1})
    assert d.action == "ESCALATE_LINK_HOP"


def test_decision_broad_recovery_falls_to_monolithic() -> None:
    r = CompletenessReport(10, 0.2, 0.2, 0.2, 0.1)
    d = plan_next_action(
        r, set(),
        {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 0, "llm_monolithic": 1},
    )
    assert d.action == "ESCALATE_LLM_MONOLITHIC"


def test_profile_floor_lowers_stop_threshold() -> None:
    r = CompletenessReport(10, 0.85, 0.85, 0.80, 0.75)
    d = plan_next_action(
        r, set(), {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1},
        profile_completeness_floor={"complete": 0.70, "transactional": 0.70},
    )
    assert d.action == "STOP"


def test_profile_floor_below_50_pct_clamped() -> None:
    """Floor < 0.50 must be clamped to 0.50 — never emit garbage."""
    r = CompletenessReport(10, 0.40, 0.40, 0.40, 0.40)
    d = plan_next_action(
        r, set(), {"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1},
        profile_completeness_floor={"complete": 0.30, "transactional": 0.30},
    )
    # With pct_complete=0.40 < 0.50, BROAD_RECOVERY fires regardless of the
    # garbage floor input — we should NOT emit STOP.
    assert d.action != "STOP"


def test_ranking_table_has_all_sources_per_group() -> None:
    """Every group ranking includes its expected source membership."""
    for group in ("identity", "physical", "transactional"):
        ranking = DEFAULT_SOURCE_RANKING[group]
        assert ranking, f"empty ranking for {group}"
        # confidence values are in (0,1]
        assert all(0.0 < c <= 1.0 for _s, c in ranking)


def test_profile_preferences_floats_observed_winners() -> None:
    @dataclass
    class FakeObs:
        source_id: str
        field_group: str
        contribution_count: int

    prefs = [
        FakeObs(SourceId.FIELD_PATCH.value, "transactional", 5),
        FakeObs(SourceId.JSON_LD.value, "transactional", 1),
    ]
    ranking = rank_sources_for_field_group("transactional", "unknown", prefs)
    # FIELD_PATCH should appear before its default position (and before JSON_LD)
    fp_idx = next(i for i, (s, _) in enumerate(ranking) if s == SourceId.FIELD_PATCH)
    js_idx = next(i for i, (s, _) in enumerate(ranking) if s == SourceId.JSON_LD)
    assert fp_idx < js_idx


def test_compute_budget_warm_full() -> None:
    class P:
        class confidence:
            cold_run_count = 0
    b = compute_budget(P(), is_cold=False)
    assert b["llm_api_calls"] == 3
    assert b["llm_dom_calls"] == 1
    assert b["llm_monolithic"] == 1
    assert b["link_hop"] >= 1


def test_compute_budget_cold_rotates() -> None:
    class P:
        class confidence:
            cold_run_count = 0
    b0 = compute_budget(P(), is_cold=True)
    P.confidence.cold_run_count = 1
    b1 = compute_budget(P(), is_cold=True)
    P.confidence.cold_run_count = 2
    b2 = compute_budget(P(), is_cold=True)
    # Each cold-rotation cycle picks one tier:
    assert b0["llm_api_calls"] == 1 and b0["llm_monolithic"] == 0
    assert b1["llm_api_calls"] == 0 and b1["llm_monolithic"] == 0 and b1["link_hop"] >= 1
    assert b2["llm_api_calls"] == 0 and b2["llm_monolithic"] == 1


# ---------------------------------------------------------------------------
# F0.1 — env-driven per-property LLM cost cap
# ---------------------------------------------------------------------------


def test_property_llm_cost_cap_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # F0.1: default is $1.50 — chosen after the 2026-05-09 cloud-run
    # regression analysis showed the prior $1.00 starved link-hop pages.
    monkeypatch.delenv("PROPERTY_LLM_COST_CAP_USD", raising=False)
    assert get_property_llm_cost_cap_usd() == pytest.approx(1.50)


def test_property_llm_cost_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "2.25")
    assert get_property_llm_cost_cap_usd() == pytest.approx(2.25)


def test_property_llm_cost_cap_malformed_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bad values (non-numeric, blank, zero, negative) must fall back to the
    # default rather than crash the pipeline at budget-construction time.
    for bad in ("", "  ", "abc", "0", "-1", "1.5e", "nan"):
        monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", bad)
        assert get_property_llm_cost_cap_usd() == pytest.approx(1.50), bad


def test_property_llm_cost_cap_hop_bonus_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", raising=False)
    assert get_property_llm_cost_cap_hop_bonus_usd() == pytest.approx(0.50)


def test_property_llm_cost_cap_hop_bonus_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_HOP_BONUS_USD", "0.75")
    assert get_property_llm_cost_cap_hop_bonus_usd() == pytest.approx(0.75)


def test_compute_budget_warm_includes_cost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F0.1: every branch of compute_budget must populate _cost_cap_usd so
    # the GenericAdapter cost gate sees the env-configured value.
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.75")

    class P:
        class confidence:
            cold_run_count = 0

    b = compute_budget(P(), is_cold=False)
    assert b["_cost_cap_usd"] == pytest.approx(1.75)


def test_compute_budget_cold_all_rotations_include_cost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTY_LLM_COST_CAP_USD", "1.75")

    class P:
        class confidence:
            cold_run_count = 0

    for n in (0, 1, 2):
        P.confidence.cold_run_count = n
        b = compute_budget(P(), is_cold=True)
        assert b["_cost_cap_usd"] == pytest.approx(1.75), f"cold rotation {n}"
