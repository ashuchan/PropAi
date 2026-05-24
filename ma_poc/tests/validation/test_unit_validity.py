"""Tests for ``ma_poc.validation.unit_validity``.

Covers the dimensions-only validity contract with the 2026-05-11
production shapes (Skyline at Kessler neighborhoods, MAA Worthington,
Country Woods, JSON-LD Offers).
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.canonical import ABSENT
from ma_poc.extraction.infer import infer
from ma_poc.models.source import FieldValue, SourceId
from ma_poc.validation.unit_validity import (
    REASON_NO_BATHS,
    REASON_NO_BEDS,
    REASON_NO_SQFT,
    REASON_PLAN_MARKETING_TAGLINE,
    REASON_PLAN_NAME_ONLY,
    REASON_PLAN_NO_DIMENSIONS,
    absence_reasons,
    has_dimension,
    is_substantive_plan,
    is_valid_unit,
    plan_rejection_reason,
)

# ── Basic dimension presence ──────────────────────────────────────────────────


class TestHasDimension:
    def test_with_beds_only(self):
        assert has_dimension({"beds": 1}) is True

    def test_with_baths_only(self):
        assert has_dimension({"baths": 1.5}) is True

    def test_with_area_only(self):
        assert has_dimension({"area": 750}) is True

    def test_empty_dict(self):
        assert has_dimension({}) is False

    def test_only_floor_plan_name(self):
        """Plan name is identity-text, not a dimension."""
        assert has_dimension({"floor_plan_name": "A1"}) is False

    def test_only_unit_id(self):
        """Unit_id is identity, not a dimension."""
        assert has_dimension({"unit_id": "217"}) is False

    def test_only_rent(self):
        """Rent is pricing, not a dimension — explicit user contract."""
        assert has_dimension({"rent_low": 1500}) is False
        assert has_dimension({"rent_low": 1500, "rent_high": 2000}) is False

    def test_zero_beds_qualifies_studio(self):
        """Studios have beds=0. is_present(0) returns True; canonical
        treats zero as a real measurement."""
        assert has_dimension({"beds": 0}) is True

    def test_zero_baths_qualifies(self):
        """0 baths (no separate bathroom — micro-units, dorms) is a real
        value, not absence."""
        assert has_dimension({"baths": 0}) is True

    def test_absent_sentinel_area_does_not_qualify(self):
        """area=-1 is the explicit ABSENT sentinel; canonical treats it
        as not-present; alone it doesn't make a row valid."""
        assert has_dimension({"area": ABSENT}) is False

    def test_absent_area_plus_real_beds_qualifies(self):
        """area=-1 doesn't help, but beds=2 does — row is valid."""
        assert has_dimension({"area": ABSENT, "beds": 2}) is True


# ── is_valid_unit ─────────────────────────────────────────────────────────────


class TestIsValidUnit:
    def test_real_unit_passes(self):
        unit = {"beds": 2, "baths": 2, "area": 1019, "rent_low": 1500}
        assert is_valid_unit(unit) is True

    def test_studio_passes(self):
        unit = {"beds": 0, "baths": 1, "area": 400, "rent_low": 1200}
        assert is_valid_unit(unit) is True

    def test_floor_plan_name_only_fails(self):
        """Skyline at Kessler bug pre-condition: floor_plan_name='Hoboken'
        with no dimensions → must NOT be a valid unit."""
        assert is_valid_unit({"floor_plan_name": "Hoboken"}) is False

    def test_rent_only_fails(self):
        """Even with rent + no dimensions → not a unit."""
        assert is_valid_unit({"rent_low": 1500, "rent_high": 2000}) is False

    def test_empty_dict_fails(self):
        assert is_valid_unit({}) is False

    def test_non_dict_returns_false(self):
        assert is_valid_unit(None) is False  # type: ignore[arg-type]
        assert is_valid_unit("x") is False  # type: ignore[arg-type]
        assert is_valid_unit(42) is False  # type: ignore[arg-type]

    def test_one_dimension_is_enough(self):
        """beds alone → valid. baths alone → valid. area alone → valid."""
        assert is_valid_unit({"beds": 1}) is True
        assert is_valid_unit({"baths": 1}) is True
        assert is_valid_unit({"area": 500}) is True

    def test_pascalcase_alias_recognized(self):
        """Case-insensitive lookup means Windsor PascalCase passes."""
        assert is_valid_unit({"BedRooms": 1}) is True
        assert is_valid_unit({"MinimumSqft": 750}) is True

    def test_field_value_wrapped_works(self):
        """ProvenancedUnit (post-merger) dicts have FieldValue values."""
        fv = FieldValue(value=2, source=SourceId.API_RENTCAFE_FLOORPLANS, confidence=0.9)
        assert is_valid_unit({"beds": fv}) is True


