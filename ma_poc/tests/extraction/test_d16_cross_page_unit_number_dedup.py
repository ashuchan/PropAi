"""D16 test 7 — cross-page unit-number dedup.

Multi-page floor-plan-page crawls emit the same physical apartment from
two pages with different ``floor_plan_name`` values. The post-partition
pass folds these into a single row, merging complementary fields.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestSamePropertyTwoPagesSameUnit:
    def test_two_rows_share_unit_number_collapse_to_one(self):
        """Same unit "618" from two floor-plan pages → 1 unit out."""
        items = [
            {
                "unit_number": "618",
                "floor_plan_name": "1A2",
                "beds": 1,
                "baths": 1,
                "area": 750,
                "rent_low": 1500,
            },
            {
                "unit_number": "618",
                "floor_plan_name": "2B4",  # different fp name on second page
                "beds": 1,
                "baths": 1,
                "area": 750,
                "rent_low": 1500,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        assert result.cross_page_dedup_collapses == 1

    def test_field_level_merge_picks_complementary_values(self):
        """Page A has sqft, Page B has availability_date → merged row has both."""
        items = [
            {
                "unit_number": "618",
                "floor_plan_name": "1A2",
                "beds": 1,
                "baths": 1,
                "area": 750,
                "rent_low": 1500,
                # no available_date
            },
            {
                "unit_number": "618",
                "floor_plan_name": "2B4",
                "beds": 1,
                "baths": 1,
                # no area
                "available_date": "2026-06-01",
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        merged = result.units[0]
        assert merged.get("area") == 750
        assert merged.get("available_date") == "2026-06-01"

    def test_normalisation_collapses_apt_prefix(self):
        """"Apt 618" and "618" collapse to the same bucket."""
        items = [
            {"unit_number": "Apt 618", "beds": 1, "area": 750},
            {"unit_number": "618", "beds": 1, "area": 750},
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        assert result.cross_page_dedup_collapses == 1


class TestDistinctUnitsNotCollapsed:
    def test_different_unit_numbers_kept(self):
        items = [
            {"unit_number": "618", "beds": 1, "area": 750},
            {"unit_number": "619", "beds": 1, "area": 750},
            {"unit_number": "620", "beds": 1, "area": 750},
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 3
        assert result.cross_page_dedup_collapses == 0

    def test_alphanumeric_suffix_distinct(self):
        """``"101A"`` and ``"101B"`` are physically distinct apartments."""
        items = [
            {"unit_number": "101A", "beds": 1, "area": 750},
            {"unit_number": "101B", "beds": 1, "area": 750},
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 2
        assert result.cross_page_dedup_collapses == 0


class TestEdgeCases:
    def test_rows_without_unit_number_bypass_dedup(self):
        """Rows with no recognisable apartment number are kept as-is."""
        items = [
            {"available_date": "2026-06-01", "beds": 1, "area": 750},
            {"available_date": "2026-06-02", "beds": 1, "area": 750},
        ]
        result = post_process(items, property_id="P1")
        # No unit_number on either, so no bucket, no collapses.
        assert result.cross_page_dedup_collapses == 0
        # Both have per-unit signals → classified as units.
        assert len(result.units) == 2

    def test_higher_ranked_row_wins_as_primary(self):
        """When two rows share unit_number, the row with more concrete
        evidence (real unit_id, availability_date, rent) wins."""
        items = [
            {
                "unit_number": "618",
                "floor_plan_name": "1A2",
                "beds": 1,
                # low rank — no availability date, no rent, no real unit_id
            },
            {
                "unit_number": "618",
                "unit_id": "real_unit_618",
                "available_date": "2026-06-01",
                "rent_low": 1500,
                "rent_high": 1600,
                "beds": 1,
                "floor_plan_name": "2B4",
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        primary = result.units[0]
        # The higher-ranked row's unit_id wins.
        assert primary.get("unit_id") == "real_unit_618"
        assert primary.get("available_date") == "2026-06-01"
