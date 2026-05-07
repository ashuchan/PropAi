"""Copy per-run artifact files into their Postgres tables.

Scope:
  - property_reports/{cid}.md        -> property_reports table
  - llm_report.json                  -> llm_reports table
  - llm_report/{cid}.json            -> llm_property_details table
  - llm_diagnostics/{cid}_{kind}.json -> llm_diagnostics table

Profiles live in config/profiles/*.json and can already be migrated via
`backfill_pg.py --only profiles`. This script intentionally doesn't
touch them.

Idempotent: upserts on each table's PK. Re-running replaces rows in place.

Usage:
    python scripts/backfill_artifacts_pg.py --run-date 2026-04-21
    python scripts/backfill_artifacts_pg.py --run-date 2026-04-21 --data-root data/v2
    python scripts/backfill_artifacts_pg.py --run-date 2026-04-21 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))


from data_provider.sql.engine import dialect_insert, make_engine  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    LlmDiagnosticRow,
    LlmPropertyDetailRow,
    LlmReportRow,
    PropertyReportRow,
)

log = logging.getLogger("backfill_artifacts_pg")


# llm_diagnostics filenames look like `{canonical_id}_{kind}.json` —
# e.g. `10629_field_recovery.json`. We split on the first `_` so the
# canonical id keeps any leading digits and `kind` is the remainder.
_DIAG_NAME = re.compile(r"^(?P<cid>[^_]+)_(?P<kind>.+)\.json$")


def _read_json(path: Path) -> dict | list | None:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("skip %s: %s", path.name, e)
        return None


def _upsert_property_reports(engine, run_date: str, dir_: Path, dry: bool) -> int:
    n = 0
    files = sorted(p for p in dir_.glob("*.md") if p.is_file())
    log.info("property_reports: %d files", len(files))
    if dry:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        for path in files:
            cid = path.stem
            markdown = path.read_text(encoding="utf-8")
            stmt = dialect_insert(engine, PropertyReportRow).values(
                run_date=run_date,
                canonical_id=cid,
                markdown=markdown,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PropertyReportRow.run_date, PropertyReportRow.canonical_id],
                set_={"markdown": stmt.excluded.markdown, "written_at": stmt.excluded.written_at},
            )
            conn.execute(stmt)
            n += 1
            if n % 100 == 0:
                log.info("  property_reports: %d/%d", n, len(files))
    return n


def _upsert_llm_report(engine, run_date: str, path: Path, dry: bool) -> int:
    if not path.exists():
        log.info("llm_reports: no llm_report.json — skip")
        return 0
    payload = _read_json(path)
    if payload is None:
        return 0
    log.info("llm_reports: 1 file")
    if dry:
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
            set_={"payload": stmt.excluded.payload, "written_at": stmt.excluded.written_at},
        )
        conn.execute(stmt)
    return 1


def _upsert_llm_property_details(engine, run_date: str, dir_: Path, dry: bool) -> int:
    if not dir_.exists():
        log.info("llm_property_details: no llm_report/ dir — skip")
        return 0
    files = sorted(p for p in dir_.glob("*.json") if p.is_file())
    log.info("llm_property_details: %d files", len(files))
    n = 0
    if dry:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        for path in files:
            payload = _read_json(path)
            if payload is None:
                continue
            pid = path.stem
            stmt = dialect_insert(engine, LlmPropertyDetailRow).values(
                run_date=run_date,
                property_id=pid,
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[LlmPropertyDetailRow.run_date, LlmPropertyDetailRow.property_id],
                set_={"payload": stmt.excluded.payload, "written_at": stmt.excluded.written_at},
            )
            conn.execute(stmt)
            n += 1
    return n


def _upsert_llm_diagnostics(engine, run_date: str, dir_: Path, dry: bool) -> int:
    if not dir_.exists():
        log.info("llm_diagnostics: no llm_diagnostics/ dir — skip")
        return 0
    files = sorted(p for p in dir_.glob("*.json") if p.is_file())
    log.info("llm_diagnostics: %d files", len(files))
    n = 0
    skipped = 0
    if dry:
        return 0
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        for path in files:
            m = _DIAG_NAME.match(path.name)
            if not m:
                skipped += 1
                continue
            cid, kind = m.group("cid"), m.group("kind")
            payload = _read_json(path)
            if payload is None:
                continue
            stmt = dialect_insert(engine, LlmDiagnosticRow).values(
                run_date=run_date,
                property_id=cid,
                kind=kind,
                payload=payload,
                written_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    LlmDiagnosticRow.run_date,
                    LlmDiagnosticRow.property_id,
                    LlmDiagnosticRow.kind,
                ],
                set_={"payload": stmt.excluded.payload, "written_at": stmt.excluded.written_at},
            )
            conn.execute(stmt)
            n += 1
    if skipped:
        log.warning("llm_diagnostics: skipped %d files (unrecognised name pattern)", skipped)
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Copy run artifact files into Postgres.")
    ap.add_argument("--run-date", required=True, help="e.g. 2026-04-21")
    ap.add_argument(
        "--data-root",
        default=None,
        help="Root containing runs/{date}/… (default: resolves DATA_DIR then data/v2 then data/)",
    )
    ap.add_argument("--url", default=None, help="DATABASE_URL override")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    candidates: list[Path] = []
    if args.data_root:
        candidates.append(Path(args.data_root))
    else:
        env_data = os.getenv("DATA_DIR")
        if env_data:
            candidates.append(Path(env_data))
        candidates.extend([Path("data/v2"), Path("data")])

    run_dir: Path | None = None
    for root in candidates:
        abs_root = root if root.is_absolute() else (_MA_POC_ROOT / root).resolve()
        candidate = abs_root / "runs" / args.run_date
        if candidate.exists():
            run_dir = candidate
            log.info("run dir: %s", run_dir)
            break
    if run_dir is None:
        log.error(
            "no runs/%s directory found under any of: %s",
            args.run_date,
            [str(c) for c in candidates],
        )
        return 2

    engine = make_engine(args.url)

    pr = _upsert_property_reports(engine, args.run_date, run_dir / "property_reports", args.dry_run)
    lr = _upsert_llm_report(engine, args.run_date, run_dir / "llm_report.json", args.dry_run)
    lp = _upsert_llm_property_details(engine, args.run_date, run_dir / "llm_report", args.dry_run)
    ld = _upsert_llm_diagnostics(engine, args.run_date, run_dir / "llm_diagnostics", args.dry_run)

    log.info(
        "Done. property_reports=%d llm_reports=%d llm_property_details=%d llm_diagnostics=%d",
        pr,
        lr,
        lp,
        ld,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
