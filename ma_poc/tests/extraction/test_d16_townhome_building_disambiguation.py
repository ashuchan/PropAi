"""D16 test 10 — building key disambiguates townhome unit-number reuse.

A townhome complex often reuses unit numbers across buildings:
``Building A unit 1``, ``Building B unit 1`` are distinct physical
apartments. The cross-page dedup pass keys on ``(building, unit_number)``
to keep them separate.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import post_process


class TestBuildingDisambiguation:
    def test_same_unit_number_different_building_not_collapsed(self):
        items = [
            {
                "unit_number": "1",
                "building": "A",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
            },
            {
                "unit_number": "1",
                "building": "B",
                "available_date": "2026-06-01",
                "beds": 1,
                "area": 750,
            },
        ]
        result = post_process(items, property_id="P1")
        # Two physically distinct apartments must survive as separate units.
        assert len(result.units) == 2
        assert result.cross_page_dedup_collapses == 0

    def test_same_unit_number_same_building_collapses(self):
        """Within one building, same unit_number → same apartment."""
        items = [
            {
                "unit_number": "1",
                "building": "A",
                "floor_plan_name": "1BR",
                "beds": 1,
                "area": 750,
            },
            {
                "unit_number": "1",
                "building": "A",
                "floor_plan_name": "1BR-Alt",  # different fp from second page
                "beds": 1,
                "area": 750,
                "rent_low": 1500,  # this page has rent
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        assert result.cross_page_dedup_collapses == 1

    def test_no_building_collapses_normally(self):
        """When neither row carries a building, they share the empty-
        string namespace and collapse normally (the common case)."""
        items = [
            {
                "unit_number": "1",
                "floor_plan_name": "1A",
                "beds": 1,
                "area": 750,
            },
            {
                "unit_number": "1",
                "floor_plan_name": "1B",
                "beds": 1,
                "area": 750,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        assert result.cross_page_dedup_collapses == 1

    def test_building_alias_buildingname(self):
        """``buildingName`` is a recognised alias for ``building``."""
        items = [
            {
                "unit_number": "1",
                "buildingname": "Tower A",
                "available_date": "2026-06-01",
                "beds": 1,
            },
            {
                "unit_number": "1",
                "buildingname": "Tower B",
                "available_date": "2026-06-01",
                "beds": 1,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 2

    def test_building_case_insensitive(self):
        """``"A"`` and ``"a"`` are the same building name."""
        items = [
            {
                "unit_number": "1",
                "building": "A",
                "floor_plan_name": "Plan A",
                "beds": 1,
                "area": 750,
            },
            {
                "unit_number": "1",
                "building": "a",  # case variation
                "floor_plan_name": "Plan B",
                "beds": 1,
                "area": 750,
            },
        ]
        result = post_process(items, property_id="P1")
        assert len(result.units) == 1
        assert result.cross_page_dedup_collapses == 1
