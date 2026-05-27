"""Tests for fetch/providers/residential.py — ResidentialProvider (Phase E4)."""

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
    task.property_id = "prop-resi-001"
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


@pytest.mark.asyncio
async def test_residential_sets_tier() -> None:
    task = _make_task()
    profile = _make_profile()

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch(
            "ma_poc.fetch.providers.residential.make_http_client",
            return_value=_mock_adapter(_adapter_resp(200)),
        ),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@brd.superproxy.io:33335"
        mock_get_cfg.return_value = fake_cfg

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
        result = await provider.fetch(task, profile)

    assert result.fetch_tier_used == int(FetchTier.RESIDENTIAL)
    assert int(FetchTier.RESIDENTIAL) in result.fetch_tier_attempts
    assert result.outcome == FetchOutcome.OK


@pytest.mark.asyncio
async def test_residential_bot_blocked_retries_with_rotated_session() -> None:
    """BOT_BLOCKED on residential triggers exactly one retry with a
    rotated (salt+1) session — burning the same IP twice is wasted
    work, and BrightData's pool is large enough that a fresh session
    almost always lands on a different exit IP.

    If the rotated session also 403s, BOT_BLOCKED is returned for the
    escalator to take over.

    (2026-05-24: prior to session-rotation this asserted call_count==1
    on the assumption that retrying the same session was pointless.
    Now we DO retry, but with a rotated session — different IP, real
    chance of success.)"""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        return _adapter_resp(403, b"<html>blocked</html>")

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    captured_salts: list[int] = []

    def fake_get_config(*, tier, canonical_id, country="us", session_salt=0):
        captured_salts.append(session_salt)
        cfg = MagicMock()
        cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        cfg.session_id = f"s{session_salt}fake"
        return cfg

    tracker = SessionBurnTracker(rotate_after_failures=2)
    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config", side_effect=fake_get_config),
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
        patch("ma_poc.fetch.providers.residential.asyncio.sleep", AsyncMock()),
    ):
        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.BOT_BLOCKED
    assert call_count == 2, "BOT_BLOCKED triggers one rotated retry"
    assert captured_salts == [0, 1], (
        f"first attempt salt=0 (sticky), retry salt=1 (rotated); got {captured_salts}"
    )


@pytest.mark.asyncio
async def test_residential_force_rotates_session_on_threshold_bot_blocked() -> None:
    """When the burn counter crosses the rotate threshold (default 2),
    the provider must re-request a config with a bumped session_salt
    and try once more — getting a fresh BrightData exit IP.

    Verifies: 2 BOT_BLOCKED → get_config called twice with different
    session_salt values."""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        # Both attempts return BOT_BLOCKED so the rotated retry also fails
        # — exercises the full rotate-then-give-up path.
        return _adapter_resp(403, b"<html>blocked</html>")

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    # Threshold 2: the first failure leaves salt at 0, the second bumps
    # it to 1 — so the provider should retry once with salt=1.
    tracker = SessionBurnTracker(rotate_after_failures=2)
    captured_salts: list[int] = []

    def fake_get_config(*, tier, canonical_id, country="us", session_salt=0):
        captured_salts.append(session_salt)
        cfg = MagicMock()
        cfg.to_httpx_url.return_value = f"http://***@proxy:33335?salt={session_salt}"
        cfg.session_id = f"s{session_salt:02d}fake"
        return cfg

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config", side_effect=fake_get_config),
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
        patch("ma_poc.fetch.providers.residential.asyncio.sleep", AsyncMock()),  # skip 2s sleep
    ):
        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)
        result = await provider.fetch(task, profile)

    assert call_count == 2, f"expected 2 attempts (initial + rotated), got {call_count}"
    assert len(captured_salts) == 2, f"get_config should run twice, got {len(captured_salts)}"
    assert captured_salts[0] == 0, "first attempt must use salt=0 (sticky)"
    assert captured_salts[1] == 1, "second attempt must use rotated salt=1"
    assert result.outcome == FetchOutcome.BOT_BLOCKED


