"""Phase 13 — source telemetry / SLO computation tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from observability.source_telemetry import compute_slo_report


@dataclass
class FakeEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)


def test_empty_events_returns_zero_report() -> None:
    r = compute_slo_report([])
    assert r.n_runs == 0
    assert r.contribution_rates == {}
    assert r.fuzzy_link_rate == 0.0


def test_contribution_rates_computed() -> None:
    events = []
    for i in range(10):
        events.append(
            FakeEvent(
                kind="sources.merged",
                data={
                    "sources_used": ["field_patch"] if i < 7 else [],
                    "pct_complete": 0.9,
                },
            )
        )
    r = compute_slo_report(events)
    assert r.n_runs == 10
    assert r.contribution_rates["field_patch"] == 0.7


def test_planner_decision_distribution_normalized() -> None:
    events = (
        [FakeEvent(kind="sources.merged", data={"sources_used": [], "pct_complete": 1.0})] * 10
        + [FakeEvent(kind="planner.decision", data={"action": "STOP"})] * 10
        + [FakeEvent(kind="planner.decision", data={"action": "ESCALATE_LLM_TARGETED"})] * 5
    )
    r = compute_slo_report(events)
    dist = r.planner_decision_distribution
    assert abs(sum(dist.values()) - 1.0) < 1e-6
    assert abs(dist["STOP"] - 10 / 15) < 1e-6


def test_fuzzy_link_rate_per_run() -> None:
    events = [
        FakeEvent(kind="sources.merged", data={"sources_used": [], "pct_complete": 0.8})
    ] * 100
    events += [FakeEvent(kind="identity.fuzzy_link", data={})] * 5
    r = compute_slo_report(events)
    assert abs(r.fuzzy_link_rate - 0.05) < 1e-6


def test_cluster_hit_rate() -> None:
    events = [
        FakeEvent(kind="sources.merged", data={"sources_used": [], "pct_complete": 1.0})
    ] * 20
    events += [FakeEvent(kind="cluster.mapping_hit", data={})] * 4
    r = compute_slo_report(events)
    assert r.cluster_hit_rate == 0.2


def test_avg_pct_complete() -> None:
    events = [
        FakeEvent(kind="sources.merged", data={"sources_used": [], "pct_complete": p})
        for p in (0.5, 0.6, 0.7, 0.8, 0.9)
    ]
    r = compute_slo_report(events)
    assert abs(r.avg_pct_complete - 0.7) < 1e-6
