"""Tests pinning the LLM signal passthrough fixes.

Yesterday's audit identified that ``_normalize_units`` silently dropped
8 of the 18 per-unit fields the LLM is asked to return — concessions,
amenities, lease_term, move_in_date, availability_count,
floor_plan_source. The schema_v2 mapper was already wired to consume
them, so the data plumbing was complete; the leak was at one
normalisation function. These tests pin the contract that those
fields now round-trip from raw LLM dict → normalised unit so a future
refactor can't silently re-introduce the regression.
"""

from __future__ import annotations

from ma_poc.services.llm_prompt import normalize_units


def _raw_unit(**overrides: object) -> dict[str, object]:
    """Minimum-viable raw-LLM unit dict — must satisfy the keep-gate
    (unit_id OR floor_plan_name OR market_rent_low) so normalize_units
    actually emits the row."""
    base: dict[str, object] = {
        "unit_id": "U1",
        "floor_plan_name": "Aspen",
        "bedrooms": 1,
        "bathrooms": 1.0,
        "sqft": 750,
        "market_rent_low": 1500,
        "market_rent_high": 1500,
    }
    base.update(overrides)
    return base


def test_amenities_list_passes_through() -> None:
    raw = [_raw_unit(amenities=["pool", "gym", "in-unit-wd"])]
    out = normalize_units(raw)
    assert len(out) == 1
    assert out[0]["amenities"] == ["pool", "gym", "in-unit-wd"]


def test_amenities_filters_non_strings() -> None:
    raw = [_raw_unit(amenities=["pool", 42, None, "  ", "gym"])]
    out = normalize_units(raw)
    # Empty/whitespace strings drop, non-strings drop, valid items kept.
    assert out[0]["amenities"] == ["pool", "gym"]


def test_amenities_missing_yields_none() -> None:
    raw = [_raw_unit()]
    out = normalize_units(raw)
    assert out[0]["amenities"] is None


def test_concession_text_passes_through() -> None:
    raw = [_raw_unit(concession_text="1 month free on 13-month lease")]
    out = normalize_units(raw)
    assert out[0]["concession_text"] == "1 month free on 13-month lease"


def test_concession_value_coerces_to_float() -> None:
    raw = [_raw_unit(concession_value="500")]
    out = normalize_units(raw)
    assert out[0]["concession_value"] == 500.0


def test_concession_value_handles_null_and_empty() -> None:
    for val in (None, "", "null"):
        raw = [_raw_unit(concession_value=val)]
        out = normalize_units(raw)
        assert out[0]["concession_value"] is None


def test_concession_source_enum_passes_through() -> None:
    raw = [_raw_unit(concession_source="strikethrough_dom")]
    out = normalize_units(raw)
    assert out[0]["concession_source"] == "strikethrough_dom"


def test_floor_plan_source_provenance_passes_through() -> None:
    raw = [_raw_unit(floor_plan_source="page_label")]
    out = normalize_units(raw)
    assert out[0]["floor_plan_source"] == "page_label"


def test_availability_count_coerces_to_int() -> None:
    raw = [_raw_unit(availability_count="5")]
    out = normalize_units(raw)
    assert out[0]["availability_count"] == 5


def test_lease_term_coerces_to_int() -> None:
    raw = [_raw_unit(lease_term="12")]
    out = normalize_units(raw)
    assert out[0]["lease_term"] == 12


def test_move_in_date_string_passes_through() -> None:
    raw = [_raw_unit(move_in_date="2026-06-01")]
    out = normalize_units(raw)
    assert out[0]["move_in_date"] == "2026-06-01"


def test_unit_with_no_extras_still_yields_pass_through_keys() -> None:
    """Even when the LLM didn't volunteer extras, every new key must be
    present (None) on the normalised unit so downstream readers see a
    consistent shape."""
    raw = [_raw_unit()]
    out = normalize_units(raw)
    for key in (
        "amenities",
        "concession_text",
        "concession_value",
        "concession_source",
        "floor_plan_source",
        "availability_count",
        "lease_term",
        "move_in_date",
    ):
        assert key in out[0], f"normalize_units must emit '{key}' (got: {list(out[0].keys())})"
        assert out[0][key] is None
