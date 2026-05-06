"""Tests for F3 — compute_fallback_unit_id (v2 field names)."""

from __future__ import annotations

from ma_poc.validation.identity_fallback import compute_fallback_unit_id


def _u(**kw: object) -> dict:
    base = {
        "floor_plan_name": None,
        "beds": None,
        "baths": None,
        "area": None,
        "rent_low": None,
        "available_date": None,
    }
    base.update(kw)
    return base


def test_fallback_returns_none_when_all_fields_missing() -> None:
    assert compute_fallback_unit_id(_u(), "P1") is None


def test_fallback_returns_none_when_only_one_field_present() -> None:
    assert compute_fallback_unit_id(_u(beds=1), "P1") is None


def test_fallback_returns_id_when_floor_plan_plus_one_other_field_present() -> None:
    # v2 contract: floor_plan_name is required, plus at least one of
    # beds/baths/area. ``rent_low`` is mutable and intentionally NOT an
    # identifying field, so it does not satisfy the second-anchor rule.
    result = compute_fallback_unit_id(_u(floor_plan_name="1BR", beds=1), "P1")
    assert result is not None
    assert result.startswith("inferred_")


def test_fallback_id_is_stable_across_calls() -> None:
    u = _u(beds=1, floor_plan_name="1BR", rent_low=1500)
    id1 = compute_fallback_unit_id(u, "P1")
    id2 = compute_fallback_unit_id(u, "P1")
    assert id1 == id2
    assert id1 is not None


def test_fallback_id_differs_for_different_content() -> None:
    a = compute_fallback_unit_id(_u(floor_plan_name="1BR", beds=1), "P1")
    b = compute_fallback_unit_id(_u(floor_plan_name="2BR", beds=2), "P1")
    assert a != b


def test_fallback_id_differs_for_different_property_same_content() -> None:
    u = _u(floor_plan_name="1BR", beds=1)
    a = compute_fallback_unit_id(u, "P1")
    b = compute_fallback_unit_id(u, "P2")
    assert a != b


def test_fallback_id_handles_area_sentinel_minus_one() -> None:
    # area=-1 is treated as absent; with only floor_plan + sentinel area,
    # the function should still require a second real anchor and return None.
    result = compute_fallback_unit_id(_u(floor_plan_name="1BR", area=-1), "P1")
    assert result is None


def test_fallback_id_prefix_is_inferred_() -> None:
    result = compute_fallback_unit_id(_u(floor_plan_name="1BR", beds=1), "P1")
    assert result is not None
    assert result.startswith("inferred_")


def test_fallback_id_length_is_25_chars_inferred_plus_16_hex() -> None:
    result = compute_fallback_unit_id(_u(floor_plan_name="1BR", beds=1), "P1")
    assert result is not None
    # "inferred_" is 9 chars, digest is 16 hex chars = 25 total
    assert len(result) == 25


def test_fallback_ignores_case_and_whitespace() -> None:
    a = compute_fallback_unit_id(_u(floor_plan_name="  1BR  ", beds=1), "P1")
    b = compute_fallback_unit_id(_u(floor_plan_name="1br", beds=1), "P1")
    assert a == b


def test_orchestrator_fills_missing_unit_ids_via_fallback() -> None:
    """Units without unit_id get compute_fallback_unit_id applied in orchestrator."""
    from ma_poc.validation.orchestrator import validate

    class FakeExtract:
        property_id = "P1"
        records = [
            # Old-style fields the schema gate recognises, plus v2 fields for quality gate
            {
                "floor_plan_type": "1BR",
                "bedrooms": 1,
                "asking_rent": 1500,
                "sqft": 750,
                # No unit_id — schema gate will try compute_fallback_id
            }
        ]

    result = validate(FakeExtract())
    # The schema gate's compute_fallback_id fires on old-style fields
    for rec in result.accepted:
        assert rec.get("unit_id") is not None


def test_orchestrator_preserves_natural_unit_ids() -> None:
    """Units that already have unit_id must keep them unchanged."""
    from ma_poc.validation.orchestrator import validate

    class FakeExtract:
        property_id = "P1"
        records = [
            {
                "unit_id": "NATURAL-001",
                "floor_plan_type": "2BR",
                "bedrooms": 2,
                "asking_rent": 2000,
                "sqft": 900,
            }
        ]

    result = validate(FakeExtract())
    assert result.accepted[0]["unit_id"] == "NATURAL-001"
