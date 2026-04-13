"""Tests for extraction/pipeline.py — 5+ tests."""
from __future__ import annotations

from typing import Any

import pytest

from extraction import pipeline as pipe
from extraction.pipeline import run_extraction_pipeline
from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from scraper.browser import BrowserSession


def _session() -> BrowserSession:
    s = BrowserSession(property_id="P1", url="https://x/")
    s.html = "<html></html>"
    return s


def _make_async(result: ExtractionResult) -> Any:
    async def _fn(s: BrowserSession, catalogue: Any = None) -> ExtractionResult:
        return result
    return _fn


async def test_tier1_success_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def t1_ok(s: Any, c: Any = None) -> ExtractionResult:
        called.append("t1")
        return ExtractionResult(property_id="P1", status=ExtractionStatus.SUCCESS, confidence_score=0.95)

    async def t2_spy(s: Any, c: Any = None) -> ExtractionResult:
        called.append("t2")
        return ExtractionResult(property_id="P1", status=ExtractionStatus.SUCCESS)

    monkeypatch.setattr(pipe, "_t1", t1_ok)
    monkeypatch.setattr(pipe, "_t2", t2_spy)
    monkeypatch.setattr(pipe, "_t3", t2_spy)
    monkeypatch.setattr(pipe, "_t4", t2_spy)

    result = await run_extraction_pipeline(_session())
    assert result.tier == ExtractionTier.API_INTERCEPTION
    assert result.confidence_score == 0.95
    assert "t2" not in called


async def test_tier1_2_fail_tier3_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(score: float) -> ExtractionResult:
        return ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED, confidence_score=score)
    monkeypatch.setattr(pipe, "_t1", _make_async(fail(0.1)))
    monkeypatch.setattr(pipe, "_t2", _make_async(fail(0.2)))
    monkeypatch.setattr(pipe, "_t3", _make_async(ExtractionResult(property_id="P1", status=ExtractionStatus.SUCCESS, confidence_score=0.85)))
    monkeypatch.setattr(pipe, "_t4", _make_async(fail(0.0)))

    result = await run_extraction_pipeline(_session())
    assert result.tier == ExtractionTier.PLAYWRIGHT_TPL
    assert result.succeeded


async def test_all_tiers_fail_returns_best(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(score: float) -> ExtractionResult:
        return ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED, confidence_score=score)
    monkeypatch.setattr(pipe, "_t1", _make_async(fail(0.1)))
    monkeypatch.setattr(pipe, "_t2", _make_async(fail(0.4)))
    monkeypatch.setattr(pipe, "_t3", _make_async(fail(0.3)))
    monkeypatch.setattr(pipe, "_t4", _make_async(fail(0.2)))

    result = await run_extraction_pipeline(_session())
    assert result.status == ExtractionStatus.FAILED
    assert result.confidence_score == 0.4


async def test_low_confidence_marked_failed_for_vision_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(score: float) -> ExtractionResult:
        return ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED, confidence_score=score)
    monkeypatch.setattr(pipe, "_t1", _make_async(fail(0.55)))
    monkeypatch.setattr(pipe, "_t2", _make_async(fail(0.5)))
    monkeypatch.setattr(pipe, "_t3", _make_async(fail(0.4)))
    monkeypatch.setattr(pipe, "_t4", _make_async(fail(0.3)))

    result = await run_extraction_pipeline(_session())
    assert result.status == ExtractionStatus.FAILED
    assert result.confidence_score < 0.6


async def test_tier_logged_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipe, "_t1", _make_async(ExtractionResult(property_id="P1", status=ExtractionStatus.SUCCESS, confidence_score=0.9)))
    monkeypatch.setattr(pipe, "_t2", _make_async(ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED)))
    monkeypatch.setattr(pipe, "_t3", _make_async(ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED)))
    monkeypatch.setattr(pipe, "_t4", _make_async(ExtractionResult(property_id="P1", status=ExtractionStatus.FAILED)))
    result = await run_extraction_pipeline(_session())
    assert result.tier is not None
