"""Tier escalator — drives per-property fetch-tier escalation.

Only BOT_BLOCKED outcomes trigger escalation; TRANSIENT/PROXY_ERROR are handled
inside each provider's own retry loop. Escalation is capped at
MAX_ESCALATIONS_PER_RUN per call to fetch_with_escalation().

Feature-flag gated: when ENABLE_TIER_ESCALATION is False the escalator is a
no-op wrapper around DirectProvider.

Phase E3 wires DIRECT → DC_PROXY (when ENABLE_DC_PROXY_TIER is True).
Phase E4 adds RESIDENTIAL; Phase E5 adds UNLOCKER.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from ma_poc.config.feature_flags import (
    ENABLE_DC_PROXY_TIER,
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

# Tiers that are allowed to skip directly to a higher tier (e.g. from profile floor)
# Maps FetchTier → set of FetchTiers that may be skipped on the way up
TIER_SKIP_RULES: dict[FetchTier, set[FetchTier]] = {
    # If floor is already DC_PROXY, we don't retry DIRECT on BOT_BLOCKED
    FetchTier.DC_PROXY: {FetchTier.DIRECT},
    FetchTier.RESIDENTIAL: {FetchTier.DIRECT, FetchTier.DC_PROXY},
    FetchTier.UNLOCKER: {FetchTier.DIRECT, FetchTier.DC_PROXY, FetchTier.RESIDENTIAL},
}


def _build_ladder(floor: FetchTier) -> list[FetchTier]:
    """Return the ordered list of tiers to attempt, starting at floor.

    Only includes tiers that are enabled by feature flags.
    """
    all_tiers: list[tuple[FetchTier, bool]] = [
        (FetchTier.DIRECT, True),
        (FetchTier.DC_PROXY, ENABLE_DC_PROXY_TIER),
        (FetchTier.RESIDENTIAL, ENABLE_RESIDENTIAL_TIER),
        (FetchTier.UNLOCKER, ENABLE_UNLOCKER_TIER),
        (FetchTier.DLQ_PARK, True),  # always available as terminal
    ]
    return [
        t for t, enabled in all_tiers
        if enabled and t >= floor and t != FetchTier.DLQ_PARK
    ]


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
    raise ValueError(f"No provider for tier {tier!r}")


async def fetch_with_escalation(
    task: "CrawlTask",
    profile: "ScrapeProfile",
) -> FetchResult:
    """Fetch *task* with automatic tier escalation on BOT_BLOCKED.

    Args:
        task: The crawl task to fetch.
        profile: ScrapeProfile for this property (read: fetch.tier_floor).

    Returns:
        The FetchResult from the highest tier that was attempted.
        fetch_tier_used reflects the tier that produced the returned result.
        fetch_tier_attempts lists every tier attempted, in order.
    """
    if not ENABLE_TIER_ESCALATION:
        from ma_poc.fetch.providers.direct import DirectProvider
        return await DirectProvider().fetch(task, profile)

    floor = profile.fetch.tier_floor
    ladder = _build_ladder(floor)

    if not ladder:
        # Feature flags disabled everything — fall back to direct
        from ma_poc.fetch.providers.direct import DirectProvider
        return await DirectProvider().fetch(task, profile)

    all_attempts: list[int] = []
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
            # Return with the merged attempts list
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.HARD_FAIL:
            # No point escalating on a hard fail (DNS / SSL / 4xx)
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.BOT_BLOCKED:
            emit(EventKind.FETCH_TIER_PROBE_FAILED, task.property_id,
                 tier=tier.name, block_sig=result.block_signature)
            escalations += 1
            continue

        # TRANSIENT / RATE_LIMITED / PROXY_ERROR — escalate anyway
        escalations += 1

    # Ladder exhausted
    if last_result is not None:
        emit(EventKind.FETCH_LADDER_EXHAUSTED, task.property_id,
             escalations=escalations)
        return _merge_attempts(last_result, all_attempts)

    # Shouldn't happen — return a synthetic hard-fail
    from ma_poc.fetch.contracts import RenderMode
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
        fetch_tier_used=int(floor),
        fetch_tier_attempts=all_attempts,
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
