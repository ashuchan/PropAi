"""Tests for the ScrapeProfile model — claude-scrapper-arch.md Step 6.1."""
from __future__ import annotations

import json

import pytest

from models.scrape_profile import (
    ApiEndpoint,
    ExtractionConfidence,
    FieldSelectorMap,
    ProfileMaturity,
    ScrapeProfile,
    detect_platform,
)


def test_default_profile_is_cold() -> None:
    p = ScrapeProfile(canonical_id="test-001")
    assert p.confidence.maturity == ProfileMaturity.COLD
    assert p.confidence.consecutive_successes == 0
    assert p.confidence.consecutive_failures == 0
    assert p.version == 1
    assert p.updated_by == "BOOTSTRAP"


def test_profile_serialization_roundtrip() -> None:
    p = ScrapeProfile(
        canonical_id="test-002",
        confidence=ExtractionConfidence(
            preferred_tier=1, maturity=ProfileMaturity.HOT, consecutive_successes=5
        ),
        api_hints={"known_endpoints": [{"url_pattern": "/api/units", "provider": "entrata"}]},
    )
    data = p.model_dump(mode="json")
    json_str = json.dumps(data)
    loaded = ScrapeProfile.model_validate(json.loads(json_str))
    assert loaded.canonical_id == "test-002"
    assert loaded.confidence.maturity == ProfileMaturity.HOT
    assert loaded.confidence.preferred_tier == 1
    assert len(loaded.api_hints.known_endpoints) == 1
    assert loaded.api_hints.known_endpoints[0].provider == "entrata"


def test_field_selector_map_optional_fields() -> None:
    fs = FieldSelectorMap()
    assert fs.container is None
    assert fs.rent is None
    assert fs.sqft is None

    fs2 = FieldSelectorMap(container=".unit-card", rent=".price")
    assert fs2.container == ".unit-card"
    assert fs2.rent == ".price"
    assert fs2.sqft is None


def test_api_endpoint_with_json_paths() -> None:
    ep = ApiEndpoint(
        url_pattern="/api/v1/units",
        json_paths={"rent": "$.data.rent", "unit_id": "$.data.id"},
        provider="entrata",
    )
    assert ep.url_pattern == "/api/v1/units"
    assert ep.json_paths["rent"] == "$.data.rent"
    assert ep.provider == "entrata"


def test_detect_platform_rentcafe() -> None:
    assert detect_platform("https://www.rentcafe.com/apartments/foo") == "rentcafe"
    assert detect_platform("https://example.com/apartments/default.aspx") == "rentcafe"


def test_detect_platform_entrata() -> None:
    assert detect_platform("https://my.entrata.com/foo") == "entrata"


def test_detect_platform_appfolio() -> None:
    assert detect_platform("https://my.appfolio.com/listings") == "appfolio"


def test_detect_platform_unknown() -> None:
    assert detect_platform("https://some-random-property.com") is None
    assert detect_platform("") is None
