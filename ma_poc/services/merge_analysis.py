"""Merge-failure analysis — reusable data layer.

Pulls the same metrics the manual investigation produced — captured-vs-merged
unit counts, disappeared totals, tier breakdowns, identity-mix, zero-merge
samples, in-snapshot phenotype duplication, and disappeared-since history —
straight from the run-state tables (``property_snapshots`` + ``units``).

Single Responsibility
---------------------
This module *only* gathers and shapes the findings. It does not render
HTML, email, log, or write files. Consumers (email scripts, CLIs, CI gates)
import :class:`MergeAnalyzer` and consume the :class:`MergeAnalysis`
dataclass it returns.

Layout
------
- :data:`_QUERIES` — every SQL string in one place, parameterised by ``rd``.
- :class:`Findings DTOs` — frozen dataclasses that mirror the row shapes.
- :class:`MergeAnalyzer` — given a SQLAlchemy engine, returns
  :class:`MergeAnalysis` for a ``run_date``.

The queries assume a Postgres-shaped ``property_snapshots.payload`` JSON
column containing ``units[]``, ``_extract_result.tier_used``, and
``_meta.verdict`` — i.e. exactly what the Jugnu runner writes today.
SQLite fallback is not provided; merge analysis only runs against the
canonical Postgres mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


# ── DTOs ────────────────────────────────────────────────────────────────────
#
# Frozen so consumers can pass them between layers without worrying about
# accidental mutation. Keep these in line with the SELECT lists below.


@dataclass(frozen=True)
class MergeYield:
    """Summary of how many captured units survived merge into ``units``."""
    props_total: int
    prop_zero_merged: int
    prop_partial: int
    prop_full: int
    units_in_snap_total: int
    units_in_table_total: int

    @property
    def units_lost(self) -> int:
        return max(0, self.units_in_snap_total - self.units_in_table_total)

    @property
    def loss_pct(self) -> float:
        return (100.0 * self.units_lost / self.units_in_snap_total) if self.units_in_snap_total else 0.0


@dataclass(frozen=True)
class IdentityMix:
    """Distribution of unit_id types in today's snapshot."""
    no_unit_id: int
    inferred_id: int
    natural_id: int
    total: int

    def pct(self, n: int) -> float:
        return (100.0 * n / self.total) if self.total else 0.0


@dataclass(frozen=True)
class DisappearedToday:
    disappeared_units: int
    disappeared_props: int


@dataclass(frozen=True)
class TierRow:
    tier: str
    props: int
    units_no_id: int
    units_total: int

    @property
    def pct_no_id(self) -> float:
        return (100.0 * self.units_no_id / self.units_total) if self.units_total else 0.0


@dataclass(frozen=True)
class ZeroMergeRow:
    canonical_id: str
    units_in_snap: int
    tier: str


@dataclass(frozen=True)
class PhenotypeDupeRow:
    canonical_id: str
    records: int
    distinct_phys: int

    @property
    def excess(self) -> int:
        return max(0, self.records - self.distinct_phys)


@dataclass(frozen=True)
class DisappearedHistoryRow:
    disappeared_since: str
    units: int
    props: int


@dataclass(frozen=True)
class MergeAnalysis:
    run_date: str
    merge_yield: MergeYield
    disappeared: DisappearedToday
    id_mix: IdentityMix
    tier_breakdown: list[TierRow] = field(default_factory=list)
    zero_merge_samples: list[ZeroMergeRow] = field(default_factory=list)
    phenotype_dupes: list[PhenotypeDupeRow] = field(default_factory=list)
    disappeared_history: list[DisappearedHistoryRow] = field(default_factory=list)


# ── SQL ─────────────────────────────────────────────────────────────────────
#
# One bound parameter per query — the run_date being analysed. Histograms
# (e.g. disappeared_history) take none.


