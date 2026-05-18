#!/usr/bin/env python3
"""One-shot backfill for `properties` rows whose soft fields were NULL'd by
the pre-fix upsert ratchet (proj_name, address, city, state, zip_code,
pmc, website, phone, etc.).

Background
----------
Two upstream bugs converged to leave 840+ rows with NULL identity fields:

1.  **Upsert ratchet (now fixed).** Before the COALESCE-upsert fix landed in
    ``data_provider/sql/stores.py::SqlPropertyStateStore.upsert``, every
    nightly sync overwrote ``properties`` with the latest snapshot — NULLs
    included. Once a soft field went NULL it stayed NULL because the next
    run's input came from that same row. Production drifted 562 -> 880
    empty-name rows in 4 days (2026-05-14 -> 2026-05-17).

2.  **FAILED-record stub (fixed 2026-05-18).** ``_make_failed_record`` in
    ``scripts/runners/jugnu.py`` emitted ``proj_name=None`` / ``address=None``
    for every failure-path verdict (timeout, exception, wedge-rescue
    budget). The COALESCE guard protects existing rows from being clobbered
    BUT a property whose very first scrape failed got its first DB row
    written with NULL identity fields. Now that ``_make_failed_record`` pulls
    from ``csv_row``, future runs can't reintroduce this.

This script repairs the legacy damage. It is strictly NULL-guarded: every
UPDATE has ``AND ({col} IS NULL OR TRIM({col}) = '')`` so re-runs touch
nothing and concurrent writers can't be clobbered.

Source order (per field, first hit wins)
----------------------------------------
1.  ``property_snapshots.payload`` — most recent run that emitted a
    non-empty value for that field. Tries the V2 column key first
    (``proj_name``) then falls back to the legacy V1 Title-Case alias
    (``Property Name``). Optionally filtered to SUCCESS-class verdicts.
2.  ``properties.extra`` JSON — the CSV-passthrough bucket populated by
    ``scripts/ingest_properties_csv.py``. Catches properties seeded from
    CSV that never had a successful scrape.
3.  ``--csv PATH`` — when supplied, the script also reads the CSV catalog
    and uses its identity fields as a last-resort source. This is the
    only path that recovers rows whose snapshots have all aged out of the
    3-day retention window.

Usage — workstation
-------------------
::

    # Dry-run (default) — counts what would change, makes no writes.
    python -m scripts.diagnostics.backfill_property_soft_fields

    # Apply the writes.
    python -m scripts.diagnostics.backfill_property_soft_fields --apply

    # Restrict to specific fields.
    python -m scripts.diagnostics.backfill_property_soft_fields \
        --fields proj_name,city,state --apply

    # Stage rollout — verify on 5 PIDs first, then run for real.
    python -m scripts.diagnostics.backfill_property_soft_fields \
        --limit 5 --apply
    python -m scripts.diagnostics.backfill_property_soft_fields \
        --cid 12586,12963 --apply

    # Include CSV catalog as a last-resort source (recovers rows whose
    # snapshots have aged out of the 3-day window).
    python -m scripts.diagnostics.backfill_property_soft_fields \
        --csv ma_poc/config/properties.csv --apply

Connection: reads ``DATABASE_URL`` from env (or ``ma_poc/.env``). For
Cloud SQL from a workstation, start cloud-sql-proxy and point
``DATABASE_URL`` at the proxy port (e.g.
``postgresql+asyncpg://postgres:<pw>@127.0.0.1:5433/jugnu``).

Usage — Cloud Run (jugnu-adhoc-{env})
-------------------------------------
Set the override on EXECUTE::

    SCRIPT_NAME=diagnostics.backfill_property_soft_fields
    SCRIPT_ARGS=                      # dry-run first
    SCRIPT_ARGS=--apply               # then commit

The adhoc job already has ``CLOUD_SQL_INSTANCE`` + ``DATABASE_URL`` wired,
so the script connects via the Cloud SQL Connector with IAM auth (same
path the daily sync uses). No extra setup needed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# parents[2] = ma_poc/, parents[3] = PropAi/. Both go on sys.path because
# the project's modules mix two import styles: `data_provider.X` (needs
# ma_poc/ on path) and `ma_poc.models.X` (needs PropAi/ on path). Same
# trick `scripts/sync/run_to_pg.py` uses. Harmless inside the jugnu-adhoc
# container — Python dedupes duplicate sys.path entries on lookup.
_MA_POC_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
for _p in (_PROJECT_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

log = logging.getLogger("backfill_property_soft_fields")

# The set is also the source of truth for which keys we'll probe out of the
# snapshot payload's JSON ``payload`` column. Mirror of
# ``data_provider.contracts.SOFT_PROPERTY_COLS`` minus ``apartment_id`` and
# ``country`` — those aren't reliably present in the historic snapshot
# payloads and ``country`` defaults to 'US' server-side anyway.
_BACKFILLABLE: tuple[str, ...] = (
    "proj_name",
    "address",
    "city",
    "state",
    "zip_code",
    "phone",
    "email_address",
    "website",
    "pmc",
    "website_design",
    "concessions",
)

# For each V2 column the legacy V1 Title-Case key that may appear in older
# snapshot payloads. Empty string means no known alias. The snapshot CTE
# COALESCEs the V2 key with the V1 alias so a snapshot written before the
# V2 cutover still surfaces a candidate value.
_V1_ALIAS: dict[str, str] = {
    "proj_name": "Property Name",
    "address": "Property Address",
    "city": "City",
    "state": "State",
    "zip_code": "ZIP Code",
    "phone": "Phone",
    "email_address": "Email",
    "website": "Website",
    "pmc": "Management Company",
    "website_design": "",
    "concessions": "",
}

# CSV-row keys to pull each backfillable field from. CatalogRow.as_csv_row()
# populates both V2 and Title-Case aliases, so any of these may carry a
# value. Tried in order; first non-empty wins.
_CSV_KEYS: dict[str, tuple[str, ...]] = {
    "proj_name": ("proj_name", "Property Name", "name", "Name"),
    "address": ("address", "Property Address", "Address"),
    "city": ("city", "City"),
    "state": ("state", "State"),
    "zip_code": ("zip_code", "ZIP Code", "zip", "Zip"),
    "phone": ("phone", "Phone"),
    "email_address": ("email_address", "Email"),
    "website": ("website", "Website", "url"),
    "pmc": ("pmc", "Management Company"),
    "website_design": ("website_design",),
    "concessions": ("concessions",),
}

# Source labels written into the audit log + counters.
_SOURCE_SNAPSHOT = "snapshot"
_SOURCE_EXTRA = "properties_extra"
_SOURCE_CSV = "csv"


def _load_dotenv_if_present() -> None:
    """Pull DATABASE_URL etc. from ma_poc/.env when run from a workstation.
    No-op inside Cloud Run — the dispatcher already wires every env var."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = _MA_POC_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _build_query(
    fields: tuple[str, ...],
    *,
    cid_filter: tuple[str, ...] | None,
    limit: int | None,
    require_success_verdict: bool,
) -> tuple[str, dict[str, Any]]:
    """Return a SELECT that yields one candidate row per (canonical_id, field).

    For every property whose canonical column is NULL/empty, pick the most
    recent snapshot whose payload has a non-empty value for that same field
    (V2 key OR V1 Title-Case alias). DISTINCT ON keeps just one candidate
    per (canonical_id, field).

    Also returns the extra JSON from properties.extra as a sibling source —
    callers can use it when the snapshot value is missing.
    """
    # Build one CTE per field so each can DISTINCT ON independently. Cheaper
    # than a single giant pivot and easier to debug.
    ctes: list[str] = []
    select_cols: list[str] = []
    joins: list[str] = []
    null_predicates: list[str] = []
    params: dict[str, Any] = {}

    verdict_filter = ""
    if require_success_verdict:
        # Match reporting.verdict._SUCCESS_VERDICTS. Kept inline because the
        # CTE is plain SQL — pulling values from the Python set would force
        # parameter binding inside a DISTINCT-ON CTE which Postgres dislikes.
        verdict_filter = (
            " AND ((ps.payload->'_meta'->>'verdict') IS NULL"
            " OR (ps.payload->'_meta'->>'verdict')"
            " IN ('SUCCESS','SUCCESS_PLAN_LEVEL','SUCCESS_PARTIAL'))"
        )

    for f in fields:
        alias = _V1_ALIAS.get(f, "")
        # Choose snapshot payload expression. COALESCE V2 -> V1 -> NULL.
        if alias:
            value_expr = (
                f"COALESCE("
                f"NULLIF(TRIM(ps.payload->>'{f}'), ''), "
                f"NULLIF(TRIM(ps.payload->>'{alias}'), '')"
                f")"
            )
        else:
            value_expr = f"NULLIF(TRIM(ps.payload->>'{f}'), '')"
        ctes.append(f"""
        cand_{f} AS (
            SELECT DISTINCT ON (ps.canonical_id)
                ps.canonical_id,
                {value_expr} AS value
            FROM property_snapshots ps
            WHERE {value_expr} IS NOT NULL{verdict_filter}
            ORDER BY ps.canonical_id, ps.run_date DESC
        )
        """)
        select_cols.append(f"cand_{f}.value AS new_{f}")
        # Also surface the extra-JSON fallback so the Python layer can
        # decide between snapshot and extra without a second query.
        if alias:
            extra_expr = (
                f"COALESCE("
                f"NULLIF(TRIM(p.extra->>'{f}'), ''), "
                f"NULLIF(TRIM(p.extra->>'{alias}'), '')"
                f")"
            )
        else:
            extra_expr = f"NULLIF(TRIM(p.extra->>'{f}'), '')"
        select_cols.append(f"{extra_expr} AS extra_{f}")
        joins.append(f"LEFT JOIN cand_{f} ON cand_{f}.canonical_id = p.canonical_id")
        null_predicates.append(f"(p.{f} IS NULL OR TRIM(p.{f}) = '')")

    where_clauses = ["(" + " OR ".join(null_predicates) + ")"]
    if cid_filter:
        # Bind as a SQLAlchemy expanding list parameter (``:cids``).
        where_clauses.append("p.canonical_id IN :cids")
        params["cids"] = list(cid_filter)

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"\n    LIMIT {int(limit)}" if limit else ""

    sql = f"""
    WITH {",".join(ctes)}
    SELECT
        p.canonical_id,
        {",\n            ".join(select_cols)}
    FROM properties p
    {chr(10).join(joins)}
    WHERE {where_sql}{limit_sql}
    """
    return sql, params


