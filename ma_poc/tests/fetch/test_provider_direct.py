"""Tests for fetch/providers/direct.py — DirectProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.fetch.http_client import _AdapterResponse
from ma_poc.fetch.providers.direct import DirectProvider
from ma_poc.models.fetch_tier import FetchTier

_URL = "https://example.com/apartments"


def _make_task(render_mode: RenderMode = RenderMode.GET) -> MagicMock:
    task = MagicMock()
    task.url = _URL
    task.property_id = "prop-abc"
    task.render_mode = render_mode
    task.budget_ms = 10000
    task.etag = None
    task.last_modified = None
    return task


def _make_profile() -> MagicMock:
    return MagicMock()


def _adapter_resp(status: int = 200, body: bytes = b"<html>ok</html>") -> _AdapterResponse:
    return _AdapterResponse(
        status_code=status,
        headers={"content-type": "text/html"},
        content=body,
        final_url=_URL,
        cookies={},
    )


def _mock_adapter(resp: _AdapterResponse) -> AsyncMock:
    adapter = AsyncMock()
    adapter.request = AsyncMock(return_value=resp)
    adapter.aclose = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_direct_ok_response() -> None:
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    with patch("ma_poc.fetch.providers.direct.make_http_client", return_value=_mock_adapter(_adapter_resp(200))):
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

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return _adapter_resp(403, b"<html>cf-ray blocked</html>")

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    with patch("ma_poc.fetch.providers.direct.make_http_client", return_value=adapter):
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert call_count == 1  # No retry on BOT_BLOCKED


@pytest.mark.asyncio
async def test_direct_sets_tier_fields() -> None:
    provider = DirectProvider()
    task = _make_task()
    profile = _make_profile()

    with patch("ma_poc.fetch.providers.direct.make_http_client", return_value=_mock_adapter(_adapter_resp(200))):
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

    adapter = AsyncMock()
    adapter.request = AsyncMock(side_effect=ConnectionError("network down"))
    adapter.aclose = AsyncMock()

    with patch("ma_poc.fetch.providers.direct.make_http_client", return_value=adapter):
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.TRANSIENT


@pytest.mark.asyncio
async def test_direct_tier_name() -> None:
    provider = DirectProvider()
    assert provider.tier_name == "DIRECT"
