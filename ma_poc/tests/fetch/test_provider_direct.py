"""Tests for fetch/providers/direct.py — DirectProvider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.providers.direct import DirectProvider
from ma_poc.models.fetch_tier import FetchTier


def _make_task(render_mode: RenderMode = RenderMode.GET) -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com/apartments"
    task.property_id = "prop-abc"
    task.render_mode = render_mode
    task.budget_ms = 10000
    return task


def _make_profile() -> MagicMock:
    return MagicMock()


def _mock_response(status: int = 200, body: bytes = b"<html>ok</html>") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": "text/html"}
    resp.url = "https://example.com/apartments"
    return resp


@pytest.mark.asyncio
async def test_direct_ok_response() -> None:
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    mock_resp = _mock_response(200)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.DIRECT)
    assert int(FetchTier.DIRECT) in result.fetch_tier_attempts


@pytest.mark.asyncio
async def test_direct_bot_blocked_no_retry() -> None:
    """BOT_BLOCKED should be returned immediately without retry."""
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    mock_resp = _mock_response(403, b"<html>cf-ray blocked</html>")
    mock_resp.headers = {"cf-ray": "abc123", "content-type": "text/html"}

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return mock_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = fake_request

        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert call_count == 1  # No retry on BOT_BLOCKED


@pytest.mark.asyncio
async def test_direct_sets_tier_fields() -> None:
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    mock_resp = _mock_response(200)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        result = await provider.fetch(task, profile)

    assert result.fetch_tier_used == int(FetchTier.DIRECT)
    assert len(result.fetch_tier_attempts) >= 1
    assert all(t == int(FetchTier.DIRECT) for t in result.fetch_tier_attempts)


@pytest.mark.asyncio
async def test_direct_exception_returns_transient() -> None:
    """Network exception should produce a TRANSIENT result."""
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(side_effect=ConnectionError("network down"))

        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.TRANSIENT


@pytest.mark.asyncio
async def test_direct_tier_name() -> None:
    provider = DirectProvider()
    assert provider.tier_name == "DIRECT"
