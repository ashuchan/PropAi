"""Production-shaped record fixtures for validation tests.

Each fixture mirrors a real production source:
  - jugnu_v2_record: shape emitted by GenericAdapter._format_v2_unit
                     (canonical v2 names: floor_plan_name, beds, baths, area,
                     rent_low, rent_high)
  - legacy_v1_record: shape emitted by scripts/scrape_properties.py
                     (legacy names: floor_plan_type, bedrooms, bathrooms,
                     sqft, asking_rent, market_rent_low)
  - mixed_record: shape emitted when a Jugnu adapter falls back through
                     scrape_properties._add (carries both alias families)
  - jugnu_v2_no_unit_id_record: the cohort that drove 25,634 rejections
                     in the 2026-05-05 RCA — full plan signal, no natural id
  - coming_soon_record: F4 placeholder shape ("Spring 2026" date)
"""
from __future__ import annotations

import pytest


@pytest.fixture
def jugnu_v2_record() -> dict:
    """Canonical Jugnu v2 record. Mirrors _format_v2_unit output."""
    return {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1.0,
        "area": 750,
        "rent_low": 1450,
        "rent_high": 1450,
        "available_date": "2026-05-12",
    }


@pytest.fixture
def legacy_v1_record() -> dict:
    """Pre-Jugnu legacy record. Mirrors scrape_properties._add output."""
    return {
        "unit_id": "1004",
        "floor_plan_type": "A1",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "asking_rent": 1450,
        "market_rent_low": 1450,
        "availability_date": "2026-05-12",
    }


@pytest.fixture
def mixed_record() -> dict:
    """Record carrying both v1 and v2 names (transitional shape).

    v1 names should win for back-compat per F2's lookup precedence (H15).
    """
    return {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "floor_plan_type": "A1",
        "beds": 1,
        "bedrooms": 1,
        "area": 750,
        "sqft": 750,
        "rent_low": 1500,
        "asking_rent": 1450,
        "available_date": "2026-05-12",
    }


@pytest.fixture
def jugnu_v2_no_unit_id_record() -> dict:
    """Production-real shape: extractor emitted plan + signal but no unit_id.
    This is the case that drove 25,634 of 30,117 rejections in the RCA.
    """
    return {
        "floor_plan_name": "Aspen 1BR",
        "beds": 1,
        "baths": 1.0,
        "area": 740,
        "rent_low": 2150,
        "rent_high": 2150,
    }


@pytest.fixture
def coming_soon_record() -> dict:
    """Production-real shape: 'Coming Soon' marketing string in date field.
    F4 reroutes this to placeholder pass-through.
    """
    return {
        "floor_plan_name": "B2",
        "beds": 2,
        "area": 1100,
        "rent_low": 2400,
        "available_date": "Spring 2026",
    }
