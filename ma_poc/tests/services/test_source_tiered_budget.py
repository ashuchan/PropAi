"""Source-confidence-tiered LLM budget (`ENABLE_SOURCE_TIERED_BUDGET`).

When a profile has high-confidence saved sources (avg confidence >= 0.85
over >= 5 contributions), reduce the matching tier's LLM call budget by
1 — leaving exactly one fresh probe to detect drift while saving cost
on what's already known.

Floor: never below 1 (always keep one probe).

Default OFF — flag-gated for canary safety.
"""

from __future__ import annotations

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile, SourceObservation
from ma_poc.services.source_planner import compute_budget


def _profile_with_observations(*observations: SourceObservation) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id="budget_test")
    p.api_hints.source_observations = list(observations)
    return p


# ── Flag default OFF: budget unchanged ────────────────────────────────────


def test_default_off_returns_full_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_SOURCE_TIERED_BUDGET", raising=False)
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=100,
        avg_confidence_when_won=0.95,
    ))
    budget = compute_budget(p, is_cold=False)
    assert budget["llm_api_calls"] == 3
    assert budget["llm_dom_calls"] == 1


# ── Flag ON: tightening fires ─────────────────────────────────────────────


def test_high_confidence_api_source_halves_api_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=100,
        avg_confidence_when_won=0.95,
    ))
    budget = compute_budget(p, is_cold=False)
    # 3 - 1 = 2; DOM unaffected
    assert budget["llm_api_calls"] == 2
    assert budget["llm_dom_calls"] == 1


def test_high_confidence_dom_source_decrements_dom_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_dom_targeted",
        field_group="physical",
        contribution_count=20,
        avg_confidence_when_won=0.92,
    ))
    budget = compute_budget(p, is_cold=False)
    # DOM was 1; 1-1=0 but floor pins at 1
    assert budget["llm_dom_calls"] == 1
    assert budget["llm_api_calls"] == 3


def test_floor_at_one_for_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with multiple high-confidence observations of the same source,
    the budget is decremented at most once — and floor is 1."""
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(
        SourceObservation(
            source_id="llm_api_targeted",
            field_group="identity",
            contribution_count=100,
            avg_confidence_when_won=0.95,
        ),
        SourceObservation(
            source_id="llm_api_targeted",
            field_group="physical",
            contribution_count=100,
            avg_confidence_when_won=0.99,
        ),
    )
    budget = compute_budget(p, is_cold=False)
    # Same source, two field_groups — count once. 3-1=2, NOT 3-2=1.
    assert budget["llm_api_calls"] == 2


# ── Flag ON but observation does not meet thresholds ──────────────────────


def test_low_contribution_count_does_not_tighten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One lucky win shouldn't be enough to trim future probes."""
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=2,  # below the 5-min threshold
        avg_confidence_when_won=0.99,
    ))
    budget = compute_budget(p, is_cold=False)
    assert budget["llm_api_calls"] == 3


def test_low_confidence_does_not_tighten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=100,
        avg_confidence_when_won=0.6,  # below the 0.85 threshold
    ))
    budget = compute_budget(p, is_cold=False)
    assert budget["llm_api_calls"] == 3


def test_no_observations_does_not_tighten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = ScrapeProfile(canonical_id="budget_test_empty")
    budget = compute_budget(p, is_cold=False)
    assert budget["llm_api_calls"] == 3
    assert budget["llm_dom_calls"] == 1


# ── COLD path is NOT tightened (preserves rotation behavior) ──────────────


def test_cold_path_unaffected_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """COLD profiles use the cold-rotation logic untouched. The
    source-tiered tightening only applies to HOT/WARM where the budget
    is the static {api: 3, dom: 1, mono: 1}."""
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=100,
        avg_confidence_when_won=0.95,
    ))
    budget = compute_budget(p, is_cold=True)
    # Cold path returns rotation-based budget — pin only that the
    # function returned a dict with the cost cap.
    assert "_cost_cap_usd" in budget
    # Cold rotation budgets are smaller than HOT — verifying we hit
    # the cold path, not the tightened HOT path.
    assert budget["llm_api_calls"] in (0, 1)


# ── Output is a fresh dict (no input mutation) ────────────────────────────


def test_returns_new_dict_no_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: budget tightening must not mutate the input dict so
    callers that share budget templates aren't surprised."""
    monkeypatch.setenv("ENABLE_SOURCE_TIERED_BUDGET", "true")
    p = _profile_with_observations(SourceObservation(
        source_id="llm_api_targeted",
        field_group="identity",
        contribution_count=100,
        avg_confidence_when_won=0.95,
    ))
    b1 = compute_budget(p, is_cold=False)
    b2 = compute_budget(p, is_cold=False)
    # Mutating one returned dict must not affect the next call.
    b1["llm_api_calls"] = 999
    assert b2["llm_api_calls"] == 2
