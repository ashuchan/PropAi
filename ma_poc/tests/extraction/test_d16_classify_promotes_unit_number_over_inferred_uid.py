"""D16 test 6 — ``classify`` promotes ``unit_number`` to natural identity.

A row with an inferred ``unit_id`` (``inferred_<hash>``) but a real
``unit_number`` is unit-level — the apartment number is genuine even
when the upstream extractor couldn't compute a non-inferred id. This
test pins the promotion behaviour and the plan-code guard that prevents
plan-aggregate rows from sneaking in via the same path.
"""

from __future__ import annotations

from ma_poc.extraction.classify import classify


class TestUnitNumberPromotion:
    def test_inferred_uid_with_real_unit_number_is_unit(self):
        """Row with inferred unit_id + real unit_number → unit-level."""
        unit = {
            "unit_id": "inferred_abc123",
            "_inferred_id": True,
            "unit_number": "618",
            "beds": 1,
            "area": 750,
        }
        assert classify(unit) == "unit"

    def test_inferred_uid_no_unit_number_is_plan(self):
        """Row with inferred unit_id and no unit_number → plan-level."""
        unit = {
            "unit_id": "inferred_abc123",
            "_inferred_id": True,
            "beds": 1,
            "area": 750,
        }
        assert classify(unit) == "plan"

    def test_unit_number_alias_apartmentname_promotes(self):
        """MAA-style apartmentName field is a unit_number alias."""
        unit = {
            "unit_id": "inferred_xyz",
            "_inferred_id": True,
            "apartmentname": "217",
            "beds": 2,
        }
        assert classify(unit) == "unit"


class TestPlanCodeGuard:
    def test_unit_number_matching_plan_code_denied(self):
        """Row with unit_number="2B4" denied promotion when "2B4" is a
        known plan code for the property."""
        unit = {
            "unit_id": "inferred_xyz",
            "_inferred_id": True,
            "unit_number": "2B4",
            "beds": 2,
            "area": 1100,
        }
        plan_codes = frozenset({"2b4", "1a2", "1a7"})  # lowercased
        assert classify(unit, plan_codes=plan_codes) == "plan"

    def test_unit_number_not_in_plan_codes_admitted(self):
        """Row with unit_number="618" promoted normally when 618 is not
        in the plan_codes set."""
        unit = {
            "unit_id": "inferred_xyz",
            "_inferred_id": True,
            "unit_number": "618",
            "beds": 2,
            "area": 1100,
        }
        plan_codes = frozenset({"2b4", "1a2", "1a7"})
        assert classify(unit, plan_codes=plan_codes) == "unit"

    def test_empty_plan_codes_no_op(self):
        """An empty/None plan_codes set means no guard fires."""
        unit = {
            "unit_id": "inferred_xyz",
            "_inferred_id": True,
            "unit_number": "2B4",
            "beds": 2,
        }
        assert classify(unit, plan_codes=None) == "unit"
        assert classify(unit, plan_codes=frozenset()) == "unit"

    def test_plan_code_check_is_case_insensitive(self):
        """unit_number "2B4" matches plan_code "2b4" after normalisation."""
        unit = {
            "unit_id": "inferred_xyz",
            "_inferred_id": True,
            "unit_number": "2B4",
            "beds": 2,
        }
        plan_codes = frozenset({"2b4"})
        assert classify(unit, plan_codes=plan_codes) == "plan"


class TestBackwardCompat:
    def test_natural_unit_id_still_unit_level(self):
        """The original Path 1 rule still works."""
        assert classify({"unit_id": "217", "beds": 2}) == "unit"

    def test_per_unit_signal_still_unit_level(self):
        """Path 3 (availability_date / floor / building) still fires."""
        assert classify({"available_date": "2026-05-15", "beds": 1}) == "unit"
        assert classify({"floor": "3rd Floor", "beds": 1}) == "unit"

    def test_plan_only_row_still_plan(self):
        assert classify({"floor_plan_name": "1BR", "beds": 1, "area": 750}) == "plan"
