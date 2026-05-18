"""Tests for Bug #5 Layer 2 (2026-05-18) — in-job domain quarantine.

When a host racks up N consecutive RATE_LIMITED responses in one shard's
run, the fetcher quarantines the host for the rest of that run. Subsequent
fetches return RATE_LIMITED immediately with no actual request — saving
proxy budget + shard wallclock.

These tests pin the fetcher-state mechanics. The end-to-end retry-loop
behaviour is covered by the existing fetcher integration tests.
"""

from __future__ import annotations

import inspect

from ma_poc.fetch import fetcher as _fetcher_mod
from ma_poc.fetch.fetcher import Fetcher, _DOMAIN_QUARANTINE_THRESHOLD


_SRC = inspect.getsource(_fetcher_mod)


def test_quarantine_threshold_constant_exists_and_positive() -> None:
    """Threshold must be ≥1; otherwise quarantine fires on first 429
    (no give-and-take with the rate-limiter decay)."""
    assert _DOMAIN_QUARANTINE_THRESHOLD >= 1


def test_fetcher_initialises_quarantine_state() -> None:
    """Both the counter and the quarantine set must exist on the fetcher
    instance so future fetches can mutate / read them."""
    # Build a minimal fetcher; we only need it to exercise __init__.
    from unittest.mock import MagicMock

    f = Fetcher(
        proxy_pool=MagicMock(),
        rate_limiter=MagicMock(),
        robots=MagicMock(),
        cond_cache=MagicMock(),
        identities=MagicMock(),
        browsers=MagicMock(),
        retry=MagicMock(),
    )
    assert hasattr(f, "_domain_429_consecutive")
    assert isinstance(f._domain_429_consecutive, dict)
    assert hasattr(f, "_domain_quarantined")
    assert isinstance(f._domain_quarantined, set)
    assert len(f._domain_quarantined) == 0


# ── Source-grep guardrails (Bug #5 Layer 2 + Layer 1 contracts) ──────────────


def test_fetcher_short_circuits_on_quarantined_host() -> None:
    """The top of fetch() must check ``host in self._domain_quarantined``
    and bail out with RATE_LIMITED before any network I/O."""
    assert "host in self._domain_quarantined" in _SRC, (
        "Bug #5 Layer 2 regression: the in-job domain quarantine check "
        "was removed from the top of fetch()."
    )
    assert "DOMAIN_QUARANTINED_IN_RUN" in _SRC, (
        "The quarantine short-circuit must emit a distinct error_signature "
        "so failures.csv can be bucketed correctly."
    )


def test_fetcher_increments_consecutive_429_counter() -> None:
    """On every RATE_LIMITED outcome, the per-host counter must increment."""
    # Look for the counter-increment pattern.
    assert "_domain_429_consecutive[host]" in _SRC


def test_fetcher_quarantines_at_threshold() -> None:
    """When the counter reaches _DOMAIN_QUARANTINE_THRESHOLD, the host
    must be added to _domain_quarantined."""
    assert "_domain_quarantined.add(host)" in _SRC
    assert "_DOMAIN_QUARANTINE_THRESHOLD" in _SRC


def test_fetcher_resets_counter_on_OK() -> None:
    """Counter must reset to 0 on any OK response; otherwise a host with
    a transient 429 hours ago accumulates to quarantine eventually."""
    # The reset is the elif branch on FetchOutcome.OK.
    assert "FetchOutcome.OK" in _SRC
    # Verify the reset is wired to the counter.
    assert "_domain_429_consecutive[host] = 0" in _SRC


def test_fetcher_calls_rate_limiter_on_429() -> None:
    """Bug #5 Layer 1 hook: every RATE_LIMITED triggers rate-limiter decay."""
    assert "self._rate_limiter.on_rate_limited(host)" in _SRC


def test_fetcher_quarantine_threshold_env_overridable() -> None:
    """Operators must be able to tune the threshold without a redeploy."""
    assert "FETCH_DOMAIN_QUARANTINE_429" in _SRC
