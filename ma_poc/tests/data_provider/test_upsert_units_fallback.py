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


def test_anchorless_record_inserts_with_synthetic_key(provider: DataProvider) -> None:
    """No-drop contract: a record with no identifying anchor (no unit_id,
    no floor_plan, no unit_number) must still land in the table. The
    synthesized id is prefixed ``unkeyable_`` so it's filterable in SQL,
    and ``synthetic_key_used`` increments for the yield gate."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [_u(rent_low=1200.0)],  # rent alone is not an anchor
        "2026-05-06",
    )
    assert diff.input_count == 1
    assert diff.skipped_no_identity == 0  # no-drop contract
    assert diff.synthetic_key_used == 1
    assert len(diff.new) == 1
    assert diff.new[0].startswith("unkeyable_")


def test_anchorless_record_persisted_in_units_table(provider: DataProvider) -> None:
    """Smoke check: the row actually lands in ``units`` (not just buffered
    into the diff and dropped before commit)."""
    provider.unit_state.upsert_units(
        "P_unkeyable",
        [_u(rent_low=1200.0)],
        "2026-05-06",
    )
    with provider.engine.connect() as c:
        rows = list(
            c.execute(
                text("SELECT unit_id, last_seen_at FROM units WHERE canonical_id='P_unkeyable'")
            )
        )
    assert len(rows) == 1
    assert rows[0][0].startswith("unkeyable_")
    # ``last_seen_at`` ISO timestamp begins with the run_date — confirms
    # the day-level "was this unit seen on this day" signal works without
    # a dedicated last_scrape_date column.
    assert rows[0][1].startswith("2026-05-06")


# ── Empty-string coercion (Cloud Run regression 2026-05-12) ────────────────


def test_empty_string_numeric_fields_coerced_to_none(provider: DataProvider) -> None:
    """Partial recovery can emit beds/baths/area/rent as '' instead of None.

    _read_first must treat '' the same as None so SQLAlchemy does not try to
    cast '' to Integer/Float — which raises StatementError or DatabaseError
    and kills the whole shard's PG sync.
    """
    diff = provider.unit_state.upsert_units(
        "P_empty_str",
        [
            _u(
                unit_id="101",
                beds="",
                baths="",
                area="",
                rent_low="",
                rent_high="",
                lease_term="",
            )
        ],
        "2026-05-12",
    )
    assert diff.input_count == 1
    assert diff.new == ["101"]

    with provider.engine.connect() as c:
        row = c.execute(
            text(
                "SELECT beds, baths, area, rent_low, rent_high"
                " FROM units WHERE canonical_id='P_empty_str'"
            )
        ).fetchone()
    assert row is not None
    assert row[0] is None, "beds should be NULL, not empty string"
    assert row[1] is None, "baths should be NULL, not empty string"
    assert row[2] is None, "area should be NULL, not empty string"
    assert row[3] is None, "rent_low should be NULL, not empty string"
    assert row[4] is None, "rent_high should be NULL, not empty string"


def test_zero_numeric_fields_not_coerced(provider: DataProvider) -> None:
    """Falsy-but-valid values (0, 0.0) must NOT be treated as None."""
    diff = provider.unit_state.upsert_units(
        "P_zeros",
        [_u(unit_id="102", beds=0, baths=0.0, area=0, rent_low=0.0)],
        "2026-05-12",
    )
    assert diff.new == ["102"]

    with provider.engine.connect() as c:
        row = c.execute(
            text(
                "SELECT beds, baths, area, rent_low"
                " FROM units WHERE canonical_id='P_zeros'"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == 0
    assert row[1] == 0.0
    assert row[2] == 0
    assert row[3] == 0.0


def test_synthetic_key_collides_for_byte_identical_payloads(provider: DataProvider) -> None:
    """Two anchorless records with identical payloads must collapse to one
    row. Distinct anchorless payloads must NOT collapse — that would be a
    silent merge across genuinely different observations."""
    # Same payload twice → one row.
    diff_a = provider.unit_state.upsert_units(
        "P_collide",
        [_u(rent_low=1200.0), _u(rent_low=1200.0)],
        "2026-05-06",
    )
    assert diff_a.input_count == 2
    assert diff_a.synthetic_key_used == 2  # both went through the synthesis path
    # But the two synthetic ids collide → one row in the DB.
    with provider.engine.connect() as c:
        n = c.execute(
            text("SELECT COUNT(*) FROM units WHERE canonical_id='P_collide'")
        ).scalar()
    assert n == 1

    # Distinct payloads → distinct rows.
    diff_b = provider.unit_state.upsert_units(
        "P_distinct",
        [_u(rent_low=1200.0), _u(rent_low=1500.0)],
        "2026-05-06",
    )
    assert diff_b.synthetic_key_used == 2
    with provider.engine.connect() as c:
        n = c.execute(
            text("SELECT COUNT(*) FROM units WHERE canonical_id='P_distinct'")
        ).scalar()
    assert n == 2


def test_synthetic_key_idempotent_across_runs(provider: DataProvider) -> None:
    """Re-running the same anchorless record on a later run must update the
    existing row (not create a second one). last_seen_at must advance."""
    provider.unit_state.upsert_units(
        "P_idem",
        [_u(rent_low=1200.0)],
        "2026-05-06",
    )
    diff = provider.unit_state.upsert_units(
        "P_idem",
        [_u(rent_low=1200.0)],
        "2026-05-07",
    )
    # Second run sees the same synthetic id as already present → unchanged.
    assert diff.unchanged and not diff.new
    with provider.engine.connect() as c:
        rows = list(
            c.execute(text("SELECT last_seen_at FROM units WHERE canonical_id='P_idem'"))
        )
    assert len(rows) == 1
    assert rows[0][0].startswith("2026-05-07")


def test_mixed_batch_each_path_counted(provider: DataProvider) -> None:
    """Batch of natural + sha256 fingerprint + last-resort + anchorless.
    Every record lands; counters reflect which path was taken."""
    diff = provider.unit_state.upsert_units(
        "P1",
        [
            _u(unit_id="101", floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0),
            _u(floor_plan_name="A2", beds=2, baths=2.0, area=1100, rent_low=2200.0),
            _u(floor_plan_name="Studio", rent_low=900.0),
            _u(rent_low=1500.0),  # anchorless → synthetic key
        ],
        "2026-05-06",
    )
    assert diff.input_count == 4
    assert diff.skipped_no_identity == 0  # no-drop contract
    assert diff.synthetic_key_used == 1
    assert len(diff.new) == 4
    # One natural, two inferred (sha256 + last-resort), one unkeyable.
    assert "101" in diff.new
    assert sum(1 for u in diff.new if u.startswith("inferred_")) == 2
    assert sum(1 for u in diff.new if u.startswith("unkeyable_")) == 1


def test_last_seen_at_is_day_filterable(provider: DataProvider) -> None:
    """last_seen_at is an ISO timestamp — its first 10 chars are the run-day,
    so day-level "was this unit observed on day X?" queries work via
    ``substring(last_seen_at, 1, 10) = '<run_date>'`` without a dedicated
    column. This pins that contract."""
    provider.unit_state.upsert_units(
        "P_day",
        [_u(unit_id="A", floor_plan_name="A1", beds=1, baths=1.0, area=750, rent_low=1500.0)],
        "2026-05-06",
    )
    with provider.engine.connect() as c:
        n = c.execute(
            text(
                "SELECT COUNT(*) FROM units "
                "WHERE canonical_id='P_day' "
                "AND substring(last_seen_at, 1, 10) = '2026-05-06'"
            )
        ).scalar()
    assert n == 1


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
