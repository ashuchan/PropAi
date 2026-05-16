"""Tests for the numeric-coercion writer boundary (2026-05-16 shard-48 fix).

The 2026-05-16 incident: an LLM-rescue path for property 284136 emitted
``bedrooms`` / ``sqft`` as strings (``"1.0"`` / ``"812.0"``) into legacy
v1 keys. The ``_SNAPSHOT_SOURCES`` fallback in ``upsert_units`` reads
those when the v2 keys (``beds`` / ``area``) are null. They then reach
the Integer columns verbatim and pg8000 rejects them with SQLSTATE 22P02
``invalid input syntax for type integer: "1.0"``.

``_coerce_numeric_columns`` (called inside ``upsert`` / ``upsert_units``
right before the SQL bind) converts those strings to ints at the writer
boundary so the data actually lands in the DB instead of nuking the
shard's PG sync transaction.

Two layers of coverage:

  1. Unit tests of ``_coerce_one_numeric`` / ``_coerce_numeric_columns``
     — every coercion branch, including the placeholder-token + bad-input
     paths.
  2. End-to-end tests against the real ``upsert_units`` / property
     ``upsert`` using SQLite (the column types and bind processors are
     the same as Postgres for these cases).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import text

from data_provider.contracts import DataProvider
from data_provider.sql.models import PropertyRow, UnitRow
from data_provider.sql.stores import (
    _coerce_numeric_columns,
    _coerce_one_numeric,
    _numeric_python_types,
)
from data_provider.sqlite import SqliteDataProvider


@pytest.fixture
def provider(tmp_path: Path) -> DataProvider:
    p = SqliteDataProvider(url=f"sqlite:///{tmp_path / 'numeric.db'}")
    yield p
    p.close()


# ─────────────────────────────────────────────────────────────────────────────
# _numeric_python_types — introspection
# ─────────────────────────────────────────────────────────────────────────────


class TestNumericPythonTypes:
    def test_unitrow_has_expected_int_and_float_columns(self) -> None:
        types = _numeric_python_types(UnitRow)
        # Integer columns (where the 22P02 bug actually manifests).
        assert types.get("beds") is int
        assert types.get("area") is int
        assert types.get("lease_term") is int
        assert types.get("carryforward_days") is int
        # Float columns.
        assert types.get("baths") is float
        assert types.get("rent_low") is float
        assert types.get("rent_high") is float

    def test_skips_strings_json_and_datetime(self) -> None:
        types = _numeric_python_types(UnitRow)
        # Strings + JSON must never be coerced numerically.
        assert "unit_id" not in types
        assert "floor_plan_name" not in types
        assert "concessions" not in types
        assert "extra" not in types

    def test_property_row_apartment_id_is_int(self) -> None:
        # PropertyRow.apartment_id can also receive a stringy value from
        # CSV / extractor — coverage so the property upsert path is also
        # protected.
        types = _numeric_python_types(PropertyRow)
        assert types.get("apartment_id") is int
        assert types.get("last_units_count") is int


# ─────────────────────────────────────────────────────────────────────────────
# _coerce_one_numeric — pure function, every branch
# ─────────────────────────────────────────────────────────────────────────────


class TestCoerceOneNumericInt:
    def test_none_passes_through(self) -> None:
        assert _coerce_one_numeric(None, int) == (None, False)

    def test_int_passes_through(self) -> None:
        assert _coerce_one_numeric(3, int) == (3, False)

    def test_zero_passes_through(self) -> None:
        # Defensive — 0 is falsy in Python; the coercer must not treat
        # it as missing.
        assert _coerce_one_numeric(0, int) == (0, False)

    def test_int_valued_float_converts_without_warn(self) -> None:
        assert _coerce_one_numeric(1.0, int) == (1, False)
        assert _coerce_one_numeric(812.0, int) == (812, False)

    def test_fractional_float_becomes_none_with_warn(self) -> None:
        # 1.5 beds is genuinely bad data — silently rounding hides the
        # extractor bug.
        v, warn = _coerce_one_numeric(1.5, int)
        assert v is None and warn is True

    def test_nan_becomes_none_with_warn(self) -> None:
        v, warn = _coerce_one_numeric(float("nan"), int)
        assert v is None and warn is True

    def test_numeric_string_parses(self) -> None:
        assert _coerce_one_numeric("3", int) == (3, False)

    def test_stringy_decimal_zero_parses_the_284136_case(self) -> None:
        # The exact value pg8000 rejected on shard 48.
        assert _coerce_one_numeric("1.0", int) == (1, False)
        assert _coerce_one_numeric("812.0", int) == (812, False)

    def test_string_with_surrounding_whitespace_parses(self) -> None:
        assert _coerce_one_numeric(" 7 ", int) == (7, False)
        assert _coerce_one_numeric("\t42\n", int) == (42, False)

    def test_null_token_strings_become_none_without_warn(self) -> None:
        # Extractors use these interchangeably with null; no WARN.
        for token in ("", "  ", "N/A", "n/a", "NA", "None", "null", "-"):
            v, warn = _coerce_one_numeric(token, int)
            assert v is None, token
            assert warn is False, token

    def test_unparseable_string_becomes_none_with_warn(self) -> None:
        v, warn = _coerce_one_numeric("call for price", int)
        assert v is None and warn is True

    def test_fractional_numeric_string_becomes_none_with_warn(self) -> None:
        v, warn = _coerce_one_numeric("1.5", int)
        assert v is None and warn is True

    def test_bool_passes_through(self) -> None:
        # bool is a subclass of int — coercer must not silently strip
        # semantics. Boolean columns are excluded from the coercion
        # map so this branch is defensive only.
        assert _coerce_one_numeric(True, int) == (True, False)
        assert _coerce_one_numeric(False, int) == (False, False)

    def test_arbitrary_object_becomes_none_with_warn(self) -> None:
        v, warn = _coerce_one_numeric(object(), int)
        assert v is None and warn is True


class TestCoerceOneNumericFloat:
    def test_none_passes_through(self) -> None:
        assert _coerce_one_numeric(None, float) == (None, False)

    def test_int_promotes_to_float(self) -> None:
        assert _coerce_one_numeric(3, float) == (3.0, False)

    def test_float_passes_through(self) -> None:
        assert _coerce_one_numeric(1.5, float) == (1.5, False)

    def test_numeric_string_parses(self) -> None:
        assert _coerce_one_numeric("1500.50", float) == (1500.5, False)
        assert _coerce_one_numeric("1500", float) == (1500.0, False)

    def test_null_token_strings_become_none_without_warn(self) -> None:
        for token in ("", "n/a", "None"):
            v, warn = _coerce_one_numeric(token, float)
            assert v is None and warn is False

    def test_unparseable_string_becomes_none_with_warn(self) -> None:
        v, warn = _coerce_one_numeric("$1,500", float)
        assert v is None and warn is True


# ─────────────────────────────────────────────────────────────────────────────
# _coerce_numeric_columns — dict-level helper
# ─────────────────────────────────────────────────────────────────────────────


class TestCoerceNumericColumns:
    def test_coerces_units_payload_in_place(self) -> None:
        values: dict[str, object] = {
            "unit_id": "U101",  # untouched
            "beds": "1.0",
            "baths": "1.5",
            "area": "812.0",
            "rent_low": "1500",
            "rent_high": 1600,
            "floor_plan_name": "1BR",  # untouched
        }
        out = _coerce_numeric_columns(values, UnitRow)
        assert out is values  # mutates in place
        assert out["unit_id"] == "U101"
        assert out["beds"] == 1
        assert out["baths"] == 1.5
        assert out["area"] == 812
        assert out["rent_low"] == 1500.0
        assert out["rent_high"] == 1600.0
        assert out["floor_plan_name"] == "1BR"

    def test_warns_on_unparseable_value(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            _coerce_numeric_columns(
                {"beds": "call for price"}, UnitRow, log_prefix="test"
            )
        assert any(
            "coerced bad numeric" in r.message and "UnitRow.beds" in r.message
            for r in caplog.records
        )

    def test_silent_on_null_token_strings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Null-shaped tokens are the extractor's "I don't know" — they
        # become NULL silently so the WARN signal stays meaningful.
        with caplog.at_level(logging.WARNING):
            out = _coerce_numeric_columns({"beds": "N/A", "area": ""}, UnitRow)
        assert out["beds"] is None
        assert out["area"] is None
        assert not any("coerced bad numeric" in r.message for r in caplog.records)

    def test_silent_on_clean_int_pass_through(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            _coerce_numeric_columns({"beds": 2, "area": 1000}, UnitRow)
        assert not any("coerced bad numeric" in r.message for r in caplog.records)

    def test_ignores_columns_not_on_model(self) -> None:
        # Caller may pass extra keys (e.g. the ``extra`` JSON catch-all).
        # The helper introspects the model and only touches known cols.
        out = _coerce_numeric_columns(
            {"beds": "3", "made_up_column": "1.0"}, UnitRow
        )
        assert out["beds"] == 3
        assert out["made_up_column"] == "1.0"  # untouched

    def test_works_for_property_row_apartment_id(self) -> None:
        out = _coerce_numeric_columns(
            {"apartment_id": "12345", "proj_name": "Test"}, PropertyRow
        )
        assert out["apartment_id"] == 12345
        assert out["proj_name"] == "Test"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end — upsert_units actually succeeds with shard-48 shaped data
# ─────────────────────────────────────────────────────────────────────────────


class TestUpsertUnitsAcceptsStringyNumerics:
    """Reproduces shard 48 / property 284136 — units land instead of
    crashing the PG sync. Uses the real upsert_units code path."""

    def test_stringy_decimal_beds_and_area_land_as_ints(
        self, provider: DataProvider
    ) -> None:
        units = [
            {
                "unit_id": "#125",
                "floor_plan_name": "A6",
                # Legacy v1 keys that ``_SNAPSHOT_SOURCES`` falls back to
                # when ``beds`` / ``area`` are absent.
                "bedrooms": "1.0",
                "bathrooms": "1.0",
                "sqft": "812.0",
                "rent_low": None,
                "rent_high": None,
                "availability_status": "AVAILABLE",
            },
            {
                "unit_id": "#160",
                "floor_plan_name": "B2",
                "bedrooms": "2.0",
                "bathrooms": "2.0",
                "sqft": "1171.0",
            },
        ]
        # Pre-fix: this would have raised at the DB layer with SQLSTATE 22P02.
        diff = provider.unit_state.upsert_units("CID-284136", units, "2026-05-16")
        assert len(diff.new) == 2

        stored = provider.unit_state.get_units("CID-284136")
        assert stored["#125"].beds == 1
        assert stored["#125"].area == 812
        assert stored["#125"].baths == 1.0
        assert stored["#160"].beds == 2
        assert stored["#160"].area == 1171

    def test_v2_keys_win_over_legacy_keys(self, provider: DataProvider) -> None:
        # Belt-and-braces — coercion must not override a clean v2 value
        # when both keys are present.
        units = [
            {
                "unit_id": "U1",
                "floor_plan_name": "A",
                "beds": 3,
                "bedrooms": "1.0",
                "area": 1500,
                "sqft": "812.0",
            },
        ]
        provider.unit_state.upsert_units("CID-V2", units, "2026-05-16")
        stored = provider.unit_state.get_units("CID-V2")
        assert stored["U1"].beds == 3
        assert stored["U1"].area == 1500

    def test_unparseable_legacy_value_becomes_null_not_crash(
        self, provider: DataProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        units = [
            {
                "unit_id": "U_BAD",
                "floor_plan_name": "X",
                "bedrooms": "call for price",  # nonsense
                "sqft": "",
            },
        ]
        with caplog.at_level(logging.WARNING):
            provider.unit_state.upsert_units("CID-BAD", units, "2026-05-16")

        stored = provider.unit_state.get_units("CID-BAD")
        # Row landed; the unparseable values became NULL.
        assert "U_BAD" in stored
        assert stored["U_BAD"].beds is None
        assert stored["U_BAD"].area is None
        # And the upstream extractor bug was logged loudly.
        assert any(
            "coerced bad numeric" in r.message and "UnitRow.beds" in r.message
            for r in caplog.records
        )

    def test_mixed_batch_does_not_strand_clean_rows(
        self, provider: DataProvider
    ) -> None:
        # The whole point of this fix combined with isolate_row_writes:
        # one row's bad data must not affect the rest of the batch. Here
        # the bad row no longer even hits the DB layer — it just lands
        # with NULLs.
        units = [
            {"unit_id": "ok1", "floor_plan_name": "1BR", "beds": 1, "area": 700},
            {"unit_id": "ok2", "floor_plan_name": "2BR", "bedrooms": "2.0", "sqft": "900.0"},
            {"unit_id": "bad", "floor_plan_name": "X", "bedrooms": "garbage"},
            {"unit_id": "ok3", "floor_plan_name": "3BR", "beds": 3, "area": 1200},
        ]
        diff = provider.unit_state.upsert_units("CID-MIX", units, "2026-05-16")
        assert len(diff.new) == 4

        stored = provider.unit_state.get_units("CID-MIX")
        assert {"ok1", "ok2", "bad", "ok3"} == set(stored.keys())
        assert stored["ok2"].beds == 2  # coerced from "2.0"
        assert stored["ok2"].area == 900
        assert stored["bad"].beds is None  # gracefully null
        assert stored["ok3"].beds == 3


class TestPropertyUpsertAcceptsStringyNumerics:
    def test_apartment_id_string_coerces_to_int(self, provider: DataProvider) -> None:
        snap = {
            "apartment_id": "12345",  # stringy from CSV
            "proj_name": "Test Property",
            "city": "Phoenix",
        }
        provider.property_state.upsert("CID-P1", snap, "2026-05-16")
        loaded = provider.property_state.get("CID-P1")
        assert loaded is not None
        assert loaded.apartment_id == 12345

    def test_bad_apartment_id_becomes_null_with_warn(
        self, provider: DataProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            provider.property_state.upsert(
                "CID-P2", {"apartment_id": "n/a", "proj_name": "X"}, "2026-05-16"
            )
        loaded = provider.property_state.get("CID-P2")
        assert loaded is not None
        assert loaded.apartment_id is None
        # "n/a" is a null token — no WARN, just silent null.
        assert not any("coerced bad numeric" in r.message for r in caplog.records)


class TestColumnTypesInDb:
    """Confirms the bind processors actually send numeric values, not text."""

    def test_int_columns_store_native_ints(self, provider: DataProvider) -> None:
        provider.unit_state.upsert_units(
            "CID-T",
            [
                {
                    "unit_id": "u1",
                    "floor_plan_name": "1BR",
                    "bedrooms": "1.0",
                    "sqft": "812.0",
                },
            ],
            "2026-05-16",
        )
        with provider.engine.connect() as c:
            row = c.execute(
                text(
                    "SELECT beds, area, typeof(beds), typeof(area) "
                    "FROM units WHERE canonical_id='CID-T' AND unit_id='u1'"
                )
            ).one()
        beds, area, beds_type, area_type = row
        assert beds == 1 and area == 812
        # SQLite reports 'integer' for native ints; 'text' would mean
        # the coercion didn't happen.
        assert beds_type == "integer"
        assert area_type == "integer"
