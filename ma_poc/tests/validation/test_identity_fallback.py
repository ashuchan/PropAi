"""Tests for identity_fallback — deterministic SHA256 unit ID computation."""

from __future__ import annotations

from ma_poc.validation.identity_fallback import compute_fallback_id


def _base_record(**overrides: object) -> dict:
    r = {
        "floor_plan_type": "1BR / 1BA",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "asking_rent": 1500,
    }
    r.update(overrides)
    return r


def test_fallback_deterministic_for_same_input() -> None:
    r = _base_record()
    id1 = compute_fallback_id(r)
    id2 = compute_fallback_id(r)
    assert id1 == id2
    assert id1 is not None


def test_fallback_normalises_floor_plan_whitespace_and_case() -> None:
    id1 = compute_fallback_id(_base_record(floor_plan_type="1BR / 1BA"))
    id2 = compute_fallback_id(_base_record(floor_plan_type="  1br / 1ba  "))
    assert id1 == id2


def test_fallback_rounds_rent_to_25() -> None:
    id1 = compute_fallback_id(_base_record(asking_rent=1998))
    id2 = compute_fallback_id(_base_record(asking_rent=2002))
    assert id1 == id2  # Both round to 2000


def test_fallback_rounds_sqft_to_10() -> None:
    id1 = compute_fallback_id(_base_record(sqft=748))
    id2 = compute_fallback_id(_base_record(sqft=752))
    assert id1 == id2  # Both round to 750


def test_fallback_returns_none_when_floor_plan_missing() -> None:
    r = _base_record()
    del r["floor_plan_type"]
    assert compute_fallback_id(r) is None


def test_fallback_returns_none_when_bedrooms_missing() -> None:
    r = _base_record()
    del r["bedrooms"]
    assert compute_fallback_id(r) is None


def test_fallback_prefix_is_inferred() -> None:
    result = compute_fallback_id(_base_record())
    assert result is not None
    assert result.startswith("inferred_")
