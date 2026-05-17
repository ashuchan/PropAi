"""Contract tests for Fix 1 (2026-05-17 Bug A) — plan_summaries surface
as ``Floor Plans`` (v1) / ``floor_plans`` (v2) in the formatted output.

Pre-fix, ``_format_v1`` / ``_format_v2`` read only ``result["units"]``
and silently dropped ``result["plan_summaries"]``. PIDs 20959, 55317
shipped 6/12 and 5/8 units respectively because the lost rows had valid
rent + AVAILABLE status but no ``available_date`` and were therefore
classified as plan-level by ``post_process``. The classified rows
existed in ``result["plan_summaries"]`` but never reached
``properties.json``.

These tests pin the new emit contract so a future refactor can't drop
the partition again.
"""

from __future__ import annotations

import pytest


def _make_plan_row(fp_name: str, rent: int) -> dict:
    return {
        "floor_plan_name": fp_name,
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 700,
        "market_rent_low": rent,
        "market_rent_high": rent,
        "availability_status": "AVAILABLE",
    }


class TestV2FloorPlansEmit:
    def test_plan_summaries_ship_under_floor_plans(self):
        """v2 output carries ``floor_plans[]`` with one record per
        plan_summary, formatted by the same ``_format_v2_unit`` pipeline
        as ``units``."""
        from ma_poc.scripts.runners.jugnu import _format_v2

        result = {
            "units": [],
            "plan_summaries": [
                _make_plan_row("A1", 1759),
                _make_plan_row("A2", 1794),
            ],
        }
        formatted = _format_v2(result, {})
        assert "floor_plans" in formatted, (
            "_format_v2 must surface plan_summaries under ``floor_plans``; "
            "missing this field is Bug A and silently drops plan-level data."
        )
        assert len(formatted["floor_plans"]) == 2
        # Plan rows preserve their floor_plan_name through the unit-formatter.
        names = sorted(p.get("floor_plan_name") for p in formatted["floor_plans"])
        assert names == ["A1", "A2"]

    def test_empty_plan_summaries_emits_empty_list_not_missing(self):
        """When there are no plan_summaries the field still exists as
        ``[]``, so consumers can rely on its presence."""
        from ma_poc.scripts.runners.jugnu import _format_v2

        result = {"units": [], "plan_summaries": []}
        formatted = _format_v2(result, {})
        assert formatted.get("floor_plans") == []

    def test_units_and_floor_plans_coexist(self):
        """Both partitions ship simultaneously — they describe distinct
        inventory layers (per-apartment vs per-plan-summary)."""
        from ma_poc.scripts.runners.jugnu import _format_v2

        unit_row = {
            "unit_id": "217",
            "floor_plan_name": "A1",
            "bedrooms": 1,
            "market_rent_low": 1604,
            "availability_status": "AVAILABLE",
            "available_date": "2026-06-08",
        }
        result = {
            "units": [unit_row],
            "plan_summaries": [_make_plan_row("B1", 1850)],
        }
        formatted = _format_v2(result, {})
        assert len(formatted["units"]) == 1
        assert len(formatted["floor_plans"]) == 1
        assert formatted["units"][0].get("rent_low") == 1604
        assert formatted["floor_plans"][0].get("rent_low") == 1850


class TestV1FloorPlansEmit:
    def test_plan_summaries_ship_under_floor_plans_v1(self):
        """v1 (46-key) output carries plan_summaries under the
        capitalised ``Floor Plans`` field name to match the v1 convention
        (Property Name, Year Built, etc.)."""
        from ma_poc.scripts.runners.jugnu import _format_v1

        result = {
            "units": [],
            "plan_summaries": [
                _make_plan_row("A1", 1759),
                _make_plan_row("B1", 1850),
            ],
        }
        formatted = _format_v1(result, {})
        assert "Floor Plans" in formatted, (
            "_format_v1 must surface plan_summaries under ``Floor Plans``."
        )
        assert len(formatted["Floor Plans"]) == 2

    def test_missing_plan_summaries_key_handled(self):
        """A result dict without ``plan_summaries`` (legacy / adapter
        that hasn't populated the partition) still formats — ``Floor Plans``
        is an empty list rather than a KeyError."""
        from ma_poc.scripts.runners.jugnu import _format_v1

        result = {"units": []}  # no plan_summaries key at all
        formatted = _format_v1(result, {})
        assert formatted.get("Floor Plans") == []
