"""Local Canary — replay yesterday's cloud-run failures against today's code.

One command, one run, one verdict per property.

Phases executed in order:
  SETUP    — Create a disposable SQLite canary DB; bootstrap schema.
  SELECT   — Read failures from a prior cloud run; apply filters; write
             canary_input.csv.
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
# Mirrors the pattern in analyze_cloud_run.py so the same DA_POC packages
# resolve regardless of working directory.
_SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/diagnostics/
_MA_POC_ROOT = _SCRIPT_DIR.parent.parent               # ma_poc/
_REPO_ROOT = _MA_POC_ROOT.parent                       # PropAi/
for _p in (_REPO_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("local_canary")

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_LIMIT = 50
_DEFAULT_TIMEOUT_PER_PROPERTY = 180          # seconds
_JUGNU_RUNNER = _MA_POC_ROOT / "scripts" / "runners" / "jugnu.py"
_DEFAULT_OUT_ROOT = _MA_POC_ROOT / "data" / "canary" / "local_runs"
# Mirrors DEFAULT_OUT_ROOT from analyze_cloud_run.py
_ANALYZER_OUT_ROOT = _MA_POC_ROOT / "data" / "reports"

_SUCCESS_VERDICTS = frozenset({"SUCCESS"})
# CARRY_FORWARD counts as a "soft success" from the cloud run perspective;
# a canary that fails on it is a regression.
_CLOUD_OK_VERDICTS = frozenset({"SUCCESS", "CARRY_FORWARD"})

VERDICT_IMPROVED = "IMPROVED"
VERDICT_REGRESSED = "REGRESSED"
VERDICT_UNCHANGED_OK = "UNCHANGED_OK"
VERDICT_UNCHANGED_FAIL = "UNCHANGED_FAIL"
VERDICT_TIMEOUT_IN_CANARY = "TIMEOUT_IN_CANARY"

# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class _CanaryOutcome:
    """Per-property result derived from the canary run's events.jsonl."""

    property_id: str
    verdict: str       # SUCCESS / FAILED_* / TIMEOUT_IN_CANARY
    units: int = 0


@dataclass
class ComparedRow:
    """One row in the delta report table."""

    property_id: str
    url: str
    cloud_outcome: str
    cloud_tier: str
    cloud_units: int
    canary_outcome: str
    canary_tier: str     # populated in L4 (attribution); empty in L1
    canary_units: int
    verdict: str         # IMPROVED / REGRESSED / UNCHANGED_OK / UNCHANGED_FAIL
    attributed_fix: str = ""   # L4


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


# ── DB setup / teardown ───────────────────────────────────────────────────────


