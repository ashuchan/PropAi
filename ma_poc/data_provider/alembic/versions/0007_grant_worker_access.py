"""grant_worker_access: give the worker SA IAM user DML on the jugnu schema.

Revision ID: 0007_grant_worker_access
Revises: 0006_dlq_entries
Create Date: 2026-04-24

Background
----------
The Cloud Run worker authenticates to Cloud SQL via IAM as a
service-account-backed Postgres role (created by terraform's
google_sql_user). Having the role is not the same as having grants —
by default an IAM-created Postgres role can CONNECT and nothing else.
When the shard tried to write through ``sync_run_to_postgres`` the
deploy on 2026-04-24 got past auth and then died on the first
UPSERT with "permission denied for relation properties".

This migration grants the minimum the worker needs to run the sync:
  - CONNECT on the database (usually already present; explicit is safer)
  - USAGE on schema public
  - SELECT/INSERT/UPDATE/DELETE on all existing tables + sequences
  - ALTER DEFAULT PRIVILEGES so future tables created by the deployer
    are readable/writable by the worker without another migration

Reads ``JUGNU_WORKER_DB_USER`` from the environment — set by
``ma_poc/scripts/migrate.py`` based on ``--env``. If unset, raise
loudly rather than skipping silently: a no-op here is how we'd ship
another broken-auth prod deploy.

Safety
------
The migration runs as the deployer SA, which owns every table created
by prior migrations. The deployer therefore has implicit GRANT OPTION
on those objects. Cloud SQL's default cloudsqlsuperuser role on the
deployer also covers schema-level grants. Both up and down are
idempotent (GRANT/REVOKE are no-ops if the state already matches).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0007_grant_worker_access"
down_revision: str | None = "0006_dlq_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _worker_user() -> str:
    """Return the Postgres IAM user name for the worker SA.

    Format matches ``trimsuffix(SA_email, ".gserviceaccount.com")`` —
    mirrored in ``infra/terraform/modules/cloud_sql/main.tf``.
    """
    user = os.environ.get("JUGNU_WORKER_DB_USER")
    if not user:
        raise RuntimeError(
            "JUGNU_WORKER_DB_USER is unset — set it in ma_poc/scripts/migrate.py "
            "for the target env. Format: jugnu-worker-{env}@{project}.iam"
        )
    return user


def _quote_identifier(ident: str) -> str:
    """Double-quote a Postgres identifier, escaping embedded quotes.

    Worker SA IAM usernames contain ``@`` and ``.`` — they MUST be
    double-quoted in GRANT statements or Postgres treats them as
    two tokens and parses garbage.
    """
    return '"' + ident.replace('"', '""') + '"'


def upgrade() -> None:
    user = _quote_identifier(_worker_user())
    bind = op.get_bind()
    # Database name is whatever this connection is pointed at. In prod
    # that's "jugnu"; in tests it may be a throwaway sqlite file (no-op
    # below) or a test pg database.
    dialect = bind.dialect.name
    if dialect != "postgresql":
        # Grants don't apply to sqlite — silent no-op matches the
        # pattern used by other PG-specific migrations in this tree.
        return

    # Online mode: pull dbname off the live engine's URL. Offline
    # (--sql) mode hands us a MockConnection with no .engine, so fall
    # back to "jugnu" — the static name terraform creates in every env
    # (infra/terraform/modules/cloud_sql/main.tf:google_sql_database).
    url = getattr(getattr(bind, "engine", None), "url", None)
    dbname = (url.database if url and url.database else None) or "jugnu"
    dbname_quoted = _quote_identifier(dbname)

    op.execute(f"GRANT CONNECT ON DATABASE {dbname_quoted} TO {user}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {user}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}"
    )
    op.execute(
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {user}"
    )
    # Future tables created by the current (deployer) role automatically
    # inherit these grants — so the next alembic migration doesn't need
    # to re-run this whole block. Matches what a human DBA would do.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {user}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {user}"
    )


def downgrade() -> None:
    user = _quote_identifier(_worker_user())
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    url = getattr(getattr(bind, "engine", None), "url", None)
    dbname = (url.database if url and url.database else None) or "jugnu"
    dbname_quoted = _quote_identifier(dbname)

    # Revoke in reverse order. ALTER DEFAULT PRIVILEGES REVOKE doesn't
    # touch already-granted privileges on existing tables, so we do
    # both the object-level REVOKE and the default-privileges REVOKE.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {user}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {user}"
    )
    op.execute(
        f"REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public FROM {user}"
    )
    op.execute(
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {user}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {user}")
    op.execute(f"REVOKE CONNECT ON DATABASE {dbname_quoted} FROM {user}")
