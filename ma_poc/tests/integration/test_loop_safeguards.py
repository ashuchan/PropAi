"""Loop-safeguard tests for the H1, H2, H3 invariants from
CLAUDE_XSOURCE_AND_LEARNING.

H1: merger is a pure function — same input always yields same output, no input mutation.
H2: planner returns at most ONE Decision per call (one escalation per merge cycle).
H3: LLM budget caps — exhausted budget --> ACCEPT_PARTIAL, never both LLM-targeted
    and LLM-monolithic in the same run.

These tests are independent of the cascade restructure (Phase 5) — they exercise
the merger and planner directly with pre-built ExtractedSources. The Phase 5 +
Phase 9 cascade restructures will further wire these invariants into the
runtime; the merger / planner contract guarantees them at the building-block level.
"""

from __future__ import annotations

import pytest

from ma_poc.models.source import ExtractedSource, FieldValue, SourceId
from ma_poc.services.source_merger import merge_sources
from ma_poc.services.source_planner import (
    CompletenessReport,
    Decision,
    plan_next_action,
)


def _src_unit_level(unit_id: str, rent: int, conf: float = 0.95) -> ExtractedSource:
    return ExtractedSource(
        source_id=SourceId.API_SIGHTMAP,
        source_url="https://example.com",
        envelope_hash="h",
        units=[
            {
                "unit_id": FieldValue(value=unit_id, source=SourceId.API_SIGHTMAP, confidence=conf),
                "asking_rent": FieldValue(value=rent, source=SourceId.API_SIGHTMAP, confidence=conf),
            }
        ],
        has_unit_ids=True,
        is_floor_plan_level=False,
    )


# ── H1: merger is a pure function ──────────────────────────────────────


def test_h1_merger_pure_function() -> None:
    sources = [_src_unit_level("U1", 1500), _src_unit_level("U2", 1700)]
    snapshot_a = [list(s.units) for s in sources]
    out_a = merge_sources(sources, "test_prop")
    out_b = merge_sources(sources, "test_prop")
    assert out_a == out_b
    snapshot_b = [list(s.units) for s in sources]
    assert snapshot_a == snapshot_b, "merge_sources must not mutate inputs"


def test_h1_merger_idempotent_under_repeated_call() -> None:
    sources = [_src_unit_level("U1", 1500)]
    for _ in range(20):
        out = merge_sources(sources, "p")
        assert len(out) == 1


# ── H2: planner returns one Decision per call ──────────────────────────


def test_h2_planner_returns_single_decision() -> None:
    """Every plan_next_action call returns exactly ONE Decision object.
    The cascade-restructure (Phase 5) calls plan_next_action once per page;
    this test guards the building-block contract."""
    r = CompletenessReport(10, 0.4, 0.5, 0.3, 0.2)
    for _ in range(50):
        d = plan_next_action(
            r,
            sources_already_run=set(),
            budget_remaining={"llm_api_calls": 1, "llm_dom_calls": 1, "link_hop": 1, "llm_monolithic": 1},
        )
        assert isinstance(d, Decision)
        assert d.action in (
            "STOP",
            "ESCALATE_LLM_TARGETED",
            "ESCALATE_LINK_HOP",
            "ESCALATE_LLM_MONOLITHIC",
            "ACCEPT_PARTIAL",
        )


# ── H3: LLM budget caps ─────────────────────────────────────────────────


def test_h3_budget_zero_blocks_all_escalations() -> None:
    """With every budget at 0, ANY low-completeness scenario --> ACCEPT_PARTIAL."""
    r_broad = CompletenessReport(10, 0.1, 0.1, 0.1, 0.1)
    r_target = CompletenessReport(10, 0.95, 0.95, 0.40, 0.55)
    for r in (r_broad, r_target):
        d = plan_next_action(
            r,
            sources_already_run=set(),
            budget_remaining={"llm_api_calls": 0, "llm_dom_calls": 0, "link_hop": 0, "llm_monolithic": 0},
        )
        assert d.action == "ACCEPT_PARTIAL"


def test_h3_targeted_then_monolithic_never_simultaneous() -> None:
    """A single planner call cannot return BOTH LLM-targeted AND LLM-monolithic.

    Phase 5 cascade is responsible for honoring the per-run budget = 1 by
    decrementing it after each escalation. The planner contract: ONE Decision
    per call, and the action enum is single-valued.
    """
    r = CompletenessReport(10, 0.95, 0.95, 0.40, 0.55)
    d = plan_next_action(
        r, set(), {"llm_api_calls": 1, "llm_dom_calls": 0, "link_hop": 0, "llm_monolithic": 1}
    )
    # In the TARGET_GAP regime, the planner should escalate to LLM_TARGETED,
    # not LLM_MONOLITHIC (which is reserved for BROAD_RECOVERY).
    assert d.action == "ESCALATE_LLM_TARGETED"
    assert d.action != "ESCALATE_LLM_MONOLITHIC"


def test_h3_broad_recovery_prefers_link_hop_over_monolithic() -> None:
    r = CompletenessReport(10, 0.10, 0.10, 0.10, 0.05)
    d = plan_next_action(
        r, set(), {"llm_api_calls": 0, "llm_dom_calls": 0, "link_hop": 1, "llm_monolithic": 1}
    )
    assert d.action == "ESCALATE_LINK_HOP"


def test_h3_decision_action_is_singular() -> None:
    """A Decision has a single action string — never both."""
    d = Decision(action="STOP")
    assert d.action == "STOP"
    assert isinstance(d.action, str)
