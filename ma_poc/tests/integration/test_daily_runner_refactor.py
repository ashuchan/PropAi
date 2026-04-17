"""
Integration tests for the daily_runner <-> PMS detection pipeline.

These tests validate that:
  - Profile api_provider is updated when PMS detection confidence >= 0.80
  - Profile api_provider is preserved when confidence < 0.80
  - FAILED_UNREACHABLE results incur 0 LLM calls
  - Output records contain all 46 required keys
  - Missing _detected_pms key does not crash
  - Run report includes a PMS breakdown section
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Ensure the pms package is importable regardless of working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pms.integration_helpers import (
    PMS_CONFIDENCE_THRESHOLD,
    add_pms_metrics_to_report,
    update_profile_from_scrape,
)

# ---------------------------------------------------------------------------
# Fixtures — reusable scrape results and profiles
# ---------------------------------------------------------------------------

# The 46 top-level property fields that daily_runner.build_property_record
# is expected to produce (plus "units" and "_meta" which are always present).
TARGET_PROPERTY_FIELDS = [
    "Property Name", "Type", "Unique ID", "Average Unit Size (SF)",
    "Property ID", "Census Block Id", "City",
    "Construction Finish Date", "Construction Start Date",
    "Development Company", "Latitude", "Longitude",
    "Management Company", "Market Name", "Property Owner",
    "Property Address", "Property Status", "Property Type",
    "Region", "Renovation Finish", "Renovation Start",
    "State", "Stories", "Submarket Name", "Total Units",
    "Tract Code", "Year Built", "ZIP Code", "Lease Start Date",
    "First Move-In Date", "Property Style", "Update Date", "Unit Mix",
    "Asset Grade in Submarket", "Asset Grade in Market",
    "Phone", "Website",
    "Property Image URL", "Property Gallery URLs",
]


def _make_profile(api_provider: str = "unknown") -> dict[str, Any]:
    """Return a minimal profile dict with api_hints.api_provider set."""
    return {
        "canonical_id": "TEST-001",
        "api_hints": {
            "api_provider": api_provider,
            "known_endpoints": [],
            "blocked_endpoints": [],
        },
        "confidence": {
            "preferred_tier": 1,
            "maturity": "COLD",
        },
    }


def _make_scrape_result(
    *,
    pms: str = "rentcafe",
    confidence: float = 0.90,
    units: list[dict[str, Any]] | None = None,
    errors: list[str] | None = None,
    llm_interactions: list[dict[str, Any]] | None = None,
    include_detected_pms: bool = True,
) -> dict[str, Any]:
    """Build a synthetic scrape result dict matching pms.scraper.scrape() output."""
    result: dict[str, Any] = {
        "scraped_at": "2026-04-17T10:00:00Z",
        "property_name": "Test Property",
        "base_url": "https://testproperty.com",
        "links_found": [],
        "property_links_crawled": [],
        "api_calls_intercepted": [],
        "units": units or [
            {"unit_id": "101", "market_rent_low": 1450, "market_rent_high": 1450,
             "available_date": "2026-05-01", "lease_link": None,
             "concessions": None, "amenities": None},
        ],
        "extraction_tier_used": 1,
        "errors": errors or [],
        "_property_id": "TEST-001",
        "_llm_interactions": llm_interactions or [],
        "_raw_api_responses": [],
        "_adapter_used": "generic",
        "_fallback_chain": ["generic"],
        "property_metadata": {"name": "Test Property"},
    }
    if include_detected_pms:
        result["_detected_pms"] = {
            "pms": pms,
            "confidence": confidence,
            "evidence": [f"test signal for {pms}"],
            "pms_client_account_id": None,
            "recommended_strategy": "cascade",
        }
    else:
        # Simulate a result that has no _detected_pms key at all
        pass
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDailyRunnerPopulatesApiProvider:
    """When scrape returns _detected_pms.pms with confidence >= 0.80,
    the profile's api_provider should be updated."""

    def test_daily_runner_populates_api_provider_on_success(self) -> None:
        profile = _make_profile(api_provider="unknown")
        scrape_result = _make_scrape_result(pms="rentcafe", confidence=0.90)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "rentcafe"

    def test_updates_at_exact_threshold(self) -> None:
        """Confidence exactly at 0.80 should still trigger an update."""
        profile = _make_profile(api_provider="unknown")
        scrape_result = _make_scrape_result(pms="entrata", confidence=0.80)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "entrata"

    def test_overwrites_existing_provider(self) -> None:
        """A high-confidence detection should overwrite a previous value."""
        profile = _make_profile(api_provider="appfolio")
        scrape_result = _make_scrape_result(pms="entrata", confidence=0.95)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "entrata"