@pytest.mark.asyncio
async def test_residential_rotated_attempt_succeeds_returns_ok() -> None:
    """The happy-path of rotation: first attempt 403s, the rotated
    attempt with a fresh IP gets through. Provider returns OK."""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()

    responses = [
        _adapter_resp(403, b"<html>blocked</html>"),
        _adapter_resp(200, b"<html>real listings</html>"),
    ]
    call_idx = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_idx
        resp = responses[call_idx]
        call_idx += 1
        return resp

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    tracker = SessionBurnTracker(rotate_after_failures=2)

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
        patch("ma_poc.fetch.providers.residential.asyncio.sleep", AsyncMock()),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)
        result = await provider.fetch(task, profile)

    assert call_idx == 2
    assert result.outcome == FetchOutcome.OK
    # The burn counter must be reset on success
    failures, salt, _ = tracker.state_snapshot(task.property_id)
    assert failures == 0, "successful fetch must reset failure count"
    # But the salt must remain — the rotated IP is the one that works
    assert salt == 1, "salt must stick after a successful rotation"


@pytest.mark.asyncio
async def test_residential_carries_burn_state_across_fetches() -> None:
    """Burn state is process-wide. A property that crossed the threshold
    in run N must start run N+1 already on the rotated salt — no second
    burn cycle required to reach the working IP."""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()

    async def always_blocked(*args, **kwargs):  # noqa: ANN202
        return _adapter_resp(403, b"<html>blocked</html>")

    adapter = AsyncMock()
    adapter.request = always_blocked
    adapter.aclose = AsyncMock()

    tracker = SessionBurnTracker(rotate_after_failures=2)
    captured_salts: list[int] = []

    def fake_get_config(*, tier, canonical_id, country="us", session_salt=0):
        captured_salts.append(session_salt)
        cfg = MagicMock()
        cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        cfg.session_id = f"s{session_salt}"
        return cfg

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config", side_effect=fake_get_config),
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
        patch("ma_poc.fetch.providers.residential.asyncio.sleep", AsyncMock()),
    ):
        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)

        # Run 1: should attempt salt=0, then rotate to salt=1
        await provider.fetch(task, profile)
        # Run 2: should START on salt=1 (carried over), then rotate to salt=2
        await provider.fetch(task, profile)

    # Expected sequence: [0, 1, 1, 2] — run1 uses 0 then 1, run2 inherits
    # the rotated salt and bumps once more.
    assert captured_salts == [0, 1, 1, 2], (
        f"expected [0,1,1,2] across two fetches, got {captured_salts}"
    )


@pytest.mark.asyncio
async def test_residential_ok_resets_failure_count_not_salt() -> None:
    """A successful fetch resets the consecutive-failure counter but
    keeps the current salt — so the working IP stays in use."""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()
    tracker = SessionBurnTracker(rotate_after_failures=2)

    # Prime the tracker: simulate one prior failure that hasn't rotated yet
    tracker.mark_failure(task.property_id)
    failures, salt, _ = tracker.state_snapshot(task.property_id)
    assert (failures, salt) == (1, 0)

    adapter = AsyncMock()
    adapter.request = AsyncMock(return_value=_adapter_resp(200, b"<html>ok</html>"))
    adapter.aclose = AsyncMock()

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.OK
    failures_after, salt_after, _ = tracker.state_snapshot(task.property_id)
    assert failures_after == 0, "OK must reset counter"
    assert salt_after == 0, "salt was not bumped (we were below threshold)"