def _build_update(field: str) -> str:
    # Single-column update keyed by canonical_id. We only call this for cells
    # we already confirmed are NULL/empty, so no extra guard needed beyond
    # the trim check (defensive against concurrent writers).
    return f"""
    UPDATE properties
       SET {field} = :value
     WHERE canonical_id = :cid
       AND ({field} IS NULL OR TRIM({field}) = '')
    """


def _load_csv_lookup(csv_path: Path | None) -> dict[str, dict[str, Any]]:
    """Read the catalog CSV and return ``{canonical_id: as_csv_row dict}``.

    Empty dict when ``csv_path`` is None. Errors surface — caller decides
    whether to skip or abort.
    """
    if csv_path is None:
        return {}
    from data_provider.filesystem import CsvPropertyCatalogSource  # local import

    source = CsvPropertyCatalogSource(csv_path)
    out: dict[str, dict[str, Any]] = {}
    for prop in source.list_active():
        cid = prop.canonical_id
        if not cid:
            continue
        out[cid] = prop.as_csv_row()
    log.info("loaded %d CSV rows from %s", len(out), csv_path)
    return out


def _pick_csv_value(csv_row: dict[str, Any] | None, field: str) -> Any | None:
    """First non-empty value from the CSV row for *field*, or None."""
    if not csv_row:
        return None
    for k in _CSV_KEYS.get(field, ()):
        v = csv_row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _select_value(
    row: dict[str, Any],
    field: str,
    csv_lookup: dict[str, dict[str, Any]],
) -> tuple[Any | None, str | None]:
    """Pick the value to write for (cid, field). Returns (value, source).

    Source order: snapshot -> properties.extra -> CSV. Returns (None, None)
    when no source had a non-empty value.
    """
    snap = row.get(f"new_{field}")
    if snap not in (None, ""):
        return snap, _SOURCE_SNAPSHOT
    extra = row.get(f"extra_{field}")
    if extra not in (None, ""):
        return extra, _SOURCE_EXTRA
    cid = row["canonical_id"]
    csv_val = _pick_csv_value(csv_lookup.get(cid), field)
    if csv_val:
        return csv_val, _SOURCE_CSV
    return None, None


