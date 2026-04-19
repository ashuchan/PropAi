# CLAUDE_MIGRATIONS.md

**Goal:** Introduce Alembic-based schema management for the Cloud SQL Postgres database. Every schema change ships as a reviewable migration with a tested rollback path. Deploys apply migrations before updating the Cloud Run job image, never after.

**Read before starting:**
- `Jugnu_Deployment_Architecture_GCP.docx` — especially the persistence-layer section (Cloud SQL, stop-when-idle, JSONB profiles)
- `CLAUDE_TERRAFORM.md` §4.4 — the Cloud SQL module this migration system targets
- The existing `ma_poc/models/` directory for Pydantic models (informs what columns need to exist)
- The existing data written by `jugnu_runner.py` — specifically the 46-key output schema documented in `README.md` and `Jugnu_Robust_Crawler_Architecture.docx`

**Prerequisite:** `CLAUDE_TERRAFORM.md` must be applied to staging. Migrations need a real database to run against, and the initial migration's job is to bring the staging Postgres instance to parity with whatever the app expects.

---

## 1. Scope

What this handoff produces:

- `infra/sql/` directory with the migration runner and Alembic config
- `infra/sql/alembic.ini` — Alembic configuration
- `infra/sql/env.py` — Alembic environment that reads DB connection from env vars
- `infra/sql/versions/` — directory containing the initial migration
- `infra/sql/migrations/000_initial_schema.py` — creates all tables the app currently writes to
- `scripts/migrate.py` — wrapper that runs migrations through the Cloud SQL Auth Proxy
- `tests/migrations/test_round_trip.py` — every migration up-then-down on a throwaway Postgres container
- `docs/MIGRATIONS.md` — the operator runbook