_QUERIES: dict[str, Any] = {
    "merge_yield": text(
        """
        WITH today_snap AS (
          SELECT canonical_id,
                 jsonb_array_length((payload->'units')::jsonb) AS units_in_snap
          FROM property_snapshots WHERE run_date = :rd
        ),
        today_merged AS (
          SELECT canonical_id, COUNT(*) AS units_in_table
          FROM units
          WHERE substring(last_seen_at, 1, 10) = :rd
          GROUP BY canonical_id
        )
        SELECT
          COUNT(*) FILTER (WHERE COALESCE(tm.units_in_table, 0) = 0
                                AND ts.units_in_snap > 0) AS prop_zero_merged,
          COUNT(*) FILTER (WHERE ts.units_in_snap > COALESCE(tm.units_in_table, 0)
                                AND COALESCE(tm.units_in_table, 0) > 0) AS prop_partial,
          COUNT(*) FILTER (WHERE ts.units_in_snap = COALESCE(tm.units_in_table, 0)
                                AND ts.units_in_snap > 0) AS prop_full,
          COALESCE(SUM(ts.units_in_snap), 0) AS units_in_snap_total,
          COALESCE(SUM(COALESCE(tm.units_in_table, 0)), 0) AS units_in_table_total,
          COUNT(*) AS props_total
        FROM today_snap ts LEFT JOIN today_merged tm
          ON tm.canonical_id = ts.canonical_id
        """
    ),
    "disappeared": text(
        """
        SELECT
          COUNT(*) AS disappeared_units,
          COUNT(DISTINCT canonical_id) AS disappeared_props
        FROM units WHERE disappeared_since = :rd
        """
    ),
    "tier_breakdown": text(
        """
        WITH today AS (
          SELECT canonical_id,
                 COALESCE(payload->'_extract_result'->>'tier_used', 'UNKNOWN') AS tier,
                 (SELECT COUNT(*) FROM jsonb_array_elements((payload->'units')::jsonb) u
                    WHERE u->>'unit_id' IS NULL OR u->>'unit_id' = '') AS no_id_units,
                 (SELECT COUNT(*) FROM jsonb_array_elements((payload->'units')::jsonb) u) AS total_units
          FROM property_snapshots WHERE run_date = :rd
        )
        SELECT tier,
          COUNT(*) AS props,
          SUM(no_id_units) AS units_no_id,
          SUM(total_units) AS units_total
        FROM today
        GROUP BY tier
        HAVING SUM(total_units) > 0
        ORDER BY units_total DESC
        LIMIT 20
        """
    ),
    "id_mix": text(
        """
        SELECT
          COUNT(*) FILTER (WHERE u->>'unit_id' IS NULL OR u->>'unit_id' = '') AS no_unit_id,
          COUNT(*) FILTER (WHERE (u->>'unit_id') LIKE 'inferred_%') AS inferred_id,
          COUNT(*) FILTER (WHERE (u->>'unit_id') NOT LIKE 'inferred_%'
                                AND (u->>'unit_id') IS NOT NULL
                                AND (u->>'unit_id') != '') AS natural_id,
          COUNT(*) AS total
        FROM (
          SELECT jsonb_array_elements((payload->'units')::jsonb) AS u
          FROM property_snapshots WHERE run_date = :rd
        ) t
        """
    ),
    "zero_merge_samples": text(
        """
        WITH today_snap AS (
          SELECT canonical_id,
                 jsonb_array_length((payload->'units')::jsonb) AS units_in_snap,
                 COALESCE(payload->'_extract_result'->>'tier_used', 'UNKNOWN') AS tier
          FROM property_snapshots WHERE run_date = :rd
        ),
        today_merged AS (
          SELECT canonical_id, COUNT(*) AS units_in_table
          FROM units
          WHERE substring(last_seen_at, 1, 10) = :rd
          GROUP BY canonical_id
        )
        SELECT ts.canonical_id, ts.units_in_snap, ts.tier
        FROM today_snap ts LEFT JOIN today_merged tm ON tm.canonical_id = ts.canonical_id
        WHERE COALESCE(tm.units_in_table, 0) = 0 AND ts.units_in_snap > 0
        ORDER BY ts.units_in_snap DESC
        LIMIT :sample_limit
        """
    ),
    "phenotype_dupes": text(
        """
        WITH today AS (
          SELECT canonical_id, jsonb_array_elements((payload->'units')::jsonb) AS u
          FROM property_snapshots WHERE run_date = :rd
        )
        SELECT
          canonical_id,
          COUNT(*) AS records,
          COUNT(DISTINCT (u->>'floor_plan_name', u->>'beds', u->>'baths', u->>'area')) AS distinct_phys
        FROM today
        WHERE u->>'unit_id' IS NULL OR u->>'unit_id' = ''
        GROUP BY canonical_id
        HAVING COUNT(*)
             > COUNT(DISTINCT (u->>'floor_plan_name', u->>'beds', u->>'baths', u->>'area'))
        ORDER BY (COUNT(*)
              - COUNT(DISTINCT (u->>'floor_plan_name', u->>'beds', u->>'baths', u->>'area'))) DESC
        LIMIT :sample_limit
        """
    ),
    "disappeared_history": text(
        """
        SELECT disappeared_since, COUNT(*) AS units,
               COUNT(DISTINCT canonical_id) AS props
        FROM units
        WHERE disappeared_since IS NOT NULL
        GROUP BY disappeared_since
        ORDER BY disappeared_since DESC
        LIMIT :history_limit
        """
    ),
}


