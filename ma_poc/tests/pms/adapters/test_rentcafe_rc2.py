"""RC2 — RentCafe unit-level key recognition tests.

Validates that _is_rentcafe_response() correctly identifies:
- floor-plan shaped responses (existing behaviour, must not regress)
- unit-level shaped responses with RentCafe IDs (new RC2 fix)
- unit-level shaped responses with rent fields (new RC2 fix)
"""

from __future__ import annotations

from ma_poc.pms.adapters.rentcafe import _is_rentcafe_response


# ── Floor-plan level (existing) ────────────────────────────────────────────────

def test_floor_plan_shape_still_recognised() -> None:
    body = [
        {
            "FloorPlanName": "1BR Classic",
            "FloorPlanId": "101",
            "MinimumRent": 1500,
            "MaximumRent": 1800,
            "AvailableUnitsCount": 3,
        }
    ]
    assert _is_rentcafe_response(body) is True


def test_api_field_sentinel_still_recognised() -> None:
    body = [{"Api": "RentCafe", "FloorPlanName": "2BR"}]
    assert _is_rentcafe_response(body) is True


def test_non_rentcafe_shape_rejected() -> None:
    body = [{"unitNumber": "101", "rent": 1500, "beds": 1}]
    assert _is_rentcafe_response(body) is False


def test_empty_body_rejected() -> None:
    assert _is_rentcafe_response([]) is False
    assert _is_rentcafe_response(None) is False
    assert _is_rentcafe_response({}) is False


# ── Unit level — RC2 new ───────────────────────────────────────────────────────

def test_rentcafe_unit_id_keys_recognised() -> None:
    """RentCafe unit endpoint with RentCafeApartmentId + RentCafeFloorplanId."""
    body = [
        {
            "RentCafeApartmentId": "A-101",
            "RentCafeFloorplanId": "FP-001",
            "ApartmentName": "101",
            "Rent": 1650,
        }
    ]
    assert _is_rentcafe_response(body) is True


def test_rentcafe_unit_with_propertyid_recognised() -> None:
    """RentCafe unit endpoint with ApartmentId + PropertyId (missing FloorplanId)."""
    body = [
        {
            "RentCafeApartmentId": "A-102",
            "RentCafePropertyId": "PROP-999",
            "Sqft": 750,
        }
    ]
    assert _is_rentcafe_response(body) is True


def test_rentcafe_unit_rent_fields_recognised() -> None:
    """Alternate unit shape: ApartmentId + UnitRent + MarketRent."""
    body = [
        {
            "RentCafeApartmentId": "A-103",
            "UnitRent": 1700,
            "MarketRent": 1800,
        }
    ]
    assert _is_rentcafe_response(body) is True


def test_only_one_rentcafe_id_key_not_enough() -> None:
    """A single RentCafe ID key (without a second key) is insufficient evidence."""
    body = [{"RentCafeApartmentId": "A-104", "SomeOtherField": "x"}]
    assert _is_rentcafe_response(body) is False


def test_normalisation_of_pascal_case_unit_keys() -> None:
    """PascalCase keys on unit-level payloads are normalised before matching."""
    body = [
        {
            "RENTCAFEAPARTMENTID": "A-105",
            "RENTCAFEFLOORPLANID": "FP-002",
        }
    ]
    assert _is_rentcafe_response(body) is True
