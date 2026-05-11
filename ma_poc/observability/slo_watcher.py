"""SLO watcher — checks run-level metrics against thresholds.

Raises alerts as SloViolation dataclasses. Optional events.jsonl
consultation (when ``run_dir`` is provided to ``check``) makes the
success-rate signal robust against ``_meta.verdict`` corruption — see
docs/2026_05_11_regressions_fix_design.md (Bug A v0.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ma_poc.reporting.verdict import (
    resolve_verdict,
    scan_event_ledger_verdicts,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SloThresholds:
    """Configurable SLO thresholds."""

    success_rate_min: float = 0.95
    llm_cost_per_run_max_usd: float = 1.00
    vision_fallback_max_pct: float = 0.05
    drift_noise_max_pct: float = 0.02
    # Minimum fraction of successful properties that won via the
    # zero-LLM-cost replay path (``TIER_1_PROFILE_MAPPING``). Captures
    # the LLM-tax avoidance rate end-to-end. Default 0.0 keeps this
    # observational while the self-learning loop ramps; raise once
    # we have a stable baseline (target ~0.5–0.7 for mature properties
    # per BRD economics: 7.77 → ~0.1 LLM calls/property/day requires
    # the replay tier to win on at least half the population).
    llm_tax_avoidance_min: float = 0.0


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
    run_dir: Path | None = None,
) -> list[SloViolation]:
    """Check run-level metrics against SLO thresholds.

    Args:
        cost_rollup: Dict from CostLedger.total() — {category: total_usd}.
        property_results: List of property result dicts from the run.
        thresholds: SLO thresholds (default: production thresholds).
        run_dir: When provided, ``events.jsonl`` in this directory is
            consulted as the authoritative secondary source for
            per-property verdicts. The runner emits
            ``output.property_emitted`` unconditionally at the tail of
            ``_process_property`` — that signal is preserved even when
            ``_meta.verdict`` is dropped by a downstream serialisation
            step (Bug A 2026-05-11). Omitting ``run_dir`` falls back to
            ``_meta.verdict`` only, preserving the pre-v0.2 behaviour.

    Returns:
        List of SloViolation for any breached thresholds.
    """
    if thresholds is None:
        thresholds = SloThresholds()

    violations: list[SloViolation] = []
    total = len(property_results) or 1

    # Resolve verdicts once. When ``run_dir`` is omitted (legacy callers
    # and most tests), ``verdict_by_pid`` is an empty dict and the
    # resolver falls through to ``_meta.verdict`` — matching pre-v0.2
    # behaviour.
    verdict_by_pid: dict[str, str] = (
        scan_event_ledger_verdicts(run_dir) if run_dir is not None else {}
    )

    def _verdict(p: dict[str, Any]) -> str:
        meta = p.get("_meta", {}) or {}
        pid = str(
            meta.get("canonical_id")
            or meta.get("property_id")
            or p.get("property_id")
            or ""
        )
        return resolve_verdict(
            meta_verdict=str(meta.get("verdict") or ""),
            event_verdict=verdict_by_pid.get(pid) if pid else None,
            pid=pid,
        )

    def _failed(p: dict[str, Any]) -> bool:
        # ``_meta.scrape_tier_used`` is written by the legacy pipeline
        # but NOT by Jugnu, so reading the tier-string heuristic alone
        # would flag every Jugnu property as failed. Keep the heuristic
        # only as a tail-case guard for records whose verdict resolved
        # to empty (no ``_meta.verdict`` AND no emit event — the
        # ``_make_failed_record`` early-return shape).
        verdict = _verdict(p)
        if verdict.startswith("FAILED"):
            return True
        if verdict:
            return False
        meta = p.get("_meta", {}) or {}
        tier_legacy = str(meta.get("scrape_tier_used") or "")
        return "FAIL" in tier_legacy.upper()

    def _tier(p: dict[str, Any]) -> str:
        meta = p.get("_meta", {}) or {}
        extract_result = p.get("_extract_result") or {}
        tier_from_extract = (
            extract_result.get("tier_used")
            if isinstance(extract_result, dict)
            else getattr(extract_result, "tier_used", None)
        )
        return str(meta.get("scrape_tier_used") or tier_from_extract or "")

    # Success rate
    failed = sum(1 for p in property_results if _failed(p))
    success_rate = 1.0 - (failed / total)
    if success_rate < thresholds.success_rate_min:
        fail_cids = [
            (p.get("_meta", {}) or {}).get("canonical_id", "?") for p in property_results if _failed(p)
        ][:5]
        violations.append(
            SloViolation(
                name="success_rate",
                threshold=thresholds.success_rate_min,
                observed=round(success_rate, 4),
                sample=fail_cids,
            )
        )

    # LLM cost
    llm_cost = cost_rollup.get("llm", 0.0) + cost_rollup.get("vision", 0.0)
    if llm_cost > thresholds.llm_cost_per_run_max_usd:
        violations.append(
            SloViolation(
                name="llm_cost_per_run",
                threshold=thresholds.llm_cost_per_run_max_usd,
                observed=round(llm_cost, 4),
            )
        )

    # Vision fallback rate
    vision_used = sum(1 for p in property_results if "vision" in _tier(p).lower())
    vision_pct = vision_used / total
    if vision_pct > thresholds.vision_fallback_max_pct:
        violations.append(
            SloViolation(
                name="vision_fallback_rate",
                threshold=thresholds.vision_fallback_max_pct,
                observed=round(vision_pct, 4),
            )
        )

    # Drift noise (flagged records)
    flagged = sum(1 for p in property_results if p.get("_meta", {}).get("flagged"))
    drift_pct = flagged / total
    if drift_pct > thresholds.drift_noise_max_pct:
        violations.append(
            SloViolation(
                name="drift_noise",
                threshold=thresholds.drift_noise_max_pct,
                observed=round(drift_pct, 4),
            )
        )

    # LLM-tax avoidance rate — fraction of successful properties that
    # extracted via the zero-LLM-cost replay tier. This is THE metric
    # for the self-learning loop's health. The previous regression
    # (every Cloud Run container starting with an empty profile dir,
    # so 0/250 properties replayed) would have shown up here as 0.0
    # observed against any non-zero threshold. Computed only over
    # successful properties — failed scrapes can't have replayed by
    # definition, including them in the denominator would punish bad
    # fetch outcomes via a learning metric they don't control.
    successful = total - failed
    if successful > 0:
        replay_won = sum(
            1 for p in property_results
            if not _failed(p) and "PROFILE_MAPPING" in _tier(p).upper()
        )
        avoidance_rate = replay_won / successful
        if avoidance_rate < thresholds.llm_tax_avoidance_min:
            violations.append(
                SloViolation(
                    name="llm_tax_avoidance_rate",
                    threshold=thresholds.llm_tax_avoidance_min,
                    observed=round(avoidance_rate, 4),
                )
            )

    return violations
