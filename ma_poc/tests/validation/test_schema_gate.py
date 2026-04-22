"""Tests for schema_gate — unit record validation."""

from __future__ import annotations

from ma_poc.validation.schema_gate import check


def _valid_record(**overrides: object) -> dict:
    r = {
        "unit_id": "u101",
        "floor_plan_type": "1BR",
        "bedrooms": 1,
        "asking_rent": 1500,
        "sqft": 750,
    }
    r.update(overrides)
    return r


def test_schema_accepts_full_valid_record() -> None:
    result = check(_valid_record())
    assert result.accepted is not None
    assert result.rejection_reasons == []


def test_schema_accepts_record_missing_unit_id_via_fallback() -> None:
    r = _valid_record()
    del r["unit_id"]
    result = check(r)
    assert result.accepted is not None
    assert result.inferred_id is True


def test_schema_rejects_missing_floor_plan() -> None:
    r = _valid_record()
    del r["unit_id"]
    del r["floor_plan_type"]
    result = check(r)
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


def test_schema_rejects_missing_bedrooms() -> None:
    r = _valid_record()
    del r["unit_id"]
    del r["bedrooms"]
    result = check(r)
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


def test_schema_rejects_negative_rent() -> None:
    result = check(_valid_record(asking_rent=-100))
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons


def test_schema_rejects_absurd_rent_50k() -> None:
    result = check(_valid_record(asking_rent=60000))
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons


def test_schema_rejects_date_in_wrong_format() -> None:
    result = check(_valid_record(availability_date="not-a-date"))
    assert result.accepted is None
    assert "INVALID_DATE_FORMAT" in result.rejection_reasons


def test_schema_inferred_id_flagged_on_accept() -> None:
    r = _valid_record()
    del r["unit_id"]
    result = check(r)
    assert result.inferred_id is True
    assert result.accepted is not None
    assert result.accepted["unit_id"].startswith("inferred_")


def test_schema_reports_multiple_reasons() -> None:
    r = {"asking_rent": -100, "availability_date": "bad"}
    result = check(r)
    assert len(result.rejection_reasons) >= 2
