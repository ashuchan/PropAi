#!/usr/bin/env python3
"""One-shot backfill for `properties` rows whose soft fields were NULL'd by
the pre-fix upsert ratchet (proj_name, address, city, state, zip_code,
pmc, website, phone, etc.).

Background
----------
Before the COALESCE-upsert fix landed in
``data_provider/sql/stores.py::SqlPropertyStateStore.upsert``, every nightly
sync overwrote the canonical ``properties`` row with the new snapshot
including any NULLs the scraper emitted. Once a soft field went NULL it
stayed NULL because ``SqlPropertyCatalogSource`` fed the next run's input
from that same row — a permanent ratchet. Production drifted 562 → 880
empty-name rows in 4 days (2026-05-14 → 2026-05-17). The frontend reads
``proj_name`` directly, so those rows rendered as blank cards.

What this script does
---------------------
Per canonical_id, find the most recent ``property_snapshots`` row whose
``payload`` still has a non-empty value for each soft field, and write
that value into ``properties`` IF the current cell is NULL/empty. Skips
rows that are already populated — won't overwrite anything.

Caveat: ``property_snapshots`` is on a 3-day retention window
(``sync_run_to_pg.py::_apply_retention``). Names lost more than 3 days
ago can't be recovered from this table — re-run the CSV ingest
(``scripts/ingest_properties_csv.py``) if needed.

Usage — workstation
-------------------
    # Dry-run (default) — counts what would change, makes no writes.
    python -m scripts.diagnostics.backfill_property_soft_fields

    # Apply the writes.
    python -m scripts.diagnostics.backfill_property_soft_fields --apply

    # Restrict to specific fields.
    python -m scripts.diagnostics.backfill_property_soft_fields --fields proj_name,city,state --apply

Connection: reads ``DATABASE_URL`` from env (or ``ma_poc/.env``). For
Cloud SQL from a workstation, start cloud-sql-proxy and point DATABASE_URL
at the proxy port (e.g.
``postgresql+asyncpg://postgres:<pw>@127.0.0.1:5433/jugnu``).

Usage — Cloud Run (jugnu-adhoc-{env})
-------------------------------------
Set the override on EXECUTE:
    SCRIPT_NAME=diagnostics.backfill_property_soft_fields
    SCRIPT_ARGS=                      # dry-run first
    SCRIPT_ARGS=--apply               # then commit

The adhoc job already has ``CLOUD_SQL_INSTANCE`` + ``DATABASE_URL`` wired,
so the script connects via the Cloud SQL Connector with IAM auth (same
path the daily sync uses). No extra setup needed.
"""

from __future__ import annotations

import argparse
import logging
import sys
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
_BACKFILLABLE = (
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


def _build_query(fields: tuple[str, ...]) -> str:
    """Return a SELECT that yields one candidate row per (canonical_id, field).

    For every property whose canonical column is NULL/empty, pick the most
    recent snapshot whose payload has a non-empty value for that same field.
    DISTINCT ON keeps just one candidate per (canonical_id, field).
    """
    # Build one CTE per field so each can DISTINCT ON independently. Cheaper
    # than a single giant pivot and easier to debug.
    ctes = []
    for f in fields:
        ctes.append(f"""
        cand_{f} AS (
            SELECT DISTINCT ON (ps.canonical_id)
                ps.canonical_id,
                NULLIF(TRIM(ps.payload->>'{f}'), '') AS value
            FROM property_snapshots ps
            WHERE NULLIF(TRIM(ps.payload->>'{f}'), '') IS NOT NULL
            ORDER BY ps.canonical_id, ps.run_date DESC
        )
        """)
    select_cols = ",\n            ".join(
        f"cand_{f}.value AS new_{f}" for f in fields
    )
    joins = "\n".join(
        f"LEFT JOIN cand_{f} ON cand_{f}.canonical_id = p.canonical_id"
        for f in fields
    )
    null_predicates = " OR ".join(
        f"((p.{f} IS NULL OR TRIM(p.{f}) = '') AND cand_{f}.value IS NOT NULL)"
        for f in fields
    )
    return f"""
    WITH {",".join(ctes)}
    SELECT
        p.canonical_id,
        {select_cols}
    FROM properties p
    {joins}
    WHERE {null_predicates}
    """


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually run the UPDATEs. Default is dry-run.")
    parser.add_argument("--fields", default=",".join(_BACKFILLABLE),
                        help=f"Comma-separated subset of {','.join(_BACKFILLABLE)}")
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

    _load_dotenv_if_present()
    # `make_engine` honours CLOUD_SQL_INSTANCE (IAM via the Cloud SQL
    # Connector on the jugnu-adhoc job) and falls back to a plain
    # create_engine on DATABASE_URL when the instance var is unset (the
    # workstation path: cloud-sql-proxy on 127.0.0.1 + password URL).
    #
    # This script is sync (no event loop), so swap +asyncpg → +pg8000 on
    # the URL before handing it to make_engine. Inside the Connector
    # branch the URL is parsed for user+db only and the driver is
    # overridden to pg8000 anyway, so this is a no-op there.
    from sqlalchemy import text
    from data_provider.sql.engine import make_engine, resolve_database_url
    sync_url = resolve_database_url().replace("+asyncpg", "+pg8000")
    engine = make_engine(sync_url)

    # Stage 1: identify candidates.
    sql = _build_query(requested)
    log.info("scanning property_snapshots for backfill candidates (fields=%s)…", requested)
    with engine.connect() as conn:
        rows = list(conn.execute(text(sql)).mappings())
    log.info("found %d properties with at least one recoverable soft field", len(rows))

    # Stage 2: tally + (optionally) apply.
    counts: dict[str, int] = {f: 0 for f in requested}
    updates: list[tuple[str, str, Any]] = []  # (cid, field, value)
    for row in rows:
        cid = row["canonical_id"]
        for f in requested:
            new = row[f"new_{f}"]
            if new is None:
                continue
            counts[f] += 1
            updates.append((cid, f, new))

    log.info("update tally (per field):")
    for f, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        log.info("  %s: %d", f, n)
    log.info("total cell writes planned: %d (across %d properties)", len(updates), len(rows))

    if not args.apply:
        log.info("dry-run: no DB writes performed. re-run with --apply to commit.")
        return 0

    if not updates:
        log.info("nothing to write.")
        return 0

    written = 0
    with engine.begin() as conn:
        for cid, f, value in updates:
            res = conn.execute(text(_build_update(f)), {"cid": cid, "value": value})
            if res.rowcount:
                written += 1
                if args.verbose:
                    log.debug("updated cid=%s field=%s value=%r", cid, f, value)
    log.info("applied %d cell updates", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
