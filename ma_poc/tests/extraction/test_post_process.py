"""Tests for ``ma_poc.extraction.post_process``.

End-to-end coverage of the Stage 1+2 pipeline:
``infer → sanity → is_valid_unit → classify`` composed by ``post_process``.
Real-data fixtures from the 2026-05-11 cloud-run regressions anchor the
production semantics.

Partition contract: admitted rows split into ``result.units`` (carries a
natural per-apartment identifier) and ``result.plan_summaries`` (floor-
plan-level summaries). ``n_admitted = n_unit_level + n_plan_level``.
"""

from __future__ import annotations

from ma_poc.extraction.post_process import PostProcessResult, post_process

# ── Basic orchestration ───────────────────────────────────────────────────────


class TestPostProcessBasics:
    def test_empty_list_returns_empty_result(self):
        result = post_process([])
        assert isinstance(result, PostProcessResult)
        assert result.units == []
        assert result.rejected == []
        assert result.n_admitted == 0
        assert result.n_rejected == 0

    def test_none_input_returns_empty_result(self):
        result = post_process(None)
        assert result.units == []
        assert result.rejected == []

    def test_non_list_iterable_handled(self):
        """Generators / tuples should work too — anything iterable."""
        gen = ({"beds": i, "baths": 1} for i in range(3))
        result = post_process(gen, property_id="P1")
        assert result.n_admitted == 3

    def test_non_dict_entries_rejected_not_raised(self):
        """Defensive: malformed payloads with non-dict entries get logged
        in rejected, not raise."""
        items = [{"beds": 1, "baths": 1}, None, "bogus", 42, {"beds": 2}]
        result = post_process(items, property_id="P1")
        assert result.n_admitted == 2  # the two dicts with beds
        assert result.n_rejected == 3
        # Each non-dict rejection carries "NOT_A_DICT" reason
        for unit, reasons in result.rejected:
            assert "NOT_A_DICT" in reasons


# ── End-to-end pipeline correctness ──────────────────────────────────────────


class TestPostProcessPipeline:
    def test_unit_with_dim_only_routes_to_plan(self):
        """A row with dims but no per-apartment identity (no unit_id,
        no available_date, no floor, no building) is plan-level."""
        items = [{"beds": 2, "baths": 2, "area": 1019, "rent_low": 1500}]
        result = post_process(items, property_id="P1")
        assert result.n_admitted == 1
        assert result.n_unit_level == 0
        assert result.n_plan_level == 1
        assert result.n_rejected == 0

    def test_unit_with_natural_id_routes_to_units(self):
        """A row with a real ``unit_id`` (not inferred) is unit-level."""
        items = [{"unit_id": "217", "beds": 2, "baths": 2, "area": 1019}]
        result = post_process(items, property_id="P1")
        assert result.n_unit_level == 1
        assert result.n_plan_level == 0

    def test_unit_with_available_date_routes_to_units(self):
        """Per-apartment availability signals unit-level."""
        items = [{"available_date": "2026-05-15", "beds": 2, "area": 1019}]
        result = post_process(items, property_id="P1")
        assert result.n_unit_level == 1

    def test_unit_without_dim_rejected(self):
        """Skyline at Kessler neighborhood — no dims, no rent — rejected."""
        items = [{"floor_plan_name": "Hoboken"}]
        result = post_process(items, property_id="P11611")
        assert result.n_admitted == 0
        assert result.n_rejected == 1
        rejected_unit, reasons = result.rejected[0]
        assert "NO_BEDS" in reasons
        assert "NO_BATHS" in reasons
        assert "NO_SQFT" in reasons

    def test_inference_recovers_dims_from_name(self):
        """``floor_plan_name="1 Bedroom"`` alone — pre-infer would fail
        validity. post_process infers beds=1, admits the row — as
        plan-level (no per-apartment identity)."""
        items = [{"floor_plan_name": "1 Bedroom"}]
        result = post_process(items, property_id="P1")
        assert result.n_admitted == 1
        # Inferred row routes to plan_summaries (no natural identity)
        assert result.n_plan_level == 1
        assert result.plan_summaries[0].get("beds") == 1

    def test_sanity_nulls_bad_field_but_keeps_row(self):
        """beds=99 (bad) but baths=2 (good) — sanity nulls beds, validity
        admits via baths. The countryridge sqft=100 pattern: area=100 →
        nulled (below 150 floor), beds=2 → admitted (as plan-level since
        no per-apartment identity)."""
        items = [{"floor_plan_name": "2BR/1.5BA", "beds": 99, "baths": 1.5, "area": 100}]
        result = post_process(items, property_id="P1")
        assert result.n_admitted == 1
        # No unit_id / available_date → plan-level
        admitted = result.plan_summaries[0]
        # beds nulled (99 out of range)
        assert admitted.get("beds") is None
        # area nulled (100 below 150 floor)
        assert admitted.get("area") is None
        # baths preserved
        assert admitted.get("baths") == 1.5
        # Sanity drops recorded
        dropped = admitted.get("_sanity_dropped", [])
        assert "beds" in dropped
        assert "area" in dropped

    def test_skyline_kessler_258_city_list_all_rejected(self):
        """Skyline at Kessler regression: Nestio neighborhoods endpoint
        returned 258 items, each with only ``name`` + geographic ``area``
        (a string). post_process must reject all 258."""
        # Real shape from run 2026-05-11/shard_0/11611.md
        neighborhoods = [
            {"id": 306, "name": "Bayonne", "area": "Hudson County",
             "city": "Bayonne", "state": "NJ"},
            {"id": 304, "name": "Hoboken", "area": "Hudson County",
             "city": "Hoboken", "state": "NJ"},
            {"id": 282, "name": "Bedford Park", "area": "None",
             "city": "Bronx", "state": "NY"},
            # ... 255 more in the real response
        ]
        result = post_process(neighborhoods, property_id="P11611")
        assert result.n_admitted == 0
        assert result.n_rejected == 3

    def test_maa_worthington_units_all_unit_level(self):
        """MAA Worthington: real RentCafe-backed units with apartmentName
        per row → unit-level."""
        items = [
            {
                "id": "01KGMK3HTJ0YW5MZ45EWDR05PP",
                "apartmentName": "217",
                "floorplanName": "Traditional 2x2 1019 SF",
                "beds": 2, "baths": 2, "sqft": 1019,
                "minimumRent": 2680, "maximumRent": 5165,
                "availableDate": "05/23/2026",
            },
            {
                "id": "01KGMK3HTJ109M7KS3CGSYGXZX",
                "apartmentName": "302",
                "floorplanName": "Traditional 1x1 770 SF",
                "beds": 1, "baths": 1, "sqft": 720,
                "minimumRent": 1940, "maximumRent": 3270,
            },
        ]
        result = post_process(items, property_id="P11992")
        assert result.n_admitted == 2
        assert result.n_unit_level == 2
        assert result.n_plan_level == 0
        assert result.n_rejected == 0

    def test_country_woods_plans_all_plan_level(self):
        """Country Woods (apts247) plan rows. No apartmentName / unit_id —
        these are floor-plan summaries, not per-apartment units. Routes
        to plan_summaries under Stage 2 partition."""
        items = [
            {"floor_plan_name": "682-708sqft", "beds": 1, "baths": 1, "rent_low": 2140},
            {"floor_plan_name": "886-912sqft", "beds": 2, "baths": 1, "rent_low": 2675},
            {"floor_plan_name": "970-996sqft", "beds": 2, "baths": 2, "rent_low": 2925},
        ]
        result = post_process(items, property_id="P1163")
        assert result.n_admitted == 3
        assert result.n_plan_level == 3
        # Each plan got area inferred from its name
        for u in result.plan_summaries:
            assert u.get("area") in (682, 886, 970)

    def test_biltmore_dimless_merged_phantoms_rejected(self):
        """Biltmore (10854) — 6 merged-cross-page phantom rows with no
        dims. All rejected even though they carry unit_id strings."""
        items = [
            {"floor_plan_name": "B2", "unit_id": "203", "available_date": "2026-05-07"},
            {"floor_plan_name": "C1", "unit_id": "104"},
            {"floor_plan_name": "A1", "unit_id": "212"},
        ]
        result = post_process(items, property_id="P10854")
        # No dims (no beds, no baths, no area, plan name alone is not enough)
        assert result.n_admitted == 0
        assert result.n_rejected == 3

    def test_jsonld_offer_admitted_as_plan_level(self):
        """JSON-LD Offer with rent + beds but no per-apartment id — Luxe 88
        shape. Stage 2 routes to plan_summaries (the row describes a plan,
        not a specific apartment)."""
        items = [
            {"floor_plan_name": "Dean /D", "rent_low": 1629, "beds": 1, "baths": 1},
        ]
        result = post_process(items, property_id="P_LUXE_88")
        assert result.n_admitted == 1
        assert result.n_plan_level == 1
        assert result.n_unit_level == 0


