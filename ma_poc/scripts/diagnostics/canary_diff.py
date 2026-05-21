#!/usr/bin/env python3
"""Diff two ``cloud_run_<date>`` report dirs for the May-13 canary gate.

Consumes baseline and candidate outputs of
``scripts/diagnostics/analyze_cloud_run.py`` and produces:

  1. A markdown table on stdout suitable for the PR description.
  2. An optional JSON dump at ``--out`` for programmatic checks.
  3. An exit code: 0 if every gate in §5.5 of
     ``ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md`` passes, 1 otherwise.

Joining ``failures.csv`` and ``successes.csv`` from both runs by
``property_id`` is what makes the win/regression columns possible. The
analyser's ``summary.json`` alone gives only aggregates; we need the
per-PID rows to surface "this PID newly fails" or "this PID newly
succeeds with N units".

Usage::

    python ma_poc/scripts/diagnostics/canary_diff.py \\
        --baseline ma_poc/data/reports/cloud_run_2026-05-19 \\
        --candidate ma_poc/data/reports/cloud_run_2026-05-22 \\
        --out /tmp/canary_diff.json

Exit codes:
    0 — all gates green
    1 — one or more gates failed (see the FAIL rows in the table)
    2 — bad input (missing files, malformed JSON)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ────────────────────────────────────────────────────────────────────
# Source-row model — mirrors build_canary_csv.PropertyRow but kept
# local so this script has no cross-file import dependency.
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PropertyRow:
    property_id: str
    url: str
    verdict: str
    pms_detected: str
    terminal_tier: str
    units: int


def _safe_int(s: str | None) -> int:
    if s is None:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


SUCCESS_VERDICTS = {"SUCCESS", "SUCCESS_PLAN_LEVEL", "SUCCESS_PARTIAL"}
FAILURE_VERDICTS = {"FAILED_NO_DATA", "FAILED_UNREACHABLE"}


def load_per_pid(report_dir: Path) -> dict[str, PropertyRow]:
    """Load both failures.csv and successes.csv into one dict keyed by PID."""
    out: dict[str, PropertyRow] = {}
    for filename, has_units in (("successes.csv", True), ("failures.csv", False)):
        path = report_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = (row.get("property_id") or "").strip()
                if not pid:
                    continue
                out[pid] = PropertyRow(
                    property_id=pid,
                    url=(row.get("url") or "").strip(),
                    verdict=(row.get("verdict") or "").strip(),
                    pms_detected=(row.get("pms_detected") or "").strip(),
                    terminal_tier=(row.get("terminal_tier") or "").strip(),
                    units=_safe_int(row.get("units")) if has_units else 0,
                )
    return out


def load_summary(report_dir: Path) -> dict[str, Any]:
    path = report_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ────────────────────────────────────────────────────────────────────
# Metric extractors
# ────────────────────────────────────────────────────────────────────


# Tier-label prefixes we count as Tier-1 or Tier-2 (deterministic
# extraction tiers). Anything else — TIER_3_DOM, TIER_4_LLM_*,
# SYNDICATION_*, LLM_GATE_*, FAILED — does not count toward the
# Tier 1+2 share gate.
TIER12_PREFIXES = (
    "TIER_1_API",
    "TIER_1_DOM",
    "TIER_1_5_EMBEDDED",
    "TIER_1_PROFILE_MAPPING",
    "TIER_2_JSONLD",
    "TIER_MERGED_CROSS_PAGE",  # cross-page merges still aggregate Tier-1 sources
)


def tier12_share(tier_dist: dict[str, int]) -> float:
    total = sum(tier_dist.values()) or 1
    t12 = sum(
        n for tier, n in tier_dist.items()
        if isinstance(tier, str)
        and any(tier.startswith(p) for p in TIER12_PREFIXES)
    )
    return t12 / total


def total_units(pid_map: dict[str, PropertyRow]) -> int:
    return sum(r.units for r in pid_map.values())


def _is_success(r: PropertyRow) -> bool:
    return r.verdict in SUCCESS_VERDICTS


def _is_failure(r: PropertyRow) -> bool:
    return r.verdict in FAILURE_VERDICTS


def units_by_pms(pid_map: dict[str, PropertyRow]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in pid_map.values():
        if _is_success(r) and r.units > 0:
            out[r.pms_detected or "unknown"] += r.units
    return dict(out)


# ────────────────────────────────────────────────────────────────────
# Gate definitions — keep these mechanically aligned with §5.5
# ────────────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    name: str
    baseline: Any
    candidate: Any
    delta: Any
    threshold: str
    status: str  # PASS / FAIL / WARN
    detail: str = ""


@dataclass
class DiffReport:
    gates: list[GateResult] = field(default_factory=list)
    newly_succeeded: list[PropertyRow] = field(default_factory=list)  # candidate rows
    newly_failed: list[PropertyRow] = field(default_factory=list)
    unit_count_dropped_hard: list[tuple[PropertyRow, PropertyRow]] = field(default_factory=list)  # (baseline, candidate) where drop >20%
    unit_count_dropped_soft: list[tuple[PropertyRow, PropertyRow]] = field(default_factory=list)  # 1–20%
    new_tier_labels: list[str] = field(default_factory=list)
    per_pms_yield_change: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def failed_any(self) -> bool:
        return any(g.status == "FAIL" for g in self.gates)


def _pct(delta: float, base: float) -> str:
    if base == 0:
        return "n/a"
    return f"{(delta / base) * 100:+.2f}%"


def compute_diff(
    baseline_dir: Path,
    candidate_dir: Path,
) -> DiffReport:
    """Run every gate and return a full DiffReport."""
    base_summary = load_summary(baseline_dir)
    cand_summary = load_summary(candidate_dir)
    base_pids = load_per_pid(baseline_dir)
    cand_pids = load_per_pid(candidate_dir)

    report = DiffReport()

    # ── Gate 1: total unit yield ──────────────────────────────────
    base_units = total_units(base_pids)
    cand_units = total_units(cand_pids)
    delta_units = cand_units - base_units
    pct_delta = (delta_units / base_units) if base_units else 0.0
    status = "PASS" if pct_delta >= 0.05 else ("FAIL" if pct_delta < 0 else "WARN")
    report.gates.append(GateResult(
        name="total_unit_yield",
        baseline=base_units,
        candidate=cand_units,
        delta=f"{delta_units:+d} ({_pct(delta_units, base_units)})",
        threshold="delta >= +5%",
        status=status,
    ))

    # ── Gate 2: Tier 1+2 share ────────────────────────────────────
    base_t12 = tier12_share(base_summary.get("tier_distribution") or {})
    cand_t12 = tier12_share(cand_summary.get("tier_distribution") or {})
    delta_t12 = cand_t12 - base_t12
    status = "PASS" if delta_t12 >= 0 else "FAIL"
    report.gates.append(GateResult(
        name="tier_1_plus_2_share",
        baseline=f"{base_t12*100:.2f}%",
        candidate=f"{cand_t12*100:.2f}%",
        delta=f"{delta_t12*100:+.2f}pp",
        threshold="delta >= 0 (no ground lost)",
        status=status,
    ))

    # ── Gate 3: SUCCESS → FAILED regressions ──────────────────────
    base_success_pids = {p for p, r in base_pids.items() if _is_success(r)}
    cand_failure_pids = {p for p, r in cand_pids.items() if _is_failure(r)}
    regressed = base_success_pids & cand_failure_pids
    # Threshold: ≤ 0.5% of baseline-known-success bucket. Round up so
    # 150 known-SUCCESS allows at most 1.
    threshold_count = max(1, int(len(base_success_pids) * 0.005))
    status = "PASS" if len(regressed) <= threshold_count else "FAIL"
    report.gates.append(GateResult(
        name="success_to_failed_regressions",
        baseline=0,
        candidate=len(regressed),
        delta=f"+{len(regressed)} PID(s)",
        threshold=f"<= {threshold_count} (0.5% of {len(base_success_pids)} known-SUCCESS)",
        status=status,
        detail=", ".join(sorted(regressed)[:10]) + (" ..." if len(regressed) > 10 else ""),
    ))
    for pid in sorted(regressed):
        report.newly_failed.append(cand_pids[pid])

    # ── Gate 4: per-PMS unit yield (any single PMS) ───────────────
    base_by_pms = units_by_pms(base_pids)
    cand_by_pms = units_by_pms(cand_pids)
    worst_pms: tuple[str, float] | None = None
    for pms, base_n in base_by_pms.items():
        if base_n < 50:  # ignore thin slices to avoid noise
            continue
        cand_n = cand_by_pms.get(pms, 0)
        pct = (cand_n - base_n) / base_n
        if worst_pms is None or pct < worst_pms[1]:
            worst_pms = (pms, pct)
    if worst_pms is None:
        status = "PASS"
        detail = "no PMS with >=50 baseline units (sample too thin)"
        delta = "n/a"
    else:
        pms, pct = worst_pms
        status = "PASS" if pct >= -0.05 else "FAIL"
        detail = f"worst: {pms} ({pct*100:+.2f}%)"
        delta = f"{pct*100:+.2f}%"
    report.gates.append(GateResult(
        name="per_pms_unit_yield_floor",
        baseline="-",
        candidate="-",
        delta=delta,
        threshold="every PMS with >=50 base units: delta >= -5%",
        status=status,
        detail=detail,
    ))
    report.per_pms_yield_change = {
        pms: {"baseline": base_by_pms.get(pms, 0), "candidate": cand_by_pms.get(pms, 0)}
        for pms in sorted(set(base_by_pms) | set(cand_by_pms))
    }

    # ── Gate 5: RealPage OLL tier label appears ───────────────────
    base_tiers = set(base_summary.get("tier_distribution") or {})
    cand_tiers = set(cand_summary.get("tier_distribution") or {})
    oll_label = "TIER_1_API_REALPAGE_OLL"
    cand_oll = (cand_summary.get("tier_distribution") or {}).get(oll_label, 0)
    base_oll = (base_summary.get("tier_distribution") or {}).get(oll_label, 0)
    # FAIL only if baseline had zero and candidate also zero. WARN if
    # candidate < 20 (low coverage); PASS at ≥30.
    if cand_oll >= 30:
        status = "PASS"
    elif cand_oll >= 20:
        status = "WARN"
    elif cand_oll == 0 and base_oll == 0:
        status = "FAIL"
    else:
        status = "WARN"
    report.gates.append(GateResult(
        name="realpage_oll_label_coverage",
        baseline=base_oll,
        candidate=cand_oll,
        delta=f"{cand_oll - base_oll:+d}",
        threshold=">=30 PASS, >=20 WARN, 0 FAIL when bucket was 0",
        status=status,
    ))

    # ── Gate 6: new adapter tier labels appearing ─────────────────
    new_labels = sorted(cand_tiers - base_tiers)
    report.new_tier_labels = new_labels
    expected_new = {
        "TIER_1_API_G5",
        "TIER_1_API_KNOCK",
        "TIER_1_API_EQUITY",
        "TIER_1_API_CORTLAND",
        "TIER_1_API_RENTMANAGER",
        "TIER_1_API_APTS247",
        "TIER_1_API_IRVINE",
        "TIER_1_API_ESSEX",
        "TIER_1_API_MAAC",
        "TIER_1_API_RENTVISION",
        "TIER_1_API_REALPAGE_OLL",
        "TIER_1_API_ENTRATA_PROBE",
        "TIER_1_API_ONESITE_EMPTY",
        "TIER_1_API_ONESITE_NO_RESPONSE",
    }
    appeared = expected_new & set(new_labels)
    missing = sorted(expected_new - set(new_labels))
    if appeared:
        status = "PASS" if not missing else "WARN"
    else:
        status = "WARN"
    report.gates.append(GateResult(
        name="new_adapter_tier_labels",
        baseline=0,
        candidate=len(appeared),
        delta=f"+{len(appeared)} labels appearing",
        threshold=">=1 of the expected new TIER_1_API_* labels",
        status=status,
        detail=f"appeared: {sorted(appeared)} | missing: {missing[:6]}{' ...' if len(missing) > 6 else ''}",
    ))

    # ── Gate 7: LLM cost regression watch (advisory) ──────────────
    # Not in §5.5 but a port that doubles LLM spend would warrant
    # discussion before merge. WARN-only.
    base_llm = _safe_float(base_summary.get("llm_cost_total"))
    cand_llm = _safe_float(cand_summary.get("llm_cost_total"))
    if base_llm == 0:
        status = "WARN" if cand_llm > 0 else "PASS"
        delta = f"${cand_llm:.2f} (baseline 0)"
    else:
        pct = (cand_llm - base_llm) / base_llm
        status = "PASS" if pct <= 0.20 else ("WARN" if pct <= 0.50 else "FAIL")
        delta = f"{pct*100:+.2f}%"
    report.gates.append(GateResult(
        name="llm_cost_total",
        baseline=f"${base_llm:.2f}",
        candidate=f"${cand_llm:.2f}",
        delta=delta,
        threshold="<=+20% PASS, <=+50% WARN, >+50% FAIL",
        status=status,
        detail="Tier-1 ports should *reduce* LLM spend by promoting properties to API tier.",
    ))

    # ── Win column: newly SUCCESS ────────────────────────────────
    cand_success_pids = {p for p, r in cand_pids.items() if _is_success(r)}
    base_failure_pids = {p for p, r in base_pids.items() if _is_failure(r)}
    newly_succeeded = (cand_success_pids - base_success_pids) & base_failure_pids
    for pid in sorted(newly_succeeded):
        report.newly_succeeded.append(cand_pids[pid])

    # ── Soft regressions: shared SUCCESS PIDs with material unit drop ─
    shared_success = base_success_pids & cand_success_pids
    for pid in shared_success:
        b = base_pids[pid]
        c = cand_pids[pid]
        if b.units == 0:
            continue
        drop = (b.units - c.units) / b.units
        if drop >= 0.20:
            report.unit_count_dropped_hard.append((b, c))
        elif drop > 0.01:
            report.unit_count_dropped_soft.append((b, c))

    # Hard unit-count drops are a blocker (they're silent regressions)
    if report.unit_count_dropped_hard:
        # Add a separate gate row so it surfaces in the markdown table.
        report.gates.append(GateResult(
            name="hard_unit_drops_on_shared_success",
            baseline=0,
            candidate=len(report.unit_count_dropped_hard),
            delta=f"+{len(report.unit_count_dropped_hard)} PIDs lost >=20% of units",
            threshold="zero PIDs may lose >=20% of their baseline units silently",
            status="FAIL",
            detail=", ".join(b.property_id for b, _ in report.unit_count_dropped_hard[:10])
                   + (" ..." if len(report.unit_count_dropped_hard) > 10 else ""),
        ))

    return report


# ────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────


def render_markdown(report: DiffReport) -> str:
    out: list[str] = []
    out.append("## Canary gate")
    out.append("")
    out.append("| Gate | Baseline | Candidate | Delta | Threshold | Status |")
    out.append("|---|---|---|---|---|---|")
    for g in report.gates:
        out.append(
            f"| {g.name} | {g.baseline} | {g.candidate} | {g.delta} | "
            f"{g.threshold} | {g.status} |"
        )
    out.append("")
    if any(g.detail for g in report.gates):
        out.append("### Gate detail")
        for g in report.gates:
            if g.detail:
                out.append(f"- **{g.name}** -- {g.detail}")
        out.append("")

    if report.newly_succeeded:
        out.append(f"### Wins ({len(report.newly_succeeded)} PIDs newly SUCCESS)")
        # Group by PMS / tier for legibility
        by_tier: dict[str, list[PropertyRow]] = defaultdict(list)
        for r in report.newly_succeeded:
            by_tier[r.terminal_tier or "<no-tier>"].append(r)
        for tier, rows in sorted(by_tier.items(), key=lambda kv: -len(kv[1])):
            out.append(f"- **{tier}** ({len(rows)}): {', '.join(r.property_id for r in rows[:8])}"
                       f"{' ...' if len(rows) > 8 else ''}")
        out.append("")

    if report.newly_failed:
        out.append(f"### Regressions ({len(report.newly_failed)} PIDs newly FAILED)")
        for r in report.newly_failed:
            out.append(f"- {r.property_id} (was {r.pms_detected or '?'}, now {r.verdict})")
        out.append("")

    if report.unit_count_dropped_hard:
        out.append(f"### Hard unit-count drops ({len(report.unit_count_dropped_hard)} PIDs lost >=20%)")
        for b, c in report.unit_count_dropped_hard[:20]:
            out.append(f"- {b.property_id}: {b.units} -> {c.units} units")
        out.append("")

    if report.new_tier_labels:
        out.append(f"### New tier labels appearing")
        for label in report.new_tier_labels:
            out.append(f"- `{label}`")
        out.append("")

    return "\n".join(out)


def to_jsonable(report: DiffReport) -> dict[str, Any]:
    return {
        "gates": [
            {
                "name": g.name,
                "baseline": g.baseline,
                "candidate": g.candidate,
                "delta": g.delta,
                "threshold": g.threshold,
                "status": g.status,
                "detail": g.detail,
            }
            for g in report.gates
        ],
        "newly_succeeded": [r.property_id for r in report.newly_succeeded],
        "newly_failed": [r.property_id for r in report.newly_failed],
        "unit_count_dropped_hard": [
            {"pid": b.property_id, "baseline": b.units, "candidate": c.units}
            for b, c in report.unit_count_dropped_hard
        ],
        "unit_count_dropped_soft": [
            {"pid": b.property_id, "baseline": b.units, "candidate": c.units}
            for b, c in report.unit_count_dropped_soft
        ],
        "new_tier_labels": report.new_tier_labels,
        "per_pms_yield_change": report.per_pms_yield_change,
        "merge_decision": "BLOCK" if report.failed_any else "ALLOW",
    }


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 stdout so Windows cp1252 doesn't crash on any non-ASCII
    # that creeps into output strings. Safe no-op on platforms already
    # using UTF-8. Python 3.7+ guarantees ``reconfigure`` exists.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        description="Diff two analyze_cloud_run output dirs for the canary gate.",
    )
    p.add_argument("--baseline", type=Path, required=True,
                   help="Baseline cloud_run_<date> report dir.")
    p.add_argument("--candidate", type=Path, required=True,
                   help="Candidate cloud_run_<date> report dir.")
    p.add_argument("--out", type=Path,
                   help="Optional path for JSON dump.")
    args = p.parse_args(argv)

    if not args.baseline.exists():
        print(f"[error] baseline dir does not exist: {args.baseline}", file=sys.stderr)
        return 2
    if not args.candidate.exists():
        print(f"[error] candidate dir does not exist: {args.candidate}", file=sys.stderr)
        return 2

    try:
        report = compute_diff(args.baseline, args.candidate)
    except FileNotFoundError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"[error] malformed summary.json: {e}", file=sys.stderr)
        return 2

    print(render_markdown(report))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(to_jsonable(report), indent=2), encoding="utf-8")
        print(f"\n[info] JSON written to {args.out}", file=sys.stderr)

    return 1 if report.failed_any else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