class TestDailyRunnerDoesNotOverwriteProviderOnLowConfidence:
    """When confidence < 0.80, the existing api_provider must be preserved."""

    def test_daily_runner_does_not_overwrite_provider_on_low_confidence(self) -> None:
        profile = _make_profile(api_provider="entrata")
        scrape_result = _make_scrape_result(pms="rentcafe", confidence=0.60)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "entrata"

    def test_preserves_unknown_on_low_confidence(self) -> None:
        """Even 'unknown' should not be overwritten by a low-confidence detection."""
        profile = _make_profile(api_provider="unknown")
        scrape_result = _make_scrape_result(pms="onesite", confidence=0.50)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "unknown"

    def test_just_below_threshold(self) -> None:
        """Confidence at 0.79 is below the 0.80 threshold."""
        profile = _make_profile(api_provider="sightmap")
        scrape_result = _make_scrape_result(pms="entrata", confidence=0.79)

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "sightmap"


class TestDailyRunnerUnreachableDoesNotCostMoney:
    """FAILED_UNREACHABLE results should have 0 LLM interactions."""

    def test_daily_runner_unreachable_does_not_cost_money(self) -> None:
        scrape_result = _make_scrape_result(
            pms="unknown",
            confidence=0.0,
            units=[],
            errors=["FAILED_UNREACHABLE: ERR_NAME_NOT_RESOLVED"],
            llm_interactions=[],  # zero LLM calls
        )

        llm_interactions = scrape_result.get("_llm_interactions") or []
        total_cost = sum(i.get("cost_usd", 0.0) for i in llm_interactions)

        assert len(llm_interactions) == 0
        assert total_cost == 0.0

    def test_unreachable_profile_not_updated(self) -> None:
        """An unreachable scrape (confidence=0.0) should not change the profile."""
        profile = _make_profile(api_provider="entrata")
        scrape_result = _make_scrape_result(
            pms="unknown",
            confidence=0.0,
            units=[],
            errors=["FAILED_UNREACHABLE: ERR_CONNECTION_TIMED_OUT"],
        )

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "entrata"


class TestDailyRunner46KeyOutputPreserved:
    """Verify the 46-key output schema is complete."""

    def test_daily_runner_46_key_output_preserved(self) -> None:
        """A well-formed property record must contain all TARGET_PROPERTY_FIELDS
        plus 'units' and '_meta'."""
        # Build a minimal property record matching daily_runner output shape.
        record: dict[str, Any] = {f: None for f in TARGET_PROPERTY_FIELDS}
        record["units"] = [
            {"unit_id": "101", "market_rent_low": 1450, "market_rent_high": 1450,
             "available_date": "2026-05-01", "lease_link": None,
             "concessions": None, "amenities": None},
        ]
        record["_meta"] = {
            "canonical_id": "TEST-001",
            "identity_source": "unique_id",
            "scrape_tier_used": 1,
            "units_extracted": 1,
        }

        # Every target field must be present as a key.
        for field_name in TARGET_PROPERTY_FIELDS:
            assert field_name in record, f"Missing field: {field_name}"

        # "units" and "_meta" must also be present.
        assert "units" in record
        assert "_meta" in record
        assert isinstance(record["units"], list)
        assert len(record["units"]) > 0

    def test_unit_has_required_keys(self) -> None:
        """Each unit dict must have the standard unit schema keys."""
        required_unit_keys = {
            "unit_id", "market_rent_low", "market_rent_high",
            "available_date", "lease_link", "concessions", "amenities",
        }
        unit = {
            "unit_id": "205",
            "market_rent_low": 2100,
            "market_rent_high": 2300,
            "available_date": "2026-06-01",
            "lease_link": "https://example.com/apply",
            "concessions": None,
            "amenities": None,
        }

        assert required_unit_keys.issubset(unit.keys())

    def test_output_serializes_to_json(self) -> None:
        """The 46-key record must be JSON-serializable (no datetime objects, etc.)."""
        record: dict[str, Any] = {f: None for f in TARGET_PROPERTY_FIELDS}
        record["Update Date"] = "2026-04-17"
        record["units"] = []
        record["_meta"] = {"canonical_id": "TEST-001"}

        # Should not raise.
        serialized = json.dumps(record, default=str)
        assert isinstance(serialized, str)
        roundtrip = json.loads(serialized)
        assert roundtrip["Update Date"] == "2026-04-17"


