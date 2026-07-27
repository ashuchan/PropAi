"""Tests for Jugnu J3 deltas on pms/scraper.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ma_poc.pms.scraper import scrape_jugnu

# ---------------------------------------------------------------------------
# Network seam — see ma_poc/conftest.py
# ---------------------------------------------------------------------------
# The tests that do NOT short-circuit run the full scrape() body, whose Step-4b
# detection rescue re-fetches ``/``, ``/floorplans/`` … through the sync
# ``_probe.probe_get`` curl_cffi seam whenever detection is unknown. That was a
# live hit on example.com. These tests only assert which short-circuit branch
# scrape_jugnu took, so the seam returns an inert 200 page with no PMS marker —
# detection stays unknown exactly as it did against the live example.com body.

_INERT_HTML = (
    "<html><head><title>Test page</title></head>"
    "<body><p>No PMS markers, no floor plans, no rents.</p></body></html>"
)


class _InertProbeResponse:
    """Minimal curl_cffi-response stand-in (``.status_code/.text/.content``)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.text = _INERT_HTML
        self.content = _INERT_HTML.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    def json(self) -> Any:
        """Match curl_cffi/requests semantics for a non-JSON body."""
        raise ValueError("inert probe response is not JSON")


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve every ``probe_get`` in this module an inert local page.

    Also covers the TRANSIENT/BOT_BLOCKED salvage at ``scraper.py:4140``, which
    calls ``curl_cffi`` DIRECTLY rather than through ``probe_get`` — a second,
    separate network seam. ``test_scrape_jugnu_short_circuits_on_bot_blocked``
    drives BOT_BLOCKED straight into it and used to fetch example.com for real.

    The inert 200 is behaviour-preserving, not merely quiet: salvage only fires
    on ``status_code == 200 and len(body) >= 5000``, and the live example.com
    body is ~1.2 KB, so salvage declined then and declines now. The
    BOT_BLOCKED short-circuit each test asserts on is reached identically.
    """

    def _fake_probe_get(url: str, **_kw: Any) -> _InertProbeResponse:
        return _InertProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get, raising=True
    )
    monkeypatch.setattr(
        "curl_cffi.requests.get",
        lambda url, **_kw: _InertProbeResponse(url),
        raising=True,
    )


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
