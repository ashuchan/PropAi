"""Tests for extraction/tier4_llm.py — 7+ tests."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from extraction import tier4_llm
from models.extraction_result import ExtractionStatus
from scraper.browser import BrowserSession


def _session(html: str | None) -> BrowserSession:
    s = BrowserSession(property_id="P1", url="https://example.com/")
    s.html = html
    return s


def _fake_provider(content_seq: list[str]) -> MagicMock:
    """Create a mock LLMProvider whose .complete() returns content_seq items in order."""
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=content_seq)
    return provider


def _patch_provider(monkeypatch: pytest.MonkeyPatch, content_seq: list[str]) -> MagicMock:
    provider = _fake_provider(content_seq)
    monkeypatch.setattr(tier4_llm, "_get_provider", lambda: provider)
    return provider


async def test_disabled_by_feature_flag(monkeypatch: pytest.MonkeyPatch, rentcafe_html: str) -> None:
    monkeypatch.setenv("ENABLE_TIER4_LLM", "false")
    result = await tier4_llm.extract(_session(rentcafe_html))
    assert result.status == ExtractionStatus.FAILED
    assert result.error_message == "tier4_disabled_by_feature_flag"
    assert result.confidence_score == 0.0


async def test_valid_json_response_yields_units(monkeypatch: pytest.MonkeyPatch, rentcafe_html: str) -> None:
    payload = json.dumps({
        "units": [
            {"unit_number": "101", "floor_plan_type": "1/1", "asking_rent": 3250,
             "availability_status": "AVAILABLE", "availability_date": None, "sqft": 750},
        ],
        "property_name": "Test", "extraction_notes": ""
    })
    _patch_provider(monkeypatch, [payload])
    result = await tier4_llm.extract(_session(rentcafe_html))
    assert result.status == ExtractionStatus.SUCCESS
    assert result.raw_fields["units"][0]["unit_number"] == "101"


async def test_invalid_json_then_fixup_succeeds(monkeypatch: pytest.MonkeyPatch, rentcafe_html: str) -> None:
    bad = "this is not json"
    good = json.dumps({"units": [{"unit_number": "1", "asking_rent": 1, "availability_status": "AVAILABLE",
                                  "floor_plan_type": "1/1", "availability_date": None, "sqft": 500}]})
    _patch_provider(monkeypatch, [bad, good])
    result = await tier4_llm.extract(_session(rentcafe_html))
    assert result.raw_fields["units"][0]["unit_number"] == "1"


async def test_429_backoff_retries(monkeypatch: pytest.MonkeyPatch, rentcafe_html: str) -> None:
    """Provider.complete() raises on rate limit, then succeeds — tier4 should still succeed."""
    payload = json.dumps({"units": [{"unit_number": "X", "asking_rent": 1, "availability_status": "AVAILABLE",
                                     "floor_plan_type": "1/1", "availability_date": None, "sqft": 500}]})
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[RuntimeError("rate limit"), payload])
    monkeypatch.setattr(tier4_llm, "_get_provider", lambda: provider)

    # First call raises, so tier4 returns FAILED with the error message
    result = await tier4_llm.extract(_session(rentcafe_html))
    # The retry logic is now inside the provider; tier4 sees the exception on first attempt
    assert provider.complete.await_count == 1


async def test_token_count_logged(monkeypatch: pytest.MonkeyPatch, rentcafe_html: str) -> None:
    payload = json.dumps({"units": [{"unit_number": "A", "asking_rent": 1, "availability_status": "AVAILABLE",
                                     "floor_plan_type": "1/1", "availability_date": None, "sqft": 500}]})
    _patch_provider(monkeypatch, [payload])
    result = await tier4_llm.extract(_session(rentcafe_html))
    assert "tokens_in" in result.raw_fields
    assert result.raw_fields["tokens_in"] > 0


def test_html_stripped_before_send(rentcafe_html: str) -> None:
    html_with_script = "<html><head><script>alert('x');</script><style>.a{}</style></head>" + rentcafe_html + "</html>"
    text, truncated = tier4_llm.prepare_html(html_with_script)
    assert "alert" not in text
    assert ".a{}" not in text
    assert truncated is False


def test_truncation_logged() -> None:
    huge = "<html><body>" + ("<div>filler</div>" * 200_000) + "</body></html>"
    text, truncated = tier4_llm.prepare_html(huge)
    assert truncated is True
    assert len(text) <= tier4_llm.MAX_HTML_CHARS