# ── Idempotency ───────────────────────────────────────────────────────────────


class TestPostProcessIdempotency:
    def test_admitted_unit_level_pass_second_call(self):
        """Unit-level rows re-run through post_process stay unit-level."""
        items = [{"unit_id": "217", "beds": 2, "baths": 2, "area": 1019}]
        first = post_process(items, property_id="P1")
        assert first.n_unit_level == 1
        second = post_process(first.units, property_id="P1")
        assert second.n_unit_level == first.n_unit_level
        assert second.n_plan_level == 0
        assert second.n_rejected == 0

    def test_admitted_plan_level_pass_second_call(self):
        """Plan-level rows re-run stay plan-level."""
        items = [{"beds": 2, "baths": 2, "area": 1019, "rent_low": 1500}]
        first = post_process(items, property_id="P1")
        assert first.n_plan_level == 1
        second = post_process(first.plan_summaries, property_id="P1")
        assert second.n_plan_level == first.n_plan_level
        assert second.n_unit_level == 0
        assert second.n_rejected == 0


# ── Non-mutation ──────────────────────────────────────────────────────────────


class TestPostProcessNonMutation:
    def test_does_not_mutate_input_list(self):
        items = [{"floor_plan_name": "1 Bedroom"}]
        before = [dict(u) for u in items]
        post_process(items, property_id="P1")
        assert items == before

    def test_admitted_unit_is_a_new_object(self):
        """The admitted row must be a fresh object so caller-side
        mutation doesn't leak back to upstream extractor state."""
        items = [{"unit_id": "217", "beds": 2}]   # natural id → unit-level
        result = post_process(items, property_id="P1")
        assert result.units[0] is not items[0]


# ── Result API ────────────────────────────────────────────────────────────────


class TestPostProcessResultAPI:
    def test_n_counts_match_lists(self):
        items = [
            {"unit_id": "217", "beds": 2},     # unit-level admit
            {"floor_plan_name": "Hoboken"},    # reject (no dim)
            {"beds": 1, "area": 800},          # plan-level admit (no natural id)
        ]
        result = post_process(items, property_id="P1")
        assert result.n_unit_level == len(result.units)
        assert result.n_plan_level == len(result.plan_summaries)
        assert result.n_admitted == result.n_unit_level + result.n_plan_level
        assert result.n_rejected == len(result.rejected)
        assert result.n_admitted == 2
        assert result.n_rejected == 1
        assert result.n_unit_level == 1
        assert result.n_plan_level == 1
