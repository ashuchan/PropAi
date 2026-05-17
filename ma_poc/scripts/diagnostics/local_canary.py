"""Local Canary — replay yesterday's cloud-run failures against today's code.

One command, one run, one verdict per property.

Phases executed in order:
  SETUP    — Create a disposable SQLite canary DB; bootstrap schema.
  SELECT   — Read failures from a prior cloud run; apply filters; write
             canary_input.csv.
  SEED     — (optional, --seed-from-prod) Copy scrape_profiles for the
             selected properties from the live DB into the canary DB.
  REPLAY   — Invoke jugnu_runner as a subprocess with DATABASE_URL pointed
             at the canary DB and env flags injected from --flag args.
  COMPARE  — Diff each property's cloud-run outcome vs. canary outcome.
  REPORT   — Write markdown delta report; print summary to stdout.
  TEARDOWN — (default) Delete the canary DB; (--keep) print DSN for forensics.

Exit codes:
  0 — no REGRESSED properties; safe to proceed with deploy.
  1 — at least one REGRESSED property; halt and investigate.
  2 — canary infrastructure failure (DB setup, subprocess crash).

Usage:
  python scripts/diagnostics/local_canary.py --from-run 2026-05-10
  python scripts/diagnostics/local_canary.py --from-run 2026-05-10 --limit 100 --filter-tier TIER_4_LLM_API
  python scripts/diagnostics/local_canary.py --from-run 2026-05-10 --include-property-id 37685 --keep --verbose
  python scripts/diagnostics/local_canary.py compare --baseline-dir /tmp/run_a --treatment-dir /tmp/run_b
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# ── Path bootstrap ────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/diagnostics/
_MA_POC_ROOT = _SCRIPT_DIR.parent.parent               # ma_poc/
_REPO_ROOT = _MA_POC_ROOT.parent                       # PropAi/
for _p in (_REPO_ROOT, _MA_POC_ROOT, _SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("local_canary")

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_LIMIT = 50
_DEFAULT_TIMEOUT_PER_PROPERTY = 180          # seconds
_JUGNU_RUNNER = _MA_POC_ROOT / "scripts" / "runners" / "jugnu.py"
_DRY_RUN_RUNNER = _SCRIPT_DIR / "_canary_dry_run_runner.py"
_DEFAULT_OUT_ROOT = _MA_POC_ROOT / "data" / "canary" / "local_runs"
# Mirrors DEFAULT_OUT_ROOT from analyze_cloud_run.py
_ANALYZER_OUT_ROOT = _MA_POC_ROOT / "data" / "reports"

# 2026-05-16: mirror the production success classifier in
# ``ma_poc/reporting/verdict.py``. That module's ``verdict_is_success``
# admits SUCCESS, SUCCESS_PLAN_LEVEL, AND PARTIAL — the canary must use
# the same set, otherwise a property that went from SUCCESS (9 units) to
# PARTIAL (205 units, timeout-rescued) reads as REGRESSED here while
# reporting/run_report.py correctly counts it as success in production
# (a false REGRESSED blocks deploy). Sync these sets when adding new
# success-class verdicts.
from ma_poc.reporting.verdict import _SUCCESS_VERDICTS as _PROD_SUCCESS_VERDICTS

_SUCCESS_VERDICTS = frozenset(_PROD_SUCCESS_VERDICTS)
# CARRY_FORWARD counts as a "soft success" from the cloud run perspective;
# a canary that fails on it is a regression.
_CLOUD_OK_VERDICTS = frozenset(_PROD_SUCCESS_VERDICTS | {"CARRY_FORWARD"})
# Verdicts that qualify a property for the regression sentinel basket.
_REGRESSION_SENTINEL_VERDICTS = frozenset(_PROD_SUCCESS_VERDICTS | {"CARRY_FORWARD"})

VERDICT_IMPROVED = "IMPROVED"
VERDICT_REGRESSED = "REGRESSED"
VERDICT_UNCHANGED_OK = "UNCHANGED_OK"
VERDICT_UNCHANGED_FAIL = "UNCHANGED_FAIL"
VERDICT_TIMEOUT_IN_CANARY = "TIMEOUT_IN_CANARY"

# Basket labels written to canary_input.csv so operators know why each
# property was selected.
BASKET_FAILURE = "FAILURE"
BASKET_REGRESSION = "REGRESSION_SENTINEL"

# Default stratification dimensions for each basket.
# QA coverage rationale:
#   terminal_tier  — exercises a distinct extraction code path per tier
#   pms_detected   — exercises a distinct adapter per PMS platform
# Regression basket adds fetch_outcome to ensure CARRY_FORWARD (state
# persistence) code path is always represented.
_DEFAULT_FAILURE_STRATA: list[str] = ["terminal_tier", "pms_detected"]
_DEFAULT_REGRESSION_STRATA: list[str] = ["terminal_tier", "pms_detected", "fetch_outcome"]

_DEFAULT_REGRESSION_BASKET_SIZE = 20

# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class _CanaryOutcome:
    """Per-property result derived from the canary run's events.jsonl."""

    property_id: str
    verdict: str          # SUCCESS / FAILED_* / TIMEOUT_IN_CANARY
    units: int = 0
    # D16: plan-aggregate row count and cross-page dedup telemetry. These
    # are read from properties.json (per-property ``_post_process_meta``)
    # when available — events.jsonl does not currently carry them.
    plan_summaries: int = 0
    cross_page_dedup_collapses: int = 0
    inverse_b4_rerouted: int = 0
    tier: str = ""        # populated from extract.tier_won events (L4)
    event_kinds: set[str] = field(default_factory=set)  # all event kinds seen (L4)


@dataclass
class ComparedRow:
    """One row in the delta report table."""

    property_id: str
    url: str
    cloud_outcome: str
    cloud_tier: str
    cloud_units: int
    canary_outcome: str
    canary_tier: str     # populated from tier_won events (L4)
    canary_units: int
    verdict: str         # IMPROVED / REGRESSED / UNCHANGED_OK / UNCHANGED_FAIL
    basket: str = BASKET_FAILURE   # FAILURE or REGRESSION_SENTINEL
    attributed_fix: str = ""       # L4 attribution
    # D16: per-property partition + dedup telemetry.
    canary_plan_summaries: int = 0
    canary_cross_page_dedup_collapses: int = 0
    canary_inverse_b4_rerouted: int = 0


@dataclass
class CanaryReport:
    """Aggregate result of one canary run."""

    run_date: str
    source_run_date: str
    properties_total: int = 0
    improved: int = 0
    regressed: int = 0
    unchanged_ok: int = 0
    unchanged_fail: int = 0
    timeout_in_canary: int = 0
    # Regression sentinel basket counts (subset of the totals above).
    sentinel_total: int = 0
    sentinel_regressed: int = 0
    rows: list[ComparedRow] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.regressed == 0


# ── Failures-CSV helpers ──────────────────────────────────────────────────────