# ── absence_reasons ──────────────────────────────────────────────────────────


class TestAbsenceReasons:
    def test_empty_dict_all_three_missing(self):
        reasons = absence_reasons({})
        assert REASON_NO_BEDS in reasons
        assert REASON_NO_BATHS in reasons
        assert REASON_NO_SQFT in reasons
        assert len(reasons) == 3

    def test_only_beds_present(self):
        reasons = absence_reasons({"beds": 1})
        assert REASON_NO_BEDS not in reasons
        assert REASON_NO_BATHS in reasons
        assert REASON_NO_SQFT in reasons

    def test_all_dims_present(self):
        reasons = absence_reasons({"beds": 1, "baths": 1, "area": 750})
        assert reasons == []

    def test_non_dict_returns_all_reasons(self):
        # Defensive: callers passing non-dicts get the full reason list
        reasons = absence_reasons(None)  # type: ignore[arg-type]
        assert REASON_NO_BEDS in reasons
        assert REASON_NO_BATHS in reasons
        assert REASON_NO_SQFT in reasons


# ── Real-data regression cases ────────────────────────────────────────────────


class TestRealDataValidity:
    """Production fixtures: pre-condition is infer() has run."""

    def test_skyline_neighborhood_after_infer_rejected(self):
        """Nestio neighborhoods endpoint item, after canonical pipeline.

        Raw item: ``{"id": 306, "name": "Bayonne", "area": "Hudson County",
        "city": "Bayonne", "state": "NJ"}``.

        After infer(): no beds/baths inferable from "Bayonne", area string
        not numeric, no rent in source. is_valid_unit must reject.
        """
        raw = {"id": 306, "name": "Bayonne", "area": "Hudson County",
               "city": "Bayonne", "state": "NJ"}
        # Note: "name" is NOT a floor_plan_name alias, so infer() won't
        # see it as a plan name to derive beds/baths from. Even if it
        # were, "Bayonne" has no bed/bath/sqft pattern.
        inferred = infer(raw, property_id="P11611")
        assert is_valid_unit(inferred) is False

    def test_maa_worthington_unit_after_infer_passes(self):
        """MAA Worthington raw API item. After infer() fills beds/baths/area
        from name (even if source had them, they're confirmed in range),
        is_valid_unit accepts."""
        raw = {
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2,
            "baths": 2,
            "area": 1019,
            "rent_low": 2680,
            "rent_high": 5165,
        }
        inferred = infer(raw, property_id="P11992")
        assert is_valid_unit(inferred) is True

    def test_country_woods_inferred_from_name_passes(self):
        """Country Woods plan with sqft range in name. After infer(),
        area is set to 682 (low end), beds/baths from source. Valid."""
        raw = {
            "floor_plan_name": "682-708sqft",
            "beds": 1,
            "baths": 1,
            "rent_low": 2140,
        }
        inferred = infer(raw, property_id="P1163")
        assert is_valid_unit(inferred) is True

    def test_dimensionless_jsonld_offer_rejected(self):
        """JSON-LD Offer with rent but no per-unit dims. The Luxe 88 case
        from xlsx evidence — every "unit" had area=-1, no beds, no baths.
        is_valid_unit rejects (no dimension). Bug B (plan-level routing)
        will move these into floor_plans[]."""
        raw = {
            "floor_plan_name": "Dean /D",
            "rent_low": 1629,
            # no beds, no baths, no area
        }
        inferred = infer(raw, property_id="P_LUXE_88")
        # Even after infer (which can't extract dims from "Dean /D"),
        # has no dimension. Rejected as unit-level.
        assert is_valid_unit(inferred) is False

    def test_plan_name_with_embedded_beds_passes_after_infer(self):
        """A row that arrives with only ``floor_plan_name="1 Bedroom"`` and
        no other dims is invalid pre-infer (beds=None) but valid post-infer
        (beds=1 derived from name). Demonstrates the inference-before-
        validation principle."""
        raw = {"floor_plan_name": "1 Bedroom"}
        # Pre-infer: invalid (no numeric dimension)
        assert is_valid_unit(raw) is False
        # Post-infer: valid (beds=1 derived from name)
        inferred = infer(raw, property_id="P_TEST")
        assert is_valid_unit(inferred) is True


# ── Boundary cases ────────────────────────────────────────────────────────────