def setup_canary_db(canary_dsn: str) -> None:
    """Create and bootstrap the canary SQLite DB.

    Uses SqliteDataProvider with create_schema=True so the full ORM schema
    (Base.metadata.create_all) is applied idempotently.  The provider is
    closed immediately; the subprocess gets a fresh connection via DATABASE_URL.

    Args:
        canary_dsn: SQLAlchemy connection string, e.g. ``sqlite:///path.sqlite``.

    Raises:
        RuntimeError: If schema creation fails (surfaces as exit code 2).
    """
    from data_provider.sqlite import SqliteDataProvider

    dp = SqliteDataProvider(url=canary_dsn, create_schema=True)
    dp.close()
    log.info("Canary DB initialised: %s", canary_dsn)


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

    Args:
        rows: Selected rows from failures.csv.
        out_path: Destination file path.
    """
    _FIELDNAMES = [
        "property_id",
        "url",
        "verdict",
        "terminal_tier",
        "pms_detected",
        "domain",
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
) -> int:
    """Invoke jugnu_runner as a subprocess against the canary input.

    The subprocess gets a fresh env with DATABASE_URL pointed at the canary
    DB and any --flag overrides injected.  stdout+stderr are captured to
    ``{out_dir}/jugnu.log`` so the canary output dir contains a full audit
    trail of what the runner did.

    Args:
        canary_input_csv: Path to the property CSV produced by select_properties.
        canary_dsn: SQLAlchemy DSN for the canary DB.
        out_dir: Base data directory for the jugnu subprocess (run output lands
            under ``{out_dir}/runs/{run_date}/``).
        run_date: YYYY-MM-DD string passed to jugnu via ``--run-date``.
        flag_overrides: ``{ENV_VAR: value}`` dict injected into the subprocess
            env — the mechanism for feature-flag toggling without side-effects.
        timeout_per_property: Seconds to allow per property (default 180).
        limit: Number of properties being run; used to compute the aggregate
            timeout ceiling for the subprocess.

    Returns:
        Jugnu subprocess exit code (0 = completed, non-zero = partial / error).
    """
    subprocess_timeout = timeout_per_property * limit + 300  # 5-min headroom

    env = {
        **os.environ,
        "DATABASE_URL": canary_dsn,
        **flag_overrides,
    }

    cmd = [
        sys.executable,
        str(_JUGNU_RUNNER),
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

    Each PROPERTY_EMITTED event carries ``verdict`` and ``units`` in its
    flat payload (events are serialised with ``**self.data`` unpacked into
    the top-level dict, not nested under a ``data`` key).

    Args:
        out_dir: Base data directory used for the jugnu subprocess.
        run_date: YYYY-MM-DD of the canary run (matches ``--run-date`` passed
            to jugnu; determines the ``runs/{run_date}/`` subdirectory).

    Returns:
        Dict mapping ``property_id → _CanaryOutcome``.  Properties that emitted
        no PROPERTY_EMITTED event are absent (caller treats them as
        TIMEOUT_IN_CANARY).
    """
    events_path = out_dir / "runs" / run_date / "events.jsonl"
    if not events_path.exists():
        log.warning("events.jsonl not found at %s", events_path)
        return {}

    outcomes: dict[str, _CanaryOutcome] = {}
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("kind") != "output.property_emitted":
                continue
            pid = str(ev.get("property_id", "")).strip()
            if not pid:
                continue
            verdict = str(ev.get("verdict", "FAILED_NO_DATA"))
            units = int(ev.get("units", 0))
            # Last PROPERTY_EMITTED per property wins (carry-forward can re-emit).
            outcomes[pid] = _CanaryOutcome(
                property_id=pid, verdict=verdict, units=units
            )

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

    Args:
        cloud_rows: Rows from canary_input.csv (includes the cloud verdict).
        canary_outcomes: Per-property results from the canary run's events.
        source_run_date: YYYY-MM-DD of the original cloud run.

    Returns:
        Populated CanaryReport.
    """
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

        canary = canary_outcomes.get(pid)
        if canary is None:
            canary_verdict = VERDICT_TIMEOUT_IN_CANARY
            canary_units = 0
        else:
            canary_verdict = canary.verdict
            canary_units = canary.units

        canary_was_ok = canary_verdict in _SUCCESS_VERDICTS

        if canary_verdict == VERDICT_TIMEOUT_IN_CANARY:
            row_verdict = VERDICT_TIMEOUT_IN_CANARY
            report.timeout_in_canary += 1
        elif not cloud_was_ok and canary_was_ok:
            row_verdict = VERDICT_IMPROVED
            report.improved += 1
        elif cloud_was_ok and not canary_was_ok:
            row_verdict = VERDICT_REGRESSED
            report.regressed += 1
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
                cloud_units=0,  # failures.csv has no units count for failures
                canary_outcome=canary_verdict,
                canary_tier="",  # populated in L4
                canary_units=canary_units,
                verdict=row_verdict,
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
    lines = [
        f"# Local Canary Report — {report.run_date}",
        "",
        f"**Source run:** {report.source_run_date}  ",
        f"**Properties tested:** {report.properties_total}  ",
        f"**Pre-deploy gate:** `REGRESSED == 0` → **{gate}**",
        "",
        "## Summary",
        "",
        "```",
        f"Local canary: {report.properties_total} properties from cloud-run {report.source_run_date}",
        f"  IMPROVED:        {report.improved:4d}  (was failing, now succeed)",
        f"  UNCHANGED_OK:    {report.unchanged_ok:4d}  (was succeeding, still succeed — sanity baseline)",
        f"  UNCHANGED_FAIL:  {report.unchanged_fail:4d}  (was failing, still failing — fix doesn't cover them)",
        f"  REGRESSED:       {report.regressed:4d}  (was succeeding, now fail — STOP, do not deploy)",
        f"  TIMEOUT:         {report.timeout_in_canary:4d}  (no PROPERTY_EMITTED — investigate canary tool)",
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

    lines += [
        "## Per-property delta",
        "",
        "| property_id | cloud_outcome | cloud_tier | cloud_units"
        " | canary_outcome | canary_units | verdict |",
        "|---|---|---|---|---|---|---|",
    ]

    for row in report.rows:
        lines.append(
            f"| `{row.property_id}` | {row.cloud_outcome} | {row.cloud_tier}"
            f" | {row.cloud_units} | {row.canary_outcome} | {row.canary_units}"
            f" | **{row.verdict}** |"
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
                "verdict": r.verdict,
                "attributed_fix": r.attributed_fix,
            }
            for r in report.rows
        ],
    }
    return json.dumps(payload, indent=2)


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
        help="[L2] Copy yesterday's scrape_profiles from the live DB into the canary DB. Not implemented in L1.",
    )
    db.add_argument(
        "--keep",
        action="store_true",
        default=False,
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
        help="[L5] Run twice — baseline (flags off) then treatment (flags on). Not implemented in L1.",
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

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for the local canary CLI.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 = pass, 1 = regressions found, 2 = infra failure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    if args.limit <= 0:
        parser.error("--limit must be > 0")

    if args.baseline:
        log.warning("--baseline is not implemented in L1; flag ignored.")
    if args.seed_from_prod:
        log.warning("--seed-from-prod is not implemented in L1; flag ignored.")
    if args.db_mode == "postgres":
        log.warning("--db-mode postgres is not implemented in L1; falling back to sqlite.")

    # ── SETUP ─────────────────────────────────────────────────────────────────
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out_dir: Path = args.out_dir or (_DEFAULT_OUT_ROOT / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    canary_db_path = out_dir / "canary.sqlite"
    canary_dsn = f"sqlite:///{canary_db_path}"

    try:
        setup_canary_db(canary_dsn)
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
        selected = select_properties(
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

    # ── REPLAY ────────────────────────────────────────────────────────────────
    flag_overrides = _parse_flag_overrides(args.flag)
    run_date = date.today().isoformat()

    jugnu_exit = replay(
        canary_input_csv=canary_input_csv,
        canary_dsn=canary_dsn,
        out_dir=out_dir,
        run_date=run_date,
        flag_overrides=flag_overrides,
        timeout_per_property=args.timeout_per_property,
        limit=args.limit,
    )

    if jugnu_exit == 2:
        log.error("Jugnu subprocess failed to start; cannot produce a meaningful report.")
        return 2

    # ── COMPARE ───────────────────────────────────────────────────────────────
    canary_outcomes = read_canary_outcomes(out_dir, run_date)
    report = compare(selected, canary_outcomes, source_run_date=args.from_run)

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


def _print_summary(report: CanaryReport, out_dir: Path | None = None) -> None:
    """Print the summary block to stdout."""
    gate = "PASS" if report.passed else "FAIL"
    print(f"\nLocal canary: {report.properties_total} properties from cloud-run {report.source_run_date}")
    print(f"  IMPROVED:        {report.improved:4d}  (was failing, now succeed)")
    print(f"  UNCHANGED_OK:    {report.unchanged_ok:4d}  (was succeeding, still succeed)")
    print(f"  UNCHANGED_FAIL:  {report.unchanged_fail:4d}  (was failing, still failing)")
    print(f"  REGRESSED:       {report.regressed:4d}  (was succeeding, now fail — STOP)")
    print(f"  TIMEOUT:         {report.timeout_in_canary:4d}  (no PROPERTY_EMITTED — infra issue)")
    print(f"\nPre-deploy gate: REGRESSED == 0 → {gate}")
    if report.regressed > 0:
        print("\nREGRESSED properties (must investigate before deploy):")
        for row in report.rows:
            if row.verdict == VERDICT_REGRESSED:
                print(f"  {row.property_id}  {row.url}")
    if out_dir:
        print(f"\nFull report: {out_dir / 'report.md'}")


if __name__ == "__main__":
    sys.exit(main())
