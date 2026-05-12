"""Regression tests for the 2026-05-12 area=-1 quality fixes.

Four root causes addressed:
  1. has_unit_signals() admitted location-widget APIs where values were all null.
  2. schema_gate._has_physical_signal accepted floor_plan_name alone (Sagestone Village).
  3. generic.py post_process exception handler silently fell through with pre-gate junk.
  4. RentCafe sqft lookup missed many Yardi field name variants.
  5. _container_yields_unit admitted sub-150 sqft matches from amenity text.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._html_extract import _container_yields_unit
from ma_poc.pms.adapters._merge_fns import has_unit_signals
from ma_poc.pms.adapters.rentcafe import parse_rentcafe_floorplans
from ma_poc.validation.schema_gate import check


# ══════════════════════════════════════════════════════════════════════════════
# Fix 1 — has_unit_signals() value quorum
# ══════════════════════════════════════════════════════════════════════════════


class TestHasUnitSignals:
    def test_key_names_present_but_all_values_null_rejected(self) -> None:
        """Nestiolistings locations API: keys match but every value is null."""
        items = [
            {"name": "Hoboken", "minRent": None, "bedrooms": None, "unitNumber": None}
        ] * 5
        assert has_unit_signals(items) is False

    def test_key_names_with_real_values_accepted(self) -> None:
        items = [
            {"unitNumber": "101", "minRent": 1500, "bedrooms": 1},
            {"unitNumber": "102", "minRent": 1600, "bedrooms": 2},
        ]
        assert has_unit_signals(items) is True

    def test_mixed_sample_majority_null_rejected(self) -> None:
        """If the majority of sampled items have null values, reject."""
        items = [
            {"minRent": None, "bedrooms": None},  # null
            {"minRent": None, "bedrooms": None},  # null
            {"minRent": None, "bedrooms": None},  # null
            {"minRent": 1500, "bedrooms": 1},     # real
        ]
        assert has_unit_signals(items) is False

    def test_majority_real_values_accepted(self) -> None:
        items = [
            {"minRent": 1500, "bedrooms": 1},
            {"minRent": 1600, "bedrooms": 2},
            {"minRent": None, "bedrooms": None},
        ]
        assert has_unit_signals(items) is True

    def test_zero_values_not_counted_as_real(self) -> None:
        """beds=0 and minRent=0 should not be counted as signal values."""
        items = [{"bedrooms": 0, "minRent": 0}] * 5
        assert has_unit_signals(items) is False

    def test_empty_list_rejected(self) -> None:
        assert has_unit_signals([]) is False

    def test_single_item_with_real_values_accepted(self) -> None:
        items = [{"unitNumber": "1A", "rent": 1200}]
        assert has_unit_signals(items) is True


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2 — schema_gate._has_physical_signal tightened
# ══════════════════════════════════════════════════════════════════════════════


class TestSchemaGatePhysicalSignal:
    def test_unit_id_plus_fp_name_only_rejected(self) -> None:
        """Sagestone Village shape: unit_number + floor_plan_name, no rent/dims."""
        res = check({"unit_id": "11", "floor_plan_name": "B2"}, property_id="P57767")
        assert res.accepted is None
        assert "IDENTITY_REQUIRES_PHYSICAL_SIGNAL" in res.rejection_reasons

    def test_appfolio_listing_id_as_fp_name_rejected(self) -> None:
        res = check(
            {"unit_id": "3918", "floor_plan_name": "AppFolio listing 3918"},
            property_id="P295964",
        )
        assert res.accepted is None
        assert "IDENTITY_REQUIRES_PHYSICAL_SIGNAL" in res.rejection_reasons

    def test_unit_id_plus_rent_accepted(self) -> None:
        """SightMap units: unit_number + rent, no dims — still accepted."""
        res = check({"unit_id": "0302", "rent_low": 2049}, property_id="P235183")
        assert res.accepted is not None

    def test_unit_id_plus_beds_accepted(self) -> None:
        res = check({"unit_id": "101", "beds": 1}, property_id="P1")
        assert res.accepted is not None

    def test_unit_id_plus_sqft_accepted(self) -> None:
        res = check({"unit_id": "101", "sqft": 750}, property_id="P1")
        assert res.accepted is not None

    def test_unit_id_plus_market_rent_low_accepted(self) -> None:
        res = check({"unit_id": "101", "market_rent_low": 1450}, property_id="P1")
        assert res.accepted is not None

    def test_unit_id_alone_rejected(self) -> None:
        res = check({"unit_id": "101"}, property_id="P1")
        assert res.accepted is None
        assert "IDENTITY_REQUIRES_PHYSICAL_SIGNAL" in res.rejection_reasons


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4 — RentCafe sqft field name variants
# ══════════════════════════════════════════════════════════════════════════════


class TestRentCafeSqftVariants:
    def _parse_single(self, item_keys: dict) -> dict:
        units = parse_rentcafe_floorplans([item_keys], url="https://example.com")
        assert len(units) == 1
        return units[0]

    def test_sqft_field_read(self) -> None:
        u = self._parse_single({"floorplanName": "A1", "beds": 1, "baths": 1, "sqft": 750})
        assert u.get("sqft") == "750"

    def test_squarefeet_field_read(self) -> None:
        u = self._parse_single({"floorplanName": "A1", "beds": 1, "squareFeet": 820})
        assert u.get("sqft") == "820"

    def test_squarefootage_field_read(self) -> None:
        u = self._parse_single({"floorplanName": "A1", "beds": 1, "squareFootage": 900})
        assert u.get("sqft") == "900"

    def test_floorsize_field_read(self) -> None:
        u = self._parse_single({"floorplanName": "A1", "beds": 1, "floorSize": 1050})
        assert u.get("sqft") == "1050"

    def test_minimumsqft_range_when_single_absent(self) -> None:
        u = self._parse_single({
            "floorplanName": "B2", "beds": 2,
            "minimumSQFT": 1000, "maximumSQFT": 1200,
        })
        assert u.get("sqft") == "1000-1200"

    def test_null_sqft_produces_empty_not_error(self) -> None:
        u = self._parse_single({"floorplanName": "A1", "beds": 1, "sqft": None})
        # sqft absent → falls back to empty, _format_area produces -1 sentinel
        assert u.get("sqft") in ("", None, "-")


# ══════════════════════════════════════════════════════════════════════════════
# Fix 5 — DOM sqft regex bounds + context guard
# ══════════════════════════════════════════════════════════════════════════════


class TestDomSqftBounds:
    def test_sub_150_sqft_from_storage_ignored(self) -> None:
        """'100 sq ft storage' must not capture 100 as the unit sqft."""
        text = "$1,500/mo · 1 bed · 1 bath · 100 sq ft storage included"
        result = _container_yields_unit(text)
        # Should return None (no valid sqft, and beds signal is the
        # fallback so the unit CAN still be emitted, but sqft must be empty)
        if result is not None:
            assert result.get("sqft") == ""

    def test_sub_150_sqft_alone_drops_container(self) -> None:
        """Container with only rent + sub-150 sqft and no beds → dropped."""
        text = "$1,200 · 80 sq ft patio available"
        result = _container_yields_unit(text)
        assert result is None

    def test_balcony_context_word_suppresses_sqft(self) -> None:
        """'50 sq ft balcony' — noise context before number suppresses match."""
        text = "$1,800/mo · 2 bed · 1 bath · 50 sq ft balcony"
        result = _container_yields_unit(text)
        if result is not None:
            assert result.get("sqft") == ""

    def test_valid_unit_sqft_accepted(self) -> None:
        """Real unit sqft (750) within [150, 10000] is passed through."""
        text = "$1,400/mo · 1 bed · 1 bath · 750 sq ft"
        result = _container_yields_unit(text)
        assert result is not None
        assert result.get("sqft") == "750"

    def test_largest_valid_sqft_wins_over_small_amenity(self) -> None:
        """Container with real sqft + smaller amenity sqft — largest wins."""
        text = "$1,600/mo · 1 bed · 825 sq ft · includes 80 sq ft storage"
        result = _container_yields_unit(text)
        assert result is not None
        assert result.get("sqft") == "825"

    def test_deposit_word_does_not_suppress_sqft(self) -> None:
        """'deposit' is a rent-noise word, not a sqft-noise word.
        A container with '$500 deposit' + '750 sq ft' should still read the sqft.
        """
        text = "$500 deposit · 1 bed · 750 sq ft · $1,200/mo"
        result = _container_yields_unit(text)
        assert result is not None
        assert result.get("sqft") == "750"
