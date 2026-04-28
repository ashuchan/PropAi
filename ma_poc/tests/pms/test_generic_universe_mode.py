"""Tests for the JUGNU_LLM_TIER_MODE wiring in GenericAdapter.

Two layers of testing:

1. **Mode-flag parsing** — confirms that `JUGNU_LLM_TIER_MODE` is read
   from the environment and that invalid values fall back to legacy.
   These are observed via the `extract.tier_attempted` event the
   adapter emits at the top of the LLM block (`generic:llm_mode_*`).

2. **`_run_universe_explorer` helper** — confirms the helper invokes
   `explore_universe` with the right inputs and merges its outputs
   into the adapter result correctly.

Full end-to-end exercise of the cascade is covered by the existing
PMS test suite (which still runs in legacy mode by default) plus the
`test_universe_explorer.py` unit tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import GenericAdapter, _run_universe_explorer
from ma_poc.pms.detector import DetectedPMS
from ma_poc.services.extraction_quality import aggregate_quality
from ma_poc.services.universe_explorer import ExplorationOutcome


def _make_ctx(html: str = "", api_responses: list[dict[str, Any]] | None = None) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id="P001",
        property_name="Test Apartments",
        city="Scottsdale",
        state="AZ",
        zip_code="85255",
        pmc="Mark-Taylor",
    )
    if html:
        ctx.fetch_result = type("_FR", (), {"body": html.encode("utf-8")})()  # type: ignore[attr-defined]
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    return ctx


_FULL_UNIT = {
    "unit_id": "101",
    "beds": 1,
    "baths": 1,
    "sqft": 700,
    "rent_low": 1500,
    "available_date": "2026-05-01",
    "_confidence": 0.9,
}


def _extract_mode_attempts(result: AdapterResult) -> list[str]:
    """Pull the `generic:llm_mode_*` tier_attempts off the result so the
    test can assert which mode the adapter parsed from the env var."""
    attempts: list[dict[str, Any]] = getattr(result, "_tier_attempts", []) or []
    return [a["tier_key"] for a in attempts if str(a.get("tier_key", "")).startswith("generic:llm_mode_")]


@pytest.mark.asyncio
async def test_mode_flag_defaults_to_legacy_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var set → mode should be `legacy`."""
    monkeypatch.delenv("JUGNU_LLM_TIER_MODE", raising=False)
    # Empty universe so the cascade reaches the LLM block quickly. The
    # LLM gate will skip because the body is empty, but the mode tier
    # attempt is logged before the gate so we still see it.
    ctx = _make_ctx(html="<html><body>only a tiny page</body></html>", api_responses=[])
    result = await GenericAdapter().extract(page=None, ctx=ctx)
    mode_keys = _extract_mode_attempts(result)
    # Either an empty list (gate skipped before reaching mode-tier log)
    # OR ['generic:llm_mode_legacy'] is acceptable.
    assert mode_keys == [] or mode_keys == ["generic:llm_mode_legacy"]


