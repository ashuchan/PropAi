"""dlq_entries: durable cross-run DLQ state.

Revision ID: 0006_dlq_entries
Revises: 0005_drop_lsdate
Create Date: 2026-04-24

Backs the dead-letter queue that lives in ``data/state/dlq.jsonl`` with a
Postgres table so it survives Cloud Run container restarts. The local
JSONL file is an append-only transaction log; this table mirrors the
post-compaction live state (one row per currently-parked property).

Sync flow — see ``scripts/sync/run_to_pg.py``:

  scrape run ──▶ writes state/dlq.jsonl (ephemeral /tmp) ──▶ shard_entry
                                                           post-runner
                                                           sync UPSERTs
                                                           live entries
                                                           into dlq_entries

  retry run ──▶ retry_entry pre-loads DB rows ──▶ state/dlq.jsonl ──▶
                                                 retry_runner processes
                                                 ──▶ post-runner sync
                                                 UPSERTs again

Drop is unconditional — no data to preserve yet since this is net-new.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_dlq_entries"
down_revision: str | None = "0005_drop_lsdate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dlq_entries",
        # property_id is the natural PK — DLQ is *global* (not per-run) so one
        # row per property across all time. Re-parking a property overwrites
        # the row rather than appending.
        sa.Column("property_id", sa.String(256), primary_key=True),
        # ISO timestamps kept as strings — matches the JSONL file format so
        # the round-trip (DB → JSONL → Dlq._load()) doesn't need parsing at
        # the boundary. Callers that need datetime parse at read time.
        sa.Column("parked_at", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("last_error_signature", sa.String(512), nullable=False),
        sa.Column("retry_at", sa.String(64), nullable=False),
        # Sync bookkeeping: when the row was last touched by sync_run_to_pg.
        # Useful for debugging "why is this stale?" and a future TTL sweep.
        sa.Column(
            "last_synced_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # retry_at index — the retry_runner's hot path is
    # "SELECT * FROM dlq_entries WHERE retry_at <= now()". Without this, a
    # few hundred rows is already a seq scan per retry task.
    op.create_index("ix_dlq_entries_retry_at", "dlq_entries", ["retry_at"])


def downgrade() -> None:
    op.drop_index("ix_dlq_entries_retry_at", table_name="dlq_entries")
    op.drop_table("dlq_entries")
