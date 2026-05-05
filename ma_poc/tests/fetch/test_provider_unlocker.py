"""Tests for fetch/providers/unlocker.py — UnlockerProvider (Phase E5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, RenderMode
from ma_poc.fetch.http_client import _AdapterResponse
from ma_poc.models.fetch_tier import FetchTier

_URL = "https://example.com/apartments"


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = _URL
    task.property_id = "prop-ulk-001"
    task.render_mode = RenderMode.GET
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


_UNLOCKER_ENV = {
    "BRIGHTDATA_CUSTOMER_ID": "cust123",
    "BRIGHTDATA_UNLOCKER_ZONE": "unblocker_zone",
    "BRIGHTDATA_UNLOCKER_PASSWORD": "secret",
}


@pytest.mark.asyncio
async def test_unlocker_sets_tier() -> None:
    task = _make_task()
    profile = _make_profile()

    with (
        patch.dict("os.environ", _UNLOCKER_ENV),
        patch(
            "ma_poc.fetch.providers.unlocker.make_http_client",
            return_value=_mock_adapter(_adapter_resp(200)),
        ),
    ):
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

    with (
        patch.dict("os.environ", _UNLOCKER_ENV),
        patch(
            "ma_poc.fetch.providers.unlocker.make_http_client",
            return_value=_mock_adapter(_adapter_resp(200)),
        ),
    ):
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
