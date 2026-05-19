"""Tests for Jugnu J3 deltas on pms/scraper.py."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class FakeFetchResultWithFinalURL:
    outcome: object = None
    body: bytes | None = b"<html><body>captcha challenge</body></html>"
    final_url: str = ""
    network_log: list = None  # type: ignore

    def __post_init__(self) -> None:
        if self.network_log is None:
            self.network_log = []
        if self.outcome is None:
            self.outcome = type("O", (), {"value": "OK"})()

    def ok(self) -> bool:
        return self.outcome.value == "OK"  # type: ignore

    def to_dict(self) -> dict:
        return {"final_url": self.final_url, "outcome": self.outcome.value}


@pytest.mark.asyncio
async def test_scrape_jugnu_short_circuits_on_sgcaptcha_wall() -> None:
    """2026-05-13 (C1 SGCaptcha, teammate analysis): when fetch_result.final_url
    redirects to ``/.well-known/sgcaptcha/``, the page is a captcha
    interstitial. Skip the entire tier cascade to save ~25s/property."""
    task = FakeCrawlTask(url="https://www.example.com/")
    fetch = FakeFetchResultWithFinalURL(
        outcome=_make_outcome("OK"),
        body=b"<html>captcha</html>" * 500,  # ~10KB body
        final_url="https://www.example.com/.well-known/sgcaptcha/?challenge=xyz",
    )
    result = await scrape_jugnu(task, fetch)
    assert result["extraction_tier_used"] == "generic:sgcaptcha_wall"
    assert "SGCAPTCHA_WALL" in str(result["errors"])
    assert result.get("_fetch_diagnostic", {}).get("captcha_detected") is True
    assert result.get("_fetch_diagnostic", {}).get("captcha_provider") == "sgcaptcha"
    # No LLM tier should have run.
    assert result.get("_llm_interactions", []) == []


@pytest.mark.asyncio
async def test_scrape_jugnu_does_not_short_circuit_on_normal_final_url() -> None:
    """Counter-regression: normal pages must NOT be flagged as sgcaptcha
    just because final_url is non-empty."""
    task = FakeCrawlTask(url="https://www.example.com/")
    fetch = FakeFetchResultWithFinalURL(
        outcome=_make_outcome("OK"),
        body=b"<html><body>Real property page</body></html>",
        final_url="https://www.example.com/floor-plans/",
    )
    result = await scrape_jugnu(task, fetch)
    assert result["extraction_tier_used"] != "generic:sgcaptcha_wall"


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
