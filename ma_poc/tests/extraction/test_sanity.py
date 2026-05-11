"""Tests for ``ma_poc.extraction.sanity``.

Covers each bound, the "null all aliases" guarantee, idempotency,
non-mutation, and the ABSENT sentinel preservation.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.canonical import (
    ABSENT,
    BEDS_KEYS,
    SQFT_KEYS,
    get_numeric,
)
from ma_poc.extraction.sanity import sanity_bound

# ── Beds bounds ───────────────────────────────────────────────────────────────


class TestBedsBound:
    @pytest.mark.parametrize("value", [0, 1, 2, 3, 7])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"beds": value})
        assert result.get("beds") == value
        assert "_sanity_dropped" not in result

    @pytest.mark.parametrize("value", [-1, 8, 99, 100])
    def test_out_of_range_nulled(self, value):
        # -1 is the ABSENT sentinel; it's already "not present" and skipped.
        # The other out-of-range values get nulled.
        result = sanity_bound({"beds": value})
        if value == -1:
            # Sentinel is treated as absent — no drop emitted, no null overwrite.
            assert "_sanity_dropped" not in result
            assert result.get("beds") == -1
        else:
            assert result.get("beds") is None
            assert "beds" in result.get("_sanity_dropped", [])

    def test_studio_zero_beds_preserved(self):
        """Studios have beds=0 — must NOT be nulled (zero is a real value)."""
        result = sanity_bound({"beds": 0})
        assert result.get("beds") == 0

    def test_out_of_range_alias_nulled_too(self):
        """If beds=99 is stored under the non-canonical ``bedrooms`` alias,
        sanity must null it. Otherwise a subsequent ``get_numeric`` falls
        through to find 99 again."""
        result = sanity_bound({"bedrooms": 99})
        # Both V2 canonical and the alias should be None
        assert result.get("bedrooms") is None
        assert get_numeric(result, BEDS_KEYS) is None

    def test_pascalcase_alias_nulled_case_insensitively(self):
        """If a raw producer (Windsor RentCafe) emits BedRooms=99 (PascalCase),
        case-insensitive matching catches it."""
        result = sanity_bound({"BedRooms": 99})
        assert result.get("BedRooms") is None


# ── Baths bounds ──────────────────────────────────────────────────────────────


class TestBathsBound:
    @pytest.mark.parametrize("value", [0, 1, 1.5, 2, 2.5, 10])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"baths": value})
        assert result.get("baths") == value

    @pytest.mark.parametrize("value", [-0.5, 10.5, 100])
    def test_out_of_range_nulled(self, value):
        result = sanity_bound({"baths": value})
        assert result.get("baths") is None
        assert "baths" in result.get("_sanity_dropped", [])


# ── Sqft bounds ───────────────────────────────────────────────────────────────


class TestSqftBound:
    @pytest.mark.parametrize("value", [150, 500, 1019, 5000, 10000])
    def test_in_range_preserved(self, value):
        result = sanity_bound({"area": value})
        assert result.get("area") == value

    @pytest.mark.parametrize("value", [10, 100, 149, 10001, 50000])
    def test_out_of_range_nulled(self, value):
        result = sanity_bound({"area": value})
        assert result.get("area") is None
        assert "area" in result.get("_sanity_dropped", [])

    def test_absent_sentinel_preserved(self):
        """``area=-1`` is the explicit ABSENT sentinel — must survive sanity.
        ``get_numeric`` treats it as absent, so it never reaches the bounds
        check, and the dict value stays unchanged."""
        result = sanity_bound({"area": ABSENT})
        assert result.get("area") == ABSENT
        assert "_sanity_dropped" not in result

    def test_sqft_alias_under_pascalcase(self):
        """MinimumSqft=12000 from a Windsor payload → out-of-range → nulled."""
        result = sanity_bound({"MinimumSqft": 12000})
        assert get_numeric(result, SQFT_KEYS) is None


# ── Rent bounds ───────────────────────────────────────────────────────────────


class TestRentBound:
    @pytest.mark.parametrize("value", [200, 1500, 5000, 50000])
    def test_rent_low_in_range_preserved(self, value):
        result = sanity_bound({"rent_low": value})
        assert result.get("rent_low") == value

    @pytest.mark.parametrize("value", [-100, 0, 100, 199, 50001, 100000])
    def test_rent_low_out_of_range_nulled(self, value):
        result = sanity_bound({"rent_low": value})
        assert result.get("rent_low") is None
        assert "rent_low" in result.get("_sanity_dropped", [])

    @pytest.mark.parametrize("value", [200, 5000, 50000])
    def test_rent_high_in_range_preserved(self, value):
        result = sanity_bound({"rent_high": value})
        assert result.get("rent_high") == value

    def test_rent_high_out_of_range_nulled_independently_of_low(self):
        """rent_high can fail sanity while rent_low passes."""
        result = sanity_bound({"rent_low": 1500, "rent_high": 999999})
        assert result.get("rent_low") == 1500
        assert result.get("rent_high") is None


# ── Compound (multi-field) cases ──────────────────────────────────────────────


class TestCompoundCases:
    def test_unit_with_one_bad_field_keeps_others(self):
        """A row with beds=99 (bad) but otherwise good should keep its
        baths/area/rent. is_valid_unit will then accept via baths/area."""
        unit = {
            "beds": 99,
            "baths": 2,
            "area": 1019,
            "rent_low": 1500,
            "rent_high": 2000,
        }
        result = sanity_bound(unit)
        assert result.get("beds") is None
        assert result.get("baths") == 2
        assert result.get("area") == 1019
        assert result.get("rent_low") == 1500
        assert result.get("rent_high") == 2000
        assert result.get("_sanity_dropped") == ["beds"]

    def test_multiple_dropped_fields_recorded_in_order(self):
        unit = {"beds": 99, "baths": 100, "area": 5}
        result = sanity_bound(unit)
        # Drop order matches the function's check order
        assert result.get("_sanity_dropped") == ["beds", "baths", "area"]

    def test_all_fields_in_range_no_marker(self):
        unit = {"beds": 2, "baths": 2, "area": 1019, "rent_low": 1500}
        result = sanity_bound(unit)
        assert "_sanity_dropped" not in result

    def test_does_not_drop_a_row(self):
        """Sanity NEVER deletes the row — even if every field is bad,
        the returned dict still has the same keys (with None values)."""
        unit = {"beds": 99, "baths": 100, "area": 5, "rent_low": -1, "rent_high": 999999}
        result = sanity_bound(unit)
        assert isinstance(result, dict)
        # The dict is non-empty (carries the nulled keys + _sanity_dropped)
        assert len(result) > 0


# ── Idempotency + non-mutation ────────────────────────────────────────────────


class TestIdempotencyAndNonMutation:
    def test_does_not_mutate_input(self):
        unit = {"beds": 99, "area": 1019}
        before = dict(unit)
        sanity_bound(unit)
        assert unit == before

    def test_is_idempotent(self):
        """A second call on the result yields the same content."""
        unit = {"beds": 99, "baths": 2, "area": 1019}
        first = sanity_bound(unit)
        second = sanity_bound(first)
        assert first == second

    def test_empty_dict(self):
        result = sanity_bound({})
        assert result == {}

    def test_non_dict_input_returned_unchanged(self):
        assert sanity_bound(None) is None  # type: ignore[arg-type]
        assert sanity_bound("x") == "x"  # type: ignore[arg-type]


# ── Real-data regression ──────────────────────────────────────────────────────


class TestRealDataSanity:
    def test_skyline_kessler_neighborhood_passes_through_unchanged(self):
        """Skyline at Kessler nestiolistings item — no dimensions, no rent.
        Sanity has nothing to do; the dict comes out the same shape.
        is_valid_unit will reject it later."""
        unit = {"name": "Hoboken", "area": "Hudson County", "city": "Bayonne"}
        result = sanity_bound(unit)
        # ``area`` is a geographic string — get_numeric returns None — sanity
        # has nothing to bound — no drop emitted.
        assert "_sanity_dropped" not in result
        assert result["area"] == "Hudson County"  # untouched

    def test_maa_worthington_unit_passes_sanity(self):
        """Real MAA Worthington API item — all values in range."""
        unit = {
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2,
            "baths": 2,
            "area": 1019,
            "rent_low": 2680,
            "rent_high": 5165,
        }
        result = sanity_bound(unit)
        assert "_sanity_dropped" not in result
        assert result == {**unit}

    def test_dom_scan_balcony_sqft_100_dropped(self):
        """The 22658 countryridge case: DOM scan picked up "100 sq ft" from
        balcony text. After inference (if any), sanity catches sqft=100 as
        below the 150 floor and nulls it."""
        unit = {"floor_plan_name": "2BR / 1.5BA", "beds": 2, "baths": 1.5, "area": 100, "rent_low": 1125}
        result = sanity_bound(unit)
        assert result.get("area") is None
        assert "area" in result.get("_sanity_dropped", [])
        # Other fields survive — beds=2, baths=1.5, rent=1125 enough for is_valid_unit
        assert result.get("beds") == 2
        assert result.get("baths") == 1.5
        assert result.get("rent_low") == 1125
