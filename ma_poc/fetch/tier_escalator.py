"""Tier escalator — drives per-property fetch-tier escalation.

Only BOT_BLOCKED outcomes trigger escalation; TRANSIENT/PROXY_ERROR are handled
inside each provider's own retry loop. Escalation is capped at
MAX_ESCALATIONS_PER_RUN per call to fetch_with_escalation().

Feature-flag gated: when ENABLE_TIER_ESCALATION is False the escalator is a
no-op wrapper around DirectProvider.

Phase E3: DIRECT → DC_PROXY
Phase E4: adds RESIDENTIAL + probe-demotion check
Phase E5: adds UNLOCKER + DLQ_PARK terminal
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ma_poc.config.feature_flags import (
    ENABLE_DC_PROXY_TIER,
    ENABLE_FLARESOLVERR_TIER,
    ENABLE_RESIDENTIAL_TIER,
    ENABLE_TIER_ESCALATION,
    ENABLE_UNLOCKER_TIER,
)
from ma_poc.fetch.contracts import FetchOutcome, FetchResult
from ma_poc.models.fetch_tier import FetchTier
from ma_poc.observability.events import EventKind, emit

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.fetch.providers.base import FetchProvider
    from ma_poc.models.scrape_profile import ScrapeProfile

log = logging.getLogger(__name__)

# Hard cap on per-call escalation hops (prevents runaway cost on a bad actor)
MAX_ESCALATIONS_PER_RUN: int = 3

# After this many consecutive successes at a non-DIRECT floor, try probing lower
PROBE_AFTER_N_SUCCESSES: int = 5

# Minimum time between demotion probes for the same property
PROBE_MIN_INTERVAL_HOURS: int = 24

# Tiers that are allowed to skip directly to a higher tier (e.g. from profile floor)
# Maps FetchTier → set of FetchTiers that may be skipped on the way up
TIER_SKIP_RULES: dict[FetchTier, set[FetchTier]] = {
    FetchTier.DC_PROXY: {FetchTier.DIRECT},
    FetchTier.RESIDENTIAL: {FetchTier.DIRECT, FetchTier.DC_PROXY},
    FetchTier.UNLOCKER: {FetchTier.DIRECT, FetchTier.DC_PROXY, FetchTier.RESIDENTIAL},
}


def _build_ladder(floor: FetchTier) -> list[FetchTier]:
    """Return the ordered list of tiers to attempt, starting at floor.

    Only includes tiers that are enabled by feature flags.
    DLQ_PARK is never included in the active ladder — it's signalled by
    exhaustion.
    """
    all_tiers: list[tuple[FetchTier, bool]] = [
        (FetchTier.DIRECT, True),
        (FetchTier.DC_PROXY, ENABLE_DC_PROXY_TIER),
        (FetchTier.RESIDENTIAL, ENABLE_RESIDENTIAL_TIER),
        # FlareSolverr sits between RESIDENTIAL and UNLOCKER. It handles CF
        # JS challenges locally (no proxy cost) but can't bypass WAF blocks.
        (FetchTier.FLARESOLVERR, ENABLE_FLARESOLVERR_TIER),
        (FetchTier.UNLOCKER, ENABLE_UNLOCKER_TIER),
    ]
    return [t for t, enabled in all_tiers if enabled and t >= floor]


def _should_probe_lower(profile: "ScrapeProfile") -> FetchTier | None:
    """Return a lower tier to probe, or None if no probe is needed.

    Conditions for a probe:
    - floor > DIRECT (there's somewhere lower to probe)
    - consecutive_successes_at_floor >= PROBE_AFTER_N_SUCCESSES
    - last_demotion_probe_at is None, or >= PROBE_MIN_INTERVAL_HOURS ago
    """
    fp = profile.fetch
    floor = fp.tier_floor
    if floor == FetchTier.DIRECT or int(floor) == 0:
        return None
    if fp.consecutive_successes_at_floor < PROBE_AFTER_N_SUCCESSES:
        return None

    # Find the highest enabled tier strictly below the current floor.
    # We iterate the enabled ladder in reverse and return the first tier < floor.
    # This skips STEALTH_LOCAL if it's not in the enabled ladder.
    lower_ladder = [t for t in _build_ladder(FetchTier.DIRECT) if t < floor]
    if not lower_ladder:
        return None
    probe_tier = lower_ladder[-1]  # highest enabled tier below floor

    # Check interval
    last_probe = fp.last_demotion_probe_at
    if last_probe is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=PROBE_MIN_INTERVAL_HOURS)
        if hasattr(last_probe, "tzinfo") and last_probe.tzinfo is not None:
            if last_probe > cutoff:
                return None
        else:
            last_probe_aware = last_probe.replace(tzinfo=UTC)
            if last_probe_aware > cutoff:
                return None

    return probe_tier


def _make_provider(tier: FetchTier) -> "FetchProvider":
    """Instantiate the appropriate provider for a given tier."""
    if tier == FetchTier.DIRECT:
        from ma_poc.fetch.providers.direct import DirectProvider
        return DirectProvider()
    if tier == FetchTier.DC_PROXY:
        from ma_poc.fetch.providers.dc_proxy import DcProxyProvider
        return DcProxyProvider()
    if tier == FetchTier.RESIDENTIAL:
        from ma_poc.fetch.providers.residential import ResidentialProvider
        return ResidentialProvider()
    if tier == FetchTier.UNLOCKER:
        from ma_poc.fetch.providers.unlocker import UnlockerProvider
        return UnlockerProvider()
    if tier == FetchTier.FLARESOLVERR:
        from ma_poc.fetch.providers.flaresolverr import FlareSolverrProvider
        return FlareSolverrProvider()
    raise ValueError(f"No provider for tier {tier!r}")


async def fetch_with_escalation(
    task: "CrawlTask",
    profile: "ScrapeProfile | None" = None,
) -> FetchResult:
    """Fetch *task* with automatic tier escalation on BOT_BLOCKED.

    Also runs a periodic demotion probe: when a property has succeeded N times
    at a non-DIRECT floor, try a lower tier first to see if bot-blocking has
    relaxed.

    Args:
        task: The crawl task to fetch.
        profile: ScrapeProfile for this property (read: fetch.tier_floor).
            When None (the common case — the top-level ``fetch/__init__``
            entry point doesn't thread a profile through), a default
            ScrapeProfile is constructed with tier_floor=DIRECT so the
            ladder still runs. The default profile is per-call and not
            persisted, so demotion-probe state doesn't accumulate across
            calls — this is intentional for the stateless entry-point case.

    Returns:
        The FetchResult from the highest tier that was attempted.
        fetch_tier_used reflects the tier that produced the returned result.
        fetch_tier_attempts lists every tier attempted, in order.
    """
    if not ENABLE_TIER_ESCALATION:
        from ma_poc.fetch.providers.direct import DirectProvider
        return await DirectProvider().fetch(task, profile)

    # 2026-05-24 — synthesize a default profile when the caller didn't
    # thread one through. Required for the production entry-point case
    # (``fetch/__init__.py:fetch(task)`` passes no profile, so the gate at
    # ``Fetcher.fetch`` would otherwise skip the escalator entirely).
    # The default profile gets tier_floor=DIRECT and no demotion-probe
    # history, so the ladder starts at the lowest tier and walks up on
    # BOT_BLOCKED — exactly the expected "follow tier escalation" semantic.
    if profile is None:
        from ma_poc.models.scrape_profile import ScrapeProfile  # noqa: I001
        canonical = getattr(task, "property_id", None) or "unknown"
        profile = ScrapeProfile(canonical_id=str(canonical))

    floor = profile.fetch.tier_floor
    all_attempts: list[int] = []

    # ── Demotion probe (Phase E4/E5) ─────────────────────────────────────────
    # Before the normal ladder, check if we should probe a lower tier.
    # If the probe succeeds, the profile updater will demote the floor.
    probe_tier = _should_probe_lower(profile)
    if probe_tier is not None:
        try:
            profile.fetch.last_demotion_probe_at = datetime.now(UTC)
            probe_provider = _make_provider(probe_tier)
            probe_result = await probe_provider.fetch(task, profile)
            all_attempts.extend(probe_result.fetch_tier_attempts or [int(probe_tier)])

            if probe_result.outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
                emit(EventKind.FETCH_TIER_PROBE_SUCCESS, task.property_id,
                     tier=probe_tier.name, probe=True)
                return _merge_attempts(probe_result, all_attempts)

            emit(EventKind.FETCH_TIER_PROBE_FAILED, task.property_id,
                 tier=probe_tier.name, probe=True, block_sig=probe_result.block_signature)
        except Exception as exc:
            log.debug("Demotion probe failed for %s: %s", task.property_id, exc)

    # ── Normal escalation ladder ──────────────────────────────────────────────
    ladder = _build_ladder(floor)

    if not ladder:
        from ma_poc.fetch.providers.direct import DirectProvider
        return await DirectProvider().fetch(task, profile)

    escalations = 0
    last_result: FetchResult | None = None

    for tier in ladder:
        if escalations > MAX_ESCALATIONS_PER_RUN:
            emit(EventKind.FETCH_LADDER_BUDGET_EXHAUSTED, task.property_id,
                 escalations=escalations, max=MAX_ESCALATIONS_PER_RUN)
            break

        emit(EventKind.FETCH_TIER_ESCALATED, task.property_id,
             tier=tier.name, escalations=escalations)

        try:
            provider = _make_provider(tier)
        except Exception as exc:
            log.warning("Could not build provider for tier %s: %s", tier.name, exc)
            escalations += 1
            continue

        try:
            result = await provider.fetch(task, profile)
        except NotImplementedError:
            log.debug("Provider %s not yet implemented, skipping", tier.name)
            escalations += 1
            continue
        except Exception as exc:
            log.warning("Provider %s raised unexpectedly: %s", tier.name, exc)
            escalations += 1
            continue

        all_attempts.extend(result.fetch_tier_attempts or [int(tier)])
        last_result = result

        outcome = result.outcome
        if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
            emit(EventKind.FETCH_TIER_PROBE_SUCCESS, task.property_id,
                 tier=tier.name, escalations=escalations)
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.HARD_FAIL:
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.BOT_BLOCKED:
            emit(EventKind.FETCH_TIER_PROBE_FAILED, task.property_id,
                 tier=tier.name, block_sig=result.block_signature)
            escalations += 1
            continue

        # TRANSIENT / RATE_LIMITED / PROXY_ERROR — escalate
        escalations += 1

    # ── Ladder exhausted → DLQ_PARK signal ───────────────────────────────────
    if last_result is not None:
        emit(EventKind.FETCH_LADDER_EXHAUSTED, task.property_id,
             escalations=escalations)
        # Return with DLQ_PARK tier marker so callers can park the property
        return _mark_dlq_park(last_result, all_attempts)

    # Synthetic hard-fail (ladder was empty)
    return FetchResult(
        url=task.url,
        outcome=FetchOutcome.HARD_FAIL,
        status=None,
        body=None,
        headers={},
        render_mode=task.render_mode,
        final_url=task.url,
        attempts=0,
        elapsed_ms=0,
        error_signature="LADDER_EMPTY",
        fetch_tier_used=int(FetchTier.DLQ_PARK),
        fetch_tier_attempts=all_attempts,
    )


def _mark_dlq_park(result: FetchResult, all_attempts: list[int]) -> FetchResult:
    """Return result with fetch_tier_used set to DLQ_PARK."""
    return FetchResult(
        url=result.url,
        outcome=result.outcome,
        status=result.status,
        body=result.body,
        headers=result.headers,
        render_mode=result.render_mode,
        final_url=result.final_url,
        attempts=result.attempts,
        elapsed_ms=result.elapsed_ms,
        network_log=result.network_log,
        etag=result.etag,
        last_modified=result.last_modified,
        error_signature=result.error_signature,
        proxy_used=result.proxy_used,
        fetch_tier_used=int(FetchTier.DLQ_PARK),
        fetch_tier_attempts=all_attempts,
        block_signature=result.block_signature,
    )


def _merge_attempts(result: FetchResult, all_attempts: list[int]) -> FetchResult:
    """Return a new FetchResult with fetch_tier_attempts replaced by all_attempts."""
    return FetchResult(
        url=result.url,
        outcome=result.outcome,
        status=result.status,
        body=result.body,
        headers=result.headers,
        render_mode=result.render_mode,
        final_url=result.final_url,
        attempts=result.attempts,
        elapsed_ms=result.elapsed_ms,
        network_log=result.network_log,
        etag=result.etag,
        last_modified=result.last_modified,
        error_signature=result.error_signature,
        proxy_used=result.proxy_used,
        fetch_tier_used=result.fetch_tier_used,
        fetch_tier_attempts=all_attempts,
        block_signature=result.block_signature,
    )