@pytest.mark.asyncio
async def test_residential_hard_fail_does_not_trigger_rotation() -> None:
    """HARD_FAIL (TLS errors, NXDOMAIN, 4xx-not-blocked) is terminal —
    no proxy rotation will help, so the provider must NOT burn a
    fresh session on these."""
    from ma_poc.fetch.proxy.session_burn import SessionBurnTracker

    task = _make_task()
    profile = _make_profile()
    tracker = SessionBurnTracker(rotate_after_failures=1)  # aggressive

    call_count = 0

    async def fake_request(*args, **kwargs):  # noqa: ANN202
        nonlocal call_count
        call_count += 1
        # 451 → HARD_FAIL after classify (4xx not in dead-url set)
        return _adapter_resp(401, b"<html>nope</html>")

    adapter = AsyncMock()
    adapter.request = fake_request
    adapter.aclose = AsyncMock()

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch("ma_poc.fetch.providers.residential.make_http_client", return_value=adapter),
        patch("ma_poc.fetch.providers.residential.asyncio.sleep", AsyncMock()),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider(burn_tracker=tracker)
        result = await provider.fetch(task, profile)

    assert result.outcome == FetchOutcome.HARD_FAIL
    assert call_count == 1, "HARD_FAIL must not trigger a rotated retry"
    # Tracker must not record a failure on HARD_FAIL
    failures, salt, _ = tracker.state_snapshot(task.property_id)
    assert failures == 0 and salt == 0


@pytest.mark.asyncio
async def test_residential_uses_sticky_session() -> None:
    """Residential provider must pass property_id as canonical_id to BrightData."""
    task = _make_task()
    profile = _make_profile()

    with (
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.get_config") as mock_get_cfg,
        patch(
            "ma_poc.fetch.providers.residential.make_http_client",
            return_value=_mock_adapter(_adapter_resp(200)),
        ),
    ):
        fake_cfg = MagicMock()
        fake_cfg.to_httpx_url.return_value = "http://***@proxy:33335"
        mock_get_cfg.return_value = fake_cfg

        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
        await provider.fetch(task, profile)

    # Verify canonical_id was passed (sticky session)
    call_kwargs = mock_get_cfg.call_args
    assert call_kwargs.kwargs["canonical_id"] == task.property_id


@pytest.mark.asyncio
async def test_escalator_includes_residential_when_enabled() -> None:
    """With ENABLE_RESIDENTIAL_TIER=True, ladder should include RESIDENTIAL."""
    from ma_poc.fetch.tier_escalator import _build_ladder

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
    ):
        ladder = _build_ladder(FetchTier.DIRECT)

    assert FetchTier.RESIDENTIAL in ladder
    assert ladder.index(FetchTier.RESIDENTIAL) > ladder.index(FetchTier.DC_PROXY)


def test_residential_tier_name() -> None:
    with patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None):
        from ma_poc.fetch.providers.residential import ResidentialProvider
        provider = ResidentialProvider()
    assert provider.tier_name == "RESIDENTIAL"


@pytest.mark.asyncio
async def test_escalation_direct_blocked_dc_blocked_residential_ok() -> None:
    """Full E4 escalation path: DIRECT→DC_PROXY→RESIDENTIAL on BOT_BLOCKED."""
    from ma_poc.fetch.contracts import FetchResult
    from ma_poc.fetch.tier_escalator import fetch_with_escalation

    task = _make_task()
    profile = MagicMock()
    profile.fetch = MagicMock()
    profile.fetch.tier_floor = FetchTier.DIRECT

    blocked_direct = FetchResult(
        url=task.url, outcome=FetchOutcome.BOT_BLOCKED, status=403, body=b"",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=50, fetch_tier_used=0, fetch_tier_attempts=[0],
        block_signature="cf_turnstile",
    )
    blocked_dc = FetchResult(
        url=task.url, outcome=FetchOutcome.BOT_BLOCKED, status=403, body=b"",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=50, fetch_tier_used=2, fetch_tier_attempts=[2],
        block_signature="cf_turnstile",
    )
    ok_resi = FetchResult(
        url=task.url, outcome=FetchOutcome.OK, status=200, body=b"<html>ok</html>",
        headers={}, render_mode=RenderMode.GET, final_url=task.url,
        attempts=1, elapsed_ms=150, fetch_tier_used=3, fetch_tier_attempts=[3],
    )

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=blocked_direct)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", AsyncMock(return_value=blocked_dc)),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.ResidentialProvider.fetch", AsyncMock(return_value=ok_resi)),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.RESIDENTIAL)
    assert int(FetchTier.DIRECT) in result.fetch_tier_attempts
    assert int(FetchTier.DC_PROXY) in result.fetch_tier_attempts
    assert int(FetchTier.RESIDENTIAL) in result.fetch_tier_attempts