# ── Analyzer ────────────────────────────────────────────────────────────────


class MergeAnalyzer:
    """Compute :class:`MergeAnalysis` for a run_date.

    Holds a SQLAlchemy ``Engine`` so callers can reuse the same instance
    across multiple ``analyze()`` invocations (e.g. comparing two runs).

    Defaults for ``sample_limit`` / ``history_limit`` match what the email
    body shows; raise them when feeding a CSV/CI dump.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        sample_limit: int = 15,
        history_limit: int = 10,
    ) -> None:
        self._engine = engine
        self._sample_limit = sample_limit
        self._history_limit = history_limit

    def analyze(self, run_date: str) -> MergeAnalysis:
        with self._engine.connect() as conn:
            yield_row = conn.execute(_QUERIES["merge_yield"], {"rd": run_date}).mappings().first()
            dis_row = conn.execute(_QUERIES["disappeared"], {"rd": run_date}).mappings().first()
            id_row = conn.execute(_QUERIES["id_mix"], {"rd": run_date}).mappings().first()

            tier_rows = list(conn.execute(_QUERIES["tier_breakdown"], {"rd": run_date}).mappings())
            zero_rows = list(conn.execute(
                _QUERIES["zero_merge_samples"],
                {"rd": run_date, "sample_limit": self._sample_limit},
            ).mappings())
            dupe_rows = list(conn.execute(
                _QUERIES["phenotype_dupes"],
                {"rd": run_date, "sample_limit": self._sample_limit},
            ).mappings())
            history_rows = list(conn.execute(
                _QUERIES["disappeared_history"],
                {"history_limit": self._history_limit},
            ).mappings())

        return MergeAnalysis(
            run_date=run_date,
            merge_yield=_to_yield(yield_row),
            disappeared=_to_disappeared(dis_row),
            id_mix=_to_id_mix(id_row),
            tier_breakdown=[_to_tier(r) for r in tier_rows],
            zero_merge_samples=[_to_zero(r) for r in zero_rows],
            phenotype_dupes=[_to_dupe(r) for r in dupe_rows],
            disappeared_history=[_to_history(r) for r in history_rows],
        )


# ── Row -> DTO mappers (pure functions, zero hidden state) ──────────────────


def _i(row: Any, key: str) -> int:
    if row is None:
        return 0
    val = row.get(key) if hasattr(row, "get") else row[key]
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _s(row: Any, key: str) -> str:
    if row is None:
        return ""
    val = row.get(key) if hasattr(row, "get") else row[key]
    return "" if val is None else str(val)


def _to_yield(row: Any) -> MergeYield:
    return MergeYield(
        props_total=_i(row, "props_total"),
        prop_zero_merged=_i(row, "prop_zero_merged"),
        prop_partial=_i(row, "prop_partial"),
        prop_full=_i(row, "prop_full"),
        units_in_snap_total=_i(row, "units_in_snap_total"),
        units_in_table_total=_i(row, "units_in_table_total"),
    )


def _to_disappeared(row: Any) -> DisappearedToday:
    return DisappearedToday(
        disappeared_units=_i(row, "disappeared_units"),
        disappeared_props=_i(row, "disappeared_props"),
    )


def _to_id_mix(row: Any) -> IdentityMix:
    return IdentityMix(
        no_unit_id=_i(row, "no_unit_id"),
        inferred_id=_i(row, "inferred_id"),
        natural_id=_i(row, "natural_id"),
        total=_i(row, "total"),
    )


def _to_tier(row: Any) -> TierRow:
    return TierRow(
        tier=_s(row, "tier"),
        props=_i(row, "props"),
        units_no_id=_i(row, "units_no_id"),
        units_total=_i(row, "units_total"),
    )


def _to_zero(row: Any) -> ZeroMergeRow:
    return ZeroMergeRow(
        canonical_id=_s(row, "canonical_id"),
        units_in_snap=_i(row, "units_in_snap"),
        tier=_s(row, "tier"),
    )


def _to_dupe(row: Any) -> PhenotypeDupeRow:
    return PhenotypeDupeRow(
        canonical_id=_s(row, "canonical_id"),
        records=_i(row, "records"),
        distinct_phys=_i(row, "distinct_phys"),
    )


def _to_history(row: Any) -> DisappearedHistoryRow:
    return DisappearedHistoryRow(
        disappeared_since=_s(row, "disappeared_since"),
        units=_i(row, "units"),
        props=_i(row, "props"),
    )
