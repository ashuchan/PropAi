"""SLO watcher — checks run-level metrics against thresholds.

Pure logic, no I/O. Raises alerts as SloViolation dataclasses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SloThresholds:
    """Configurable SLO thresholds."""

    success_rate_min: float = 0.95
    llm_cost_per_run_max_usd: float = 1.00
    vision_fallback_max_pct: float = 0.05
    drift_noise_max_pct: float = 0.02


@dataclass(frozen=True)
class SloViolation:
    """A single SLO threshold breach."""

    name: str
    threshold: float
    observed: float
    sample: list[str] = field(default_factory=list)


def check(
    cost_rollup: dict[str, float],
    property_results: list[dict[str, Any]],
    thresholds: SloThresholds | None = None,
) -> list[SloViolation]:
    """Check run-level metrics against SLO thresholds.

    Args:
        cost_rollup: Dict from CostLedger.total() — {category: total_usd}.
        property_results: List of property result dicts from the run.
        thresholds: SLO thresholds (default: production thresholds).

    Returns:
        List of SloViolation for any breached thresholds.
    """
    if thresholds is None:
        thresholds = SloThresholds()

    violations: list[SloViolation] = []
    total = len(property_results) or 1

    # Success rate
    failed = sum(
        1 for p in property_results
        if "FAIL" in str(p.get("_meta", {}).get("scrape_tier_used", "")).upper()
        or not p.get("_meta", {}).get("scrape_tier_used")
    )
    success_rate = 1.0 - (failed / total)
    if success_rate < thresholds.success_rate_min:
        fail_cids = [
            p.get("_meta", {}).get("canonical_id", "?")
            for p in property_results
            if "FAIL" in str(p.get("_meta", {}).get("scrape_tier_used", "")).upper()
        ][:5]
        violations.append(SloViolation(
            name="success_rate",
            threshold=thresholds.success_rate_min,
            observed=round(success_rate, 4),
            sample=fail_cids,
        ))

    # LLM cost
    llm_cost = cost_rollup.get("llm", 0.0) + cost_rollup.get("vision", 0.0)
    if llm_cost > thresholds.llm_cost_per_run_max_usd:
        violations.append(SloViolation(
            name="llm_cost_per_run",
            threshold=thresholds.llm_cost_per_run_max_usd,
            observed=round(llm_cost, 4),
        ))

    # Vision fallback rate
    vision_used = sum(
        1 for p in property_results
        if "vision" in str(p.get("_meta", {}).get("scrape_tier_used", "")).lower()
    )
    vision_pct = vision_used / total
    if vision_pct > thresholds.vision_fallback_max_pct:
        violations.append(SloViolation(
            name="vision_fallback_rate",
            threshold=thresholds.vision_fallback_max_pct,
            observed=round(vision_pct, 4),
        ))

    # Drift noise (flagged records)
    flagged = sum(
        1 for p in property_results
        if p.get("_meta", {}).get("flagged")
    )
    drift_pct = flagged / total
    if drift_pct > thresholds.drift_noise_max_pct:
        violations.append(SloViolation(
            name="drift_noise",
            threshold=thresholds.drift_noise_max_pct,
            observed=round(drift_pct, 4),
        ))

    return violations
