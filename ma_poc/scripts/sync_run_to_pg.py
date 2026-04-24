"""sync_run_to_pg.py — copy one run's filesystem output into Postgres.

Invoked by ``jugnu_shard_entry.py`` after the scrape runner exits 0. The
production scrape pipeline writes exclusively to the local filesystem
(``/tmp/data/v2/runs/{date}/…``) and relies on this module to land every
artifact in Cloud SQL. Without this step the Postgres tables stay empty
no matter what ``DATA_PROVIDER`` env var is set to — the runner itself
has no knowledge of the data-provider layer.

Write surface (keep in sync with ``scripts/CLAUDE.md`` and
``data_provider/sql/models.py``):

  current state
    - properties        via SqlPropertyStateStore.upsert
    - units             via SqlUnitStateStore.upsert_units

  per-run
    - runs              (registry row) via SqlRunStore.write_properties
    - property_snapshots via SqlRunStore.write_properties
    - run_reports       via SqlRunStore.write_report
    - run_issues        via SqlRunStore.append_issue (issues.jsonl)
    - run_ledger        via SqlRunStore.append_ledger_entry (ledger.jsonl)

  audit + learning
    - scrape_events     via SqlScrapeEventStore.append
    - scrape_profiles   via SqlProfileStore.put
    - extraction_results via SqlExtractionResultStore.write

  run artifacts (tables added in alembic 0003_artifacts)
    - property_reports       from runs/{date}/property_reports/{cid}.md
    - llm_reports            from runs/{date}/llm_report.json
    - llm_property_details   from runs/{date}/llm_report/{cid}.json
    - llm_diagnostics        from runs/{date}/llm_diagnostics/{cid}_{kind}.json

Idempotent: every insert uses ``on_conflict_do_update`` on the table's
natural key. Re-running the sync for the same run_date replaces rows in
place — safe to invoke on Cloud Run retry.

Never raises. On any failure the caller decides whether to fail the
shard; this module returns a summary dict so the caller can log exactly
what landed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from data_provider import (  # noqa: E402
    DataProvider,
    FileSystemDataProvider,
    PostgresDataProvider,
)
from data_provider.sql.engine import dialect_insert  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    LlmDiagnosticRow,
    LlmPropertyDetailRow,
    LlmReportRow,
    PropertyReportRow,
)

log = logging.getLogger("sync_run_to_pg")

# ``{canonical_id}_{kind}.json`` — ids can contain digits but not ``_``
# in practice; we split on the first underscore.
_DIAG_NAME = re.compile(r"^(?P<cid>[^_]+)_(?P<kind>.+)\.json$")


# ── Reusable copies from FS provider → SQL provider ──────────────────────────


def _copy_state(src: DataProvider, dst: DataProvider) -> int:
    """Copy properties + units. Returns number of canonical_ids touched."""
    ids = sorted(src.property_state.all_canonical_ids())
    if not ids:
        return 0
    for cid in ids:
        entry = src.property_state.get(cid)
        if entry is None:
            continue
        snap = entry.model_dump(exclude={"canonical_id"})
        # Mirror backfill_pg: use last_seen_at's date part, fall back to
        # first_seen_date. Gives the SQL row the same provenance as FS.
        seen_at_date = (entry.last_seen_at or "")[:10]
        run_date = seen_at_date or entry.first_seen_date or "sync"
        dst.property_state.upsert(cid, snap, run_date)

        units = src.unit_state.get_units(cid)
        if units:
            units_list = [u.model_dump() for u in units.values()]
            dst.unit_state.upsert_units(cid, units_list, run_date)
    return len(ids)


def _copy_profiles(src: DataProvider, dst: DataProvider) -> int:
    ids = src.profiles.list_ids()
    for cid in ids:
        prof = src.profiles.get(cid)
        if prof is not None:
            dst.profiles.put(prof)
    return len(ids)


def _copy_events(src: DataProvider, dst: DataProvider) -> int:
    count = 0
    for ev in src.scrape_events.read_all():
        dst.scrape_events.append(ev)
        count += 1
    return count


def _copy_run(src: DataProvider, dst: DataProvider, run_date: str) -> dict[str, int]:
    """Copy one run_date's properties, report, issues, ledger."""
    out = {"properties": 0, "report": 0, "issues": 0, "ledger": 0}
    props = src.runs.read_properties(run_date)
    if props:
        dst.runs.write_properties(run_date, props)
        out["properties"] = len(props)
    report = src.runs.read_report(run_date)
    if report is not None:
        dst.runs.write_report(run_date, report)
        out["report"] = 1
    for issue in src.runs.read_issues(run_date):
        dst.runs.append_issue(run_date, issue)
        out["issues"] += 1
    for entry in src.runs.read_ledger(run_date):
        dst.runs.append_ledger_entry(run_date, entry)
        out["ledger"] += 1
    return out


