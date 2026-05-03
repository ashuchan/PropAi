"""
Source contribution telemetry — Phase 13.

Aggregates SOURCES_MERGED, MAPPING_DRIFT_DETECTED, IDENTITY_FUZZY_LINK,
PLANNER_DECISION, CLUSTER_MAPPING_HIT events into per-source dashboards.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class SourceContributionReport:
    n_runs: int
    contribution_rates: dict[str, float] = field(default_factory=dict)
    eviction_rates: dict[str, float] = field(default_factory=dict)
    fuzzy_link_rate: float = 0.0
    planner_decision_distribution: dict[str, float] = field(default_factory=dict)
    cluster_hit_rate: float = 0.0
    avg_pct_complete: float = 0.0


def compute_slo_report(events_iter: Iterable) -> SourceContributionReport:
    """Aggregate over a window of SOURCES_MERGED + drift/eviction events.

    Each event must expose ``.kind`` (string or enum) and ``.data`` (dict).
    """
    n_runs = 0
    source_appearances: Counter = Counter()
    mapping_drift = 0
    fuzzy_links = 0
    decision_counts: Counter = Counter()
    cluster_hits = 0
    pct_complete_sum = 0.0

    for ev in events_iter:
        kind = ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind)
        data = getattr(ev, "data", {}) or {}
        if kind == "sources.merged":
            n_runs += 1
            for s in data.get("sources_used", []) or []:
                source_appearances[s] += 1
            pct_complete_sum += float(data.get("pct_complete", 0.0) or 0.0)
        elif kind == "mapping.drift_detected":
            mapping_drift += 1
        elif kind == "identity.fuzzy_link":
            fuzzy_links += 1
        elif kind == "planner.decision":
            decision_counts[data.get("action", "?")] += 1
        elif kind == "cluster.mapping_hit":
            cluster_hits += 1

    if n_runs == 0:
        return SourceContributionReport(n_runs=0)

    n_decisions = max(sum(decision_counts.values()), 1)
    return SourceContributionReport(
        n_runs=n_runs,
        contribution_rates={src: count / n_runs for src, count in source_appearances.items()},
        eviction_rates={
            "mapping_drift_per_run": mapping_drift / n_runs,
            "fuzzy_link_per_run": fuzzy_links / n_runs,
        },
        fuzzy_link_rate=fuzzy_links / n_runs,
        planner_decision_distribution={
            a: c / n_decisions for a, c in decision_counts.items()
        },
        cluster_hit_rate=cluster_hits / n_runs,
        avg_pct_complete=pct_complete_sum / n_runs,
    )
