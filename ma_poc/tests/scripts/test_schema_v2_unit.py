"""F10: schema_v2 _format_v2_unit pass-through for concessions, amenities,
and validation provenance flags. H16, H17 invariants."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ma_poc.core.schema_v2 import _format_v2_unit, _normalize_amenities


_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)


def test_h16_v2_unit_schema_includes_new_keys() -> None:
    """All F10 keys are always present (None when unset) so downstream
    readers see a stable schema."""
    out = _format_v2_unit({"floor_plan_name": "A1"}, _TS)
    for key in (
        "concession_text",
        "concession_value",
        "concession_source",
        "amenities",
        "_inferred_id",
        "_date_placeholder",
    ):
        assert key in out, f"F10 key {key} missing from _format_v2_unit output"


def test_h17_concession_text_passthrough() -> None:
    """LLM-tier output with concession_text survives the v2 transform."""
    unit = {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession_text": "$500 off first month",
        "concession_value": 500.0,
        "concession_source": "specials_section",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "$500 off first month"
    assert out["concession_value"] == 500.0
    assert out["concession_source"] == "specials_section"


def test_sightmap_specials_description_maps_to_concession_text() -> None:
    """SightMap stores the specials text under specials_description; F10
    surfaces it as concession_text when no canonical key is present."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "specials_description": "1 month free on 13-month lease",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "1 month free on 13-month lease"


def test_legacy_concession_key_maps_to_canonical() -> None:
    """A unit dict carrying the legacy `concession` (no `_text` suffix) key
    surfaces under the canonical name. SightMap path uses this shape."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession": "Move-in special",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] == "Move-in special"


def test_concession_dict_value_does_not_poison_text_field() -> None:
    """Bug-hunt regression: some legacy adapter paths emit ``concession``
    as a dict ({"description": "...", "value": 500}). build_concessions_report
    iterates string content, so non-string values must coerce to None
    rather than ending up in the report.
    """
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession": {"description": "Move-in special", "value": 500},
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] is None  # dict coerced away


def test_empty_string_concession_text_normalizes_to_none() -> None:
    """``concession_text=""`` must become None so the report's
    ``properties_with_any_concession`` count doesn't get inflated by
    empty strings adapters might leak through."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession_text": "",
        "concession": "   ",  # whitespace-only legacy value
    }
    out = _format_v2_unit(unit, _TS)
    assert out["concession_text"] is None


def test_amenities_normalized_and_deduped() -> None:
    """Adapter emits amenities with mixed casing; v2 output normalizes
    and de-duplicates."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "amenities": ["Pool", "pool ", "POOL", "Gym"],
    }
    out = _format_v2_unit(unit, _TS)
    assert out["amenities"] == ["pool", "gym"]


def test_inferred_id_flag_passthrough() -> None:
    """Schema-gate-set _inferred_id propagates to v2 output."""
    unit = {
        "unit_id": "inferred_aabbccddeeff0011",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "_inferred_id": True,
    }
    out = _format_v2_unit(unit, _TS)
    assert out["_inferred_id"] is True


def test_date_placeholder_flag_passthrough() -> None:
    """F4 placeholder string surfaces under the canonical key."""
    unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "_date_placeholder": "Spring 2026",
    }
    out = _format_v2_unit(unit, _TS)
    assert out["_date_placeholder"] == "Spring 2026"
    # available_date should be None when only the placeholder is present
    assert out["available_date"] is None


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ([], None),
    ("not a list", None),
    (["pool", "gym"], ["pool", "gym"]),
    (["Pool", "  pool  ", "pool"], ["pool"]),
    (["Pool", 42, "Gym"], ["pool", "gym"]),  # non-strings filtered
])
def test_normalize_amenities_table_driven(raw, expected) -> None:
    assert _normalize_amenities(raw) == expected
