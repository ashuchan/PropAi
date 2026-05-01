"""E5 escalator tests — DLQ_PARK terminal, probe demotion, cost ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.tier_escalator import (
    PROBE_AFTER_N_SUCCESSES,
    PROBE_MIN_INTERVAL_HOURS,
    _should_probe_lower,
    fetch_with_escalation,
)
from ma_poc.models.fetch_tier import FetchTier


def _make_task() -> MagicMock:
    task = MagicMock()
    task.url = "https://example.com"
    task.property_id = "prop-e5"
    task.render_mode = RenderMode.GET
    task.budget_ms = 10000
    return task


def _make_profile(
    floor: FetchTier = FetchTier.DC_PROXY,
    successes: int = 0,
    last_probe_at: datetime | None = None,
) -> MagicMock:
    profile = MagicMock()
    fp = MagicMock()
    fp.tier_floor = floor
    fp.consecutive_successes_at_floor = successes
    fp.last_demotion_probe_at = last_probe_at
    profile.fetch = fp
    return profile


def _make_result(
    outcome: FetchOutcome = FetchOutcome.OK,
    tier: int = 0,
) -> FetchResult:
    return FetchResult(
        url="https://example.com",
        outcome=outcome,
        status=200 if outcome == FetchOutcome.OK else 403,
        body=b"",
        headers={},
        render_mode=RenderMode.GET,
        final_url="https://example.com",
        attempts=1,
        elapsed_ms=50,
        fetch_tier_used=tier,
        fetch_tier_attempts=[tier],
    )


# ── _should_probe_lower ────────────────────────────────────────────────────────

def test_probe_not_needed_when_floor_is_direct() -> None:
    profile = _make_profile(floor=FetchTier.DIRECT, successes=99)
    assert _should_probe_lower(profile) is None


def test_probe_not_needed_when_too_few_successes() -> None:
    profile = _make_profile(floor=FetchTier.DC_PROXY, successes=PROBE_AFTER_N_SUCCESSES - 1)
    assert _should_probe_lower(profile) is None


def test_probe_triggered_after_n_successes() -> None:
    profile = _make_profile(floor=FetchTier.DC_PROXY, successes=PROBE_AFTER_N_SUCCESSES)
    probe = _should_probe_lower(profile)
    assert probe == FetchTier.DIRECT


def test_probe_blocked_by_interval() -> None:
    recent = datetime.now(UTC) - timedelta(hours=PROBE_MIN_INTERVAL_HOURS - 1)
    profile = _make_profile(
        floor=FetchTier.DC_PROXY,
        successes=PROBE_AFTER_N_SUCCESSES + 10,
        last_probe_at=recent,
    )
    assert _should_probe_lower(profile) is None


def test_probe_allowed_after_interval() -> None:
    old = datetime.now(UTC) - timedelta(hours=PROBE_MIN_INTERVAL_HOURS + 1)
    profile = _make_profile(
        floor=FetchTier.DC_PROXY,
        successes=PROBE_AFTER_N_SUCCESSES + 10,
        last_probe_at=old,
    )
    assert _should_probe_lower(profile) == FetchTier.DIRECT


# ── DLQ_PARK terminal ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ladder_exhaustion_returns_dlq_park_tier() -> None:
    """When all enabled tiers return BOT_BLOCKED, result.fetch_tier_used == DLQ_PARK."""
    task = _make_task()
    profile = _make_profile(floor=FetchTier.DIRECT, successes=0)
    profile.fetch.last_demotion_probe_at = None

    blocked = _make_result(FetchOutcome.BOT_BLOCKED, tier=0)

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=blocked)),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.fetch_tier_used == int(FetchTier.DLQ_PARK)
    assert result.outcome == FetchOutcome.BOT_BLOCKED


@pytest.mark.asyncio
async def test_full_e5_ladder_direct_dc_resi_unlocker_dlq() -> None:
    """Full E5 path: all tiers blocked → DLQ_PARK."""
    task = _make_task()
    profile = _make_profile(floor=FetchTier.DIRECT, successes=0)
    profile.fetch.last_demotion_probe_at = None

    b0 = _make_result(FetchOutcome.BOT_BLOCKED, tier=int(FetchTier.DIRECT))
    b2 = _make_result(FetchOutcome.BOT_BLOCKED, tier=int(FetchTier.DC_PROXY))
    b3 = _make_result(FetchOutcome.BOT_BLOCKED, tier=int(FetchTier.RESIDENTIAL))
    b4 = _make_result(FetchOutcome.BOT_BLOCKED, tier=int(FetchTier.UNLOCKER))

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", True),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=b0)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", AsyncMock(return_value=b2)),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.residential.ResidentialProvider.fetch", AsyncMock(return_value=b3)),
        patch("ma_poc.fetch.providers.residential.BrightDataProvider.__init__", return_value=None),
        patch("ma_poc.fetch.providers.unlocker.UnlockerProvider.fetch", AsyncMock(return_value=b4)),
        patch("ma_poc.fetch.providers.unlocker._build_proxy_url", return_value="http://fake@proxy:1"),
    ):
        result = await fetch_with_escalation(task, profile)

    assert result.fetch_tier_used == int(FetchTier.DLQ_PARK)


# ── Probe demotion in escalator ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_demotion_success_short_circuits() -> None:
    """When probe at lower tier succeeds, return immediately without running full ladder."""
    task = _make_task()
    profile = _make_profile(
        floor=FetchTier.DC_PROXY,
        successes=PROBE_AFTER_N_SUCCESSES,
        last_probe_at=None,
    )

    probe_ok = _make_result(FetchOutcome.OK, tier=int(FetchTier.DIRECT))
    dc_mock = AsyncMock(return_value=_make_result(FetchOutcome.OK, tier=int(FetchTier.DC_PROXY)))

    with (
        patch("ma_poc.fetch.tier_escalator.ENABLE_TIER_ESCALATION", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_DC_PROXY_TIER", True),
        patch("ma_poc.fetch.tier_escalator.ENABLE_RESIDENTIAL_TIER", False),
        patch("ma_poc.fetch.tier_escalator.ENABLE_UNLOCKER_TIER", False),
        patch("ma_poc.fetch.providers.direct.DirectProvider.fetch", AsyncMock(return_value=probe_ok)),
        patch("ma_poc.fetch.providers.dc_proxy.DcProxyProvider.fetch", dc_mock),
        patch("ma_poc.fetch.providers.dc_proxy.BrightDataProvider.__init__", return_value=None),
    ):
        result = await fetch_with_escalation(task, profile)

    # Probe at DIRECT returned OK, so DC_PROXY should never have been called
    dc_mock.assert_not_awaited()
    assert result.outcome == FetchOutcome.OK
    assert result.fetch_tier_used == int(FetchTier.DIRECT)


# ── Cost ledger rollup_by_fetch_tier ──────────────────────────────────────────

def test_cost_ledger_record_and_rollup_fetch_tier(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from ma_poc.observability.cost_ledger import CostLedger

    ledger = CostLedger(tmp_path / "cost.db")
    ledger.record_fetch_tier("prop-001", "DIRECT", 0.0, body_bytes=1024)
    ledger.record_fetch_tier("prop-001", "DC_PROXY", 0.0005, body_bytes=2048, escalation_count=1)
    ledger.record_fetch_tier("prop-002", "DC_PROXY", 0.0005, body_bytes=1024)

    rollup = ledger.rollup_by_fetch_tier()
    assert "DIRECT" in rollup
    assert "DC_PROXY" in rollup
    assert rollup["DC_PROXY"] == pytest.approx(0.001, rel=1e-3)
    ledger.close()


# ── escalation_report.py ──────────────────────────────────────────────────────

def test_escalation_report_empty_events(tmp_path) -> None:
    from ma_poc.scripts.escalation_report import _summarise

    summary = _summarise([])
    assert summary["total_events"] == 0
    assert summary["dlq_parks"] == 0


def test_escalation_report_counts_events(tmp_path) -> None:
    from ma_poc.scripts.escalation_report import _summarise

    events = [
        {"kind": "fetch.tier_escalated", "tier": "DC_PROXY"},
        {"kind": "fetch.tier_escalated", "tier": "DC_PROXY"},
        {"kind": "fetch.tier_persisted", "new_floor": "DC_PROXY"},
        {"kind": "fetch.ladder_exhausted"},
        {"kind": "fetch.tier_probe_success"},
        {"kind": "fetch.tier_probe_failed"},
    ]
    summary = _summarise(events)
    assert summary["escalations_per_tier"]["DC_PROXY"] == 2
    assert summary["promotions_to_tier"]["DC_PROXY"] == 1
    assert summary["dlq_parks"] == 1
    assert summary["probe_successes"] == 1
    assert summary["probe_failures"] == 1
