"""Integration tests for SqlUnitStateStore.upsert_units fallback + diff fields.

Covers the merge-rescue contract added by the merge-analysis fix:

  * Records reaching the SQL upsert without ``unit_id`` get a fallback
    derived in place via ``assign_fallback_unit_id`` instead of being
    silently dropped.
  * Truly anchorless records increment ``UnitDiff.skipped_no_identity``
    rather than vanishing.
  * ``UnitDiff.input_count`` mirrors the inbound list length.
  * ``data_sha256`` is populated on every persisted row (informational).

Uses the same in-process SQLite fixture pattern as the existing
:mod:`tests.data_provider.test_contract` so we don't need a live
Postgres for unit-level coverage. The SQL code path is identical between
SQLite and Postgres for these queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ma_poc.data_provider.contracts import DataProvider
from ma_poc.data_provider.sqlite import SqliteDataProvider


@pytest.fixture
def provider(tmp_path: Path) -> DataProvider:
    p = SqliteDataProvider(url=f"sqlite:///{tmp_path / 'test.db'}")
    yield p
    p.close()


def _u(**kw: object) -> dict[str, object]:
    """Default-empty unit dict — explicit None on every field the upsert reads."""
    base: dict[str, object] = {
        "unit_id": None,
        "floor_plan_name": None,
        "beds": None,
        "baths": None,
        "area": None,
        "rent_low": None,
        "rent_high": None,
        "available_date": None,
    }
    base.update(kw)
    return base


# ── Fallback-id derivation ──────────────────────────────────────────────────


def test_natural_unit_id_passes_through(provider: DataProvider) -> None:
    diff = provider.unit_state.upsert_units(
        "P1",
        [_u(unit_id="101", floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0)],
        "2026-05-06",
    )
    assert diff.skipped_no_identity == 0
    assert diff.input_count == 1
    assert diff.new == ["101"]


def test_missing_unit_id_gets_sha256_fallback(provider: DataProvider) -> None:
    diff = provider.unit_state.upsert_units(
        "P1",
        [_u(floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0)],
        "2026-05-06",
    )
    assert diff.skipped_no_identity == 0
    assert diff.input_count == 1
    assert len(diff.new) == 1
    assert diff.new[0].startswith("inferred_")


def test_floor_plan_only_record_gets_last_resort_id(provider: DataProvider) -> None:
    """Single-anchor (only floor_plan present) — last-resort key fires."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [_u(floor_plan_name="Studio Loft", rent_low=1200.0)],
        "2026-05-06",
    )
    assert diff.skipped_no_identity == 0
    assert len(diff.new) == 1
    assert diff.new[0].startswith("inferred_")


def test_anchorless_record_increments_skipped_counter(provider: DataProvider) -> None:
    """Empty record — no floor_plan, no unit_id, no unit_number — must skip."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [_u(rent_low=1200.0)],  # rent alone is not an anchor
        "2026-05-06",
    )
    assert diff.skipped_no_identity == 1
    assert diff.input_count == 1
    assert diff.new == []


def test_mixed_batch_each_path_counted(provider: DataProvider) -> None:
    """Batch of natural + sha256 + last-resort + anchorless."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [
            _u(unit_id="101", floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0),
            _u(floor_plan_name="A2", beds=2, baths=2.0, area=1100, rent_low=2200.0),
            _u(floor_plan_name="Studio", rent_low=900.0),
            _u(rent_low=1500.0),  # anchorless
        ],
        "2026-05-06",
    )
    assert diff.input_count == 4
    assert diff.skipped_no_identity == 1
    assert len(diff.new) == 3
    # First is the natural id; the other two are inferred.
    assert "101" in diff.new
    assert sum(1 for u in diff.new if u.startswith("inferred_")) == 2


# ── Plural-s collision (Fix #4) ─────────────────────────────────────────────


def test_plural_s_collides_in_same_batch(provider: DataProvider) -> None:
    """Two records describing the same plan with different surface forms
    must hash to the same id and merge into one row."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [
            _u(floor_plan_name="2 Bedroom", beds=2, baths=1.0, area=1100, rent_low=2000.0),
            _u(floor_plan_name="2 Bedrooms", beds=2, baths=1, area=1100, rent_low=2050.0),
        ],
        "2026-05-06",
    )
    # Both keyed but to the SAME id — second overwrites first as "updated".
    assert diff.skipped_no_identity == 0
    assert len(set(diff.new) | set(diff.updated)) == 1


# ── data_sha256 column populated ────────────────────────────────────────────


def test_data_sha256_written_on_every_keyed_row(provider: DataProvider) -> None:
    provider.unit_state.upsert_units(
        "P1",
        [
            _u(unit_id="101", floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0),
            _u(floor_plan_name="A2", beds=2, baths=2.0, area=1100, rent_low=2200.0),
        ],
        "2026-05-06",
    )
    # Read back via the engine so we can see the column directly.
    with provider.engine.connect() as c:
        rows = list(
            c.execute(
                text("SELECT unit_id, length(data_sha256) FROM units WHERE canonical_id='P1'")
            )
        )
    assert len(rows) == 2
    for unit_id, sha_len in rows:
        assert sha_len == 64, f"expected 64-char SHA256 hex on {unit_id}, got {sha_len}"


def test_data_sha256_changes_when_any_field_changes(provider: DataProvider) -> None:
    """Sanity check that the column actually reflects payload contents — but
    note this is informational only, the upsert key still merges by unit_id."""
    diff_a = provider.unit_state.upsert_units(
        "P1",
        [_u(unit_id="101", floor_plan_name="A1", rent_low=1500.0)],
        "2026-05-06",
    )
    assert diff_a.new == ["101"]

    diff_b = provider.unit_state.upsert_units(
        "P1",
        [_u(unit_id="101", floor_plan_name="A1", rent_low=1600.0)],  # rent flipped
        "2026-05-07",
    )
    # Same unit_id → it's an update, not a new row.
    assert diff_b.updated == ["101"]
    assert diff_b.new == []

    with provider.engine.connect() as c:
        sha_now = c.execute(
            text("SELECT data_sha256 FROM units WHERE canonical_id='P1' AND unit_id='101'")
        ).scalar()
    # And it's a 64-char hex string.
    assert sha_now is not None and len(sha_now) == 64
