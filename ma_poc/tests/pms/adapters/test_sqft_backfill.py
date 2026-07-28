"""Tests for pms/adapters/_sqft_backfill.py.

Covers:
  * build_floorplan_sqft_context — catalog detection, field name variants,
    range parsing, ambiguity resolution
  * backfill_unit_sqft — lookup cascade, no-overwrite guarantee, source tag
  * run_sqft_backfill — end-to-end integration
  * Real-world shape: repli360.com floor-plan aggregate API
"""

from ma_poc.pms.adapters._sqft_backfill import (
    backfill_unit_sqft,
    build_floorplan_sqft_context,
    run_sqft_backfill,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resp(body, url="https://example.com/api/floorplans"):
    return {"url": url, "body": body}


def _unit(beds=1, baths=1, sqft="", fp="Plan A"):
    return {
        "floor_plan_name": fp,
        "bedrooms": str(beds) if beds is not None else "",
        "bathrooms": str(baths) if baths is not None else "",
        "sqft": sqft,
        "unit_number": "101",
        "market_rent_low": 1500,
    }


# ══════════════════════════════════════════════════════════════════════════════
# build_floorplan_sqft_context
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildContext:
    def test_basic_single_sqft_field(self):
        body = [
            {"beds": 1, "baths": 1, "sqft": 750},
            {"beds": 2, "baths": 2, "sqft": 1050},
        ]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx[(1, 1)] == 750
        assert ctx[(2, 2)] == 1050

    def test_squarefeet_field_variant(self):
        body = [{"beds": 1, "baths": 1, "squareFeet": 820}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx[(1, 1)] == 820

    def test_minimumsqft_maximumsqft_range_is_not_collapsed(self):
        body = [{"beds": 2, "baths": 2, "minimumSQFT": 900, "maximumSQFT": 1100}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert (2, 2) not in ctx

    def test_min_sqft_only_used_when_max_absent(self):
        body = [{"beds": 1, "baths": 1, "minimumSQFT": 650}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx[(1, 1)] == 650

    def test_sqft_range_string_is_not_collapsed(self):
        body = [{"beds": 1, "baths": 1, "sqft": "700-800"}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert (1, 1) not in ctx

    def test_studio_label_inferred_as_0_beds(self):
        body = [{"floorplanName": "Studio", "baths": 1, "sqft": 480}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx[(0, 1)] == 480

    def test_ambiguous_sqft_for_same_beds_baths_excluded(self):
        """Two 1BR/1BA plans with different sqft → ambiguous → excluded."""
        body = [
            {"beds": 1, "baths": 1, "sqft": 700},
            {"beds": 1, "baths": 1, "sqft": 850},
        ]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert (1, 1) not in ctx

    def test_close_but_distinct_values_remain_ambiguous(self):
        """Similar plans are still distinct plans, not a median sqft."""
        body = [
            {"beds": 2, "baths": 2, "sqft": 1000},
            {"beds": 2, "baths": 2, "sqft": 1050},  # 5% apart
        ]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert (2, 2) not in ctx

    def test_sub_150_sqft_ignored(self):
        """Values below the _SQFT_FLOOR are not real apartment sqft."""
        body = [{"beds": 1, "baths": 1, "sqft": 100}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert (1, 1) not in ctx

    def test_unit_level_api_not_treated_as_catalog(self):
        """An API where most items have unit_number is unit-level, not a catalog."""
        body = [
            {"unitNumber": "101", "beds": 1, "baths": 1, "sqft": 750, "rent": 1500},
            {"unitNumber": "102", "beds": 1, "baths": 1, "sqft": 750, "rent": 1550},
            {"unitNumber": "201", "beds": 2, "baths": 2, "sqft": 1050, "rent": 2000},
        ]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert not ctx  # unit-level → not a catalog → empty context

    def test_large_item_count_not_treated_as_catalog(self):
        """A list with > 50 items is assumed to be per-unit data."""
        body = [{"beds": 1, "baths": 1, "sqft": 750}] * 60
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert not ctx

    def test_multiple_api_responses_merged(self):
        """Context is built across all captured API responses."""
        body_a = [{"beds": 1, "baths": 1, "sqft": 750}]
        body_b = [{"beds": 2, "baths": 2, "sqft": 1100}]
        ctx = build_floorplan_sqft_context([_resp(body_a), _resp(body_b)])
        assert ctx[(1, 1)] == 750
        assert ctx[(2, 2)] == 1100

    def test_empty_api_responses_returns_empty_context(self):
        assert build_floorplan_sqft_context([]) == {}

    def test_malformed_body_skipped_gracefully(self):
        ctx = build_floorplan_sqft_context([
            _resp("not-json"),
            _resp(None),
            _resp({"wrong": "shape"}),
            _resp([{"beds": 1, "baths": 1, "sqft": 800}]),
        ])
        assert ctx[(1, 1)] == 800

    def test_repli360_shape(self):
        """Real-world repli360.com floor-plan aggregate shape."""
        body = [
            {
                "bedrooms": 0,
                "bathrooms": 1,
                "minimumSqFt": 450,
                "maximumSqFt": 520,
                "minimumRent": 1200,
                "maximumRent": 1400,
                "floorplanName": "Studio",
            },
            {
                "bedrooms": 1,
                "bathrooms": 1,
                "minimumSqFt": 700,
                "maximumSqFt": 800,
                "minimumRent": 1400,
                "maximumRent": 1600,
                "floorplanName": "One Bedroom",
            },
            {
                "bedrooms": 2,
                "bathrooms": 2,
                "minimumSqFt": 1000,
                "maximumSqFt": 1150,
                "minimumRent": 1800,
                "maximumRent": 2100,
                "floorplanName": "Two Bedroom",
            },
        ]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx == {}

    def test_floorsize_nested_dict(self):
        """JSON-LD floorSize dict shape."""
        body = [{"beds": 2, "baths": 2, "floorSize": {"value": 1100, "unitCode": "SQFT"}}]
        ctx = build_floorplan_sqft_context([_resp(body)])
        assert ctx[(2, 2)] == 1100


# ══════════════════════════════════════════════════════════════════════════════
# backfill_unit_sqft
# ══════════════════════════════════════════════════════════════════════════════


class TestBackfillUnitSqft:
    def test_fills_unit_with_empty_sqft(self):
        units = [_unit(beds=1, baths=1, sqft="")]
        ctx = {(1, 1): 750}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 1
        assert units[0]["sqft"] == "750"

    def test_fills_unit_with_null_sqft(self):
        units = [{"bedrooms": "2", "bathrooms": "2", "sqft": None, "unit_number": "201"}]
        ctx = {(2, 2): 1050}
        backfill_unit_sqft(units, ctx)
        assert units[0]["sqft"] == "1050"

    def test_does_not_overwrite_existing_sqft(self):
        units = [_unit(beds=1, baths=1, sqft="900")]
        ctx = {(1, 1): 750}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 0
        assert units[0]["sqft"] == "900"

    def test_fallback_to_beds_only_key(self):
        """If (beds, baths) not in context, try (beds, None)."""
        units = [_unit(beds=2, baths=2, sqft="")]
        ctx = {(2, None): 1100}  # baths not in catalog
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 1
        assert units[0]["sqft"] == "1100"

    def test_tags_filled_unit_with_source(self):
        units = [_unit(beds=1, baths=1, sqft="")]
        ctx = {(1, 1): 750}
        backfill_unit_sqft(units, ctx)
        assert units[0].get("_sqft_backfill_source") == "floorplan_context"

    def test_no_fill_when_context_empty(self):
        units = [_unit(beds=1, baths=1, sqft="")]
        filled = backfill_unit_sqft(units, {})
        assert filled == 0
        assert units[0]["sqft"] == ""

    def test_zero_sentinel_treated_as_missing(self):
        units = [{"bedrooms": "1", "bathrooms": "1", "sqft": "0"}]
        ctx = {(1, 1): 750}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 1

    def test_minus_one_sentinel_treated_as_missing(self):
        units = [{"bedrooms": "2", "bathrooms": "2", "area": -1, "sqft": ""}]
        ctx = {(2, 2): 1050}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 1

    def test_multiple_units_filled_correctly(self):
        units = [
            _unit(beds=0, baths=1, sqft=""),
            _unit(beds=1, baths=1, sqft=""),
            _unit(beds=2, baths=2, sqft=""),
            _unit(beds=2, baths=2, sqft="1000"),  # already has sqft
        ]
        ctx = {(0, 1): 480, (1, 1): 750, (2, 2): 1050}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 3
        assert units[0]["sqft"] == "480"
        assert units[1]["sqft"] == "750"
        assert units[2]["sqft"] == "1050"
        assert units[3]["sqft"] == "1000"  # untouched

    def test_studio_inferred_from_floor_plan_name(self):
        units = [{"floor_plan_name": "Studio", "sqft": "", "unit_number": "S1"}]
        ctx = {(0, 1): 480, (0, None): 480}
        filled = backfill_unit_sqft(units, ctx)
        assert filled == 1
        assert units[0]["sqft"] == "480"


# ══════════════════════════════════════════════════════════════════════════════
# run_sqft_backfill — end-to-end
# ══════════════════════════════════════════════════════════════════════════════


class TestRunSqftBackfill:
    def test_end_to_end_repli360_scenario(self):
        """A unit API plus floor-plan ranges must not invent unit sqft."""
        # API A: unit-level (has unit_number + rent, no sqft)
        unit_level_api = _resp([
            {"unitNumber": "101", "beds": 0, "rent": 1285},
            {"unitNumber": "205", "beds": 1, "rent": 1450},
            {"unitNumber": "310", "beds": 2, "rent": 1850},
        ])
        # API B: floor-plan catalog (has sqft, no unit_number)
        floorplan_api = _resp([
            {"bedrooms": 0, "baths": 1, "minimumSqFt": 450, "maximumSqFt": 520},
            {"bedrooms": 1, "baths": 1, "minimumSqFt": 700, "maximumSqFt": 800},
            {"bedrooms": 2, "baths": 2, "minimumSqFt": 1000, "maximumSqFt": 1100},
        ], url="https://app.repli360.com/admin/get_floorplans")

        units = [
            {"bedrooms": "0", "bathrooms": "1", "sqft": "", "unit_number": "101", "market_rent_low": 1285},
            {"bedrooms": "1", "bathrooms": "1", "sqft": "", "unit_number": "205", "market_rent_low": 1450},
            {"bedrooms": "2", "bathrooms": "2", "sqft": "", "unit_number": "310", "market_rent_low": 1850},
        ]

        ctx_keys, filled = run_sqft_backfill(units, [unit_level_api, floorplan_api])

        assert ctx_keys == 0
        assert filled == 0
        for u in units:
            assert u["sqft"] == ""
            assert "_sqft_backfill_source" not in u

    def test_no_catalog_api_returns_zero_fills(self):
        units = [_unit(beds=1, baths=1, sqft="")]
        ctx_keys, filled = run_sqft_backfill(units, [])
        assert ctx_keys == 0
        assert filled == 0

    def test_already_has_sqft_unchanged(self):
        units = [_unit(beds=1, baths=1, sqft="850")]
        catalog = _resp([{"beds": 1, "baths": 1, "sqft": 750}])
        ctx_keys, filled = run_sqft_backfill(units, [catalog])
        assert filled == 0
        assert units[0]["sqft"] == "850"
