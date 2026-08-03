"""Persist audited unit identity, building, area, and availability lineage.

Revision ID: 0014_units_output_identity
Revises: 0013_units_amenities
Create Date: 2026-08-02

The affected-386 canary proved that output JSON contained useful source and
building identity which the current-state ``units`` table silently discarded.
This additive migration makes those fields queryable while retaining the
existing ``(canonical_id, unit_id)`` primary key. ``unit_history_key`` is
indexed but intentionally not unique or primary until a two-day continuity
replay validates the proposed day-to-day merge identity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_units_output_identity"
down_revision: str | None = "0013_units_amenities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("units", sa.Column("source_unit_id", sa.String(512), nullable=True))
    op.add_column("units", sa.Column("canonical_unit_id", sa.String(512), nullable=True))
    op.add_column("units", sa.Column("unit_name", sa.String(1024), nullable=True))
    op.add_column("units", sa.Column("floor", sa.String(128), nullable=True))
    op.add_column("units", sa.Column("building", sa.String(1024), nullable=True))
    op.add_column("units", sa.Column("building_id", sa.String(512), nullable=True))
    op.add_column("units", sa.Column("building_id_source", sa.String(256), nullable=True))
    op.add_column(
        "units",
        sa.Column("floor_plan_name_provenance", sa.String(128), nullable=True),
    )
    op.add_column("units", sa.Column("area_sqft", sa.Integer(), nullable=True))
    op.add_column("units", sa.Column("area_is_published", sa.Boolean(), nullable=True))
    op.add_column("units", sa.Column("area_low", sa.Integer(), nullable=True))
    op.add_column("units", sa.Column("area_high", sa.Integer(), nullable=True))
    op.add_column("units", sa.Column("area_range", sa.String(64), nullable=True))
    op.add_column("units", sa.Column("area_range_raw", sa.Text(), nullable=True))
    op.add_column("units", sa.Column("area_value_type", sa.String(32), nullable=True))
    op.add_column("units", sa.Column("area_provenance", sa.String(128), nullable=True))
    op.add_column("units", sa.Column("area_source_url", sa.Text(), nullable=True))
    op.add_column("units", sa.Column("rent_range", sa.String(128), nullable=True))
    op.add_column("units", sa.Column("rent_range_raw", sa.Text(), nullable=True))
    op.add_column("units", sa.Column("rent_is_range", sa.Boolean(), nullable=True))
    op.add_column("units", sa.Column("rent_provenance", sa.String(64), nullable=True))
    op.add_column("units", sa.Column("available_date_raw", sa.Text(), nullable=True))
    op.add_column(
        "units",
        sa.Column("availability_date_provenance", sa.String(64), nullable=True),
    )
    op.add_column("units", sa.Column("availability_status", sa.String(64), nullable=True))
    op.add_column("units", sa.Column("extraction_tier", sa.String(128), nullable=True))
    op.add_column("units", sa.Column("source_ids", sa.JSON(), nullable=True))
    op.add_column(
        "units",
        sa.Column("source_response_sha256", sa.String(64), nullable=True),
    )
    op.add_column("units", sa.Column("source_response_url", sa.Text(), nullable=True))
    op.add_column("units", sa.Column("source_record_locator", sa.Text(), nullable=True))
    op.add_column(
        "units",
        sa.Column("source_parent_record_locator", sa.Text(), nullable=True),
    )
    op.add_column("units", sa.Column("source_asset_url", sa.Text(), nullable=True))
    op.add_column(
        "units",
        sa.Column("source_asset_sha256", sa.String(64), nullable=True),
    )
    op.add_column("units", sa.Column("identity_quality", sa.String(64), nullable=True))
    op.add_column("units", sa.Column("unit_id_aliases", sa.JSON(), nullable=True))
    op.add_column("units", sa.Column("unit_id_alias_sources", sa.JSON(), nullable=True))
    op.add_column("units", sa.Column("unit_history_key", sa.String(72), nullable=True))
    op.add_column("units", sa.Column("unit_history_key_basis", sa.Text(), nullable=True))
    op.add_column(
        "units",
        sa.Column("unit_history_key_quality", sa.String(64), nullable=True),
    )
    op.add_column(
        "units",
        sa.Column("unit_history_key_version", sa.String(16), nullable=True),
    )
    op.create_index(
        "ix_units_unit_history_key",
        "units",
        ["unit_history_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_units_unit_history_key", table_name="units")
    for column in (
        "unit_history_key_version",
        "unit_history_key_quality",
        "unit_history_key_basis",
        "unit_history_key",
        "unit_id_alias_sources",
        "unit_id_aliases",
        "identity_quality",
        "source_asset_sha256",
        "source_asset_url",
        "source_parent_record_locator",
        "source_record_locator",
        "source_response_url",
        "source_response_sha256",
        "source_ids",
        "extraction_tier",
        "availability_status",
        "availability_date_provenance",
        "available_date_raw",
        "area_is_published",
        "area_sqft",
        "rent_is_range",
        "rent_provenance",
        "rent_range_raw",
        "rent_range",
        "area_source_url",
        "area_provenance",
        "area_value_type",
        "area_range_raw",
        "area_range",
        "area_high",
        "area_low",
        "floor_plan_name_provenance",
        "building_id_source",
        "building_id",
        "building",
        "floor",
        "unit_name",
        "canonical_unit_id",
        "source_unit_id",
    ):
        op.drop_column("units", column)