class TestBoundaryCases:
    @pytest.mark.parametrize(
        "unit",
        [
            {"beds": 0, "baths": 0, "area": ABSENT},          # studio
            {"beds": 7, "baths": 0, "area": ABSENT},          # 7BR
            {"beds": ABSENT, "baths": 10, "area": ABSENT},    # baths only at high end
            {"beds": ABSENT, "baths": ABSENT, "area": 150},   # smallest legitimate sqft
        ],
    )
    def test_edge_dimensions_still_valid(self, unit):
        """Boundary in-range values still produce valid units."""
        assert is_valid_unit(unit) is True

    @pytest.mark.parametrize(
        "unit",
        [
            {"beds": ABSENT, "baths": ABSENT, "area": ABSENT},  # all absent
            {"beds": None, "baths": None, "area": None},
            {"beds": "", "baths": "", "area": ""},
            {"beds": "null", "baths": "none", "area": "n/a"},
        ],
    )
    def test_all_dims_absent_rejected(self, unit):
        assert is_valid_unit(unit) is False


# ── Substantive-plan quality gate (2026-05-24) ──────────────────────────────


class TestIsSubstantivePlan:
    """Plan-level rows admitted by ``is_valid_unit`` must also pass a
    stronger quality bar before being shipped as ``floor_plans``.

    Motivated by run 2026-05-24: 252 properties shipped
    ``SUCCESS_PLAN_LEVEL`` with junk plan rows (area=-1, rent=None,
    floor_plan_name=None or a marketing tagline).
    """

    def test_real_rent_passes(self):
        assert is_substantive_plan({"rent_low": 1450}) is True

    def test_real_rent_high_passes(self):
        assert is_substantive_plan({"rent_high": 2000}) is True

    def test_absent_rent_below_floor_rejected(self):
        # rent==1 is in-bounds for sanity but well below the substantive
        # floor — treat as deposit/fee leak, not real rent.
        assert is_substantive_plan({"rent_low": 1}) is False

    def test_real_area_passes(self):
        assert is_substantive_plan({"area": 750}) is True

    def test_absent_area_sentinel_rejected(self):
        # ABSENT (-1) is the explicit "we looked, nothing there" sentinel.
        assert is_substantive_plan({"area": ABSENT}) is False

    def test_beds_plus_real_plan_name_passes(self):
        assert is_substantive_plan({"beds": 2, "floor_plan_name": "A1"}) is True

    def test_beds_zero_studio_name_passes(self):
        # Studio = real plan type even with beds=0.
        assert is_substantive_plan({"beds": 0, "floor_plan_name": "Studio"}) is True

    def test_beds_only_no_name_rejected(self):
        # Run 2026-05-24 PID 1509 parkplacetampa shape: beds=1, area=-1,
        # rent=None, floor_plan_name=None — should not ship as a plan.
        assert is_substantive_plan({"beds": 1, "area": ABSENT}) is False

    def test_marketing_tagline_rejected(self):
        # Run 2026-05-24 PID 257570 moderacherrycreek canonical case.
        plan = {
            "beds": 0,
            "floor_plan_name": "Studio, 1-, 2-, and 3-bedroom homes with loft layouts",
        }
        assert is_substantive_plan(plan) is False

    def test_marketing_tagline_long_phrase_rejected(self):
        plan = {
            "beds": 1,
            "floor_plan_name": "Choose from various spacious 1 and 2 bedroom layouts",
        }
        assert is_substantive_plan(plan) is False

    def test_short_plan_name_with_slash_passes(self):
        # "1BR/1BA" is a legitimate compact plan label, not a tagline.
        assert is_substantive_plan({"beds": 1, "floor_plan_name": "1BR/1BA"}) is True

    def test_empty_dict_rejected(self):
        assert is_substantive_plan({}) is False

    def test_non_dict_rejected(self):
        assert is_substantive_plan(None) is False  # type: ignore[arg-type]
        assert is_substantive_plan("plan") is False  # type: ignore[arg-type]

    def test_real_rent_overrides_missing_name(self):
        # Real rent alone is enough — name is nice-to-have.
        assert is_substantive_plan({"rent_low": 1200, "area": ABSENT}) is True


class TestPlanRejectionReason:
    def test_marketing_tagline_reason(self):
        plan = {
            "beds": 0,
            "floor_plan_name": "Studio, 1-, and 2-bedroom homes available now",
        }
        assert plan_rejection_reason(plan) == REASON_PLAN_MARKETING_TAGLINE

    def test_name_only_reason(self):
        # Has beds but no name, no area, no rent.
        plan = {"beds": 1, "area": ABSENT}
        assert plan_rejection_reason(plan) == REASON_PLAN_NAME_ONLY

    def test_no_dimensions_reason(self):
        # Empty / completely absent row.
        assert plan_rejection_reason({}) == REASON_PLAN_NO_DIMENSIONS
