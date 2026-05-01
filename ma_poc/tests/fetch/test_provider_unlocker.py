"""Tests for fetch/providers/unlocker.py — UnlockerProvider (Phase E5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.models.fetch_tier import FetchTier


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com/apartments"
    task.property_id = "prop-ulk-001"
    task.render_mode = RenderMode.GET
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


_UNLOCKER_ENV = {
    "BRIGHTDATA_CUSTOMER_ID": "cust123",
    "BRIGHTDATA_UNLOCKER_ZONE": "unblocker_zone",
    "BRIGHTDATA_UNLOCKER_PASSWORD": "secret",
}


@pytest.mark.asyncio
async def test_unlocker_sets_tier() -> None:
    task = _make_task()
    profile = _make_profile()
    mock_resp = _mock_response(200)

    with (
        patch.dict("os.environ", _UNLOCKER_ENV),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        from ma_poc.fetch.providers.unlocker import UnlockerProvider
        provider = UnlockerProvider()
        result = await provider.fetch(task, profile)

    assert result.fetch_tier_used == int(FetchTier.UNLOCKER)
    assert int(FetchTier.UNLOCKER) in result.fetch_tier_attempts
    assert result.outcome == FetchOutcome.OK


@pytest.mark.asyncio
async def test_unlocker_missing_env_raises() -> None:
    """UnlockerProvider must raise RuntimeError when env vars are absent."""
    import importlib

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="BRIGHTDATA"):
            # Re-import to bypass any cached module state
            import ma_poc.fetch.providers.unlocker as _mod
            importlib.reload(_mod)
            _mod.UnlockerProvider()


@pytest.mark.asyncio
async def test_unlocker_redacts_proxy_in_result() -> None:
    task = _make_task()
    profile = _make_profile()
    mock_resp = _mock_response(200)

    with (
        patch.dict("os.environ", _UNLOCKER_ENV),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.request = AsyncMock(return_value=mock_resp)

        from ma_poc.fetch.providers.unlocker import UnlockerProvider
        provider = UnlockerProvider()
        result = await provider.fetch(task, profile)

    # No raw credentials in proxy_used
    assert result.proxy_used is not None
    assert "secret" not in (result.proxy_used or "")


@pytest.mark.asyncio
async def test_unlocker_in_escalation_ladder() -> None:
    """With ENABLE_UNLOCKER_TIER=True, ladder must include UNLOCKER."""
    from ma_poc.fetch.tier_escalator import _build_ladder

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", True),
    ):
        ladder = _build_ladder(FetchTier.DIRECT)

    assert FetchTier.UNLOCKER in ladder
    assert ladder[-1] == FetchTier.UNLOCKER
