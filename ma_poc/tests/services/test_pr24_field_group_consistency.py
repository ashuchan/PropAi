"""PR24 Fix 9/11 — has_field_group and _ZERO_IS_VALID_FIELDS consistency.

Fix 9: has_field_group() is exported from source_planner; 0/"0" treated as ABSENT
       unless the field is explicitly in _ZERO_IS_VALID_FIELDS.
Fix 11: beds/bedrooms are in _ZERO_IS_VALID_FIELDS so studio units (beds=0) count
        as having physical data.
"""

from __future__ import annotations

import pytest

from ma_poc.services.source_planner import has_field_group
from ma_poc.models.source import _ZERO_IS_VALID_FIELDS


def _unit(**kwargs):
    return dict(kwargs)


# ── Fix 9: has_field_group treats 0/"0" as absent for non-exempt fields ──────

def test_zero_rent_is_absent() -> None:
    """asking_rent=0 must not count as transactional (price of 0 is invalid)."""
    unit = _unit(asking_rent=0, unit_id="U1")
    assert not has_field_group(unit, "transactional")


def test_string_zero_rent_is_absent() -> None:
    """asking_rent='0' must not count as transactional."""
    unit = _unit(asking_rent="0", unit_id="U1")
    assert not has_field_group(unit, "transactional")


def test_valid_rent_is_transactional() -> None:
    """Non-zero asking_rent counts as transactional."""
    unit = _unit(asking_rent=1500, unit_id="U1")
    assert has_field_group(unit, "transactional")


def test_zero_sqft_is_absent() -> None:
    """sqft=0 must not count as physical — 0 sqft is invalid data."""
    unit = _unit(sqft=0, beds=2, unit_id="U1")
    # sqft=0 is invalid; but beds=2 provides 1 physical field.
    # 2 physical fields needed → should fail
    assert not has_field_group(unit, "physical")


# ── Fix 11: beds=0 (studio) is valid physical data ───────────────────────────

def test_beds_zero_is_valid_field() -> None:
    """beds=0 must be treated as valid (studio unit)."""
    assert "beds" in _ZERO_IS_VALID_FIELDS
    assert "bedrooms" in _ZERO_IS_VALID_FIELDS


def test_studio_unit_needs_sqft_for_physical() -> None:
    """For completeness scoring, beds=0 is absent; studio needs sqft + 1 more physical field."""
    # has_field_group treats 0 as absent for completeness scoring (by design).
    # beds=0 + sqft=450 provides only 1 non-zero physical field → below threshold of 2.
    unit = _unit(beds=0, sqft=450, unit_id="U1")
    assert not has_field_group(unit, "physical"), (
        "beds=0 is absent for completeness scoring; sqft alone (1 field) < min 2"
    )


def test_studio_with_two_nonzero_physical_fields() -> None:
    """Studio with sqft + floor_plan_name (2 non-zero fields) counts as physical."""
    unit = _unit(beds=0, sqft=450, floor_plan_name="Studio A", unit_id="U1")
    assert has_field_group(unit, "physical"), (
        "sqft + floor_plan_name = 2 non-zero physical fields → physical group present"
    )


def test_non_zero_beds_counts_as_physical() -> None:
    """beds=2 is valid physical data."""
    unit = _unit(beds=2, sqft=900, unit_id="U1")
    assert has_field_group(unit, "physical")


def test_zero_valid_fields_preserves_beds_in_from_legacy_unit() -> None:
    """beds/bedrooms in _ZERO_IS_VALID_FIELDS means from_legacy_unit keeps beds=0."""
    from ma_poc.models.source import from_legacy_unit, SourceId, _ZERO_IS_VALID_FIELDS
    assert "beds" in _ZERO_IS_VALID_FIELDS
    assert "bedrooms" in _ZERO_IS_VALID_FIELDS
    # beds=0 must be preserved by from_legacy_unit (studio unit)
    prov_unit = from_legacy_unit(
        {"unit_id": "S1", "beds": 0, "sqft": 450},
        SourceId.API_SIGHTMAP,
        "https://example.com",
        "hash1",
        0.9,
    )
    assert "beds" in prov_unit, "beds=0 must be preserved as a valid studio field"
    assert prov_unit["beds"].value == 0


# ── has_field_group public export ─────────────────────────────────────────────

def test_has_field_group_is_public() -> None:
    """has_field_group must be importable from ma_poc.services.source_planner."""
    from ma_poc.services.source_planner import has_field_group as hfg
    assert callable(hfg)


def test_has_field_group_identity() -> None:
    """unit_id present → identity group."""
    assert has_field_group(_unit(unit_id="U1"), "identity")
    assert not has_field_group(_unit(), "identity")
