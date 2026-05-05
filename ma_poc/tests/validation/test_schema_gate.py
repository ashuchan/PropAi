"""Tests for schema_gate — unit record validation.

Updated 2026-05-05 (F1–F4):
  - All check() calls now thread property_id (signature change).
  - test_schema_rejects_missing_bedrooms reframed: under v2 fallback,
    floor_plan_type + sqft IS sufficient. The truly-insufficient case
    now deletes BOTH bedrooms and sqft, leaving only floor_plan_type.
  - test_schema_rejects_date_in_wrong_format → F4 placeholder behavior.
  - test_schema_reports_multiple_reasons → uses a non-string date to
    keep the date-reason, since F4 only re-routes string dates.
  - v2 mirror tests (canonical names) added at the bottom.
"""

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


def _valid_v2_record(**overrides: object) -> dict:
    """v2-canonical mirror of _valid_record: floor_plan_name, beds, area,
    rent_low/rent_high. Used by the v2 sibling tests at the bottom of
    this file to keep behavioral coverage parallel between alias families.
    """
    r = {
        "unit_id": "u101",
        "floor_plan_name": "1BR",
        "beds": 1,
        "rent_low": 1500,
        "rent_high": 1500,
        "area": 750,
    }
    r.update(overrides)
    return r


def test_schema_accepts_full_valid_record() -> None:
    result = check(_valid_record(), property_id="P1")
    assert result.accepted is not None
    assert result.rejection_reasons == []


def test_schema_accepts_record_missing_unit_id_via_fallback() -> None:
    r = _valid_record()
    del r["unit_id"]
    result = check(r, property_id="P1")
    assert result.accepted is not None
    assert result.inferred_id is True


def test_schema_rejects_missing_floor_plan() -> None:
    r = _valid_record()
    del r["unit_id"]
    del r["floor_plan_type"]
    result = check(r, property_id="P1")
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


def test_schema_rejects_when_only_floor_plan_present() -> None:
    """Under v2 fallback, fp + ≥1 other identifying field is sufficient.

    Pre-F1 (v1) this test deleted only `bedrooms` — but v2 binds with
    fp + sqft, so the v1 phrasing no longer triggers rejection. Reframed
    to delete both bedrooms and sqft so only floor_plan_type remains.
    """
    r = _valid_record()
    del r["unit_id"]
    del r["bedrooms"]
    del r["sqft"]
    result = check(r, property_id="P1")
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


def test_schema_rejects_negative_rent() -> None:
    result = check(_valid_record(asking_rent=-100), property_id="P1")
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons


def test_schema_rejects_absurd_rent_50k() -> None:
    result = check(_valid_record(asking_rent=60000), property_id="P1")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons


def test_schema_routes_unparseable_string_date_to_placeholder() -> None:
    """F4: 'not-a-date' is accepted with _date_placeholder stashed.

    The reason ``INVALID_DATE_FORMAT`` is no longer emitted for strings
    that fail to parse — the rest of the record is salvaged.
    """
    result = check(_valid_record(availability_date="not-a-date"), property_id="P1")
    assert result.accepted is not None
    assert result.accepted.get("_date_placeholder") == "not-a-date"
    assert result.accepted.get("availability_date") is None
    assert "INVALID_DATE_FORMAT" not in (result.rejection_reasons or [])


def test_schema_inferred_id_flagged_on_accept() -> None:
    r = _valid_record()
    del r["unit_id"]
    result = check(r, property_id="P1")
    assert result.inferred_id is True
    assert result.accepted is not None
    assert result.accepted["unit_id"].startswith("inferred_")


def test_schema_reports_multiple_reasons() -> None:
    """Two distinct rejection reasons in one record.

    Post-F4, unparseable string dates pass through as placeholders, so
    the test uses a non-string corrupted date (int) which still
    legitimately fails INVALID_DATE_FORMAT (H7).
    """
    r = {"asking_rent": -100, "availability_date": 42}
    result = check(r, property_id="P1")
    assert len(result.rejection_reasons) >= 2


# ---- v2 mirror tests (canonical name family) -------------------------------


def test_schema_accepts_full_valid_v2_record() -> None:
    result = check(_valid_v2_record(), property_id="P1")
    assert result.accepted is not None
    assert result.rejection_reasons == []


def test_schema_v2_accepts_record_missing_unit_id_via_fallback() -> None:
    r = _valid_v2_record()
    del r["unit_id"]
    result = check(r, property_id="P1")
    assert result.accepted is not None
    assert result.inferred_id is True


def test_schema_v2_rejects_negative_rent() -> None:
    result = check(_valid_v2_record(rent_low=-100), property_id="P1")
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons


def test_schema_v2_rejects_absurd_rent() -> None:
    result = check(_valid_v2_record(rent_low=60000), property_id="P1")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons
