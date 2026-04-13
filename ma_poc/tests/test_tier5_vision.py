"""Tests for extraction/tier5_vision.py + vision_banner + vision_sample — 5+ tests."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from extraction import tier5_vision, vision_banner, vision_sample
from llm.anthropic import AnthropicLLMProvider
from llm.azure import AzureLLMProvider
from llm.images import check_size
from scraper.browser import BrowserSession

ANTHROPIC_LIMIT_BYTES = 5 * 1024 * 1024
AZURE_LIMIT_BYTES = 20 * 1024 * 1024


def test_size_check_passthrough_small() -> None:
    data = b"x" * 100
    assert check_size(data, AZURE_LIMIT_BYTES) == data
    assert check_size(data, ANTHROPIC_LIMIT_BYTES) == data


def test_size_check_truncates_over_limit() -> None:
    data = b"x" * (ANTHROPIC_LIMIT_BYTES + 10)
    out = check_size(data, ANTHROPIC_LIMIT_BYTES)
    assert len(out) <= ANTHROPIC_LIMIT_BYTES


async def test_azure_provider_image_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    provider = AzureLLMProvider()
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"units":[]}'))])

    provider._client.chat.completions.create = fake_create  # type: ignore[method-assign]
    await provider.extract_from_images([b"PNGDATA"], "prompt")
    msg = captured["messages"][0]
    parts = msg["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_anthropic_provider_image_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider = AnthropicLLMProvider()
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text='{"units":[]}')])

    provider._client.messages.create = fake_create  # type: ignore[method-assign]
    await provider.extract_from_images([b"PNG"], "prompt")
    parts = captured["messages"][0]["content"]
    assert parts[0]["type"] == "image"
    assert parts[0]["source"]["media_type"] == "image/png"


async def test_role_a_falls_back_to_full_screenshot(tmp_path: Path) -> None:
    s = BrowserSession(property_id="P1", url="https://x/")
    p = tmp_path / "shot.png"
    p.write_bytes(b"FAKEPNG")
    s.screenshot_path = p
    images = await tier5_vision._capture_targeted_sections(s)
    assert images == [b"FAKEPNG"]


async def test_role_b_banner_text_detected(rentcafe_html: str) -> None:
    s = BrowserSession(property_id="P1", url="https://x/")
    s.html = rentcafe_html
    banner = await vision_banner.capture_banner(s)
    assert banner is not None
    assert banner["source"] in ("TEXT_SNIPPET", "IMAGE_BANNER")


def test_role_c_sample_selection_deterministic() -> None:
    today = date(2026, 4, 8)
    a = vision_sample.select_for_sample("PROP-1", today)
    b = vision_sample.select_for_sample("PROP-1", today)
    assert a == b


async def test_role_c_writes_comparison_file(tmp_path: Path) -> None:
    from models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier

    primary = ExtractionResult(
        property_id="P1",
        tier=ExtractionTier.PLAYWRIGHT_TPL,
        status=ExtractionStatus.SUCCESS,
        confidence_score=0.9,
        raw_fields={"units": [{"unit_number": "1", "asking_rent": 100}]},
    )
    out = await vision_sample.write_sample_comparison(tmp_path, "P1", date(2026, 4, 8), primary=primary)
    assert out.exists()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["property_id"] == "P1"
