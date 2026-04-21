# Database Migrations Runbook

## Overview

Migrations are managed with [Alembic](https://alembic.sqlalchemy.org/) and live in `infra/sql/versions/`. They are applied by the deploy workflow — workers do not run migrations.

**Key rules:**
- Every migration must have a working `downgrade()` — no exceptions.
- Never modify an applied migration. Changes go in a new file.
- autogenerate is deliberately disabled. All migrations are hand-written.

---

## Adding a new migration

1. Create a file in `infra/sql/versions/`:

```bash
# File naming: {revision_id}_{slug}.py
# Use the next sequential revision ID
cp infra/sql/script.py.mako infra/sql/versions/001_add_proxy_tier.py
# Edit the file; set revision, down_revision, upgrade(), downgrade()
```

2. Test locally with a Docker Postgres:

```bash
docker run -d --name pg-test -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:15-alpine
export DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres
alembic -c infra/sql/alembic.ini upgrade head
alembic -c infra/sql/alembic.ini downgrade -1
alembic -c infra/sql/alembic.ini upgrade head
docker stop pg-test && docker rm pg-test
```

3. Run the round-trip test:

```bash
cd ma_poc
pytest tests/migrations/ -v
```

4. Open a PR. Include in the description:
   - What the change is
   - Why it's needed
   - The rollback plan

---

## Applying migrations to staging

```bash
python ma_poc/scripts/migrate.py --env staging up
python ma_poc/scripts/migrate.py --env staging status
```

The script automatically starts the Cloud SQL instance if it's stopped (stop-when-idle pattern).

## Checking current revision

```bash
python ma_poc/scripts/migrate.py --env staging status
python ma_poc/scripts/migrate.py --env prod status
```

## Rolling back one step

```bash
python ma_poc/scripts/migrate.py --env staging down --steps 1
```

## Rolling back to a specific revision

```bash
python ma_poc/scripts/migrate.py --env staging up-to 000_initial_schema
```

---

## Migration rules (non-negotiable)

| Rule | Why |
|------|-----|
| Additive before destructive | Never drop a column in the same release that stops writing to it. Three-release drop cycle: add → stop writing → drop. |
| Every migration has downgrade() | CI blocks merge if downgrade fails. |
| No data migrations in DDL files | DDL in N, data move in N+1, cleanup in N+2. |
| Never modify an applied migration | Puts environments into unreconcilable state. |
| Meaningful names | `add_proxy_tier_to_properties.py`, not `update_schema.py`. |
| Concurrent index creation on large tables | `op.create_index(..., postgresql_concurrently=True)` with `autocommit_block()`. |

---

## Emergency rollback (database)

For additive migrations, leave them in place — they don't break old code.

For destructive or incompatible migrations:

```bash
python ma_poc/scripts/migrate.py --env prod down --steps 1
```

If the rollback itself fails, restore from the automated Cloud SQL backup (daily at 03:30 UTC):

```bash
gcloud sql backups list --instance=jugnu-db-prod
gcloud sql backups restore <backup-id> --restore-instance=jugnu-db-prod
```

Restore takes ~5 minutes and loses at most ~24h of data.
