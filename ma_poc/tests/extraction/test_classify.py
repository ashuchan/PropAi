"""Tests for ``ma_poc.extraction.classify``.

Covers unit-level vs plan-level routing across the 2026-05-11 cloud-run
shapes: MAA Worthington (real unit_id), Luxe 88 JSON-LD (plan-only),
Country Woods (no per-unit signal), inferred-fallback rows.
"""

from __future__ import annotations

import pytest

from ma_poc.extraction.classify import classify
from ma_poc.models.source import FieldValue, SourceId


# ── Unit-level recognition ────────────────────────────────────────────────────


class TestClassifyUnitLevel:
    def test_natural_unit_id_is_unit_level(self):
        """A row carrying a real ``unit_id`` (not inferred) is unit-level."""
        assert classify({"unit_id": "217", "beds": 2}) == "unit"

    def test_natural_apartment_name_is_unit_level(self):
        """MAA Worthington's ``apartmentName`` field resolves via UID alias
        and is treated as a real unit identifier."""
        assert classify({"apartmentName": "217", "beds": 2}) == "unit"

    def test_available_date_signals_unit_level(self):
        """A per-apartment availability date implies the row describes a
        specific physical unit, even without an explicit unit_id."""
        assert classify({"available_date": "2026-05-15", "beds": 2}) == "unit"

    def test_floor_signals_unit_level(self):
        """A specific ``floor`` (not "studio") indicates per-unit identity."""
        assert classify({"floor": "2nd Floor", "beds": 2}) == "unit"

    def test_building_signals_unit_level(self):
        assert classify({"building": "Bldg A", "beds": 2}) == "unit"


# ── Plan-level recognition ────────────────────────────────────────────────────


class TestClassifyPlanLevel:
    def test_no_unit_id_no_per_unit_signal_is_plan_level(self):
        """Row with only floor_plan_name + dims → plan-level summary."""
        assert classify({"floor_plan_name": "1BR", "beds": 1, "area": 750}) == "plan"

    def test_inferred_unit_id_is_plan_level(self):
        """``inferred_<hash>`` IDs are computed from physical attributes,
        not extracted from per-apartment sources. They are plan-level."""
        assert classify({"unit_id": "inferred_abc123", "beds": 1}) == "plan"

    def test_inferred_id_marker_is_plan_level(self):
        """``_inferred_id=True`` is the explicit marker."""
        unit = {"unit_id": "abc", "_inferred_id": True, "beds": 1}
        assert classify(unit) == "plan"

    def test_field_value_with_identity_fallback_source_is_plan_level(self):
        """Merger output carries unit_id as a FieldValue. When the source is
        ``IDENTITY_FALLBACK``, the id was computed, not extracted."""
        unit = {
            "unit_id": FieldValue(
                value="inferred_xyz",
                source=SourceId.IDENTITY_FALLBACK,
                confidence=0.65,
            ),
            "beds": 1,
        }
        assert classify(unit) == "plan"

    def test_field_value_with_real_source_is_unit_level(self):
        """A FieldValue sourced from a real API path → unit-level."""
        unit = {
            "unit_id": FieldValue(
                value="217",
                source=SourceId.API_RENTCAFE_FLOORPLANS,
                confidence=0.95,
            ),
            "beds": 2,
        }
        assert classify(unit) == "unit"

    def test_dimensions_only_no_identity_is_plan_level(self):
        assert classify({"beds": 1, "baths": 1, "area": 750}) == "plan"


# ── Real-data regression cases ────────────────────────────────────────────────


class TestRealDataClassify:
    def test_maa_worthington_unit_classified_as_unit(self):
        """MAA Worthington API item — has real ``apartmentName`` per unit."""
        unit = {
            "apartmentname": "217",
            "floor_plan_name": "Traditional 2x2 1019 SF",
            "beds": 2, "baths": 2, "area": 1019,
            "rent_low": 2680, "rent_high": 5165,
            "available_date": "05/23/2026",
        }
        assert classify(unit) == "unit"

    def test_luxe_88_jsonld_offer_classified_as_plan(self):
        """JSON-LD ``Offer`` has rent ranges but no per-apartment identity.
        After ``infer()``, ``unit_id`` is the ``inferred_<hash>`` fallback.
        Classify must route this to plan-level, not unit-level."""
        unit = {
            "floor_plan_name": "Dean /D",
            "rent_low": 1629,
            "beds": 1,
            "unit_id": "inferred_a1b2c3d4e5f6",
            "_inferred_id": True,
        }
        assert classify(unit) == "plan"

    def test_country_woods_plan_no_per_unit_signal_classified_as_plan(self):
        """Country Woods plan rows have beds/baths/area + rent but no
        per-apartment ``apartmentName`` or ``available_date``. Plan-level."""
        unit = {
            "floor_plan_name": "682-708sqft",
            "beds": 1, "baths": 1, "area": 682,
            "rent_low": 2140,
        }
        assert classify(unit) == "plan"


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestClassifyEdgeCases:
    @pytest.mark.parametrize("bad_input", [None, "x", 42, [], ()])
    def test_non_dict_returns_plan(self, bad_input):
        """Defensive: a non-dict input cannot carry unit-level identity."""
        assert classify(bad_input) == "plan"

    def test_empty_dict_returns_plan(self):
        assert classify({}) == "plan"

    def test_empty_string_unit_id_is_not_natural(self):
        """An empty ``unit_id`` string is canonical-absent; not unit-level."""
        assert classify({"unit_id": "", "beds": 1}) == "plan"

    def test_whitespace_unit_id_is_not_natural(self):
        assert classify({"unit_id": "   ", "beds": 1}) == "plan"
