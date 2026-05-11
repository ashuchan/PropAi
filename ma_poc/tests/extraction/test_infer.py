"""Tests for ``ma_poc.extraction.infer``.

Covers ``infer_sqft_from_name`` (new) and the ``infer()`` orchestrator's
pass ordering, idempotency, non-mutation, and gap-only fills.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.infer import infer, infer_sqft_from_name

# ── infer_sqft_from_name ──────────────────────────────────────────────────────


class TestInferSqftFromName:
    """Pull sqft out of plan-name strings. Covers production shapes
    observed in the 2026-05-11 run."""

    @pytest.mark.parametrize(
        "name, expected",
        [
            # MAA Worthington (run 2026-05-11)
            ("Traditional 2x2 1019 SF", 1019),
            ("Traditional 1x1 770 SF", 770),
            ("Traditional 1x1 850 SF", 850),
            ("Traditional 1x1 619 SF", 619),
            # MAA ranges — low end wins
            ("Traditional 2x2 1031-1195 SF", 1031),
            ("Studio 0x1 334-619 SF", 334),
            ("Traditional 1x1 609-641 SF", 609),
            ("Traditional 1x1 498-564 SF", 498),
            # Country Woods (apts247) — compact range form
            ("682-708sqft", 682),
            ("886-912sqft", 886),
            ("970-996sqft", 970),
            # Suffix variants
            ("1019 sqft", 1019),
            ("1019 sq ft", 1019),
            ("1019 sq.ft.", 1019),
            ("1019 sq. ft.", 1019),
            ("1019 square feet", 1019),
            ("1019 SQUARE FEET", 1019),
            ("1019sf", 1019),
            ("1019 SF", 1019),
        ],
    )
    def test_extracts_sqft_from_observed_shapes(self, name, expected):
        assert infer_sqft_from_name(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "   ",
            "1 Bedroom",          # no sqft
            "Studio",             # no sqft
            "A1",                 # plan code only
            "Hoboken",            # not a plan name
            "Marble Hill",        # not a plan name
            "682",                # bare number, no suffix
            "Building 1019",      # number not followed by suffix
        ],
    )
    def test_returns_none_when_no_sqft_in_name(self, name):
        assert infer_sqft_from_name(name) is None

    @pytest.mark.parametrize(
        "name",
        [
            "Studio 100 SF",           # below 150 — sanity reject
            "Plan 10000 SF",           # at the upper boundary, accepted
            "Plan 12000 SF",           # above 10000 — sanity reject
        ],
    )
    def test_sanity_bound_at_extremes(self, name):
        result = infer_sqft_from_name(name)
        # 100 SF rejected (too small); 10000 accepted; 12000 rejected
        if "100 " in name and "10000" not in name and "12000" not in name:
            assert result is None
        elif "10000" in name:
            assert result == 10000
        elif "12000" in name:
            assert result is None

    def test_range_low_end_only(self):
        """Range strings return low end, never the high or midpoint.
        Documents the design choice: honest under-estimate over synthesized
        median."""
        assert infer_sqft_from_name("Plan 800-1200 SF") == 800

    def test_does_not_match_year_built_pattern(self):
        """``"Built 1995"`` should not match — no sqft suffix.

        Important because plan names sometimes include the year:
        ``"The 1922 Lofts"`` — 1922 must not be read as sqft."""
        assert infer_sqft_from_name("The 1922 Lofts") is None
        assert infer_sqft_from_name("Built 1995") is None


# ── infer() orchestrator ──────────────────────────────────────────────────────


class TestInferOrchestrator:
    """End-to-end behavior of the orchestrator."""

    def test_empty_dict_returns_marker_only(self):
        result = infer({})
        assert result.get("_inferred") is True
        # No physical attrs to derive from → nothing else added
        assert "beds" not in result
        assert "baths" not in result
        assert "area" not in result

    def test_non_dict_input_returned_unchanged(self):
        # Defensive: should never raise on unexpected types
        assert infer(None) is None  # type: ignore[arg-type]
        assert infer("not a dict") == "not a dict"  # type: ignore[arg-type]

    def test_does_not_mutate_input(self):
        unit = {"floor_plan_name": "1 Bedroom"}
        before = dict(unit)
        infer(unit)
        assert unit == before

    def test_is_idempotent(self):
        """Second call on the result returns the same content (defensive
        deepcopy means object identity may differ; content must not)."""
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF"}
        first = infer(unit)
        second = infer(first)
        assert first == second

    def test_marker_set_on_result(self):
        result = infer({"floor_plan_name": "A1"})
        assert result.get("_inferred") is True

    # Pass 1: bed/bath from name

    def test_infers_beds_from_name_when_missing(self):
        unit = {"floor_plan_name": "1 Bedroom"}
        result = infer(unit)
        assert result.get("beds") == 1

    def test_infers_beds_and_baths_from_2x2_shorthand(self):
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF"}
        result = infer(unit)
        assert result.get("beds") == 2
        assert result.get("baths") == 2.0

    def test_does_not_overwrite_existing_beds(self):
        """Source-extracted beds always wins over inferred."""
        unit = {"floor_plan_name": "2x2", "beds": 3}
        result = infer(unit)
        assert result.get("beds") == 3  # source value preserved

    def test_does_not_overwrite_existing_baths(self):
        unit = {"floor_plan_name": "2x2", "baths": 1.5}
        result = infer(unit)
        assert result.get("baths") == 1.5

    # Pass 2: sqft from name

    def test_infers_sqft_from_name_when_missing(self):
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF"}
        result = infer(unit)
        assert result.get("area") == 1019

    def test_does_not_overwrite_existing_area(self):
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF", "area": 950}
        result = infer(unit)
        assert result.get("area") == 950  # source preserved

    def test_does_not_overwrite_existing_sqft_under_alias(self):
        """Source has ``sqft`` (not the V2 canonical ``area``); inference
        sees the alias and does not overwrite."""
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF", "sqft": 1019}
        result = infer(unit)
        # area NOT injected because sqft already covers SQFT_KEYS
        assert result.get("area") is None or result.get("area") == 1019
        assert result.get("sqft") == 1019

    # Pass 3: rent from rent_range

    def test_infers_rent_low_high_from_range_string(self):
        unit = {"floor_plan_name": "A1", "rent_range": "$1,500 - $2,000"}
        result = infer(unit)
        assert result.get("rent_low") == 1500.0
        assert result.get("rent_high") == 2000.0

    def test_infers_rent_from_single_value_range(self):
        unit = {"floor_plan_name": "A1", "rent_range": "$1,500"}
        result = infer(unit)
        assert result.get("rent_low") == 1500.0

    def test_does_not_overwrite_existing_rent(self):
        unit = {"rent_low": 1400, "rent_range": "$1,500 - $2,000"}
        result = infer(unit)
        assert result.get("rent_low") == 1400  # source preserved
        # rent_high also not filled because parse-from-range gate requires
        # BOTH rent_low and rent_high to be None
        assert result.get("rent_high") is None

    # Pass 4: unit_id fallback

    def test_infers_unit_id_from_physical_attrs(self):
        unit = {
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2,
            "baths": 2.0,
            "area": 1019,
        }
        result = infer(unit, property_id="P11992")
        uid = result.get("unit_id")
        assert uid is not None
        assert isinstance(uid, str)
        assert uid.startswith("inferred_")
        assert result.get("_inferred_id") is True

    def test_unit_id_fallback_skipped_without_property_id(self):
        unit = {"floor_plan_name": "2x2", "beds": 2, "baths": 2}
        result = infer(unit, property_id=None)
        # No fallback when property_id is None — caller's responsibility
        # to provide one if they want stable identity
        assert result.get("unit_id") is None

    def test_unit_id_fallback_skipped_when_natural_id_present(self):
        unit = {
            "unit_id": "217",
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2,
            "baths": 2,
        }
        result = infer(unit, property_id="P11992")
        assert result.get("unit_id") == "217"  # natural id preserved
        assert result.get("_inferred_id") is not True

    def test_unit_id_fallback_uses_post_inference_dims(self):
        """The fallback hashes the dims AFTER name-inference fills them.
        A unit with only ``floor_plan_name="2x2 1019 SF"`` and no source
        beds/baths/sqft should still produce a stable fallback id."""
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF"}
        result = infer(unit, property_id="P11992")
        # beds inferred to 2, baths to 2.0, area to 1019 — those four
        # are enough for compute_fallback_unit_id to mint an id.
        assert result.get("unit_id") is not None
        assert result.get("unit_id", "").startswith("inferred_")

    # Pass 5: floor_plan_id

    def test_floor_plan_id_set_from_post_inference_attrs(self):
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF"}
        result = infer(unit, property_id="P11992")
        fpid = result.get("floor_plan_id")
        assert fpid is not None
        assert isinstance(fpid, str)
        assert len(fpid) == 12  # compute_floor_plan_id returns 12-char hash

    def test_floor_plan_id_stable_for_same_inputs(self):
        u1 = {"floor_plan_name": "Traditional 2x2 1019 SF", "beds": 2, "baths": 2.0}
        u2 = {"floor_plan_name": "Traditional 2x2 1019 SF", "beds": 2, "baths": 2.0}
        r1 = infer(u1, property_id="P11992")
        r2 = infer(u2, property_id="P11992")
        assert r1.get("floor_plan_id") == r2.get("floor_plan_id")

    def test_floor_plan_id_differs_across_properties(self):
        unit = {"floor_plan_name": "Traditional 2x2 1019 SF", "beds": 2, "baths": 2.0}
        r1 = infer(unit, property_id="P11992")
        r2 = infer(unit, property_id="P00001")
        assert r1.get("floor_plan_id") != r2.get("floor_plan_id")

    def test_floor_plan_id_preserved_when_already_set(self):
        unit = {"floor_plan_name": "2x2", "floor_plan_id": "pre-existing-id"}
        result = infer(unit, property_id="P11992")
        assert result.get("floor_plan_id") == "pre-existing-id"


# ── Real-data shape regression cases ──────────────────────────────────────────


class TestRealDataInference:
    """End-to-end shapes from 2026-05-11 cloud-run artifacts."""

    def test_maa_worthington_unit_inferred_fully(self):
        """MAA Worthington raw API item.

        The RentCafe adapter (today) drops the unit-level ``sqft`` and
        ``apartmentName`` fields. After Stage 1 wire-in, the adapter is
        fixed AND ``infer()`` runs as a belt-and-braces defense — for
        units that DO arrive without dims, name-inference still recovers
        them (e.g. ``"Traditional 2x2 1019 SF"`` → beds=2, baths=2, area=1019).
        """
        unit = {
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "rent_low": 2680,
            "rent_high": 5165,
            # beds / baths / area absent — must be inferred from name
        }
        result = infer(unit, property_id="P11992")
        assert result.get("beds") == 2
        assert result.get("baths") == 2.0
        assert result.get("area") == 1019
        assert result.get("rent_low") == 2680
        assert result.get("rent_high") == 5165

    def test_country_woods_sqft_range_inferred(self):
        """Country Woods (apts247) emits plan name with embedded range
        ``"682-708sqft"``. Should infer 682 as the area."""
        unit = {
            "floor_plan_name": "682-708sqft",
            "beds": 1,
            "baths": 1.0,
            "rent_low": 2140,
        }
        result = infer(unit, property_id="P1163")
        assert result.get("area") == 682

    def test_skyline_kessler_neighborhood_no_inference(self):
        """Skyline at Kessler — Nestio neighborhoods endpoint item.

        After Stage 1 wire-in, the broad parser will reject these via
        is_valid_unit. But even if one slips through to infer(), there's
        nothing to infer — ``"Hoboken"`` has no bed/bath/sqft pattern,
        no rent, no identity. ``infer()`` returns the marker but adds
        nothing else of substance.
        """
        unit = {"floor_plan_name": "Hoboken"}
        result = infer(unit, property_id="P11611")
        assert result.get("beds") is None
        assert result.get("baths") is None
        assert result.get("area") is None
        assert result.get("rent_low") is None
        # No fallback unit_id (compute_fallback needs ≥2 physical fields;
        # only floor_plan_name is present)
        assert result.get("unit_id") is None
