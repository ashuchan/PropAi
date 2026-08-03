"""COMPLIANCE_MODE kill-switch (RealPage legal 2026-07-22).

Pins that COMPLIANCE_MODE=1 forces every challenge-solver OFF while KEEPING the
code: (1) the flag helpers; (2) the web_unlocker_get chokepoint no-ops even with
a key set (covers all adapter-internal unlocker paths); (3) the escalator ladder
drops UNLOCKER + FLARESOLVERR even when their tier flags are on; and (4) the
ordinary retry loop cannot rotate the browser identity/fingerprint.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def test_flag_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    import ma_poc.config.feature_flags as ff

    monkeypatch.delenv("COMPLIANCE_MODE", raising=False)
    assert ff.compliance_mode() is False
    assert ff.web_unlocker_allowed() is True and ff.flaresolverr_allowed() is True
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    assert ff.compliance_mode() is True
    assert ff.web_unlocker_allowed() is False and ff.flaresolverr_allowed() is False


@pytest.mark.probe_seam  # asserts the compliance gate short-circuits BEFORE any network
def test_web_unlocker_get_blocked_even_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.pms.adapters._probe import web_unlocker_get

    # A key IS set — without the compliance gate this would attempt a network
    # call; the gate must short-circuit to the empty shape first (no network).
    monkeypatch.setenv("WEB_UNLOCKER_KEY", "fake-key-should-never-be-used")
    monkeypatch.setenv("COMPLIANCE_MODE", "1")
    resp = web_unlocker_get("https://walled.example/floorplans")
    assert resp.status_code == 0 and resp.text == ""  # no-op, cascade falls through


def test_ladder_drops_solvers_under_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.models.fetch_tier import FetchTier

    # Turn the solver tiers ON at import, then prove compliance overrides them.
    monkeypatch.setenv("ENABLE_TIER_ESCALATION", "true")
    monkeypatch.setenv("ENABLE_UNLOCKER_TIER", "true")
    monkeypatch.setenv("ENABLE_FLARESOLVERR_TIER", "true")
    import ma_poc.config.feature_flags as ff
    import ma_poc.fetch.tier_escalator as te
    importlib.reload(ff)
    importlib.reload(te)
    try:
        monkeypatch.delenv("COMPLIANCE_MODE", raising=False)
        ladder_off = te._build_ladder(0)
        assert FetchTier.UNLOCKER in ladder_off and FetchTier.FLARESOLVERR in ladder_off

        monkeypatch.setenv("COMPLIANCE_MODE", "1")
        ladder_on = te._build_ladder(0)
        assert FetchTier.UNLOCKER not in ladder_on
        assert FetchTier.FLARESOLVERR not in ladder_on
    finally:
        # restore clean import state for other tests
        monkeypatch.delenv("ENABLE_UNLOCKER_TIER", raising=False)
        monkeypatch.delenv("ENABLE_FLARESOLVERR_TIER", raising=False)
        importlib.reload(ff)
        importlib.reload(te)


@pytest.mark.asyncio
async def test_fetch_retry_cannot_rotate_identity_under_compliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second 429 must retry with the same sticky identity in compliance mode.

    This drives the real ``Fetcher.fetch`` retry branch with a deterministic
    fake response sequence.  It is deliberately stricter than checking the
    retry-policy decision: neither ``IdentityPool.rotate`` nor the legacy
    forced-proxy branch may emit/enter a rotation path.
    """
    from ma_poc.discovery.contracts import CrawlTask, TaskReason
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.fetch.fetcher import Fetcher
    from ma_poc.fetch.retry_policy import RetryDecision

    monkeypatch.setenv("COMPLIANCE_MODE", "1")

    identity = SimpleNamespace(user_agent="fixed-compliance-ua")

    class _Identities:
        def __init__(self) -> None:
            self.rotations = 0

        def pick(self, sticky_key: str | None = None):
            return identity

        def rotate(self, sticky_key: str) -> None:
            self.rotations += 1

    class _ProxyPool:
        def __init__(self) -> None:
            self.picks = 0

        def pick(self, sticky_key: str | None = None):
            self.picks += 1
            return "http://fresh-proxy.invalid:9999"

        def mark_failure(self, proxy: str, outcome: str) -> None:
            pass

        def mark_success(self, proxy: str) -> None:
            pass

    class _Robots:
        async def is_allowed(self, url: str, user_agent: str) -> bool:
            return True

    class _Limiter:
        async def acquire(self, host: str) -> None:
            return None

    class _Cache:
        def read(self, url: str):
            return None, None

        def write(self, url: str, etag, last_modified) -> None:
            pass

    identities = _Identities()
    proxies = _ProxyPool()

    class _Retry:
        def decide(self, outcome, attempt, retry_after_header=None):
            if outcome == FetchOutcome.OK:
                return RetryDecision(False, 0, False)
            return RetryDecision(True, 0, attempt >= 2)

    fetcher = Fetcher(
        proxy_pool=proxies,
        rate_limiter=_Limiter(),
        robots=_Robots(),
        cond_cache=_Cache(),
        identities=identities,
        browsers=SimpleNamespace(),
        retry=_Retry(),
    )

    attempts = 0

    async def _do_request(task, identity_arg, proxy, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        outcome = FetchOutcome.RATE_LIMITED if attempts < 3 else FetchOutcome.OK
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=429 if attempts < 3 else 200,
            body=b"ok" if attempts == 3 else b"rate limited",
            headers={},
            render_mode=task.render_mode,
            final_url=task.url,
            attempts=attempts,
            elapsed_ms=1,
        )

    monkeypatch.setattr(fetcher, "_do_request", _do_request)
    task = CrawlTask(
        property_id="compliance-429",
        url="https://example.invalid/floorplans",
        priority=0,
        budget_ms=30_000,
        render_mode=RenderMode.GET,
        reason=TaskReason.SCHEDULED,
    )

    result = await fetcher.fetch(task)

    assert result.outcome == FetchOutcome.OK
    assert attempts == 3
    assert identities.rotations == 0
    # ENABLE_DC_PROXY_TIER is off in the test environment.  More importantly,
    # the forced 429 branch must not ask for a fresh proxy under compliance.
    assert proxies.picks == 0
