"""Top-level ``ma_poc.fetch.fetch`` profile-arg contract.

2026-05-20 — Cluster #4 ``generic:no_body_short_circuit`` root-cause
investigation. The tier escalator at
``ma_poc.fetch.fetcher.Fetcher.fetch`` is gated on ``profile is not
None`` (line 197 fetcher.py): when no profile is supplied, escalation
is skipped entirely and only single-tier DIRECT runs. The runner's
top-level call used to be ``await jugnu_fetch(task)`` — no profile
argument — so first-run properties (whose profile hadn't been
bootstrapped yet at the L1 fetch point) never got escalated.

These tests pin two contracts:

  1. ``fetch(task)`` (no profile) — back-compat: takes the single-tier
     path through ``Fetcher.fetch`` with ``profile=None``.

  2. ``fetch(task, profile=...)`` — engages ``fetch_with_escalation``.
     Critical for Cloudflare-walled properties to reach RESIDENTIAL.

The escalator's own behavior is tested in
``test_tier_escalator.py``; this file only verifies the plumbing
between the top-level entry and the Fetcher singleton.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch import fetch as top_level_fetch
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode


def _ok_result() -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=FetchOutcome.OK,
        status=200,
        body=b"ok",
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=42,
    )


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com"
    task.property_id = "P-test"
    task.render_mode = RenderMode.GET
    return task


def _make_profile() -> MagicMock:
    profile = MagicMock()
    fp = MagicMock()
    fp.tier_floor = MagicMock()
    fp.tier_floor.value = 0  # DIRECT
    fp.consecutive_successes_at_floor = 0
    fp.last_demotion_probe_at = None
    profile.fetch = fp
    return profile


@pytest.mark.asyncio
async def test_top_level_fetch_passes_profile_none_by_default() -> None:
    """Back-compat: ``fetch(task)`` with no profile delegates to the
    Fetcher singleton with ``profile=None``. Critical because the
    singleton then gates ``fetch_with_escalation`` on that arg."""
    task = _make_task()
    expected = _ok_result()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch = AsyncMock(return_value=expected)
    with patch(
        "ma_poc.fetch.fetcher.get_default_fetcher", return_value=fake_fetcher
    ):
        result = await top_level_fetch(task)

    assert result is expected
    fake_fetcher.fetch.assert_awaited_once_with(task, profile=None)


@pytest.mark.asyncio
async def test_top_level_fetch_forwards_profile_kwarg() -> None:
    """``fetch(task, profile=...)`` propagates the profile through to
    ``Fetcher.fetch`` so the inner escalator gate (``if ... and profile
    is not None``) engages."""
    task = _make_task()
    profile = _make_profile()
    expected = _ok_result()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch = AsyncMock(return_value=expected)
    with patch(
        "ma_poc.fetch.fetcher.get_default_fetcher", return_value=fake_fetcher
    ):
        result = await top_level_fetch(task, profile=profile)

    assert result is expected
    fake_fetcher.fetch.assert_awaited_once_with(task, profile=profile)


@pytest.mark.asyncio
async def test_top_level_fetch_signature_accepts_positional_profile() -> None:
    """Defensive — accept profile as positional too. Doesn't matter
    for current callers but keeps the API forgiving."""
    task = _make_task()
    profile = _make_profile()
    expected = _ok_result()

    fake_fetcher = MagicMock()
    fake_fetcher.fetch = AsyncMock(return_value=expected)
    with patch(
        "ma_poc.fetch.fetcher.get_default_fetcher", return_value=fake_fetcher
    ):
        result = await top_level_fetch(task, profile)

    assert result is expected
    fake_fetcher.fetch.assert_awaited_once_with(task, profile=profile)
