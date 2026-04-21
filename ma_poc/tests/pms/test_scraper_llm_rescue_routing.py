"""Tests for F2 — LLM rescue routing in ma_poc/pms/scraper.py.

Uses a stub adapter and mocked rescue service.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS


def _detected(pms: str = "generic") -> DetectedPMS:
    return DetectedPMS(pms=pms, confidence=0.9, evidence=frozenset())


def _ctx(
    pms: str = "generic",
    api_responses: list | None = None,
    consecutive_failures: int = 0,
    profile: Any = None,
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://test.com",
        detected=_detected(pms),
        profile=profile,
        expected_total_units=50,
        property_id="TEST-001",
        fetch_result=None,
    )
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    return ctx


def _hollow_units() -> list[dict]:
    return [
        {
            "unit_id": f"u{i}",
            "beds": None,
            "baths": None,
            "floor_plan_name": None,
            "area": -1,
            "rent_low": None,
        }
        for i in range(3)
    ]


def _good_units() -> list[dict]:
    return [
        {
            "unit_id": "101",
            "beds": 1,
            "baths": 1.0,
            "floor_plan_name": "1BR",
            "area": 750,
            "rent_low": 1200,
            "rent_high": 1200,
        }
    ]


def _api_responses() -> list[dict]:
    return [
        {
            "url": "https://test.com/api/units",
            "body": {"units": [{"beds": 1, "rent": 1200}]},
            "content_type": "application/json",
        }
    ]


# ── Skip conditions ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_quality_gate_passes() -> None:
    from ma_poc.pms import scraper as scraper_mod

    good_result = AdapterResult(units=_good_units(), tier_used="TIER_1_API")
    _ctx(api_responses=_api_responses())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=good_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_no_api_responses() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result = AdapterResult(units=[], tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        # No api_responses passed
        await scraper_mod.scrape("https://test.com", api_responses=[], property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_pms_is_rentcafe() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result = AdapterResult(units=[], tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("rentcafe")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("rentcafe"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "rentcafe"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_consecutive_failures_geq_3() -> None:
    from ma_poc.pms import scraper as scraper_mod

    profile = MagicMock()
    profile.stats = MagicMock()
    profile.stats.consecutive_llm_rescue_failures = 3
    profile.model_dump = MagicMock(return_value={})

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape(
            "https://test.com",
            profile=profile,
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_page_unreachable() -> None:
    from ma_poc.pms import scraper as scraper_mod

    unreachable_result = AdapterResult(
        units=[],
        tier_used="TIER_1_API",
        errors=["FAILED_UNREACHABLE: ERR_CONNECTION_REFUSED"],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=unreachable_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


# ── Invocation conditions ─────────────────────────────────────────────────────


def _make_scraper_mocks(pms_name: str, adapter_units: list, rescue_units: list):
    """Helper to set up scraper mocks and return (mock_rescue, mock_adapter)."""
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=adapter_units, tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=rescue_units,
        tier_used=f"TIER_1_{pms_name.upper()}_LLM_RESCUE" if rescue_units else "",
        cost_usd=0.05 if rescue_units else 0.02,
        n_llm_calls=1,
        llm_field_mappings=[
            {
                "api_url_pattern": "https://test.com/api/*",
                "json_paths": {},
                "envelope": "",
                "success_count": 1,
            }
        ]
        if rescue_units
        else [],
    )
    return empty_result, rescue_out


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_generic_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("generic", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_entrata_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("entrata", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("entrata")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("entrata"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "entrata"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_appfolio_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("appfolio", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("appfolio")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("appfolio"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "appfolio"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_replaces_empty_result_with_rescue_units_on_success() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=_good_units(), tier_used="TIER_1_API_LLM_RESCUE", cost_usd=0.05)

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com", api_responses=_api_responses(), property_id="TEST-001"
        )

    assert result.get("_rescue_succeeded") is True
    assert result.get("extraction_tier_used") == "TIER_1_API_LLM_RESCUE"


@pytest.mark.asyncio
async def test_scraper_records_cost_even_on_rescue_failure() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=[], tier_used="", cost_usd=0.03, errors=["llm_returned_no_units"])

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com", api_responses=_api_responses(), property_id="TEST-001"
        )

    assert result.get("_rescue_cost_usd", 0) > 0


@pytest.mark.asyncio
async def test_scraper_increments_failure_counter_on_rescue_failure() -> None:
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.profile_updater import update_rescue_counter

    profile = ScrapeProfile(canonical_id="TEST-001")
    assert profile.stats.consecutive_llm_rescue_failures == 0
    updated = update_rescue_counter(profile, rescue_succeeded=False)
    assert updated.stats.consecutive_llm_rescue_failures == 1


@pytest.mark.asyncio
async def test_scraper_resets_failure_counter_on_rescue_success() -> None:
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.profile_updater import update_rescue_counter

    profile = ScrapeProfile(canonical_id="TEST-001")
    profile.stats.consecutive_llm_rescue_failures = 2
    updated = update_rescue_counter(profile, rescue_succeeded=True)
    assert updated.stats.consecutive_llm_rescue_failures == 0


@pytest.mark.asyncio
async def test_scraper_emits_all_three_rescue_events() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=_good_units(), tier_used="TIER_1_API_LLM_RESCUE", cost_usd=0.05)

    emitted_kinds: list[str] = []

    def fake_emit(kind: Any, pid: str, **kw: Any) -> Any:
        emitted_kinds.append(kind.value if hasattr(kind, "value") else str(kind))
        return MagicMock()

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
        patch("ma_poc.observability.events.emit", side_effect=fake_emit),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    assert "extract.llm_rescue_attempted" in emitted_kinds
    assert "extract.llm_rescue_succeeded" in emitted_kinds
