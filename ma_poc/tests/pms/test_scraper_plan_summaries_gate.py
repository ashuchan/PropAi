"""BUG-1 contract — adapter ``plan_summaries`` short-circuits the F2 LLM
rescue gate and the generic-adapter fallback gate.

After the D16 strict partition, an adapter that extracts only plan-level
rows from a site emits ``result.units == []`` AND
``result.plan_summaries != []``. The scraper used to read only
``result.units`` and would (a) re-run the F2 LLM rescue on the same API
bodies the adapter already classified as plan rows, and (b) re-extract
the page from scratch via the generic adapter cascade.

These tests pin the post-fix behaviour: when plan_summaries is non-empty
treat the adapter as "found something" — skip rescue, skip fallback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS


def _detected(pms: str = "generic") -> DetectedPMS:
    return DetectedPMS(pms=pms, confidence=0.9, evidence=frozenset())


def _plan_summaries() -> list[dict[str, Any]]:
    return [
        {
            "floor_plan_id": "A1",
            "floor_plan_name": "Studio",
            "beds": 0,
            "baths": 1.0,
            "area": 500,
            "rent_low": 1200,
        },
        {
            "floor_plan_id": "B1",
            "floor_plan_name": "1BR",
            "beds": 1,
            "baths": 1.0,
            "area": 750,
            "rent_low": 1500,
        },
    ]


def _api_responses() -> list[dict[str, Any]]:
    return [
        {
            "url": "https://test.com/api/units",
            "body": {"floorplans": [{"beds": 1, "rent": 1200}]},
            "content_type": "application/json",
        }
    ]


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_adapter_returns_plan_summaries_only() -> None:
    """Plan-only site: 0 units + N plan_summaries → F2 rescue NOT triggered.

    Pre-fix: the rescue gate read only ``adapter_result.units``,
    treated the empty list as "adapter found nothing", and re-ran the
    LLM on the same API bodies the adapter just classified. Wasted
    LLM budget on a property that has no unit-level inventory.
    """
    from ma_poc.pms import scraper as scraper_mod

    plan_only_result = AdapterResult(
        units=[],
        plan_summaries=_plan_summaries(),
        tier_used="TIER_1_API",
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
        mock_adapter.extract = AsyncMock(return_value=plan_only_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mock_rescue.assert_not_called()
    # plan_summaries surfaced into the final result dict for downstream
    # consumers (reporting, run_report).
    assert result["plan_summaries"] == _plan_summaries()


@pytest.mark.asyncio
async def test_scraper_skips_generic_fallback_when_adapter_returns_plan_summaries_only() -> None:
    """Plan-only site: 0 units + N plan_summaries → generic-fallback NOT triggered.

    Pre-fix: the fallback gate ``if not adapter_result.units`` saw an
    empty list and re-extracted the page via the generic adapter
    (5 tiers, possibly LLM_DOM). For plan-only sites that's a wasted
    pass; the dedicated adapter already classified the rows correctly.

    Test strategy: ``get_adapter("unknown")`` is the entry point for the
    generic-fallback path. We pin that it was never called with
    ``"unknown"`` when the adapter returns plan_summaries-only output.
    """
    from ma_poc.pms import scraper as scraper_mod

    plan_only_result = AdapterResult(
        units=[],
        plan_summaries=_plan_summaries(),
        tier_used="TIER_1_RENTCAFE",
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
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
        mock_adapter.extract = AsyncMock(return_value=plan_only_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            # Non-empty api_responses required to clear the "no page,
            # no fetch_result, no api_responses" early-exit guard.
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    # ``get_adapter`` is called once with the detected PMS. The generic
    # fallback path would have called it again with ``"unknown"`` — pin
    # that this never happens when plan_summaries is non-empty.
    called_with = [c.args[0] for c in mock_get_adapter.call_args_list]
    assert "unknown" not in called_with, (
        f"generic-fallback fired despite non-empty plan_summaries: {called_with}"
    )
    # plan_summaries surface intact into the result for downstream readers.
    assert result["plan_summaries"] == _plan_summaries()


@pytest.mark.asyncio
async def test_scraper_still_falls_back_when_units_and_plan_summaries_both_empty() -> None:
    """Truly empty result (no units AND no plan_summaries) → fallback fires.

    Guard against over-broadening: the plan_summaries check must not
    block fallback when the adapter genuinely extracted nothing. The
    fallback path is what recovers data when a dedicated adapter
    misses the property entirely.
    """
    from ma_poc.pms import scraper as scraper_mod

    empty_result = AdapterResult(
        units=[],
        plan_summaries=[],
        tier_used="TIER_1_RENTCAFE",
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
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

        await scraper_mod.scrape(
            "https://test.com",
            # Non-empty api_responses required to clear the "no page,
            # no fetch_result, no api_responses" early-exit guard.
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    # The fallback path calls ``get_adapter("unknown")`` to swap in the
    # generic adapter. Pin that this DID happen when both partitions
    # were empty (no units, no plan_summaries).
    called_with = [c.args[0] for c in mock_get_adapter.call_args_list]
    assert "unknown" in called_with, (
        f"generic-fallback should fire when both units AND plan_summaries empty; got: {called_with}"
    )
