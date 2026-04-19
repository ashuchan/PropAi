"""Tests for Jugnu J3 deltas on pms/scraper.py."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from ma_poc.pms.scraper import scrape_jugnu


@dataclass
class FakeCrawlTask:
    url: str = "https://example.com"
    property_id: str = "test_001"
    render_mode: str = "RENDER"


@dataclass
class FakeFetchResult:
    outcome: object = None
    body: bytes | None = b"<html></html>"
    network_log: list = None  # type: ignore

    def __post_init__(self) -> None:
        if self.network_log is None:
            self.network_log = []
        if self.outcome is None:
            self.outcome = type("O", (), {"value": "OK"})()

    def ok(self) -> bool:
        return self.outcome.value == "OK"  # type: ignore


def _make_outcome(value: str) -> object:
    return type("FetchOutcome", (), {"value": value})()


@pytest.mark.asyncio
async def test_scrape_jugnu_short_circuits_on_hard_fail() -> None:
    """Delta 2: non-OK fetch -> no adapter invoked, tier='generic:no_body_short_circuit'."""
    task = FakeCrawlTask()
    fetch = FakeFetchResult(outcome=_make_outcome("HARD_FAIL"), body=None)
    result = await scrape_jugnu(task, fetch)
    assert result["extraction_tier_used"] == "generic:no_body_short_circuit"
    assert result.get("_llm_interactions", []) == []
    assert "FAILED_UNREACHABLE" in str(result["errors"])


@pytest.mark.asyncio
async def test_scrape_jugnu_short_circuits_on_bot_blocked() -> None:
    """Delta 2: BOT_BLOCKED fetch -> no adapter invoked."""
    task = FakeCrawlTask()
    fetch = FakeFetchResult(outcome=_make_outcome("BOT_BLOCKED"), body=None)
    result = await scrape_jugnu(task, fetch)
    assert result["extraction_tier_used"] == "generic:no_body_short_circuit"


@pytest.mark.asyncio
async def test_scrape_jugnu_populates_extract_result() -> None:
    """Delta 7: _extract_result has cost fields."""
    task = FakeCrawlTask()
    fetch = FakeFetchResult(outcome=_make_outcome("HARD_FAIL"), body=None)
    result = await scrape_jugnu(task, fetch)
    er = result.get("_extract_result")
    assert er is not None
    assert er.llm_cost_usd == 0.0
    assert er.llm_calls == 0


@pytest.mark.asyncio
async def test_scrape_jugnu_sets_property_id() -> None:
    """Property ID flows from task to result."""
    task = FakeCrawlTask(property_id="p42")
    fetch = FakeFetchResult(outcome=_make_outcome("HARD_FAIL"), body=None)
    result = await scrape_jugnu(task, fetch)
    assert result["_property_id"] == "p42"
