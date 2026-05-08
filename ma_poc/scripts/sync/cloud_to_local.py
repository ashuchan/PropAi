"""sync_cloud_to_local.py — copy every data_provider table from Cloud SQL into local Postgres.

Mirrors the live Cloud SQL `jugnu` database into a local Postgres
instance (defaults to the `proppy` database described in
ma_poc/.env) so you can develop / debug against real production data
without round-tripping through Cloud SQL on every query.

Direction is one-way: Cloud SQL (source) -> local (destination).

Default mode (``--mode=mirror``) is destructive and atomic: every
selected destination table is TRUNCATEd in a single statement (with
``RESTART IDENTITY CASCADE``) BEFORE any rows are copied, then cloud
rows are plain-inserted. The local DB ends up byte-identical to the
cloud snapshot taken at script-start time and identity sequences are
reset so post-sync local inserts don't collide with imported PKs.

If you want additive merge semantics use ``--mode=upsert`` instead —
that path keys on the PK for business-key tables and on ``run_date``
for surrogate-PK tables (``property_snapshots``, ``run_issues``,
``run_ledger``) so independent local rows for run_dates the cloud
doesn't have are preserved.

Connection paths
----------------
- Source (Cloud SQL): the script spawns `cloud-sql-proxy` (path from
  CLOUD_SQL_PROXY_BIN, defaults to PATH lookup) on a non-default local
  port so it does not collide with the local Postgres on 5432, then
  connects via `postgresql+pg8000://...@localhost:<proxy_port>/<db>`.
  Auth comes from DATABASE_URL — same parsing rules as the rest of
  the data_provider layer (password is percent-decoded).

- Destination (local): pass `--local-url` or set `LOCAL_DATABASE_URL`.
  Defaults to the local proppy URL hinted in .env. Must be a Postgres
  URL (psycopg or pg8000); SQLite is not supported because the schema
  uses Postgres-specific JSON columns end-to-end.

Schema assumption
-----------------
The destination DB must already have the data_provider tables created
(via `alembic upgrade head` against the local URL). The script only
copies row data — it does not create or alter tables.

Usage
-----
  # Default: mirror everything from cloud -> local proppy
  python scripts/sync/cloud_to_local.py

  # Subset of tables
  python scripts/sync/cloud_to_local.py --tables properties,units,run_reports

  # Dry run (just print row counts)
  python scripts/sync/cloud_to_local.py --dry-run

  # Custom destination
  python scripts/sync/cloud_to_local.py \
      --local-url postgresql+pg8000://postgres:secret@localhost:5432/mydb
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from sqlalchemy import Engine, Table, create_engine, func, select  # noqa: E402
from sqlalchemy.engine.url import make_url  # noqa: E402

from data_provider.sql.engine import dialect_insert  # noqa: E402
from data_provider.sql.models import Base  # noqa: E402

# Tables whose declared PK is a surrogate autoincrement integer rather
# than a business identity. ON CONFLICT on the surrogate `id` would
# silently overwrite unrelated local rows that happen to share an id
# with a cloud row, so in upsert mode we instead delete the slice of
# the local table that overlaps the cloud's run_dates and plain-insert
# the cloud rows. Local rows with run_dates not present in the cloud
# snapshot survive untouched.
_SURROGATE_PK_TABLES: dict[str, str] = {
    "property_snapshots": "run_date",
    "run_issues": "run_date",
    "run_ledger": "run_date",
}

log = logging.getLogger("sync_cloud_to_local")

# Port the local cloud-sql-proxy listens on. 5433 to avoid colliding
# with a local Postgres typically running on 5432.
_PROXY_PORT = 5433
_PROXY_READY_TIMEOUT_SEC = 30.0

# Hard-coded defaults so the script runs with no flags / no env. Override
# with --cloud-instance / --cloud-database-url / --local-url or the
# matching env vars (CLOUD_SQL_INSTANCE / DATABASE_URL / LOCAL_DATABASE_URL).
_DEFAULT_CLOUD_INSTANCE = "jugnu-494013:us-central1:jugnu-db-production"
_DEFAULT_CLOUD_DATABASE_URL = (
    "postgresql://postgres:%5DxYcSFaoaeBPXdYY@/jugnu?sslmode=require"
)
_DEFAULT_LOCAL_URL = (
    "postgresql+pg8000://postgres:Ashu%40007saxe@localhost:5432/proppy"
)
# Hard-coded fallback for the cloud-sql-proxy binary location. Matches
# the CLOUD_SQL_PROXY_BIN value in ma_poc/.env so the script works even
# when the env file isn't loaded (e.g. running from a fresh shell).
_DEFAULT_PROXY_BIN = r"C:\Users\ashus\bin\cloud-sql-proxy.exe"


# ── Cloud SQL Auth Proxy management ──────────────────────────────────────────


def _drain_stream(
    stream: IO[bytes] | None,
    label: str,
    q: queue.Queue[tuple[str, str] | None],
) -> None:
    if stream is None:
        q.put(None)
        return
    try:
        for raw in iter(stream.readline, b""):
            q.put((label, raw.decode("utf-8", errors="replace")))
    finally:
        q.put(None)


@contextmanager
def _cloud_sql_proxy(
    instance: str,
    *,
    port: int,
    auto_iam_authn: bool,
    bin_path: str | None = None,
) -> Generator[None, None, None]:
    """Spawn cloud-sql-proxy on a local port; yield once ready; terminate on exit.

    Mirrors `scripts/migrate.py:cloud_sql_proxy` but parameterised on
    port + auto_iam_authn + binary path so we can run alongside the
    one migrate.py already uses on 5432 with IAM auth, while this one
    uses 5433 with password auth.
    """
    binary = bin_path or "cloud-sql-proxy"
    argv = [binary, f"--port={port}"]
    if auto_iam_authn:
        argv.append("--auto-iam-authn")
    argv.append(instance)

    log.info("Spawning cloud-sql-proxy: %s", " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    q: queue.Queue[tuple[str, str] | None] = queue.Queue()
    pumps = [
        threading.Thread(target=_drain_stream, args=(proc.stdout, "stdout", q), daemon=True),
        threading.Thread(target=_drain_stream, args=(proc.stderr, "stderr", q), daemon=True),
    ]
    for p in pumps:
        p.start()

    try:
        ready = False
        seen: list[str] = []
        closed = 0
        deadline = time.monotonic() + _PROXY_READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None and closed >= len(pumps):
                    break
                continue
            if item is None:
                closed += 1
                if closed >= len(pumps) and proc.poll() is not None:
                    break
                continue
            label, text = item
            seen.append(f"[{label}] {text.rstrip()}")
            if "ready for new connections" in text.lower():
                ready = True
                break

        if ready:
            log.info("cloud-sql-proxy ready on localhost:%d", port)
            yield
            return

        if proc.poll() is not None:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    break
                if item is None:
                    continue
                label, text = item
                seen.append(f"[{label}] {text.rstrip()}")
            sys.exit(
                f"cloud-sql-proxy exited with code {proc.returncode} "
                f"before becoming ready. output:\n" + "\n".join(seen)
            )

        sys.exit(
            f"cloud-sql-proxy did not become ready in {_PROXY_READY_TIMEOUT_SEC:.0f}s. "
            f"output:\n" + "\n".join(seen)
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Engine builders ──────────────────────────────────────────────────────────


def _build_source_engine(*, instance: str, password: str, user: str, db: str, port: int) -> Engine:
    """Build the engine pointing at the locally-tunneled cloud-sql-proxy port."""
    quoted_pw = urllib.parse.quote(password, safe="")
    url = f"postgresql+pg8000://{user}:{quoted_pw}@localhost:{port}/{db}"
    log.info("Source engine (cloud via proxy): postgresql+pg8000://%s:***@localhost:%d/%s", user, port, db)
    return create_engine(url, future=True, pool_pre_ping=True)


def _build_dest_engine(url: str) -> Engine:
    parsed = make_url(url)
    if not parsed.get_backend_name().startswith("postgresql"):
        raise SystemExit(
            f"--local-url must be a Postgres URL (got {parsed.get_backend_name()!r}). "
            "SQLite is not supported because data_provider tables use JSON columns."
        )
    log.info(
        "Dest engine (local): %s",
        parsed.render_as_string(hide_password=True),
    )
    return create_engine(url, future=True, pool_pre_ping=True)


# ── Cloud connection-info parsing ────────────────────────────────────────────


def _parse_database_url(url: str) -> tuple[str, str, str]:
    """Pull (user, password, db) out of DATABASE_URL.

    Cloud SQL DATABASE_URLs in this project look like
    `postgresql://postgres:<percent-encoded-pw>@/jugnu?sslmode=require`
    — host is empty because the connector overrides transport. We only
    need user/password/db.
    """
    parsed = make_url(url)
    user = parsed.username or ""
    password = parsed.password or ""
    db = parsed.database or ""
    if not user or not password or not db:
        raise SystemExit(
            f"DATABASE_URL is missing user/password/db: "
            f"{parsed.render_as_string(hide_password=True)!r}"
        )
    return user, password, db


# ── Table truncate (upfront, mirror mode only) ───────────────────────────────


def _truncate_tables(dst: Engine, tables: Sequence[Table]) -> None:
    """TRUNCATE every selected destination table in a single statement.

    Called once at the start of the run when ``--mode=mirror`` so that
    the local DB is wiped atomically before any cloud rows are
    inserted, instead of doing a per-table DELETE mid-copy. The single
    ``TRUNCATE`` statement is also dramatically faster than 17
    individual DELETEs on a 4M-row dataset (TRUNCATE scans the row
    pages once; DELETE walks the heap + writes a tombstone per row).

    ``RESTART IDENTITY`` resets autoincrement sequences for surrogate-PK
    tables (``property_snapshots``, ``run_issues``, ``run_ledger``) so
    we don't have to ``setval`` them after each copy.

    ``CASCADE`` is harmless: the data_provider schema has no FK edges
    between these tables today (verified in ma_poc/data_provider/sql/
    models.py — every FK is implicit, enforced in application code).
    The flag is included so a future migration that adds an FK doesn't
    silently break this path.
    """
    if not tables:
        return
    from sqlalchemy import text

    quoted = ", ".join(f'"{t.name}"' for t in tables)
    log.info("Truncating %d table(s): %s", len(tables), [t.name for t in tables])
    with dst.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


# ── Table copy ───────────────────────────────────────────────────────────────


def _copy_table(
    *,
    table: Table,
    src: Engine,
    dst: Engine,
    batch_size: int,
    mode: str,
    dry_run: bool,
) -> dict[str, int]:
    """Copy one table's rows from src -> dst.

    Returns {"source_rows": N, "written": M}.

    ``mode``:
      - 'mirror': caller has already TRUNCATEd every selected table
        upfront (see ``_truncate_tables``); this function plain-inserts
        cloud rows.
      - 'upsert' (additive merge mode):
          * Tables with business-key PKs (``properties``, ``units``,
            ``run_reports``, ``scrape_events``, etc.): INSERT … ON
            CONFLICT DO UPDATE keyed on the PK. Idempotent on re-runs;
            never produces duplicates.
          * Surrogate-PK tables listed in ``_SURROGATE_PK_TABLES``
            (``property_snapshots``, ``run_issues``, ``run_ledger``):
            DELETE only the slice of local rows whose ``run_date``
            appears in the cloud snapshot, then plain-insert the cloud
            rows. Local rows for run_dates the cloud doesn't have
            survive untouched. Avoids corrupting local rows whose
            autoincrement ``id`` coincidentally matches a cloud row.
    """
    name = table.name
    cols = list(table.columns.keys())

    with src.connect() as src_conn:
        total = src_conn.execute(select(func.count()).select_from(table)).scalar_one()
    log.info("[%s] source rows = %d", name, total)

    if dry_run:
        return {"source_rows": total, "written": 0}

    written = 0
    surrogate_slice_col = _SURROGATE_PK_TABLES.get(table.name) if mode == "upsert" else None
    with dst.begin() as dst_conn:
        if mode == "mirror":
            # The upfront TRUNCATE in main() already cleared this table
            # — nothing to do here before the insert loop runs.
            pass
        elif surrogate_slice_col is not None:
            # Surrogate-PK table: ON CONFLICT on `id` would corrupt local
            # rows that share an id with a cloud row by coincidence. Read
            # the distinct slice values present in cloud and DELETE only
            # that slice from local before plain-inserting cloud rows.
            slice_col = table.c[surrogate_slice_col]
            with src.connect() as src_conn:
                slice_values = [
                    v
                    for (v,) in src_conn.execute(
                        select(slice_col).distinct().where(slice_col.is_not(None))
                    ).all()
                ]
            if slice_values:
                log.info(
                    "[%s] surrogate-PK table: deleting %d local %s slice(s) before insert",
                    name,
                    len(slice_values),
                    surrogate_slice_col,
                )
                dst_conn.execute(table.delete().where(slice_col.in_(slice_values)))

        if total == 0:
            return {"source_rows": 0, "written": 0}

        pk_cols = [c.name for c in table.primary_key.columns]

        with src.connect() as src_conn:
            result = src_conn.execution_options(stream_results=True).execute(select(table))
            while True:
                batch = result.fetchmany(batch_size)
                if not batch:
                    break
                rows = [dict(zip(cols, row, strict=True)) for row in batch]
                if mode == "upsert" and pk_cols and surrogate_slice_col is None:
                    stmt = dialect_insert(dst, table).values(rows)
                    set_ = {c: stmt.excluded[c] for c in cols if c not in pk_cols}
                    if set_:
                        stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=set_)
                    else:
                        stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
                    dst_conn.execute(stmt)
                else:
                    # mirror mode, surrogate-PK upsert (slice already deleted),
                    # or no PK at all: plain insert.
                    dst_conn.execute(table.insert(), rows)
                written += len(rows)
                log.info("[%s] copied %d / %d", name, written, total)

        # Resync identity sequences for tables with autoincrement PKs so
        # future local inserts don't collide with the imported max(id).
        for col in table.primary_key.columns:
            if col.autoincrement is True:
                _resync_sequence(dst_conn, table.name, col.name)

    return {"source_rows": total, "written": written}


def _resync_sequence(conn: Any, table_name: str, col_name: str) -> None:
    """Bump the Postgres SERIAL/IDENTITY sequence to max(col)+1 after a bulk copy.

    Without this, the next local INSERT that lets the sequence pick the
    PK would collide with one of the rows we just copied.
    """
    from sqlalchemy import text

    try:
        conn.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:t, :c), "
                "COALESCE((SELECT MAX(\"" + col_name + "\") FROM \"" + table_name + "\"), 1))"
            ),
            {"t": table_name, "c": col_name},
        )
    except Exception as exc:
        log.warning("[%s] could not resync sequence for %s: %s", table_name, col_name, exc)


# ── Main ─────────────────────────────────────────────────────────────────────


def _select_tables(names_arg: str | None) -> Sequence[Table]:
    all_tables = list(Base.metadata.sorted_tables)
    if not names_arg:
        return all_tables
    wanted = {n.strip() for n in names_arg.split(",") if n.strip()}
    by_name = {t.name: t for t in all_tables}
    missing = wanted - set(by_name)
    if missing:
        raise SystemExit(f"Unknown table(s): {sorted(missing)}. Known: {sorted(by_name)}")
    return [by_name[n] for n in by_name if n in wanted]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--local-url",
        default=os.environ.get("LOCAL_DATABASE_URL", _DEFAULT_LOCAL_URL),
        help="Destination Postgres URL (default: local proppy via pg8000)",
    )
    ap.add_argument(
        "--cloud-instance",
        default=os.environ.get("CLOUD_SQL_INSTANCE", _DEFAULT_CLOUD_INSTANCE),
        help="Cloud SQL instance connection name (project:region:instance). "
        f"Defaults to CLOUD_SQL_INSTANCE env var, then {_DEFAULT_CLOUD_INSTANCE!r}.",
    )
    ap.add_argument(
        "--cloud-database-url",
        default=os.environ.get("DATABASE_URL", _DEFAULT_CLOUD_DATABASE_URL),
        help="Cloud SQL DATABASE_URL — only user/password/db are read. "
        "Defaults to DATABASE_URL env var, then the production jugnu URL "
        "hard-coded in this script.",
    )
    ap.add_argument(
        "--proxy-bin",
        default=os.environ.get("CLOUD_SQL_PROXY_BIN", _DEFAULT_PROXY_BIN),
        help="Path to cloud-sql-proxy binary. Defaults to CLOUD_SQL_PROXY_BIN env var, "
        f"then {_DEFAULT_PROXY_BIN!r}.",
    )
    ap.add_argument(
        "--proxy-port",
        type=int,
        default=_PROXY_PORT,
        help=f"Local port for cloud-sql-proxy (default: {_PROXY_PORT})",
    )
    ap.add_argument(
        "--auth-type",
        choices=["PASSWORD", "IAM"],
        default=os.environ.get("CLOUD_SQL_AUTH_TYPE", "PASSWORD").upper(),
        help="Cloud SQL auth mode. PASSWORD uses the password from --cloud-database-url; "
        "IAM uses --auto-iam-authn (caller's gcloud ADC identity).",
    )
    ap.add_argument(
        "--tables",
        default=None,
        help="Comma-separated subset of tables to sync. Default: all data_provider tables.",
    )
    ap.add_argument(
        "--mode",
        choices=["mirror", "upsert"],
        default="mirror",
        help="mirror: TRUNCATE local table then insert cloud rows (default — local becomes "
        "an exact copy). upsert: INSERT … ON CONFLICT DO UPDATE on primary key (preserves "
        "extra local rows).",
    )
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print source row counts without writing to the destination.",
    )
    args = ap.parse_args()

    if not args.cloud_instance:
        raise SystemExit("--cloud-instance / CLOUD_SQL_INSTANCE is required")
    if not args.cloud_database_url:
        raise SystemExit("--cloud-database-url / DATABASE_URL is required")

    user, password, db = _parse_database_url(args.cloud_database_url)
    tables = _select_tables(args.tables)
    log.info(
        "Plan: %d table(s) %s — mode=%s dry_run=%s",
        len(tables),
        [t.name for t in tables],
        args.mode,
        args.dry_run,
    )

    auto_iam = args.auth_type == "IAM"
    summary: dict[str, dict[str, int]] = {}

    with _cloud_sql_proxy(
        args.cloud_instance,
        port=args.proxy_port,
        auto_iam_authn=auto_iam,
        bin_path=args.proxy_bin,
    ):
        src = _build_source_engine(
            instance=args.cloud_instance,
            password=password,
            user=user,
            db=db,
            port=args.proxy_port,
        )
        dst = _build_dest_engine(args.local_url)

        try:
            # Mirror mode: wipe every selected table in a single
            # ``TRUNCATE … RESTART IDENTITY CASCADE`` before any cloud
            # rows are inserted. Atomic — if the script crashes mid-sync
            # the local DB is left empty (not half-old, half-new).
            # Skipped in dry-run and in upsert mode (which preserves
            # local rows the cloud doesn't have).
            if args.mode == "mirror" and not args.dry_run:
                _truncate_tables(dst, tables)
            for table in tables:
                try:
                    summary[table.name] = _copy_table(
                        table=table,
                        src=src,
                        dst=dst,
                        batch_size=args.batch_size,
                        mode=args.mode,
                        dry_run=args.dry_run,
                    )
                except Exception:
                    log.exception("[%s] copy failed", table.name)
                    summary[table.name] = {"source_rows": -1, "written": -1}
        finally:
            src.dispose()
            dst.dispose()

    log.info("=" * 60)
    log.info("Sync summary (table -> source_rows / written):")
    width = max(len(name) for name in summary) if summary else 0
    for name, counts in summary.items():
        log.info("  %-*s  %8d  /  %8d", width, name, counts["source_rows"], counts["written"])
    failed = [name for name, c in summary.items() if c["written"] < 0]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