def _open_audit(audit_path: Path | None) -> Any:
    """Open the audit JSONL file or return None when disabled.

    Returned handle is iterable with ``.write(json.dumps(record))`` calls;
    None when no audit file requested. Callers must close it.
    """
    if audit_path is None:
        return None
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    return audit_path.open("a", encoding="utf-8")


def _parse_cid_arg(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually run the UPDATEs. Default is dry-run.")
    parser.add_argument("--fields", default=",".join(_BACKFILLABLE),
                        help=f"Comma-separated subset of {','.join(_BACKFILLABLE)}")
    parser.add_argument("--cid", default=None,
                        help="Comma-separated canonical_ids — restrict the "
                             "scan to these PIDs (e.g. '12586,12963'). "
                             "Useful for staged verification.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the candidate-property scan at N rows. "
                             "Useful for staged rollout (--limit 5 to "
                             "verify, then --limit 50, then no limit).")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Commit cadence — every N cell writes the "
                             "transaction is committed and a fresh one "
                             "opened. Default 500. Set to 0 to commit "
                             "once at the end (legacy behaviour).")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Optional path to properties.csv. When given, "
                             "the script uses the CSV as a last-resort "
                             "source for rows whose snapshots have aged "
                             "out of the 3-day retention window. Without "
                             "this flag, such rows are skipped silently.")
    parser.add_argument("--require-success-verdict", action="store_true",
                        help="When picking a snapshot value, ignore "
                             "snapshots whose _meta.verdict isn't a "
                             "success-class verdict. Safer for cases "
                             "where pre-fix FAILED snapshots may have "
                             "stale identity data.")
    parser.add_argument("--audit", type=Path, default=None,
                        help="Write a JSONL audit log of every cell that "
                             "was changed (cid, field, value, source, "
                             "ts). Append-mode; safe to point at the "
                             "same file across re-runs.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Log every per-row update.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    requested = tuple(f.strip() for f in args.fields.split(",") if f.strip())
    unknown = [f for f in requested if f not in _BACKFILLABLE]
    if unknown:
        log.error("unknown fields: %s. allowed: %s", unknown, _BACKFILLABLE)
        return 2
    if not requested:
        log.error("no fields requested")
        return 2

    cid_filter = _parse_cid_arg(args.cid)
    if cid_filter:
        log.info("cid filter: %d PID(s)", len(cid_filter))
    if args.limit:
        log.info("scan limit: %d", args.limit)

    _load_dotenv_if_present()
    csv_lookup = _load_csv_lookup(args.csv)

    # `make_engine` honours CLOUD_SQL_INSTANCE (IAM via the Cloud SQL
    # Connector on the jugnu-adhoc job) and falls back to a plain
    # create_engine on DATABASE_URL when the instance var is unset (the
    # workstation path: cloud-sql-proxy on 127.0.0.1 + password URL).
    #
    # This script is sync (no event loop), so swap +asyncpg -> +pg8000 on
    # the URL before handing it to make_engine. Inside the Connector
    # branch the URL is parsed for user+db only and the driver is
    # overridden to pg8000 anyway, so this is a no-op there.
    from sqlalchemy import bindparam, text
    from data_provider.sql.engine import make_engine, resolve_database_url
    sync_url = resolve_database_url().replace("+asyncpg", "+pg8000")
    engine = make_engine(sync_url)

    # Stage 1: identify candidates.
    sql, params = _build_query(
        requested,
        cid_filter=cid_filter,
        limit=args.limit,
        require_success_verdict=args.require_success_verdict,
    )
    log.info("scanning property_snapshots for backfill candidates (fields=%s)…", requested)
    stmt = text(sql)
    if cid_filter:
        stmt = stmt.bindparams(bindparam("cids", expanding=True))
    with engine.connect() as conn:
        rows = list(conn.execute(stmt, params).mappings())
    log.info("found %d properties with at least one recoverable soft field", len(rows))

    # Stage 2: tally + (optionally) apply.
    by_source: dict[str, int] = {
        _SOURCE_SNAPSHOT: 0, _SOURCE_EXTRA: 0, _SOURCE_CSV: 0,
    }
    per_field: dict[str, int] = {f: 0 for f in requested}
    updates: list[tuple[str, str, Any, str]] = []  # (cid, field, value, source)
    skipped_no_source = 0
    for row in rows:
        cid = row["canonical_id"]
        had_any_source = False
        for f in requested:
            value, source = _select_value(row, f, csv_lookup)
            if value is None:
                continue
            had_any_source = True
            per_field[f] += 1
            by_source[source] += 1
            updates.append((cid, f, value, source))
        if not had_any_source:
            skipped_no_source += 1

    log.info("update tally (per field):")
    for f, n in sorted(per_field.items(), key=lambda kv: -kv[1]):
        log.info("  %s: %d", f, n)
    log.info("update tally (per source):")
    for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        log.info("  %s: %d", src, n)
    log.info(
        "total cell writes planned: %d (across %d properties; %d had no source)",
        len(updates), len(rows) - skipped_no_source, skipped_no_source,
    )

    if not args.apply:
        log.info("dry-run: no DB writes performed. re-run with --apply to commit.")
        return 0

    if not updates:
        log.info("nothing to write.")
        return 0

    audit_fh = _open_audit(args.audit)
    written = 0
    pending = 0
    batch_size = max(0, int(args.batch_size))
    try:
        conn = engine.connect()
        trans = conn.begin()
        try:
            for cid, f, value, source in updates:
                res = conn.execute(
                    text(_build_update(f)),
                    {"cid": cid, "value": value},
                )
                rc = res.rowcount or 0
                if rc:
                    written += 1
                    pending += 1
                    if audit_fh is not None:
                        audit_fh.write(json.dumps({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "cid": cid,
                            "field": f,
                            "value": value,
                            "source": source,
                        }) + "\n")
                    if args.verbose:
                        log.debug(
                            "updated cid=%s field=%s source=%s value=%r",
                            cid, f, source, value,
                        )
                # Commit cadence — bounded WAL pressure + bounded
                # rollback radius if the connection drops mid-run.
                if batch_size and pending >= batch_size:
                    trans.commit()
                    trans = conn.begin()
                    pending = 0
            trans.commit()
        except Exception:
            trans.rollback()
            raise
        finally:
            conn.close()
    finally:
        if audit_fh is not None:
            audit_fh.close()

    log.info("applied %d cell updates", written)
    if args.audit:
        log.info("audit log: %s", args.audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