def _copy_extractions(
    src: DataProvider,
    dst: DataProvider,
    run_date: str,
    canonical_ids: Iterable[str],
) -> int:
    count = 0
    for cid in canonical_ids:
        res = src.extraction_results.read(run_date, cid)
        if res is None:
            continue
        dst.extraction_results.write(run_date, res)
        count += 1
    return count


# ── Artifact tables (no store interface yet — use engine directly) ───────────


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skipping %s: %s", path.name, exc)
        return None


def _upsert_property_reports(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.md") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        for path in files:
            stmt = dialect_insert(engine, PropertyReportRow).values(
                run_date=run_date,
                canonical_id=path.stem,
                markdown=path.read_text(encoding="utf-8"),
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    PropertyReportRow.run_date,
                    PropertyReportRow.canonical_id,
                ],
                set_={
                    "markdown": stmt.excluded.markdown,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
    return len(files)


def _upsert_llm_report(engine: Any, run_date: str, path: Path) -> int:
    if not path.exists():
        return 0
    payload = _load_json(path)
    if payload is None:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        stmt = dialect_insert(engine, LlmReportRow).values(
            run_date=run_date,
            payload=payload,
            written_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[LlmReportRow.run_date],
            set_={
                "payload": stmt.excluded.payload,
                "written_at": stmt.excluded.written_at,
            },
        )
        conn.execute(stmt)
    return 1


def _upsert_llm_property_details(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.json") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    count = 0
    with engine.begin() as conn:
        for path in files:
            payload = _load_json(path)
            if payload is None:
                continue
            stmt = dialect_insert(engine, LlmPropertyDetailRow).values(
                run_date=run_date,
                property_id=path.stem,
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    LlmPropertyDetailRow.run_date,
                    LlmPropertyDetailRow.property_id,
                ],
                set_={
                    "payload": stmt.excluded.payload,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
            count += 1
    return count


def _upsert_llm_diagnostics(engine: Any, run_date: str, dir_: Path) -> int:
    if not dir_.exists():
        return 0
    files = [p for p in dir_.glob("*.json") if p.is_file()]
    if not files:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    count = 0
    with engine.begin() as conn:
        for path in files:
            m = _DIAG_NAME.match(path.name)
            if m is None:
                log.warning("llm_diagnostics: skipping unrecognised name %s", path.name)
                continue
            payload = _load_json(path)
            if payload is None:
                continue
            stmt = dialect_insert(engine, LlmDiagnosticRow).values(
                run_date=run_date,
                property_id=m.group("cid"),
                kind=m.group("kind"),
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    LlmDiagnosticRow.run_date,
                    LlmDiagnosticRow.property_id,
                    LlmDiagnosticRow.kind,
                ],
                set_={
                    "payload": stmt.excluded.payload,
                    "written_at": stmt.excluded.written_at,
                },
            )
            conn.execute(stmt)
            count += 1
    return count


# ── Orchestration ────────────────────────────────────────────────────────────


def sync_run_to_postgres(
    *,
    run_date: str,
    data_dir: Path,
    config_dir: Path,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Copy one run's FS output into Postgres. Returns a counts summary.

    Args:
        run_date: YYYY-MM-DD string matching a ``runs/{run_date}/`` folder
            under ``data_dir``.
        data_dir: The base dir the runner wrote to (e.g. ``/tmp/data/v2``).
            Must already contain ``runs/{run_date}/`` + ``state/``.
        config_dir: Directory whose ``profiles/`` subdir holds the
            ``{cid}.json`` files the runner updated during the scrape.
        database_url: Passed to ``PostgresDataProvider``. When omitted,
            the provider resolves ``DATABASE_URL`` internally (with the
            Cloud SQL Connector path picked up via ``CLOUD_SQL_INSTANCE``
            in ``make_engine``).

    Raises on any SQL error — the caller (shard_entry) decides whether
    to fail the shard. Fail-the-shard is the right default: a silent
    sync failure is exactly how we got here.
    """
    data_dir = Path(data_dir)
    config_dir = Path(config_dir)
    run_dir = data_dir / "runs" / run_date
    if not run_dir.exists():
        raise FileNotFoundError(
            f"run dir not found: {run_dir}. Did the runner exit successfully "
            f"with --data-dir={data_dir} and --run-date={run_date}?"
        )

    log.info(
        "sync start: run_date=%s data_dir=%s config_dir=%s provider=postgres",
        run_date,
        data_dir,
        config_dir,
    )

    src = FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)
    dst = PostgresDataProvider(url=database_url)
    summary: dict[str, Any] = {}
    try:
        # Stage 1 — everything that has a store interface goes inside a
        # single Postgres transaction. If any write fails the whole
        # stage rolls back, leaving the DB in its pre-sync state.
        canonical_ids = sorted(src.property_state.all_canonical_ids())
        with dst.transaction():
            summary["state"] = _copy_state(src, dst)
            summary["profiles"] = _copy_profiles(src, dst)
            summary["events"] = _copy_events(src, dst)
            summary["run"] = _copy_run(src, dst, run_date)
            summary["extractions"] = _copy_extractions(
                src, dst, run_date, canonical_ids
            )

        # Stage 2 — artifact tables. Each helper manages its own txn via
        # ``engine.begin()`` (the SQL provider exposes the engine).
        engine = dst.engine
        summary["property_reports"] = _upsert_property_reports(
            engine, run_date, run_dir / "property_reports"
        )
        summary["llm_reports"] = _upsert_llm_report(
            engine, run_date, run_dir / "llm_report.json"
        )
        summary["llm_property_details"] = _upsert_llm_property_details(
            engine, run_date, run_dir / "llm_report"
        )
        summary["llm_diagnostics"] = _upsert_llm_diagnostics(
            engine, run_date, run_dir / "llm_diagnostics"
        )
    finally:
        try:
            src.close()
        finally:
            dst.close()

    log.info("sync complete: %s", summary)
    return summary


# ── CLI (for manual re-sync / debugging) ─────────────────────────────────────


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        description="Copy one run's FS output into Postgres.",
    )
    ap.add_argument("--run-date", required=True)
    ap.add_argument(
        "--data-dir",
        required=True,
        help="Base dir containing runs/{date}/ + state/ (e.g. /tmp/data/v2)",
    )
    ap.add_argument(
        "--config-dir",
        default=str(_MA_POC_ROOT / "config"),
        help="Dir containing profiles/ (default: ma_poc/config)",
    )
    ap.add_argument("--url", default=None, help="DATABASE_URL override")
    args = ap.parse_args()

    try:
        sync_run_to_postgres(
            run_date=args.run_date,
            data_dir=Path(args.data_dir),
            config_dir=Path(args.config_dir),
            database_url=args.url,
        )
    except Exception:
        log.exception("sync failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