def _find_failures_csv(run_date: str) -> Path | None:
    """Locate the failures.csv from a prior cloud run.

    Search order mirrors where analyze_cloud_run.py writes its output plus
    the legacy c:/tmp local-mirror convention documented in the spec.

    Args:
        run_date: YYYY-MM-DD string identifying the source cloud run.

    Returns:
        Path to failures.csv, or None if not found in any expected location.
    """
    candidates = [
        # Primary: analyze_cloud_run.py default output directory
        _ANALYZER_OUT_ROOT / f"cloud_run_{run_date}" / "failures.csv",
        # Legacy Windows local mirror (kept for cross-platform operators)
        Path(f"c:/tmp/run-{run_date}/_analyzer_out/failures.csv"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_failures_csv(path: Path) -> list[dict[str, str]]:
    """Parse failures.csv into a list of row dicts."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _find_successes_csv(run_date: str) -> Path | None:
    """Locate the successes.csv produced by analyze_cloud_run.py.

    Args:
        run_date: YYYY-MM-DD string identifying the source cloud run.

    Returns:
        Path to successes.csv, or None if not found.
    """
    candidates = [
        _ANALYZER_OUT_ROOT / f"cloud_run_{run_date}" / "successes.csv",
        Path(f"c:/tmp/run-{run_date}/_analyzer_out/successes.csv"),
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def _read_successes_csv(path: Path) -> list[dict[str, str]]:
    """Parse successes.csv into a list of row dicts."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── Stratified population selection ──────────────────────────────────────────


def select_stratified(
    rows: list[dict[str, str]],
    strata_keys: list[str],
    n_per_stratum: int = 1,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Greedy stratified sampler: guarantee at least n_per_stratum rows per
    distinct value of each key in strata_keys.

    Algorithm:
      1. Build a (key, value) → candidate_rows index.
      2. For each stratum, pick n_per_stratum rows not yet selected.
         A single row can satisfy multiple strata simultaneously.
      3. Fill remaining slots up to limit with un-selected rows.

    This gives the minimum population that covers every code path represented
    by the strata dimensions while staying within the budget.

    Args:
        rows: Source rows (failures.csv or successes.csv dicts).
        strata_keys: Column names to stratify on, e.g. ["terminal_tier", "pms_detected"].
        n_per_stratum: Minimum rows per distinct stratum value (default 1).
        limit: Hard cap on output size; None means uncapped.

    Returns:
        Stratified sample, deduplicated by property_id.
    """
    from collections import defaultdict

    seen_ids: set[str] = set()
    result: list[dict[str, str]] = []

    # Index rows by (key, value) stratum.
    strata_bins: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        for key in strata_keys:
            val = row.get(key, "").strip()
            if val:
                strata_bins[(key, val)].append(row)

    # For each stratum, pick n_per_stratum unseen rows.
    for (_key, _val), candidates in strata_bins.items():
        picks = 0
        for row in candidates:
            if picks >= n_per_stratum:
                break
            pid = row.get("property_id", "").strip()
            if pid and pid not in seen_ids:
                result.append(row)
                seen_ids.add(pid)
                picks += 1

    # Fill up to limit with remaining rows in original order.
    if limit is not None:
        for row in rows:
            if len(result) >= limit:
                break
            pid = row.get("property_id", "").strip()
            if pid and pid not in seen_ids:
                result.append(row)
                seen_ids.add(pid)

    return result[:limit] if limit is not None else result


def select_regression_basket(
    success_rows: list[dict[str, str]],
    strata_keys: list[str] | None = None,
    limit: int = _DEFAULT_REGRESSION_BASKET_SIZE,
) -> list[dict[str, str]]:
    """Select the regression sentinel basket from cloud-run SUCCESS/CARRY_FORWARD rows.

    The regression basket contains properties that SUCCEEDED in the cloud run
    and therefore MUST still succeed in the canary.  Any REGRESSED property
    in this basket means the change under test broke a previously working
    code path — the deploy gate must fail.

    Stratification ensures each extraction tier, PMS adapter, and fetch outcome
    is represented.  A 20-property basket typically covers 6-8 tiers × 4 adapters
    with overlap.

    Args:
        success_rows: Rows from successes.csv (verdict in SUCCESS/CARRY_FORWARD).
        strata_keys: Stratification dimensions (defaults to _DEFAULT_REGRESSION_STRATA).
        limit: Maximum basket size (default 20).

    Returns:
        Stratified sample of regression sentinel properties.
    """
    if limit <= 0:
        raise ValueError(f"regression basket limit must be > 0, got {limit}")

    effective_keys = strata_keys if strata_keys is not None else _DEFAULT_REGRESSION_STRATA

    # Filter to only SUCCESS and CARRY_FORWARD verdicts.
    eligible = [
        r for r in success_rows
        if r.get("verdict", "") in _REGRESSION_SENTINEL_VERDICTS
    ]

    return select_stratified(eligible, effective_keys, n_per_stratum=1, limit=limit)


# ── DB setup / teardown ───────────────────────────────────────────────────────


def setup_canary_db(canary_dsn: str, db_mode: str = "sqlite") -> None:
    """Create and bootstrap the canary DB.

    For ``sqlite`` mode uses SqliteDataProvider (create_schema=True) so the
    full ORM schema is applied idempotently.

    For ``postgres`` mode runs ``alembic upgrade head`` against the DSN, falling
    back to ``Base.metadata.create_all()`` when alembic is unavailable.  The
    caller is responsible for provisioning and cleaning up the Postgres DB.

    Args:
        canary_dsn: SQLAlchemy connection string.
        db_mode: ``"sqlite"`` (default) or ``"postgres"``.

    Raises:
        RuntimeError: If schema creation fails (surfaces as exit code 2).
    """
    if db_mode == "sqlite":
        from data_provider.sqlite import SqliteDataProvider

        dp = SqliteDataProvider(url=canary_dsn, create_schema=True)
        dp.close()
        log.info("Canary SQLite DB initialised: %s", canary_dsn)
        return

    # postgres mode — try alembic first, fall back to create_all
    _setup_canary_db_postgres(canary_dsn)


def _setup_canary_db_postgres(canary_dsn: str) -> None:
    """Bootstrap schema for a Postgres canary DB.

    Tries alembic upgrade head; falls back to Base.metadata.create_all().
    """
    try:
        import alembic.config
        import alembic.command

        alembic_ini = _MA_POC_ROOT / "alembic.ini"
        cfg = alembic.config.Config(str(alembic_ini))
        cfg.set_main_option("sqlalchemy.url", canary_dsn)
        alembic.command.upgrade(cfg, "head")
        log.info("Canary Postgres DB bootstrapped via alembic: %s", canary_dsn)
    except ImportError:
        log.warning("alembic not installed; falling back to Base.metadata.create_all()")
        from sqlalchemy import create_engine

        from data_provider.sql.models import Base

        engine = create_engine(canary_dsn)
        Base.metadata.create_all(engine)
        engine.dispose()
        log.info("Canary Postgres DB bootstrapped via create_all: %s", canary_dsn)


def teardown_canary_db(canary_db_path: Path) -> None:
    """Remove the canary SQLite file.

    Args:
        canary_db_path: Filesystem path to the ``.sqlite`` file to delete.
    """
    try:
        canary_db_path.unlink(missing_ok=True)
        log.info("Canary DB removed: %s", canary_db_path)
    except OSError as exc:
        log.warning("Could not remove canary DB %s: %s", canary_db_path, exc)


# ── Profile seeding (L2) ──────────────────────────────────────────────────────


def seed_profiles_from_prod(
    property_ids: list[str],
    canary_dsn: str,
    source_dsn: str | None = None,
) -> int:
    """Copy scrape_profiles from the live DB into the canary DB.

    Scoped to the given property_ids so only the profiles relevant to the
    current canary batch are copied.  Uses a DELETE-then-INSERT pattern so
    the call is idempotent.

    Args:
        property_ids: Canonical IDs to seed.
        canary_dsn: SQLAlchemy DSN for the canary DB (destination).
        source_dsn: SQLAlchemy DSN for the production DB (source).
            Defaults to ``PROD_DATABASE_URL`` env var.

    Returns:
        Number of profiles seeded.

    Raises:
        RuntimeError: If the source DSN is unavailable or the copy fails.
    """
    if not property_ids:
        log.info("seed_profiles_from_prod: no property IDs — nothing to seed")
        return 0

    resolved_source = source_dsn or os.environ.get("PROD_DATABASE_URL", "")
    if not resolved_source:
        raise RuntimeError(
            "seed_profiles_from_prod requires PROD_DATABASE_URL env var "
            "(or explicit source_dsn parameter)"
        )

    from sqlalchemy import create_engine, delete, select

    from data_provider.sql.models import ScrapeProfileRow

    log.info(
        "Seeding %d profiles from prod → canary", len(property_ids)
    )

    src_engine = create_engine(resolved_source)
    dst_engine = create_engine(canary_dsn)

    try:
        # Fetch matching profiles from the source DB.
        with src_engine.connect() as src_conn:
            stmt = select(ScrapeProfileRow).where(
                ScrapeProfileRow.canonical_id.in_(property_ids)
            )
            src_rows = src_conn.execute(stmt).mappings().all()

        if not src_rows:
            log.info("No profiles found in source DB for the given property IDs")
            return 0

        # Delete any existing profiles in the canary DB for these IDs, then
        # insert the source rows.
        with dst_engine.begin() as dst_conn:
            dst_conn.execute(
                delete(ScrapeProfileRow).where(
                    ScrapeProfileRow.canonical_id.in_(property_ids)
                )
            )
            for row in src_rows:
                dst_conn.execute(
                    ScrapeProfileRow.__table__.insert().values(dict(row))
                )

        seeded = len(src_rows)
        log.info("Seeded %d profiles into canary DB", seeded)
        return seeded

    finally:
        src_engine.dispose()
        dst_engine.dispose()


# ── Property selection ────────────────────────────────────────────────────────


def select_properties(
    rows_all: list[dict[str, str]],
    *,
    filter_tier: str | None = None,
    filter_pms: str | None = None,
    filter_outcome: str | None = None,
    include_ids: list[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, str]]:
    """Apply filters to a raw failures.csv row list and return the selection.

    Filters are AND-composed.  ``include_ids`` are OR'd in *after* filtering
    so a forced property is always included regardless of filter criteria.

    Args:
        rows_all: Full list of rows from failures.csv.
        filter_tier: Keep only rows where ``terminal_tier == filter_tier``.
        filter_pms: Keep only rows where ``pms_detected == filter_pms``.
        filter_outcome: Keep only rows where ``verdict == filter_outcome``.
        include_ids: Property IDs to force-include (bypasses filters).
        limit: Maximum row count to return; must be > 0.

    Returns:
        Filtered (and forced-in) rows, capped at ``limit``.

    Raises:
        ValueError: If ``limit`` is <= 0.
    """
    if limit <= 0:
        raise ValueError(f"--limit must be > 0, got {limit}")

    filtered = list(rows_all)

    if filter_tier:
        filtered = [r for r in filtered if r.get("terminal_tier", "") == filter_tier]
    if filter_pms:
        filtered = [r for r in filtered if r.get("pms_detected", "") == filter_pms]
    if filter_outcome:
        filtered = [r for r in filtered if r.get("verdict", "") == filter_outcome]

    # OR in forced IDs: build a by-id map from the *original* full list,
    # then merge forced rows into the filtered set (deduped by property_id).
    if include_ids:
        all_by_id = {r["property_id"]: r for r in rows_all}
        merged: dict[str, dict[str, str]] = {r["property_id"]: r for r in filtered}
        for pid in include_ids:
            if pid in all_by_id:
                merged[pid] = all_by_id[pid]
        filtered = list(merged.values())

    return filtered[:limit]


def write_canary_input_csv(rows: list[dict[str, str]], out_path: Path) -> None:
    """Write the selected properties to a canary_input.csv for jugnu.

    The output schema is the minimal set jugnu_runner accepts via --csv:
    property_id, url (plus extra columns preserved for the compare phase).

    The ``basket`` column labels each row as FAILURE (was failing in the cloud
    run — we're testing our fix) or REGRESSION_SENTINEL (was succeeding — we're
    checking we didn't break it).  ``cloud_units`` records how many units the
    cloud run extracted, enabling severity assessment of regressions.

    Args:
        rows: Selected rows from failures.csv or successes.csv.
        out_path: Destination file path.
    """
    _FIELDNAMES = [
        "property_id",
        "url",
        "verdict",
        "terminal_tier",
        "pms_detected",
        "domain",
        "basket",
        "cloud_units",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "property_id": row.get("property_id", ""),
                    "url": row.get("url", ""),
                    "verdict": row.get("verdict", ""),
                    "terminal_tier": row.get("terminal_tier", ""),
                    "pms_detected": row.get("pms_detected", ""),
                    "domain": row.get("domain", ""),
                    "basket": row.get("basket", BASKET_FAILURE),
                    "cloud_units": row.get("units", row.get("cloud_units", "0")),
                }
            )
    log.info("Wrote %d rows → %s", len(rows), out_path)


# ── Replay ────────────────────────────────────────────────────────────────────


def replay(
    canary_input_csv: Path,
    canary_dsn: str,
    out_dir: Path,
    run_date: str,
    flag_overrides: dict[str, str],
    timeout_per_property: int = _DEFAULT_TIMEOUT_PER_PROPERTY,
    limit: int = _DEFAULT_LIMIT,
    runner: Path | None = None,
) -> int:
    """Invoke jugnu_runner (or a custom runner) as a subprocess against the canary input.

    The subprocess gets a fresh env with DATABASE_URL pointed at the canary
    DB and any --flag overrides injected.  stdout+stderr are captured to
    ``{out_dir}/jugnu.log`` so the canary output dir contains a full audit
    trail of what the runner did.

    Args:
        canary_input_csv: Path to the property CSV produced by select_properties.
        canary_dsn: SQLAlchemy DSN for the canary DB.
        out_dir: Base data directory for the jugnu subprocess (run output lands
            under ``{out_dir}/runs/{run_date}/`` for v1 or ``{out_dir}/v2/runs/{run_date}/`` for v2).
        run_date: YYYY-MM-DD string passed to jugnu via ``--run-date``.
        flag_overrides: ``{ENV_VAR: value}`` dict injected into the subprocess
            env — the mechanism for feature-flag toggling without side-effects.
        timeout_per_property: Seconds to allow per property (default 180).
        limit: Number of properties being run; used to compute the aggregate
            timeout ceiling for the subprocess.
        runner: Optional path to an alternative runner script.  Defaults to
            ``jugnu.py``.  Useful for dry-run/smoke tests (pass
            ``_DRY_RUN_RUNNER``).

    Returns:
        Jugnu subprocess exit code (0 = completed, non-zero = partial / error).
    """
    subprocess_timeout = timeout_per_property * limit + 300  # 5-min headroom

    env = {
        **os.environ,
        "DATABASE_URL": canary_dsn,
        **flag_overrides,
    }

    effective_runner = runner or _JUGNU_RUNNER
    cmd = [
        sys.executable,
        str(effective_runner),
        "--csv", str(canary_input_csv),
        "--data-dir", str(out_dir),
        "--run-date", run_date,
        "--force-scrape",
    ]

    log.info("Canary replay: %s", " ".join(str(c) for c in cmd))

    jugnu_log_path = out_dir / "jugnu.log"
    jugnu_log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with jugnu_log_path.open("w", encoding="utf-8") as logf:
            result = subprocess.run(
                cmd,
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=subprocess_timeout,
                check=False,
            )
        log.info("Jugnu exited %d; log at %s", result.returncode, jugnu_log_path)
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error(
            "Jugnu subprocess timed out after %ds (limit=%d, per-property=%ds)",
            subprocess_timeout,
            limit,
            timeout_per_property,
        )
        return 2
    except OSError as exc:
        log.error("Failed to launch jugnu subprocess: %s", exc)
        return 2


# ── Canary outcome extraction ─────────────────────────────────────────────────


def read_canary_outcomes(out_dir: Path, run_date: str) -> dict[str, _CanaryOutcome]:
    """Parse the canary run's events.jsonl to get per-property verdicts.

    Two-phase parse:
      1. Scan all events to collect ``event_kinds`` and ``tier_used`` per
         property (from ``extract.tier_won`` events).
      2. Build outcomes only for properties that emitted a
         ``output.property_emitted`` event (last event per property wins).

    Args:
        out_dir: Base data directory used for the jugnu subprocess.
        run_date: YYYY-MM-DD of the canary run.

    Returns:
        Dict mapping ``property_id → _CanaryOutcome``.  Properties that emitted
        no PROPERTY_EMITTED event are absent (caller treats them as
        TIMEOUT_IN_CANARY).
    """
    # Try v2 path first (SCHEMA_VERSION=v2 written by Jugnu), then v1 fallback.
    # We probe the filesystem rather than reading the env var because Jugnu
    # loads SCHEMA_VERSION from .env (python-dotenv) while the canary process
    # itself may not have that var in its shell environment.
    _candidate_paths = [
        out_dir / "v2" / "runs" / run_date / "events.jsonl",
        out_dir / "runs" / run_date / "events.jsonl",
    ]
    events_path = next((p for p in _candidate_paths if p.exists()), _candidate_paths[0])
    if not events_path.exists():
        log.warning("events.jsonl not found at %s", events_path)
        return {}

    # Phase 1 — collect ancillary info for every property.
    event_kinds_by_pid: dict[str, set[str]] = {}
    tier_by_pid: dict[str, str] = {}
    last_emitted: dict[str, dict[str, Any]] = {}

    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            pid = str(ev.get("property_id", "")).strip()
            if not pid:
                continue

            kind = str(ev.get("kind", ""))
            event_kinds_by_pid.setdefault(pid, set()).add(kind)

            if kind == "extract.tier_won":
                tier = str(ev.get("tier_used", ""))
                if tier:
                    tier_by_pid[pid] = tier

            if kind == "output.property_emitted":
                last_emitted[pid] = ev

    # Phase 2 — build outcomes only for PROPERTY_EMITTED properties.
    outcomes: dict[str, _CanaryOutcome] = {}
    for pid, ev in last_emitted.items():
        verdict = str(ev.get("verdict", "FAILED_NO_DATA"))
        units = int(ev.get("units", 0))
        outcomes[pid] = _CanaryOutcome(
            property_id=pid,
            verdict=verdict,
            units=units,
            tier=tier_by_pid.get(pid, ""),
            event_kinds=event_kinds_by_pid.get(pid, set()),
        )

    # D16: enrich outcomes with plan_summaries + cross-page dedup
    # telemetry from properties.json when present. Best-effort — a
    # missing or malformed file just leaves the new fields at zero.
    properties_path = events_path.parent / "properties.json"
    if properties_path.exists():
        try:
            with properties_path.open(encoding="utf-8") as fh:
                props_payload = json.load(fh)
            props_iter = (
                props_payload
                if isinstance(props_payload, list)
                else props_payload.get("properties", [])
            )
            for p in props_iter:
                if not isinstance(p, dict):
                    continue
                pid = str(
                    (p.get("_meta") or {}).get("canonical_id")
                    or (p.get("_meta") or {}).get("property_id")
                    or p.get("property_id")
                    or ""
                ).strip()
                if not pid or pid not in outcomes:
                    continue
                plans = p.get("plan_summaries") or []
                outcomes[pid].plan_summaries = len(plans)
                pp_meta = p.get("_post_process_meta") or {}
                if isinstance(pp_meta, dict):
                    outcomes[pid].cross_page_dedup_collapses = int(
                        pp_meta.get("cross_page_dedup_collapses") or 0
                    )
                    outcomes[pid].inverse_b4_rerouted = int(
                        pp_meta.get("inverse_b4_rerouted") or 0
                    )
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("D16 enrichment skipped (%s): %s", properties_path, exc)

    log.info("Read %d canary outcomes from %s", len(outcomes), events_path)
    return outcomes


# ── Comparison ────────────────────────────────────────────────────────────────


def compare(
    cloud_rows: list[dict[str, str]],
    canary_outcomes: dict[str, _CanaryOutcome],
    source_run_date: str,
) -> CanaryReport:
    """Produce the delta report by diffing cloud outcomes vs. canary outcomes.

    Verdict assignment:
      IMPROVED        — cloud failed, canary succeeded.
      REGRESSED       — cloud succeeded, canary failed.
      UNCHANGED_OK    — both succeeded (sanity-baseline properties).
      UNCHANGED_FAIL  — both failed (fix doesn't cover this property).
      TIMEOUT_IN_CANARY — property emitted no PROPERTY_EMITTED event;
                          treated as infrastructure failure, not as a
                          code-under-test failure.

    IMPROVED rows are annotated with a heuristic fix attribution (L4).

    Args:
        cloud_rows: Rows from canary_input.csv (includes the cloud verdict).
        canary_outcomes: Per-property results from the canary run's events.
        source_run_date: YYYY-MM-DD of the original cloud run.

    Returns:
        Populated CanaryReport.
    """
    from local_canary_attribution import attribute_fix

    report = CanaryReport(
        run_date=date.today().isoformat(),
        source_run_date=source_run_date,
        properties_total=len(cloud_rows),
    )

    for row in cloud_rows:
        pid = row.get("property_id", "")
        cloud_verdict = row.get("verdict", "FAILED_NO_DATA")
        cloud_tier = row.get("terminal_tier", "")
        cloud_was_ok = cloud_verdict in _CLOUD_OK_VERDICTS
        basket = row.get("basket", BASKET_FAILURE)
        # cloud_units: failures have 0; successes.csv carries actual unit count.
        try:
            cloud_units_val = int(row.get("cloud_units", row.get("units", 0)) or 0)
        except (ValueError, TypeError):
            cloud_units_val = 0

        canary = canary_outcomes.get(pid)
        if canary is None:
            canary_verdict = VERDICT_TIMEOUT_IN_CANARY
            canary_units = 0
            canary_tier = ""
            event_kinds: set[str] = set()
            canary_plan_summaries = 0
            canary_cross_page_collapses = 0
            canary_inverse_b4 = 0
        else:
            canary_verdict = canary.verdict
            canary_units = canary.units
            canary_tier = canary.tier
            event_kinds = canary.event_kinds
            canary_plan_summaries = canary.plan_summaries
            canary_cross_page_collapses = canary.cross_page_dedup_collapses
            canary_inverse_b4 = canary.inverse_b4_rerouted

        canary_was_ok = canary_verdict in _SUCCESS_VERDICTS

        is_sentinel = basket == BASKET_REGRESSION
        if is_sentinel:
            report.sentinel_total += 1

        attributed_fix = ""
        if canary_verdict == VERDICT_TIMEOUT_IN_CANARY:
            row_verdict = VERDICT_TIMEOUT_IN_CANARY
            report.timeout_in_canary += 1
        elif not cloud_was_ok and canary_was_ok:
            row_verdict = VERDICT_IMPROVED
            report.improved += 1
            attributed_fix = attribute_fix(event_kinds, cloud_tier, canary_tier)
        elif cloud_was_ok and not canary_was_ok:
            row_verdict = VERDICT_REGRESSED
            report.regressed += 1
            if is_sentinel:
                report.sentinel_regressed += 1
        elif cloud_was_ok and canary_was_ok:
            row_verdict = VERDICT_UNCHANGED_OK
            report.unchanged_ok += 1
        else:
            row_verdict = VERDICT_UNCHANGED_FAIL
            report.unchanged_fail += 1

        report.rows.append(
            ComparedRow(
                property_id=pid,
                url=row.get("url", ""),
                cloud_outcome=cloud_verdict,
                cloud_tier=cloud_tier,
                cloud_units=cloud_units_val,
                canary_outcome=canary_verdict,
                canary_tier=canary_tier,
                canary_units=canary_units,
                verdict=row_verdict,
                basket=basket,
                attributed_fix=attributed_fix,
                canary_plan_summaries=canary_plan_summaries,
                canary_cross_page_dedup_collapses=canary_cross_page_collapses,
                canary_inverse_b4_rerouted=canary_inverse_b4,
            )
        )

    return report


# ── Report rendering ──────────────────────────────────────────────────────────


def render_markdown(report: CanaryReport) -> str:
    """Render the delta report as a GitHub-flavoured markdown string.

    Args:
        report: Populated CanaryReport from compare().

    Returns:
        Markdown string suitable for writing to report.md.
    """
    gate = "PASS" if report.passed else "FAIL ⚠️  DO NOT DEPLOY"
    failure_total = report.properties_total - report.sentinel_total
    lines = [
        f"# Local Canary Report — {report.run_date}",
        "",
        f"**Source run:** {report.source_run_date}  ",
        f"**Properties tested:** {report.properties_total}  "
        f"({failure_total} failure-basket + {report.sentinel_total} regression-sentinel)  ",
        f"**Pre-deploy gate:** `REGRESSED == 0` → **{gate}**",
        "",
        "## Summary",
        "",
        "```",
        f"Local canary: {report.properties_total} properties from cloud-run {report.source_run_date}",
        f"  Failure basket ({failure_total} properties — fix validation):",
        f"    IMPROVED:        {report.improved:4d}  (was failing, now succeed — fix is working)",
        f"    UNCHANGED_FAIL:  {report.unchanged_fail:4d}  (was failing, still failing — fix doesn't cover them)",
        f"  Regression sentinel ({report.sentinel_total} properties — regression detection):",
        f"    UNCHANGED_OK:    {report.unchanged_ok:4d}  (was succeeding, still succeed — no regression)",
        f"    REGRESSED:       {report.sentinel_regressed:4d}  (was succeeding, now fail — STOP)",
        f"  Cross-basket:",
        f"    REGRESSED total: {report.regressed:4d}  (any regression = deploy blocked)",
        f"    TIMEOUT:         {report.timeout_in_canary:4d}  (no PROPERTY_EMITTED — investigate canary tool)",
        "",
        f"Pre-deploy gate: REGRESSED == 0 → {('PASS' if report.passed else 'FAIL')}",
        "```",
        "",
    ]

    if report.regressed > 0:
        lines += [
            "## Regressions — ACTION REQUIRED",
            "",
            "These properties were succeeding in the cloud run but failed in the canary.",
            "Do **not** deploy until they are investigated.",
            "",
        ]
        for row in report.rows:
            if row.verdict == VERDICT_REGRESSED:
                lines.append(
                    f"- **`{row.property_id}`** `{row.url}` — "
                    f"cloud: `{row.cloud_outcome}` → canary: `{row.canary_outcome}`"
                )
        lines.append("")

    if report.improved > 0:
        lines += [
            "## Improvements — FIX ATTRIBUTION",
            "",
            "These properties were failing in the cloud run but succeed in the canary.",
            "",
        ]
        for row in report.rows:
            if row.verdict == VERDICT_IMPROVED:
                attr = f" ← `{row.attributed_fix}`" if row.attributed_fix else ""
                lines.append(
                    f"- **`{row.property_id}`** `{row.url}` — "
                    f"cloud: `{row.cloud_outcome}` → canary: `{row.canary_outcome}`{attr}"
                )
        lines.append("")

    # D16 run-level partition + dedup totals.
    total_plan_summaries = sum(r.canary_plan_summaries for r in report.rows)
    total_cross_page_collapses = sum(
        r.canary_cross_page_dedup_collapses for r in report.rows
    )
    total_inverse_b4 = sum(r.canary_inverse_b4_rerouted for r in report.rows)
    properties_with_collapses = sum(
        1 for r in report.rows if r.canary_cross_page_dedup_collapses > 0
    )
    lines += [
        "## D16 — Partition + cross-page dedup totals",
        "",
        "```",
        f"  Plan summaries (canary): {total_plan_summaries}",
        f"  Cross-page unit-number dedup collapses: {total_cross_page_collapses}"
        f"  (properties: {properties_with_collapses})",
        f"  Inverse-B4 rerouted: {total_inverse_b4}",
        "```",
        "",
        "## Per-property delta",
        "",
        "| property_id | cloud_outcome | cloud_tier | cloud_units"
        " | canary_outcome | canary_tier | canary_units"
        " | canary_plans | canary_dedup | canary_inv_b4"
        " | verdict | attributed_fix |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for row in report.rows:
        lines.append(
            f"| `{row.property_id}` | {row.cloud_outcome} | {row.cloud_tier}"
            f" | {row.cloud_units} | {row.canary_outcome} | {row.canary_tier}"
            f" | {row.canary_units}"
            f" | {row.canary_plan_summaries}"
            f" | {row.canary_cross_page_dedup_collapses}"
            f" | {row.canary_inverse_b4_rerouted}"
            f" | **{row.verdict}** | {row.attributed_fix} |"
        )

    lines.append("")
    return "\n".join(lines)


def render_json(report: CanaryReport) -> str:
    """Serialise the report to a machine-readable JSON string.

    Args:
        report: Populated CanaryReport.

    Returns:
        JSON string.
    """
    payload: dict[str, Any] = {
        "run_date": report.run_date,
        "source_run_date": report.source_run_date,
        "properties_total": report.properties_total,
        "improved": report.improved,
        "regressed": report.regressed,
        "unchanged_ok": report.unchanged_ok,
        "unchanged_fail": report.unchanged_fail,
        "timeout_in_canary": report.timeout_in_canary,
        "passed": report.passed,
        "rows": [
            {
                "property_id": r.property_id,
                "url": r.url,
                "cloud_outcome": r.cloud_outcome,
                "cloud_tier": r.cloud_tier,
                "cloud_units": r.cloud_units,
                "canary_outcome": r.canary_outcome,
                "canary_tier": r.canary_tier,
                "canary_units": r.canary_units,
                "canary_plan_summaries": r.canary_plan_summaries,
                "canary_cross_page_dedup_collapses": (
                    r.canary_cross_page_dedup_collapses
                ),
                "canary_inverse_b4_rerouted": r.canary_inverse_b4_rerouted,
                "verdict": r.verdict,
                "basket": r.basket,
                "attributed_fix": r.attributed_fix,
            }
            for r in report.rows
        ],
    }
    return json.dumps(payload, indent=2)


# ── Compare subcommand (L5) ───────────────────────────────────────────────────


def _load_summary_json(out_dir: Path) -> dict[str, Any]:
    """Load and return a summary.json from an out-dir."""
    path = out_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found in {out_dir}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _build_compare_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``compare`` subcommand."""
    p = argparse.ArgumentParser(
        prog="local_canary.py compare",
        description=(
            "Compare two prior canary out-dirs (baseline vs. treatment) "
            "and emit a delta summary."
        ),
    )
    p.add_argument(
        "--baseline-dir",
        required=True,
        type=Path,
        metavar="PATH",
        help="Out-dir from the baseline canary run (all flags off).",
    )
    p.add_argument(
        "--treatment-dir",
        required=True,
        type=Path,
        metavar="PATH",
        help="Out-dir from the treatment canary run (flags on).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit delta as JSON to stdout.",
    )
    return p


def _main_compare(argv: list[str]) -> int:
    """Entry point for the ``compare`` subcommand.

    Loads summary.json from baseline and treatment out-dirs, prints a delta
    block, and exits 0 (pass) or 1 (treatment regressed vs. baseline).
    """
    parser = _build_compare_parser()
    args = parser.parse_args(argv)

    try:
        baseline = _load_summary_json(args.baseline_dir)
        treatment = _load_summary_json(args.treatment_dir)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2

    delta = {
        "baseline_dir": str(args.baseline_dir),
        "treatment_dir": str(args.treatment_dir),
        "baseline_passed": baseline.get("passed", False),
        "treatment_passed": treatment.get("passed", False),
        "delta_improved": treatment.get("improved", 0) - baseline.get("improved", 0),
        "delta_regressed": treatment.get("regressed", 0) - baseline.get("regressed", 0),
        "delta_unchanged_ok": (
            treatment.get("unchanged_ok", 0) - baseline.get("unchanged_ok", 0)
        ),
        "delta_unchanged_fail": (
            treatment.get("unchanged_fail", 0) - baseline.get("unchanged_fail", 0)
        ),
    }

    if args.json:
        print(json.dumps(delta, indent=2))
    else:
        print("\nBaseline vs. Treatment delta")
        print(f"  baseline : {args.baseline_dir}")
        print(f"  treatment: {args.treatment_dir}")
        print(f"  ΔIMPROVED:       {delta['delta_improved']:+d}")
        print(f"  ΔUNCHANGED_OK:   {delta['delta_unchanged_ok']:+d}")
        print(f"  ΔUNCHANGED_FAIL: {delta['delta_unchanged_fail']:+d}")
        print(f"  ΔREGRESSED:      {delta['delta_regressed']:+d}")
        passed = (
            treatment.get("passed", False)
            and delta["delta_regressed"] <= 0
        )
        print(f"\n  Gate: treatment_passed AND no new regressions -> {'PASS' if passed else 'FAIL'}")

    # Return 1 if treatment introduced new regressions vs. baseline.
    return 0 if delta["delta_regressed"] <= 0 else 1


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_flag_overrides(flag_args: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` strings from --flag into an env-var dict.

    Args:
        flag_args: List of strings in ``KEY=VALUE`` format.

    Returns:
        Dict suitable for merging into ``os.environ`` for the subprocess.

    Raises:
        SystemExit: If any entry is not in ``KEY=VALUE`` format.
    """
    overrides: dict[str, str] = {}
    for entry in flag_args:
        if "=" not in entry:
            sys.exit(f"[local_canary] --flag requires KEY=VALUE format, got: {entry!r}")
        key, _, value = entry.partition("=")
        overrides[key.strip()] = value.strip()
    return overrides


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for local_canary."""
    p = argparse.ArgumentParser(
        description=(
            "Replay yesterday's cloud-run failures against today's code "
            "and produce a per-property delta report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "--from-run",
        required=True,
        metavar="YYYY-MM-DD",
        help="Date of the cloud run whose failures to replay.",
    )

    # Selection
    sel = p.add_argument_group("Selection (default: all failures, capped at --limit)")
    sel.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        metavar="N",
        help=f"Cap to N properties (default: {_DEFAULT_LIMIT}).",
    )
    sel.add_argument(
        "--filter-tier",
        metavar="TIER_NAME",
        help="Only include properties whose terminal_tier equals this.",
    )
    sel.add_argument(
        "--filter-pms",
        metavar="PMS_NAME",
        help="Only include properties whose pms_detected equals this.",
    )
    sel.add_argument(
        "--filter-outcome",
        metavar="OUTCOME",
        help=(
            "FAILED_NO_DATA | FAILED_UNREACHABLE | CARRY_FORWARD | SUCCESS. "
            "Use SUCCESS to regression-check known-good properties."
        ),
    )
    sel.add_argument(
        "--include-property-id",
        metavar="ID",
        action="append",
        default=[],
        help="Force-include this property even if filters would exclude it. Repeatable.",
    )
    sel.add_argument(
        "--properties-csv",
        type=Path,
        metavar="PATH",
        help="Override the failures.csv source entirely with this external CSV.",
    )
    sel.add_argument(
        "--stratify",
        action="store_true",
        default=False,
        help=(
            "Apply stratified sampling to the failure basket so that at least "
            "one property per terminal_tier and pms_detected value is included. "
            "Off by default to preserve backward compatibility."
        ),
    )

    # Regression sentinel basket
    reg = p.add_argument_group(
        "Regression sentinel basket",
        description=(
            "Select properties that SUCCEEDED in the cloud run. "
            "These are replayed to detect regressions introduced by the change under test."
        ),
    )
    reg.add_argument(
        "--regression-basket-csv",
        type=Path,
        metavar="PATH",
        help=(
            "Explicit path to a successes.csv for the regression basket. "
            "Defaults to the successes.csv produced by analyze_cloud_run.py "
            f"alongside failures.csv in {_ANALYZER_OUT_ROOT}."
        ),
    )
    reg.add_argument(
        "--regression-basket-size",
        type=int,
        default=_DEFAULT_REGRESSION_BASKET_SIZE,
        metavar="N",
        help=(
            f"Maximum size of the regression sentinel basket (default: {_DEFAULT_REGRESSION_BASKET_SIZE}). "
            "Set 0 to disable regression sentinel selection entirely."
        ),
    )

    # DB
    db = p.add_argument_group("DB")
    db.add_argument(
        "--db-mode",
        choices=["sqlite", "postgres"],
        default="sqlite",
        help="Backing store for the canary DB (default: sqlite).",
    )
    db.add_argument(
        "--seed-from-prod",
        action="store_true",
        default=False,
        help=(
            "Copy scrape_profiles from PROD_DATABASE_URL into the canary DB "
            "for the selected property IDs. Requires PROD_DATABASE_URL env var."
        ),
    )
    db.add_argument(
        "--keep",
        default=False,
        action="store_true",
        help="Do not drop the canary DB at the end; print its DSN for forensics.",
    )

    # Fix attribution / feature flags
    fix = p.add_argument_group("Fix attribution / feature flags")
    fix.add_argument(
        "--flag",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help=(
            "Set an env var for the canary run only. Repeatable. "
            "Example: --flag ENABLE_DEGRADED_MAPPING_PERSIST=true"
        ),
    )
    fix.add_argument(
        "--baseline",
        action="store_true",
        default=False,
        help=(
            "Run twice — baseline (flags off) then treatment (flags on). "
            "Emits a delta block comparing the two runs."
        ),
    )

    # Output
    out = p.add_argument_group("Output")
    out.add_argument(
        "--out-dir",
        type=Path,
        metavar="PATH",
        help=(
            "Output directory for this canary run "
            f"(default: {_DEFAULT_OUT_ROOT}/{{timestamp}}/)."
        ),
    )
    out.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable summary.json alongside the markdown report.",
    )
    out.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Per-property progress to stdout.",
    )

    # Behaviour
    beh = p.add_argument_group("Behaviour")
    beh.add_argument(
        "--timeout-per-property",
        type=int,
        default=_DEFAULT_TIMEOUT_PER_PROPERTY,
        metavar="SEC",
        help=f"Abort a property after this many seconds (default: {_DEFAULT_TIMEOUT_PER_PROPERTY}).",
    )
    beh.add_argument(
        "--dry-run-replay",
        action="store_true",
        default=False,
        help=(
            "Use the synthetic dry-run runner instead of jugnu.py. "
            "Every property gets a synthetic SUCCESS verdict. "
            "Useful for smoke-testing the canary tool itself."
        ),
    )

    return p


def _run_single(
    args: argparse.Namespace,
    out_dir: Path,
    canary_dsn: str,
    flag_overrides: dict[str, str],
    selected: list[dict[str, str]],
    canary_input_csv: Path,
    runner: Path | None,
) -> tuple[int, CanaryReport]:
    """Execute one replay+compare cycle.  Returns (exit_code, report)."""
    run_date = date.today().isoformat()

    jugnu_exit = replay(
        canary_input_csv=canary_input_csv,
        canary_dsn=canary_dsn,
        out_dir=out_dir,
        run_date=run_date,
        flag_overrides=flag_overrides,
        timeout_per_property=args.timeout_per_property,
        limit=len(selected),
        runner=runner,
    )

    if jugnu_exit == 2:
        log.error("Jugnu subprocess failed to start; cannot produce a meaningful report.")
        return 2, CanaryReport(run_date=run_date, source_run_date=args.from_run)

    canary_outcomes = read_canary_outcomes(out_dir, run_date)
    report = compare(selected, canary_outcomes, source_run_date=args.from_run)
    return 0, report


def main(argv: list[str] | None = None) -> int:
    """Entry point for the local canary CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 = pass, 1 = regressions found, 2 = infra failure.
    """
    # Dispatch compare subcommand before full parser so it gets its own --help.
    effective_argv = argv if argv is not None else sys.argv[1:]
    if effective_argv and effective_argv[0] == "compare":
        return _main_compare(effective_argv[1:])

    parser = build_parser()
    args = parser.parse_args(effective_argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    if args.limit <= 0:
        parser.error("--limit must be > 0")

    if args.db_mode == "postgres":
        log.warning("--db-mode postgres is not implemented in L1; falling back to sqlite.")

    # ── SETUP ─────────────────────────────────────────────────────────────────
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out_dir: Path = args.out_dir or (_DEFAULT_OUT_ROOT / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    canary_db_path = out_dir / "canary.sqlite"
    canary_dsn = f"sqlite:///{canary_db_path}"

    try:
        setup_canary_db(canary_dsn, db_mode=args.db_mode)
    except Exception as exc:
        log.error("Canary DB setup failed: %s", exc)
        return 2

    # ── SELECT ────────────────────────────────────────────────────────────────
    if args.properties_csv:
        failures_csv_path = args.properties_csv
    else:
        failures_csv_path = _find_failures_csv(args.from_run)

    if failures_csv_path is None or not failures_csv_path.exists():
        log.error(
            "failures.csv not found for run %s. "
            "Run analyze_cloud_run.py first, or pass --properties-csv.",
            args.from_run,
        )
        return 2

    try:
        all_rows = _read_failures_csv(failures_csv_path)
    except Exception as exc:
        log.error("Failed to read %s: %s", failures_csv_path, exc)
        return 2

    try:
        failure_rows = select_properties(
            all_rows,
            filter_tier=args.filter_tier,
            filter_pms=args.filter_pms,
            filter_outcome=args.filter_outcome,
            include_ids=args.include_property_id or [],
            limit=args.limit,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable; parser.error() calls sys.exit

    # Optionally stratify the failure basket for better code-path coverage.
    if args.stratify and failure_rows:
        failure_rows = select_stratified(
            failure_rows, _DEFAULT_FAILURE_STRATA, n_per_stratum=1, limit=args.limit
        )

    # Tag each failure row with its basket label.
    for row in failure_rows:
        row["basket"] = BASKET_FAILURE

    # ── REGRESSION SENTINEL BASKET ────────────────────────────────────────────
    regression_rows: list[dict[str, str]] = []
    if args.regression_basket_size > 0:
        if args.regression_basket_csv:
            successes_path: Path | None = args.regression_basket_csv
        else:
            successes_path = _find_successes_csv(args.from_run)

        if successes_path is not None and successes_path.exists():
            try:
                raw_successes = _read_successes_csv(successes_path)
                regression_rows = select_regression_basket(
                    raw_successes,
                    limit=args.regression_basket_size,
                )
                for row in regression_rows:
                    row["basket"] = BASKET_REGRESSION
                log.info(
                    "Regression sentinel basket: %d properties from %s",
                    len(regression_rows), successes_path,
                )
            except Exception as exc:
                log.warning(
                    "Failed to build regression basket from %s: %s — skipping.",
                    successes_path, exc,
                )
        else:
            log.info(
                "No successes.csv found for run %s — regression sentinel basket empty.",
                args.from_run,
            )

    # Merge failure + regression baskets; deduplicate by property_id (failure
    # basket wins if both contain the same property).
    seen_pids: set[str] = {r["property_id"] for r in failure_rows}
    for row in regression_rows:
        if row["property_id"] not in seen_pids:
            failure_rows.append(row)
            seen_pids.add(row["property_id"])

    selected = failure_rows

    if not selected:
        log.warning(
            "No properties matched the filter criteria for run %s.", args.from_run
        )
        print(
            f"\nLocal canary: 0 properties selected from cloud-run {args.from_run}.\n"
            "Nothing to replay. Adjust filters or check that failures.csv is populated."
        )
        return 0

    canary_input_csv = out_dir / "canary_input.csv"
    write_canary_input_csv(selected, canary_input_csv)

    # ── SEED (L2) ─────────────────────────────────────────────────────────────
    if args.seed_from_prod:
        property_ids = [r["property_id"] for r in selected]
        try:
            seeded = seed_profiles_from_prod(property_ids, canary_dsn)
            log.info("Profile seeding complete: %d profiles seeded", seeded)
        except RuntimeError as exc:
            log.error("Profile seeding failed: %s", exc)
            return 2

    # ── REPLAY ────────────────────────────────────────────────────────────────
    flag_overrides = _parse_flag_overrides(args.flag)
    runner: Path | None = _DRY_RUN_RUNNER if args.dry_run_replay else None

    if args.baseline:
        exit_code, report = _run_baseline_treatment(args, out_dir, canary_dsn, flag_overrides, selected, canary_input_csv, runner)
    else:
        exit_code, report = _run_single(args, out_dir, canary_dsn, flag_overrides, selected, canary_input_csv, runner)

    if exit_code == 2:
        return 2

    # ── REPORT ────────────────────────────────────────────────────────────────
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    log.info("Report written to %s", md_path)

    if args.json:
        json_path = out_dir / "summary.json"
        json_path.write_text(render_json(report), encoding="utf-8")
        log.info("JSON summary written to %s", json_path)

    _print_summary(report, out_dir)

    # ── TEARDOWN ──────────────────────────────────────────────────────────────
    if args.keep:
        print(f"\nCanary DB preserved at: {canary_db_path}")
        print(f"  DSN: {canary_dsn}")
    else:
        teardown_canary_db(canary_db_path)

    return 0 if report.passed else 1


def _run_baseline_treatment(
    args: argparse.Namespace,
    out_dir: Path,
    canary_dsn: str,
    flag_overrides: dict[str, str],
    selected: list[dict[str, str]],
    canary_input_csv: Path,
    runner: Path | None,
) -> tuple[int, CanaryReport]:
    """Run two passes (baseline then treatment) and return the treatment report."""
    log.info("--baseline: running baseline pass (flags stripped)")

    baseline_dir = out_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)

    # Baseline: all feature flags stripped
    exit_b, report_b = _run_single(
        args, baseline_dir, canary_dsn,
        flag_overrides={}, selected=selected,
        canary_input_csv=canary_input_csv, runner=runner,
    )
    if exit_b == 2:
        return 2, report_b

    # Write baseline summary.json for the compare subcommand.
    baseline_json = baseline_dir / "summary.json"
    baseline_json.write_text(render_json(report_b), encoding="utf-8")

    log.info("--baseline: running treatment pass (flags on)")

    treatment_dir = out_dir / "treatment"
    treatment_dir.mkdir(parents=True, exist_ok=True)

    exit_t, report_t = _run_single(
        args, treatment_dir, canary_dsn,
        flag_overrides=flag_overrides, selected=selected,
        canary_input_csv=canary_input_csv, runner=runner,
    )
    if exit_t == 2:
        return 2, report_t

    treatment_json = treatment_dir / "summary.json"
    treatment_json.write_text(render_json(report_t), encoding="utf-8")

    # Print baseline vs. treatment delta.
    delta_regressed = report_t.regressed - report_b.regressed
    delta_improved = report_t.improved - report_b.improved
    print("\nBaseline vs. Treatment delta")
    print(f"  ΔIMPROVED:  {delta_improved:+d}")
    print(f"  ΔREGRESSED: {delta_regressed:+d}")
    gate = delta_regressed <= 0 and report_t.passed
    print(f"\n  Gate: treatment no new regressions -> {'PASS' if gate else 'FAIL'}")

    return 0, report_t


def _print_summary(report: CanaryReport, out_dir: Path | None = None) -> None:
    """Print the summary block to stdout."""
    gate = "PASS" if report.passed else "FAIL"
    print(f"\nLocal canary: {report.properties_total} properties from cloud-run {report.source_run_date}")
    print(f"  IMPROVED:        {report.improved:4d}  (was failing, now succeed)")
    print(f"  UNCHANGED_OK:    {report.unchanged_ok:4d}  (was succeeding, still succeed)")
    print(f"  UNCHANGED_FAIL:  {report.unchanged_fail:4d}  (was failing, still failing)")
    print(f"  REGRESSED:       {report.regressed:4d}  (was succeeding, now fail — STOP)")
    print(f"  TIMEOUT:         {report.timeout_in_canary:4d}  (no PROPERTY_EMITTED — infra issue)")
    print(f"\nPre-deploy gate: REGRESSED == 0 -> {gate}")
    if report.regressed > 0:
        print("\nREGRESSED properties (must investigate before deploy):")
        for row in report.rows:
            if row.verdict == VERDICT_REGRESSED:
                print(f"  {row.property_id}  {row.url}")
    if out_dir:
        print(f"\nFull report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    sys.exit(main())
