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


# ─────────────────────────────────────────────────────────────────────
# 2026-05-21 regression guard — RENDER tasks MUST bypass tier_escalator.
#
# The cluster #4 profile-bootstrap fix (337ebaa) inadvertently broke
# RenderMode.RENDER tasks: with profile non-None after bootstrap, the
# fetcher.fetch() gate routed RENDER tasks through the tier_escalator,
# which uses curl_cffi providers that don't execute JavaScript. SPA
# properties (AppFolio, Avalon, Knock, RentCafe marketing sites) all
# returned empty bodies → "generic:no_body_short_circuit".
#
# Validated 2026-05-21: baseline image 4046a2a (pre-cluster #4 fix) had
# 50/50 cohort succeed via RENDER path; HEAD (post-fix) had 50/50 hit
# the escalator and 0/50 strict units. Fix: gate the escalator on
# task.render_mode != RENDER as well.
# ─────────────────────────────────────────────────────────────────────


def test_render_gate_source_contract() -> None:
    """Structural test — fetcher.py's escalator gate MUST exclude
    RenderMode.RENDER tasks. This pins the source-shape contract so
    accidental removal during refactor (the kind that caused the
    2026-05-21 cohort-wide regression) fails CI loudly.

    The gate at fetcher.Fetcher.fetch must contain all three conditions
    AND'd together right before the ``fetch_with_escalation`` import:
      - ENABLE_TIER_ESCALATION
      - profile is not None
      - task.render_mode != RenderMode.RENDER

    Without the third condition, AppFolio/Avalon/Knock/RentCafe SPA
    properties route to the curl_cffi tier_escalator that can't run JS
    and silently emit zero units.
    """
    import inspect
    import re
    from ma_poc.fetch.fetcher import Fetcher

    src = inspect.getsource(Fetcher.fetch)
    # The three gate conditions must appear in the same if-block.
    # Look for the ``from .tier_escalator import fetch_with_escalation``
    # statement (uniquely identifies the actual code, not the docstring)
    # and walk backward to find the gate condition.
    m = re.search(
        r"from \.tier_escalator import fetch_with_escalation",
        src,
    )
    assert m is not None, "tier_escalator import not found"
    pre_block = src[: m.start()]
    assert "ENABLE_TIER_ESCALATION" in pre_block
    assert "profile is not None" in pre_block
    assert "RenderMode.RENDER" in pre_block, (
        "RenderMode.RENDER exclusion missing from the escalator gate — "
        "RENDER tasks will route through the curl_cffi tier_escalator "
        "and silently fail on SPA properties (AppFolio/Avalon/Knock)"
    )
