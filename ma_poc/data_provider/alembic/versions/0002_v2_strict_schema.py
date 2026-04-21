"""v2-strict schema: rename properties + units columns to V2 names.

Revision ID: 0002_v2_strict
Revises: 0001_initial
Create Date: 2026-04-21

The current-state tables (`properties`, `units`) were originally shaped
after the legacy `property_index.json` / `unit_index.json` files (v1 names:
`name`, `zip`, `bedrooms`, `bathrooms`, `sqft`, `market_rent_low/high`).

This migration drops both tables and re-creates them with V2-compliant
columns (see `scripts/schema_v2.py`):

  properties: apartment_id, proj_name, address, city, state, zip_code,
              country, phone, email_address, website, pmc, website_design,
              concessions  (+ state-tracking + extra JSON)
  units:      beds, baths, floor_plan_name, area, rent_low, rent_high,
              date_captured, available_date, lease_term, move_in_date
              (+ state-tracking + extra JSON)

Drop+recreate is safe because the tables have never carried production
data — proppy was only created in this development cycle.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_v2_strict"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("units")
    op.drop_table("properties")

    op.create_table(
        "properties",
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        # V2 data fields
        sa.Column("apartment_id", sa.Integer, index=True),
        sa.Column("proj_name", sa.String(512)),
        sa.Column("address", sa.String(512)),
        sa.Column("city", sa.String(128)),
        sa.Column("state", sa.String(64)),
        sa.Column("zip_code", sa.String(16)),
        sa.Column("country", sa.String(64)),
        sa.Column("phone", sa.String(64)),
        sa.Column("email_address", sa.String(256)),
        sa.Column("website", sa.String(1024)),
        sa.Column("pmc", sa.String(256)),
        sa.Column("website_design", sa.String(128)),
        sa.Column("concessions", sa.Text),
        # State-tracking
        sa.Column("first_seen_date", sa.String(16)),
        sa.Column("last_seen_date", sa.String(16)),
        sa.Column("last_seen_at", sa.String(64)),
        sa.Column("last_scrape_status", sa.String(64)),
        sa.Column("last_units_count", sa.Integer),
        sa.Column("extra", sa.JSON, nullable=False, server_default="{}"),
    )

    op.create_table(
        "units",
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        sa.Column("unit_id", sa.String(256), primary_key=True),
        # V2 data fields
        sa.Column("beds", sa.Integer),
        sa.Column("baths", sa.Float),
        sa.Column("floor_plan_name", sa.String(256)),
        sa.Column("area", sa.Integer),
        sa.Column("rent_low", sa.Float),
        sa.Column("rent_high", sa.Float),
        sa.Column("date_captured", sa.String(32)),
        sa.Column("available_date", sa.String(32)),
        sa.Column("lease_term", sa.Integer),
        sa.Column("move_in_date", sa.String(32)),
        # State-tracking
        sa.Column("first_seen_date", sa.String(16)),
        sa.Column("last_seen_date", sa.String(16)),
        sa.Column("last_seen_at", sa.String(64)),
        sa.Column("carryforward_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("disappeared_since", sa.String(16)),
        sa.Column("last_absent_date", sa.String(16)),
        sa.Column("concessions", sa.JSON),
        sa.Column("changed_fields", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("extra", sa.JSON, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_table("units")
    op.drop_table("properties")

    op.create_table(
        "properties",
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        sa.Column("name", sa.String(512)),
        sa.Column("website", sa.String(1024)),
        sa.Column("address", sa.String(512)),
        sa.Column("city", sa.String(128)),
        sa.Column("state", sa.String(64)),
        sa.Column("zip", sa.String(32)),
        sa.Column("first_seen_date", sa.String(16)),
        sa.Column("last_seen_date", sa.String(16)),
        sa.Column("last_seen_at", sa.String(64)),
        sa.Column("last_scrape_status", sa.String(64)),
        sa.Column("last_units_count", sa.Integer),
        sa.Column("extra", sa.JSON, nullable=False, server_default="{}"),
    )

    op.create_table(
        "units",
        sa.Column("canonical_id", sa.String(256), primary_key=True),
        sa.Column("unit_id", sa.String(256), primary_key=True),
        sa.Column("unit_number", sa.String(128)),
        sa.Column("market_rent_low", sa.Float),
        sa.Column("market_rent_high", sa.Float),
        sa.Column("available_date", sa.String(32)),
        sa.Column("bedrooms", sa.Float),
        sa.Column("bathrooms", sa.Float),
        sa.Column("sqft", sa.Integer),
        sa.Column("floor_plan_name", sa.String(256)),
        sa.Column("availability_status", sa.String(32)),
        sa.Column("first_seen_date", sa.String(16)),
        sa.Column("last_seen_date", sa.String(16)),
        sa.Column("last_seen_at", sa.String(64)),
        sa.Column("carryforward_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("disappeared_since", sa.String(16)),
        sa.Column("last_absent_date", sa.String(16)),
        sa.Column("concessions", sa.JSON),
        sa.Column("changed_fields", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("extra", sa.JSON, nullable=False, server_default="{}"),
    )