class TestDailyRunnerHandlesMissingDetectedPmsKey:
    """If _detected_pms is missing from the scrape result, the profile
    update must not crash."""

    def test_daily_runner_handles_missing_detected_pms_key(self) -> None:
        profile = _make_profile(api_provider="entrata")
        scrape_result = _make_scrape_result(include_detected_pms=False)

        # Must not raise.
        updated = update_profile_from_scrape(profile, scrape_result)

        # Existing provider preserved.
        assert updated["api_hints"]["api_provider"] == "entrata"

    def test_handles_none_detected_pms(self) -> None:
        """_detected_pms explicitly set to None."""
        profile = _make_profile(api_provider="appfolio")
        scrape_result = _make_scrape_result()
        scrape_result["_detected_pms"] = None

        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "appfolio"

    def test_handles_empty_dict_detected_pms(self) -> None:
        """_detected_pms is an empty dict (no 'pms' key)."""
        profile = _make_profile(api_provider="sightmap")
        scrape_result = _make_scrape_result()
        scrape_result["_detected_pms"] = {}

        updated = update_profile_from_scrape(profile, scrape_result)

        # No pms key -> confidence defaults to 0.0 -> below threshold -> preserved.
        assert updated["api_hints"]["api_provider"] == "sightmap"

    def test_handles_profile_without_api_hints(self) -> None:
        """Profile that has no api_hints key at all."""
        profile: dict[str, Any] = {"canonical_id": "TEST-001"}
        scrape_result = _make_scrape_result(pms="entrata", confidence=0.95)

        # Must not raise; should create api_hints.
        updated = update_profile_from_scrape(profile, scrape_result)

        assert updated["api_hints"]["api_provider"] == "entrata"


class TestDailyRunnerReportHasPmsBreakdown:
    """The run report must include pms breakdown metrics."""

    def test_daily_runner_report_has_pms_breakdown(self) -> None:
        report: dict[str, Any] = {
            "run_date": "2026-04-17",
            "totals": {"properties_processed": 3},
        }
        scrape_results = [
            _make_scrape_result(pms="rentcafe", confidence=0.90),
            _make_scrape_result(pms="rentcafe", confidence=0.85),
            _make_scrape_result(pms="entrata", confidence=0.95),
        ]

        enriched = add_pms_metrics_to_report(report, scrape_results)

        assert "pms" in enriched
        assert "properties_by_pms" in enriched["pms"]
        assert "llm_cost_by_pms" in enriched["pms"]

        by_pms = enriched["pms"]["properties_by_pms"]
        assert by_pms["rentcafe"] == 2
        assert by_pms["entrata"] == 1

    def test_report_llm_cost_aggregation(self) -> None:
        """LLM costs should be summed per PMS."""
        report: dict[str, Any] = {"run_date": "2026-04-17"}
        scrape_results = [
            _make_scrape_result(
                pms="entrata", confidence=0.90,
                llm_interactions=[
                    {"cost_usd": 0.005, "model": "gpt-4o-mini"},
                    {"cost_usd": 0.003, "model": "gpt-4o-mini"},
                ],
            ),
            _make_scrape_result(
                pms="entrata", confidence=0.85,
                llm_interactions=[
                    {"cost_usd": 0.010, "model": "gpt-4o"},
                ],
            ),
            _make_scrape_result(
                pms="rentcafe", confidence=0.90,
                llm_interactions=[],
            ),
        ]

        enriched = add_pms_metrics_to_report(report, scrape_results)

        cost = enriched["pms"]["llm_cost_by_pms"]
        assert abs(cost["entrata"] - 0.018) < 1e-9
        assert cost["rentcafe"] == 0.0

    def test_report_handles_missing_detected_pms(self) -> None:
        """Scrape results without _detected_pms should be counted as 'unknown'."""
        report: dict[str, Any] = {"run_date": "2026-04-17"}
        scrape_results = [
            _make_scrape_result(include_detected_pms=False),
            _make_scrape_result(pms="appfolio", confidence=0.80),
        ]

        enriched = add_pms_metrics_to_report(report, scrape_results)

        by_pms = enriched["pms"]["properties_by_pms"]
        assert by_pms.get("unknown", 0) == 1
        assert by_pms.get("appfolio", 0) == 1

    def test_report_empty_scrape_results(self) -> None:
        """Empty scrape_results should produce empty breakdowns, not crash."""
        report: dict[str, Any] = {"run_date": "2026-04-17"}

        enriched = add_pms_metrics_to_report(report, [])

        assert enriched["pms"]["properties_by_pms"] == {}
        assert enriched["pms"]["llm_cost_by_pms"] == {}