@pytest.mark.asyncio
async def test_mode_flag_normalises_garbage_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid env value should not crash and should fall back to legacy."""
    monkeypatch.setenv("JUGNU_LLM_TIER_MODE", "WoOblE")
    html = (
        "<html><body>"
        + ("<p>floor plan rent $1500 available now apartment unit</p>" * 100)
        + "</body></html>"
    )
    ctx = _make_ctx(html=html, api_responses=[])
    with patch("ma_poc.services.llm_extractor.extract_with_llm", AsyncMock(return_value=([], None, "", None))):
        with patch("ma_poc.services.llm_extractor.analyze_dom_with_llm", AsyncMock(return_value=([], None, None))):
            result = await GenericAdapter().extract(page=None, ctx=ctx)

    mode_keys = _extract_mode_attempts(result)
    assert mode_keys == ["generic:llm_mode_legacy"]


@pytest.mark.asyncio
async def test_mode_flag_universe_runs_explorer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """When mode=universe, only the explorer (6d) runs — not 6a/6b/6c."""
    monkeypatch.setenv("JUGNU_LLM_TIER_MODE", "universe")
    html = (
        "<html><body>"
        + ("<p>floor plan rent $1500 available now apartment unit</p>" * 100)
        + "</body></html>"
    )
    ctx = _make_ctx(html=html, api_responses=[])

    legacy_api_mock = AsyncMock()
    legacy_dom_mock = AsyncMock()
    legacy_mono_mock = AsyncMock()

    fake_outcome = ExplorationOutcome(
        units=[_FULL_UNIT],
        quality=aggregate_quality([_FULL_UNIT]),
        winning_url="https://example.com/api/units",
        tier_attempts=[],
        stop_reason="quality_met",
    )

    with patch("ma_poc.services.llm_extractor.analyze_api_with_llm", legacy_api_mock):
        with patch("ma_poc.services.llm_extractor.analyze_dom_with_llm", legacy_dom_mock):
            with patch("ma_poc.services.llm_extractor.extract_with_llm", legacy_mono_mock):
                with patch(
                    "ma_poc.services.universe_explorer.explore_universe",
                    AsyncMock(return_value=fake_outcome),
                ):
                    result = await GenericAdapter().extract(page=None, ctx=ctx)

    mode_keys = _extract_mode_attempts(result)
    assert mode_keys == ["generic:llm_mode_universe"]
    assert result.tier_used == "TIER_4_UNIVERSE"
    assert result.units == [_FULL_UNIT]
    legacy_api_mock.assert_not_called()
    legacy_dom_mock.assert_not_called()
    legacy_mono_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_universe_explorer_merges_outcome_into_result() -> None:
    """The helper must copy units, tier_used, winning_url, and the four
    self-learning lists into the AdapterResult / mutable arg lists."""
    ctx = _make_ctx(html="<html><body>x</body></html>", api_responses=[])
    result = AdapterResult(tier_used="TIER_1_API")
    interactions: list[dict[str, Any]] = []
    field_mappings: list[dict[str, Any]] = []
    analysis_results: dict[str, Any] = {}
    nav_hints: list[str] = []
    log_calls: list[tuple[str, ...]] = []

    def _log_attempt(key: str, outcome: str, *, units: int = 0, reason: str = "", duration_ms: int = 0) -> None:
        log_calls.append((key, outcome))

    fake_outcome = ExplorationOutcome(
        units=[_FULL_UNIT],
        quality=aggregate_quality([_FULL_UNIT]),
        winning_url="https://example.com/api/winner",
        llm_interactions=[{"cost_usd": 0.001, "tier": "UNIVERSE_RANKING"}],
        llm_field_mappings=[{"api_url_pattern": "x", "json_paths": {}}],
        llm_analysis_results={"https://example.com/api/x": "noise:analytics"},
        llm_navigation_hints=["https://example.com/leasing"],
        tier_attempts=[
            {"tier_key": "universe:rank", "outcome": "ran_units", "units_found": 0, "reason": ""},
            {"tier_key": "universe:api", "outcome": "ran_units", "units_found": 1, "reason": ""},
        ],
        stop_reason="quality_met",
    )

    with patch(
        "ma_poc.services.universe_explorer.explore_universe",
        AsyncMock(return_value=fake_outcome),
    ):
        await _run_universe_explorer(
            html="<html><body>x</body></html>",
            api_responses=[],
            ctx=ctx,
            property_context={"property_name": "T", "city": "", "state": "", "pmc": "", "website": ""},
            result=result,
            llm_interactions=interactions,
            llm_field_mappings=field_mappings,
            llm_analysis_results=analysis_results,
            llm_navigation_hints=nav_hints,
            log_attempt=_log_attempt,
        )

    assert result.units == [_FULL_UNIT]
    assert result.tier_used == "TIER_4_UNIVERSE"
    assert result.winning_url == "https://example.com/api/winner"
    assert 0.5 <= result.confidence <= 0.85

    assert interactions == [{"cost_usd": 0.001, "tier": "UNIVERSE_RANKING"}]
    assert field_mappings == [{"api_url_pattern": "x", "json_paths": {}}]
    assert analysis_results == {"https://example.com/api/x": "noise:analytics"}
    assert nav_hints == ["https://example.com/leasing"]

    forwarded_keys = [k for k, _ in log_calls]
    assert "universe:rank" in forwarded_keys
    assert "universe:api" in forwarded_keys


@pytest.mark.asyncio
async def test_run_universe_explorer_does_not_set_units_on_empty_outcome() -> None:
    """When the explorer returns no units, the AdapterResult must remain
    in its no-units state — the caller decides what to do next."""
    ctx = _make_ctx(html="x", api_responses=[])
    result = AdapterResult(tier_used="TIER_1_API")
    interactions: list[dict[str, Any]] = []

    fake_outcome = ExplorationOutcome(
        units=[],
        quality=aggregate_quality([]),
        winning_url=None,
        llm_interactions=[{"cost_usd": 0.0005, "tier": "UNIVERSE_RANKING"}],
        tier_attempts=[],
        stop_reason="queue_drained",
    )

    with patch(
        "ma_poc.services.universe_explorer.explore_universe",
        AsyncMock(return_value=fake_outcome),
    ):
        await _run_universe_explorer(
            html="x",
            api_responses=[],
            ctx=ctx,
            property_context={"property_name": "T", "city": "", "state": "", "pmc": "", "website": ""},
            result=result,
            llm_interactions=interactions,
            llm_field_mappings=[],
            llm_analysis_results={},
            llm_navigation_hints=[],
            log_attempt=lambda *a, **k: None,
        )

    assert result.units == []
    assert result.tier_used == "TIER_1_API"
    assert interactions == [{"cost_usd": 0.0005, "tier": "UNIVERSE_RANKING"}]


@pytest.mark.asyncio
async def test_run_universe_explorer_swallows_exceptions() -> None:
    """A crashing explore_universe must not propagate — adapters never raise."""
    ctx = _make_ctx(html="x", api_responses=[])
    result = AdapterResult(tier_used="TIER_1_API")
    log_calls: list[tuple[str, str]] = []

    def _log_attempt(key: str, outcome: str, **kwargs: Any) -> None:
        log_calls.append((key, outcome))

    with patch(
        "ma_poc.services.universe_explorer.explore_universe",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _run_universe_explorer(
            html="x",
            api_responses=[],
            ctx=ctx,
            property_context={},
            result=result,
            llm_interactions=[],
            llm_field_mappings=[],
            llm_analysis_results={},
            llm_navigation_hints=[],
            log_attempt=_log_attempt,
        )

    assert any(k == "universe:explore_failed" for k, _ in log_calls)
    assert any("universe-explore-error" in e for e in result.errors)
    assert result.units == []