What this handoff does **not** produce:
- Any ORM layer (the app stays on raw SQL via `asyncpg` or equivalent — migrations don't imply an ORM)
- Automated migration on container start (explicit step in the deploy workflow, not implicit)
- Backfill jobs (those are separate migrations, one per backfill)

---

## 2. Why Alembic

The three reasonable options are Alembic, sqitch, and hand-rolled SQL files with a version tracking table. The choice is Alembic because:

- Python-native — matches your existing stack, no second language to learn or install in CI
- Autogenerate from SQLAlchemy models is off by default (and should stay off — autogenerate misses constraint changes, ignores data types subtly, and produces unreviewable diffs); but the rest of Alembic's infrastructure is the best in the Python ecosystem
- Rollback (`alembic downgrade`) is first-class, not bolted on
- Transactional DDL by default — if a migration fails midway, Postgres rolls back cleanly

**We are explicitly not using Alembic's autogenerate.** Every migration is hand-written. The cost of autogenerate bugs in production is higher than the cost of typing out `op.add_column()` by hand.

---

## 3. Directory layout

```
infra/
├── sql/
│   ├── alembic.ini                    # Alembic config — points at env.py
│   ├── env.py                          # Reads DATABASE_URL from env; no ORM metadata
│   ├── script.py.mako                  # Template for new migration files
│   └── versions/
│       └── 000_initial_schema.py       # First migration; see §5
└── terraform/                          # from CLAUDE_TERRAFORM.md — unchanged
```

Migrations live under `infra/` rather than the app package. Rationale: they're infrastructure concerns with infrastructure review patterns (schema changes need more eyes than code changes), and they're applied by the deploy workflow, not the app runtime.

---

## 4. Alembic configuration

### `infra/sql/alembic.ini`

Minimal — the only non-default settings that matter:

```ini
[alembic]
script_location = infra/sql
version_locations = infra/sql/versions
sqlalchemy.url = driver://user:pass@host/db   ; placeholder; overridden in env.py
file_template = %%(rev)s_%%(slug)s             ; filenames like "abc123_add_units_table.py"
timezone = UTC
truncate_slug_length = 40

[loggers]
keys = root,sqlalchemy,alembic
[handlers]
keys = console
[formatters]
keys = generic
[logger_root]
level = WARN
handlers = console
[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine
[logger_alembic]
level = INFO
handlers =
qualname = alembic
[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic
[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

### `infra/sql/env.py`

```python
"""Alembic environment — reads connection from DATABASE_URL env var."""
import os
from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata — we're not using autogenerate
target_metadata = None

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is required. For local use: "
        "DATABASE_URL=postgresql://USER@localhost:5432/jugnu (via cloud-sql-proxy)"
    )
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout for review — used in CI's 'dry-run' gate."""
    context.configure(
        url=database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            # Transactional DDL: every migration runs in a transaction.
            # Postgres supports this; MySQL would not.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

**Note on `target_metadata = None`:** this disables autogenerate. `alembic revision --autogenerate` will fail loudly, which is what we want — any engineer tempted to use it will get an error instead of a broken migration.

---

## 5. The initial migration

The initial migration captures whatever schema `jugnu_runner.py` currently writes to. Claude Code must inspect the code to determine this — do not guess. Read:

- Every `INSERT` / `UPSERT` call in `ma_poc/` and `scripts/`
- The Pydantic models in `ma_poc/models/` (shape of what gets persisted)
- The 46-key output schema in the architecture docs

**Expected tables** (Claude Code to confirm by reading the code — do not add tables without confirmation):

1. **`properties`** — one row per `canonical_id`, stores the profile as JSONB. Per arch doc: "Profiles stored as JSONB on the properties table."
2. **`units`** — one row per (`canonical_id`, `unit_id`, `run_date`); the 46-key record.
3. **`run_ledger`** — one row per run_date, tracks completion status. Used by `trigger_retry.py` to locate "the most recent run".
4. **`events`** — append-only event log (L5 observability layer from the refactor plan).

**Skeleton of the initial migration:**

```python
"""000_initial_schema

Revision ID: 000_initial_schema
Revises:
Create Date: 2026-04-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "000_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # properties — one row per canonical_id
    op.create_table(
        "properties",
        sa.Column("canonical_id", sa.Text, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("pms", sa.Text),                              # detected PMS platform
        sa.Column("profile", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("proxy_tier", sa.Text, server_default="DATACENTER"),  # DIRECT/DATACENTER/RESIDENTIAL
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_properties_pms", "properties", ["pms"])

    # units — one row per (canonical_id, unit_id, run_date)
    op.create_table(
        "units",
        sa.Column("canonical_id", sa.Text, nullable=False),
        sa.Column("unit_id", sa.Text, nullable=False),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("record", postgresql.JSONB, nullable=False),  # the 46-key payload
        sa.Column("written_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("canonical_id", "unit_id", "run_date"),
        sa.ForeignKeyConstraint(["canonical_id"], ["properties.canonical_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_units_run_date", "units", ["run_date"])
    op.create_index("ix_units_canonical_run", "units", ["canonical_id", "run_date"])

    # run_ledger — one row per run_date per environment
    op.create_table(
        "run_ledger",
        sa.Column("run_date", sa.Date, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),            # STARTED/COMPLETED/PARTIAL/FAILED
        sa.Column("shard_count", sa.Integer, nullable=False),
        sa.Column("shards_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("properties_total", sa.Integer),
        sa.Column("properties_succeeded", sa.Integer),
        sa.Column("properties_failed", sa.Integer),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
    )

    # events — append-only log from L5 observability
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_date", sa.Date, nullable=False),
        sa.Column("shard_idx", sa.Integer),
        sa.Column("canonical_id", sa.Text),
        sa.Column("severity", sa.Text, nullable=False),          # DEBUG/INFO/WARN/ERROR
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_events_run_date", "events", ["run_date"])
    op.create_index("ix_events_severity", "events", ["severity"], postgresql_where=sa.text("severity IN ('WARN','ERROR')"))


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("run_ledger")
    op.drop_table("units")
    op.drop_table("properties")
```

Claude Code must verify this schema against the actual code paths. If the code writes to a column not listed here, add it. If a column listed here isn't written, remove it. **The initial migration must match reality, not aspirations.**

---

## 6. The migration runner script

`scripts/migrate.py` — what CI and operators call, not `alembic` directly:

```python
"""
scripts/migrate.py — run Alembic migrations through the Cloud SQL Auth Proxy.

Usage:
  python scripts/migrate.py --env staging up
  python scripts/migrate.py --env staging status
  python scripts/migrate.py --env prod up --to 00a1b2c3
  python scripts/migrate.py --env staging down --steps 1

The script:
  1. Verifies gcloud auth and project
  2. Ensures Cloud SQL instance is running (handles stop-when-idle)
  3. Starts the Cloud SQL Auth Proxy in a subprocess on localhost:5432
  4. Sets DATABASE_URL to use the proxy + IAM auth
  5. Invokes alembic
  6. Always stops the proxy cleanly in a finally block
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from contextlib import contextmanager

# Mapping from env to (project_id, sql_instance_name) — source of truth is tfvars,
# but we can't read tfstate from here. Keep this in sync manually; CI verifies.
ENV_CONFIG = {
    "staging": {"project": "jugnu-staging-<unique>", "instance": "jugnu-db-staging"},
    "prod":    {"project": "jugnu-prod-<unique>",    "instance": "jugnu-db-prod"},
}


def ensure_sql_running(project: str, instance: str) -> None:
    """Handles the stop-when-idle trap. Returns only when SQL is READY."""
    result = subprocess.run(
        ["gcloud", "sql", "instances", "describe", instance,
         f"--project={project}", "--format=value(state)"],
        capture_output=True, text=True, check=True,
    )
    state = result.stdout.strip()
    if state == "RUNNABLE":
        return
    print(f"SQL instance state: {state}; starting...", file=sys.stderr)
    subprocess.run(
        ["gcloud", "sql", "instances", "patch", instance,
         f"--project={project}", "--activation-policy=ALWAYS"],
        check=True,
    )
    # Poll until ready; cap at 3 minutes
    for _ in range(36):
        result = subprocess.run(
            ["gcloud", "sql", "instances", "describe", instance,
             f"--project={project}", "--format=value(state)"],
            capture_output=True, text=True, check=True,
        )
        if result.stdout.strip() == "RUNNABLE":
            return
        time.sleep(5)
    sys.exit(f"Timeout waiting for {instance} to become RUNNABLE")


@contextmanager
def cloud_sql_proxy(project: str, instance: str, region: str = "us-central1"):
    """Spawn cloud-sql-proxy; yield when ready; terminate on exit."""
    conn_name = f"{project}:{region}:{instance}"
    proc = subprocess.Popen(
        ["cloud-sql-proxy", "--port=5432", conn_name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Wait for "ready for new connections" line on stderr
    try:
        for _ in range(20):
            line = proc.stderr.readline().decode()
            if "ready for new connections" in line.lower():
                break
            time.sleep(0.5)
        else:
            proc.terminate()
            sys.exit("cloud-sql-proxy failed to become ready")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", choices=["staging", "prod"], required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up")
    sub.add_parser("status")
    down = sub.add_parser("down")
    down.add_argument("--steps", type=int, default=1)
    upto = sub.add_parser("up-to")
    upto.add_argument("revision")
    args = p.parse_args()

    cfg = ENV_CONFIG[args.env]
    ensure_sql_running(cfg["project"], cfg["instance"])

    # Reconstruct IAM-authenticated URL; username is the IAM identity
    result = subprocess.run(
        ["gcloud", "config", "get-value", "account"],
        capture_output=True, text=True, check=True,
    )
    iam_user = result.stdout.strip()

    with cloud_sql_proxy(cfg["project"], cfg["instance"]):
        env = os.environ.copy()
        env["DATABASE_URL"] = f"postgresql://{iam_user}@localhost:5432/jugnu?sslmode=disable"
        cmd = ["alembic", "-c", "infra/sql/alembic.ini"]
        if args.cmd == "up":
            cmd += ["upgrade", "head"]
        elif args.cmd == "up-to":
            cmd += ["upgrade", args.revision]
        elif args.cmd == "down":
            cmd += ["downgrade", f"-{args.steps}"]
        elif args.cmd == "status":
            cmd += ["current", "-v"]
        sys.exit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
```

---

## 7. Rules for writing migrations

These are non-negotiable — violations caught in code review:

**A. Additive before destructive, across releases.** Never drop a column in the same release that stops writing to it. Sequence: (1) release writes to new, both; (2) backfill runs; (3) later release stops writing to old; (4) even later release drops old. A three-release drop cycle is not paranoia; it's how you roll back cleanly.

**B. Every migration has a working `downgrade()`.** No exceptions. Tested automatically in CI (see §8).

**C. No data migrations in the same file as DDL.** If a migration needs to move data, split it: DDL in migration N, data move in migration N+1, cleanup in migration N+2. Mixing makes rollback much harder.

**D. Never modify an applied migration.** Once merged to main, the file is frozen. Changes go in a new migration, period. Editing an applied migration puts different environments into unreconcilable states.

**E. Name migrations for what they do, not what they call.** `add_proxy_tier_to_properties.py`, not `update_schema.py` or `v2_migration.py`.

**F. Index creation on large tables uses `CONCURRENTLY`.** `op.create_index(..., postgresql_concurrently=True)` — doesn't lock the table. Required to run outside a transaction, which means those migrations have a special header:

```python
def upgrade() -> None:
    # Must run outside a transaction
    with op.get_context().autocommit_block():
        op.create_index(..., postgresql_concurrently=True)
```

Claude Code should flag any migration touching a table expected to exceed 100K rows and require the concurrent pattern.

**G. JSONB schema changes are code changes, not migrations.** The `profile` and `record` JSONB columns have no DB-enforced schema. Changes to their shape are versioned in app code (e.g., `schema_version` field inside the JSONB), not in DDL.

---

## 8. Round-trip testing

`tests/migrations/test_round_trip.py` — spins up an ephemeral Postgres via `testcontainers` and walks every migration up, then every migration down, then up again. Failures here block merge.

```python
"""Round-trip test: every migration must upgrade and downgrade cleanly."""
from pathlib import Path
import subprocess

import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="module")
def pg():
    with PostgresContainer("postgres:15-alpine") as container:
        yield container


def run_alembic(pg, *args):
    env = {"DATABASE_URL": pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")}
    return subprocess.run(
        ["alembic", "-c", "infra/sql/alembic.ini", *args],
        env=env, capture_output=True, text=True, check=False,
    )


def test_upgrade_head(pg):
    result = run_alembic(pg, "upgrade", "head")
    assert result.returncode == 0, result.stderr


def test_full_round_trip(pg):
    # Up to head
    assert run_alembic(pg, "upgrade", "head").returncode == 0
    # All the way down to base
    assert run_alembic(pg, "downgrade", "base").returncode == 0
    # Back up
    assert run_alembic(pg, "upgrade", "head").returncode == 0


def test_per_revision_round_trip(pg):
    """For each revision, upgrade to it then downgrade one step."""
    versions_dir = Path("infra/sql/versions")
    revisions = sorted(f.stem.split("_")[0] for f in versions_dir.glob("*.py"))
    # Start clean
    run_alembic(pg, "downgrade", "base")
    for rev in revisions:
        assert run_alembic(pg, "upgrade", rev).returncode == 0
        assert run_alembic(pg, "downgrade", "-1").returncode == 0
        assert run_alembic(pg, "upgrade", rev).returncode == 0
```

---

## 9. Gates

| Gate | Check | Command |
|---|---|---|
| MIG-1 | Alembic config valid | `alembic -c infra/sql/alembic.ini check` exits 0 |
| MIG-2 | Initial migration files match expected shape | Directory layout per §3; filenames match `file_template` pattern |
| MIG-3 | Round-trip tests pass | `pytest tests/migrations/ -v` exits 0 |
| MIG-4 | Migrations are deterministic | Run `test_full_round_trip` 3× in a row; all pass |
| MIG-5 | Offline SQL generation clean | `alembic -c infra/sql/alembic.ini upgrade head --sql` produces valid SQL (redirect to file; review manually on first pass) |
| MIG-6 | Apply to staging succeeds | `python scripts/migrate.py --env staging up` exits 0 |
| MIG-7 | Status reports head after apply | `python scripts/migrate.py --env staging status` shows a revision (not "None") |
| MIG-8 | Apply is idempotent | Rerun `python scripts/migrate.py --env staging up`; exits 0 with "no new migrations" |
| MIG-9 | Initial schema matches app expectations | Run `jugnu_runner.py --limit 3` against staging; writes succeed; no "column does not exist" errors |
| MIG-10 | Autogenerate is disabled | `alembic -c infra/sql/alembic.ini revision --autogenerate -m test` fails with a clear error |
| MIG-11 | `migrate.py` handles stopped SQL | Stop staging SQL; run `python scripts/migrate.py --env staging up`; script starts it and migration applies |
| MIG-12 | Runbook exists | `docs/MIGRATIONS.md` covers: adding a migration, testing locally, applying to staging, emergency rollback |

---

## 10. Non-negotiables

- **No autogenerate.** `target_metadata = None` in `env.py`, never changed.
- **No `op.execute("...")`  with arbitrary DDL.** Use Alembic operations; they're portable and rollback-able. Raw SQL is a last resort, with a comment explaining why.
- **No adding columns without defaults on large tables.** Postgres 11+ handles this efficiently for most types, but `NOT NULL` without a default on a multi-million row table still locks. Always provide `server_default` or make new columns nullable.
- **No renaming columns.** Rename = add new, copy, drop old, across three releases. Direct rename breaks running app instances that haven't yet pulled the new image.
- **No migration without a linked review.** Schema changes need more eyes than code changes. PR description must include: what the change is, why, and the rollback plan.
- **No data in migrations larger than a few hundred rows.** Bulk data changes go in a separate backfill script, run manually, not as part of the deploy pipeline.

---

## 11. Open questions

- **Run migrations in deploy workflow, or as a separate manual step?** Recommendation: automated in deploy workflow (matching the industry norm), but with a `--dry-run` mode on every migration PR that the CI posts as a comment showing the generated SQL. Review-in-CI > manual-in-terminal for this category of change.
- **Should the migration runner live in the app image, or be invoked from CI?** Recommendation: **CI only**. Baking Alembic into the app image means every container can migrate the DB, which is the wrong permission model. Migrations run from GitHub Actions with the deployer SA; workers don't have `ALTER TABLE` rights.
- **Postgres version — stay on 15?** Cloud SQL supports up through 16. Recommendation: 15 for the POC; revisit at production migration. Postgres major version upgrades on Cloud SQL are non-trivial.

---

## 12. When this handoff is complete

Claude Code has:
1. Created every file in §1
2. All gates in §9 pass
3. The initial migration applies cleanly against staging and allows `jugnu_runner.py --limit 3` to write data successfully
4. `docs/MIGRATIONS.md` walks a non-author through adding a new migration, testing it, and applying it

Only then is the migration system ready for `CLAUDE_CI.md` and `CLAUDE_DEPLOY.md` to integrate with.
