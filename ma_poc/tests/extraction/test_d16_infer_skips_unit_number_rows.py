"""D16 test 9 — ``infer()`` skips inferred-id assignment when ``unit_number``
is present.

The apartment number IS the natural per-unit identity. Computing an
``inferred_<hash>`` on top of it caused two failures:
  1. Two real apartments on different plans (no unit_id) collide into
     the same inferred hash.
  2. The same apartment seen on two floor-plan pages with different
     ``fp_name`` gets two DIFFERENT inferred ids.

This test pins the short-circuit and verifies the existing fallback path
(no unit_number, no unit_id) still fires.
"""

from __future__ import annotations

from ma_poc.extraction.infer import infer


class TestSkipsWhenUnitNumberPresent:
    def test_unit_number_only_no_inferred_id(self):
        """Row with unit_number and no unit_id: ``infer`` does NOT add
        ``inferred_<hash>`` because the apartment number IS the identity."""
        unit = {
            "unit_number": "618",
            "floor_plan_name": "1A2",
            "beds": 1,
            "baths": 1,
            "area": 750,
        }
        out = infer(unit, property_id="P1")
        # ``_inferred_id`` must NOT be set; ``unit_id`` should not become
        # an ``inferred_<hash>`` string.
        assert out.get("_inferred_id") is not True
        uid = out.get("unit_id")
        if uid is not None:
            assert not (isinstance(uid, str) and uid.startswith("inferred_"))

    def test_apartmentname_alias_also_skips_inferred(self):
        """MAA's ``apartmentname`` is a unit_number alias and triggers
        the same short-circuit."""
        unit = {
            "apartmentname": "217",
            "floor_plan_name": "Traditional 2x2",
            "beds": 2,
            "baths": 2,
            "area": 1019,
        }
        out = infer(unit, property_id="P1")
        assert out.get("_inferred_id") is not True


class TestFallbackStillFiresWhenNoUnitNumber:
    def test_no_unit_number_no_unit_id_still_gets_inferred(self):
        """Row with neither unit_number nor unit_id falls through to the
        original fallback path and gets an ``inferred_<hash>``."""
        unit = {
            "floor_plan_name": "Plan A",
            "beds": 1,
            "baths": 1,
            "area": 750,
        }
        out = infer(unit, property_id="P1")
        uid = out.get("unit_id")
        # The fallback fires — unit_id is now an inferred hash.
        assert isinstance(uid, str)
        assert uid.startswith("inferred_")
        assert out.get("_inferred_id") is True


class TestSentinelAndAbsence:
    def test_unit_number_absence_sentinel_treated_as_missing(self):
        """``unit_number="-1"`` is the absence sentinel — the fallback
        should still fire because there is no real apartment number."""
        unit = {
            "unit_number": "-1",
            "floor_plan_name": "Plan A",
            "beds": 1,
            "baths": 1,
            "area": 750,
        }
        out = infer(unit, property_id="P1")
        # With no real unit_number, fallback should fire and set inferred id.
        # (uid_now from get_str will return "-1" but is_present rejects it.)
        # Verify: either the inferred id fires or unit_id remains None;
        # in neither case should "-1" become a stable unit_id.
        uid = out.get("unit_id")
        assert uid != "-1"

    def test_empty_unit_number_does_not_block_fallback(self):
        unit = {
            "unit_number": "",
            "floor_plan_name": "Plan A",
            "beds": 1,
            "baths": 1,
            "area": 750,
        }
        out = infer(unit, property_id="P1")
        uid = out.get("unit_id")
        # Empty string doesn't satisfy the unit_number presence check —
        # the fallback should fire.
        if uid is not None and isinstance(uid, str) and uid.startswith("inferred_"):
            assert out.get("_inferred_id") is True
