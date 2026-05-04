"""Phase 5 — H5 invariant tests for the schema gate.

A record carrying ``unit_id`` and nothing else is rejected with reason
``IDENTITY_REQUIRES_PHYSICAL_SIGNAL``. Records with ``unit_id`` plus any
single physical signal pass.
"""

from __future__ import annotations

from ma_poc.validation.schema_gate import check


def test_unit_id_alone_rejected() -> None:
    res = check({"unit_id": "101"})
    assert res.accepted is None
    assert "IDENTITY_REQUIRES_PHYSICAL_SIGNAL" in res.rejection_reasons


def test_unit_id_plus_floor_plan_name_accepted() -> None:
    res = check({"unit_id": "101", "floor_plan_name": "Aspen"})
    assert res.accepted is not None
    assert res.rejection_reasons == []


def test_unit_id_plus_beds_accepted() -> None:
    res = check({"unit_id": "101", "beds": 1})
    assert res.accepted is not None


def test_unit_id_plus_rent_accepted() -> None:
    res = check({"unit_id": "101", "asking_rent": 1500})
    assert res.accepted is not None


def test_unit_id_plus_sqft_accepted() -> None:
    res = check({"unit_id": "101", "sqft": 750})
    assert res.accepted is not None


def test_unit_id_plus_legacy_market_rent_low_accepted() -> None:
    res = check({"unit_id": "101", "market_rent_low": 1450})
    assert res.accepted is not None


def test_unit_id_with_inferred_fallback_still_works() -> None:
    """When unit_id is missing but identity_fallback succeeds, no rejection.

    The v1 ``compute_fallback_id`` reads ``floor_plan_type`` (not the v2
    ``floor_plan_name``); pass that name explicitly so the legacy fallback
    fires instead of dropping the record.
    """
    res = check(
        {
            "floor_plan_type": "Aspen",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 750,
            "asking_rent": 1500,
        }
    )
    assert res.accepted is not None
    assert res.inferred_id is True


def test_unit_id_zero_treated_as_present_but_still_needs_physical_signal() -> None:
    """unit_id="0" is a real string, so the existing fallback path doesn't fire.
    Without a physical signal, H5 still rejects it."""
    res = check({"unit_id": "0"})
    assert res.accepted is None
    assert "IDENTITY_REQUIRES_PHYSICAL_SIGNAL" in res.rejection_reasons


def test_record_with_no_unit_id_and_no_physical_signal_rejected_via_fallback() -> None:
    """No unit_id, no physical signal → IDENTITY_FALLBACK_INSUFFICIENT path.

    H5 only fires for records that already have unit_id; pre-fallback
    records still take the fallback path's rejection reason, which keeps
    diagnostics on the failing case clean.
    """
    res = check({})
    assert res.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in res.rejection_reasons


def test_sqft_minus_one_sentinel_does_not_fire_invalid_sqft_negative() -> None:
    """Bug fix regression test: under H2 the `-1` sqft sentinel means
    "unknown", not "negative". It must not generate INVALID_SQFT_NEGATIVE.
    """
    res = check(
        {
            "unit_id": "U1",
            "asking_rent": 1500,
            "sqft": -1,
        }
    )
    assert res.accepted is not None
    assert "INVALID_SQFT_NEGATIVE" not in res.rejection_reasons
