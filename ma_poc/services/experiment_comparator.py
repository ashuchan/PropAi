"""Compare a legacy-mode and universe-mode replay of the same property.

Quantifies the impact of the new explorer along several axes so the
experiment harness can produce a "promote / hold" verdict:

  - Unit count delta
  - Per-field coverage delta (beds, baths, sqft, rent, availability)
  - Jaccard overlap on the canonical dedupe key
  - Quality-gate flips (minimum, important)
  - LLM cost delta + call delta
  - Wall-clock delta
  - Regression flag (universe lost units that legacy had)

All inputs are plain dicts so this module has no runtime dependency on
Playwright, the L1 fetcher, or the LLM provider — the harness can call
``compare`` after both replays complete in any process.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ma_poc.services.extraction_quality import (
    aggregate_quality,
    has_important_fields,
    meets_minimum_fields,
)


# Field-name aliases — kept consistent with extraction_quality.py so the
# coverage delta uses the same definition of "field present" as the
# quality gate the explorer optimises against.
_BEDS_KEYS = ("beds", "bedrooms")
_BATHS_KEYS = ("baths", "bathrooms")
_SQFT_KEYS = ("sqft", "area", "square_feet")
_RENT_KEYS = ("rent_low", "market_rent_low", "asking_rent", "rent")
_AVAIL_KEYS = ("available_date", "availability_date", "move_in_date")
_UNIT_ID_KEYS = ("unit_id", "unit_number", "_unit_number")
_FLOOR_PLAN_KEYS = ("floor_plan_name", "_floor_plan", "floorplan_name")

_TRACKED_FIELDS: dict[str, tuple[str, ...]] = {
    "beds": _BEDS_KEYS,
    "baths": _BATHS_KEYS,
    "sqft": _SQFT_KEYS,
    "rent": _RENT_KEYS,
    "available_date": _AVAIL_KEYS,
}


@dataclass
class ReplayResult:
    """Per-mode replay output captured by the experiment harness.

    All fields are plain types so the comparator can be unit-tested
    without spinning up an adapter or LLM provider.
    """

    units: list[dict[str, Any]]
    tier_used: str
    llm_cost_usd: float = 0.0
    llm_calls: int = 0
    wall_clock_ms: int = 0
    errors: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Comparison:
    """Result of comparing legacy vs universe on one property."""

    canonical_id: str
    bucket: str

    legacy_units: int
    universe_units: int
    unit_count_delta: int

    field_coverage_delta: dict[str, float]  # universe - legacy, per field
    unit_set_overlap_jaccard: float

    legacy_minimum_passes: bool
    universe_minimum_passes: bool
    legacy_important_passes: bool
    universe_important_passes: bool
    quality_gate_minimum_flip: str  # gain | loss | unchanged
    quality_gate_important_flip: str

    cost_delta_usd: float  # universe - legacy
    llm_call_delta: int
    latency_delta_ms: int

    regression_flag: bool  # universe == [] AND legacy != []
    win_flag: bool  # universe strictly better than legacy

    legacy_errors: list[str]
    universe_errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare(
    legacy: ReplayResult,
    universe: ReplayResult,
    *,
    canonical_id: str,
    bucket: str,
) -> Comparison:
    """Quantify the difference between a legacy and a universe replay.

    Always succeeds — empty inputs produce a Comparison with all-zero
    deltas and the regression / win flags computed accordingly.
    """
    leg_n = len(legacy.units)
    uni_n = len(universe.units)

    field_delta = _field_coverage_delta(legacy.units, universe.units)
    overlap = _jaccard_overlap(legacy.units, universe.units)

    leg_q = aggregate_quality(legacy.units, threshold=0.5)
    uni_q = aggregate_quality(universe.units, threshold=0.5)

    return Comparison(
        canonical_id=canonical_id,
        bucket=bucket,
        legacy_units=leg_n,
        universe_units=uni_n,
        unit_count_delta=uni_n - leg_n,
        field_coverage_delta=field_delta,
        unit_set_overlap_jaccard=overlap,
        legacy_minimum_passes=leg_q.minimum_met,
        universe_minimum_passes=uni_q.minimum_met,
        legacy_important_passes=leg_q.important_met,
        universe_important_passes=uni_q.important_met,
        quality_gate_minimum_flip=_flip_label(leg_q.minimum_met, uni_q.minimum_met),
        quality_gate_important_flip=_flip_label(leg_q.important_met, uni_q.important_met),
        cost_delta_usd=universe.llm_cost_usd - legacy.llm_cost_usd,
        llm_call_delta=universe.llm_calls - legacy.llm_calls,
        latency_delta_ms=universe.wall_clock_ms - legacy.wall_clock_ms,
        regression_flag=(uni_n == 0 and leg_n > 0),
        win_flag=_is_strict_win(legacy, universe, leg_q, uni_q, field_delta),
        legacy_errors=list(legacy.errors),
        universe_errors=list(universe.errors),
    )


@dataclass
class ExperimentSummary:
    """Aggregated verdict over a batch of comparisons."""

    n_compared: int
    n_regressions: int
    n_wins: int
    n_ties: int
    avg_unit_count_delta: float
    avg_field_coverage_delta: dict[str, float]
    avg_cost_delta_usd: float
    avg_latency_delta_ms: float
    by_bucket: dict[str, dict[str, Any]]
    promote: bool  # gating result against the planning-doc thresholds
    promote_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Promote-gate thresholds (planning doc §14):
#   - regressions <= 2% of compared properties
#   - avg_field_coverage_delta_avg >= +5%
#   - avg_cost_delta_usd <= +$0.05/property
PROMOTE_REGRESSION_RATE_MAX = 0.02
PROMOTE_FIELD_COVERAGE_MIN = 0.05
PROMOTE_COST_DELTA_MAX = 0.05


def summarise(comparisons: list[Comparison]) -> ExperimentSummary:
    """Build the experiment summary from a list of per-property comparisons."""
    n = len(comparisons)
    if n == 0:
        return ExperimentSummary(
            n_compared=0,
            n_regressions=0,
            n_wins=0,
            n_ties=0,
            avg_unit_count_delta=0.0,
            avg_field_coverage_delta={f: 0.0 for f in _TRACKED_FIELDS},
            avg_cost_delta_usd=0.0,
            avg_latency_delta_ms=0.0,
            by_bucket={},
            promote=False,
            promote_reason="no comparisons",
        )

    n_regressions = sum(1 for c in comparisons if c.regression_flag)
    n_wins = sum(1 for c in comparisons if c.win_flag)
    n_ties = n - n_regressions - n_wins

    avg_unit = sum(c.unit_count_delta for c in comparisons) / n
    avg_cost = sum(c.cost_delta_usd for c in comparisons) / n
    avg_latency = sum(c.latency_delta_ms for c in comparisons) / n

    avg_fields: dict[str, float] = {f: 0.0 for f in _TRACKED_FIELDS}
    for c in comparisons:
        for f, delta in c.field_coverage_delta.items():
            avg_fields[f] = avg_fields.get(f, 0.0) + delta
    for f in avg_fields:
        avg_fields[f] = avg_fields[f] / n

    by_bucket = _aggregate_by_bucket(comparisons)

    avg_field_coverage = sum(avg_fields.values()) / max(1, len(avg_fields))
    promote, reason = _promote_decision(
        regressions=n_regressions,
        n=n,
        avg_field_coverage=avg_field_coverage,
        avg_cost=avg_cost,
    )

    return ExperimentSummary(
        n_compared=n,
        n_regressions=n_regressions,
        n_wins=n_wins,
        n_ties=n_ties,
        avg_unit_count_delta=avg_unit,
        avg_field_coverage_delta=avg_fields,
        avg_cost_delta_usd=avg_cost,
        avg_latency_delta_ms=avg_latency,
        by_bucket=by_bucket,
        promote=promote,
        promote_reason=reason,
    )


def _present(v: Any) -> bool:
    if v is None or v == -1:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def _coverage(units: list[dict[str, Any]], keys: Iterable[str]) -> float:
    """Fraction of units that have at least one of the aliased field names."""
    if not units:
        return 0.0
    n = sum(
        1
        for u in units
        if any(_present(u.get(k)) for k in keys)
    )
    return n / len(units)


def _field_coverage_delta(
    legacy_units: list[dict[str, Any]],
    universe_units: list[dict[str, Any]],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for field_name, keys in _TRACKED_FIELDS.items():
        leg = _coverage(legacy_units, keys)
        uni = _coverage(universe_units, keys)
        out[field_name] = uni - leg
    return out


def _dedupe_key(u: dict[str, Any]) -> str:
    """Mirror :func:`extraction_quality._dedupe_key` so overlap aligns
    with how merge_units treats collisions."""
    for k in _UNIT_ID_KEYS:
        if _present(u.get(k)):
            return f"id:{u[k]}"
    name = next((u.get(k) for k in _FLOOR_PLAN_KEYS if _present(u.get(k))), "") or ""
    beds = next((u.get(k) for k in _BEDS_KEYS if _present(u.get(k))), "") or ""
    sqft = next((u.get(k) for k in _SQFT_KEYS if _present(u.get(k))), "") or ""
    rent = next((u.get(k) for k in _RENT_KEYS if _present(u.get(k))), "") or ""
    return f"fp:{name}|{beds}|{sqft}|{rent}"


def _jaccard_overlap(
    legacy_units: list[dict[str, Any]],
    universe_units: list[dict[str, Any]],
) -> float:
    leg_keys = {_dedupe_key(u) for u in legacy_units}
    uni_keys = {_dedupe_key(u) for u in universe_units}
    if not leg_keys and not uni_keys:
        return 1.0
    union = leg_keys | uni_keys
    if not union:
        return 1.0
    return len(leg_keys & uni_keys) / len(union)


def _flip_label(legacy: bool, universe: bool) -> str:
    if legacy == universe:
        return "unchanged"
    return "gain" if universe and not legacy else "loss"


def _is_strict_win(
    legacy: ReplayResult,
    universe: ReplayResult,
    leg_q: Any,
    uni_q: Any,
    field_delta: dict[str, float],
) -> bool:
    """Universe strictly better than legacy on at least one axis without
    losing on any of the others."""
    if len(universe.units) < len(legacy.units):
        return False
    if any(d < -0.01 for d in field_delta.values()):
        return False
    has_improvement = (
        len(universe.units) > len(legacy.units)
        or any(d > 0.05 for d in field_delta.values())
        or (uni_q.minimum_met and not leg_q.minimum_met)
        or (uni_q.important_met and not leg_q.important_met)
    )
    return has_improvement


def _aggregate_by_bucket(comparisons: list[Comparison]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in comparisons:
        bucket = out.setdefault(
            c.bucket,
            {
                "n": 0,
                "regressions": 0,
                "wins": 0,
                "avg_unit_count_delta": 0.0,
                "avg_cost_delta_usd": 0.0,
            },
        )
        bucket["n"] += 1
        bucket["regressions"] += int(c.regression_flag)
        bucket["wins"] += int(c.win_flag)
        bucket["avg_unit_count_delta"] += c.unit_count_delta
        bucket["avg_cost_delta_usd"] += c.cost_delta_usd

    for bucket in out.values():
        n = bucket["n"]
        if n > 0:
            bucket["avg_unit_count_delta"] /= n
            bucket["avg_cost_delta_usd"] /= n
    return out


def _promote_decision(
    *,
    regressions: int,
    n: int,
    avg_field_coverage: float,
    avg_cost: float,
) -> tuple[bool, str]:
    """Apply the promote-gate thresholds and return (promote, reason)."""
    reasons: list[str] = []
    rate = regressions / n if n else 1.0
    if rate > PROMOTE_REGRESSION_RATE_MAX:
        reasons.append(f"regression_rate {rate:.2%} > {PROMOTE_REGRESSION_RATE_MAX:.0%}")
    if avg_field_coverage < PROMOTE_FIELD_COVERAGE_MIN:
        reasons.append(
            f"avg_field_coverage_delta {avg_field_coverage:+.2%} < {PROMOTE_FIELD_COVERAGE_MIN:+.0%}"
        )
    if avg_cost > PROMOTE_COST_DELTA_MAX:
        reasons.append(f"avg_cost_delta ${avg_cost:.4f} > ${PROMOTE_COST_DELTA_MAX:.2f}")

    if not reasons:
        return True, "all promote gates passed"
    return False, "; ".join(reasons)


__all__ = [
    "Comparison",
    "ExperimentSummary",
    "ReplayResult",
    "compare",
    "summarise",
    "PROMOTE_COST_DELTA_MAX",
    "PROMOTE_FIELD_COVERAGE_MIN",
    "PROMOTE_REGRESSION_RATE_MAX",
]


# Keep imports referenced so future static analysis doesn't strip them —
# both helpers are part of the module's documented surface.
_ = (meets_minimum_fields, has_important_fields)
