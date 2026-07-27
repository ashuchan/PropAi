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
    ENABLE_FAILFAST_TERMINAL_FETCH,
    ENABLE_FLARESOLVERR_TIER,
    ENABLE_RESIDENTIAL_RENDER_TIER,
    ENABLE_RESIDENTIAL_TIER,
    ENABLE_TIER_ESCALATION,
    ENABLE_UNLOCKER_TIER,
    SKIP_RESIDENTIAL_WHEN_UNLOCKER,
)
from ma_poc.fetch import tier_health
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
    FetchTier.RESIDENTIAL_RENDER: {
        FetchTier.DIRECT,
        FetchTier.DC_PROXY,
        FetchTier.RESIDENTIAL,
    },
    FetchTier.UNLOCKER: {FetchTier.DIRECT, FetchTier.DC_PROXY, FetchTier.RESIDENTIAL},
}


def _build_ladder(floor: FetchTier) -> list[FetchTier]:
    """Return the ordered list of tiers to attempt, starting at floor.

    Only includes tiers that are enabled by feature flags.
    DLQ_PARK is never included in the active ladder — it's signalled by
    exhaustion.
    """
    # 2026-07-12 (cost-minimization): residential is 100%-wasted upstream of
    # unlocker (prod 2026-05-27: 964/964 residential escalations fell through
    # to unlocker; residential recovers 0, unlocker 91%). When both are on and
    # SKIP_RESIDENTIAL_WHEN_UNLOCKER is set, drop residential so the ladder is
    # DIRECT→(DC)→UNLOCKER — same recovery, no wasted per-GB spend or 2s-RPS
    # delay. See feature_flags.SKIP_RESIDENTIAL_WHEN_UNLOCKER.
    _residential_on = ENABLE_RESIDENTIAL_TIER and not (
        ENABLE_UNLOCKER_TIER and SKIP_RESIDENTIAL_WHEN_UNLOCKER
    )
    from ma_poc.config.feature_flags import compliance_mode, hb_enabled

    _solvers_ok = not compliance_mode()  # COMPLIANCE_MODE forces solver rungs off
    all_tiers: list[tuple[FetchTier, bool]] = [
        (FetchTier.DIRECT, True),
        (FetchTier.DC_PROXY, ENABLE_DC_PROXY_TIER),
        # Hyperbrowser rung (FETCH_BACKEND=hyperbrowser) — PREFERRED over the
        # paid BrightData rungs (Ankur: HB is free on the RealPage account, use
        # it first, BrightData only as backup). Positioned before RESIDENTIAL so
        # a prop that fails the free static tiers hits HB *first*; on HB failure
        # escalation falls through to the BrightData residential/2a rungs below.
        # Clean render (solveCaptchas off), so compliant.
        (FetchTier.HYPERBROWSER, hb_enabled()),
        (FetchTier.RESIDENTIAL, _residential_on),
        # Clean "2a" render tier: a real browser on residential that passes
        # JS challenges by waiting and aborts on interactive captchas. Placed
        # BEFORE the solver tiers so a defensible render is tried first; run
        # it with the solvers off for the clean posture.
        (FetchTier.RESIDENTIAL_RENDER, ENABLE_RESIDENTIAL_RENDER_TIER),
        # FlareSolverr sits between RESIDENTIAL and UNLOCKER. It handles CF
        # JS challenges locally (no proxy cost) but can't bypass WAF blocks.
        # Solver rungs — FORCED OFF under COMPLIANCE_MODE (RealPage legal
        # 2026-07-22: Web Unlocker + FlareSolverr are no-fly zones). Belt-and-
        # suspenders over the per-tier flags: even ENABLE_UNLOCKER_TIER=true
        # can't re-enable them while compliance is on. Code is kept, just held off.
        (FetchTier.FLARESOLVERR, ENABLE_FLARESOLVERR_TIER and _solvers_ok),
        (FetchTier.UNLOCKER, ENABLE_UNLOCKER_TIER and _solvers_ok),
    ]
    # Explicit list order sets the attempt order; the ``t >= floor`` guard
    # only filters by the property's tier floor. RESIDENTIAL_RENDER's high
    # int value means it always passes that guard (always available when its
    # flag is on) — see FetchTier.RESIDENTIAL_RENDER.
    return [t for t, enabled in all_tiers if enabled and t >= floor]


def _should_probe_lower(profile: ScrapeProfile) -> FetchTier | None:
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


def _make_provider(tier: FetchTier) -> FetchProvider:
    """Instantiate the appropriate provider for a given tier."""
    # Vendor switch (task #46): FETCH_BACKEND=hyperbrowser swaps Hyperbrowser
    # in BEHIND the rungs named in HYPERBROWSER_TIERS (default UNLOCKER). A
    # property that succeeds on a cheaper rung never reaches here for those
    # tiers, so this is "HB for the escalated cohort only". Default off →
    # branch never taken and the BrightData/httpx providers below are unchanged.
    from ma_poc.config.feature_flags import hb_enabled, hb_tiers

    if hb_enabled() and tier.name in hb_tiers():
        from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider

        mode = "render" if tier == FetchTier.RESIDENTIAL_RENDER else "unlock"
        return HyperbrowserProvider(mode=mode)
    # The dedicated HB rung (preferred over BrightData; see _build_ladder).
    if tier == FetchTier.HYPERBROWSER:
        from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider

        return HyperbrowserProvider(mode="render")
    if tier == FetchTier.DIRECT:
        from ma_poc.fetch.providers.direct import DirectProvider
        return DirectProvider()
    if tier == FetchTier.DC_PROXY:
        from ma_poc.fetch.providers.dc_proxy import DcProxyProvider
        return DcProxyProvider()
    if tier == FetchTier.RESIDENTIAL:
        from ma_poc.fetch.providers.residential import ResidentialProvider
        return ResidentialProvider()
    if tier == FetchTier.RESIDENTIAL_RENDER:
        from ma_poc.fetch.providers.residential_render import (
            ResidentialRenderProvider,
        )
        return ResidentialRenderProvider()
    if tier == FetchTier.UNLOCKER:
        from ma_poc.fetch.providers.unlocker import UnlockerProvider
        return UnlockerProvider()
    if tier == FetchTier.FLARESOLVERR:
        from ma_poc.fetch.providers.flaresolverr import FlareSolverrProvider
        return FlareSolverrProvider()
    raise ValueError(f"No provider for tier {tier!r}")


async def fetch_with_escalation(
    task: CrawlTask,
    profile: ScrapeProfile,
) -> FetchResult:
    """Fetch *task* with automatic tier escalation on BOT_BLOCKED.

    Also runs a periodic demotion probe: when a property has succeeded N times
    at a non-DIRECT floor, try a lower tier first to see if bot-blocking has
    relaxed.

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
    all_attempts: list[int] = []

    # ── Demotion probe (Phase E4/E5) ─────────────────────────────────────────
    # Before the normal ladder, check if we should probe a lower tier.
    # If the probe succeeds, the profile updater will demote the floor.
    probe_tier = _should_probe_lower(profile)
    # A tier the breaker has killed is dead for probes too — dialling it here
    # would reintroduce the exact waste the guard exists to stop.
    if probe_tier is not None and tier_health.is_tier_disabled(probe_tier.name):
        probe_tier = None
    if probe_tier is not None:
        try:
            profile.fetch.last_demotion_probe_at = datetime.now(UTC)
            probe_provider = _make_provider(probe_tier)
            probe_result = await probe_provider.fetch(task, profile)
            all_attempts.extend(probe_result.fetch_tier_attempts or [int(probe_tier)])
            tier_health.note_result(
                probe_tier.name,
                probe_result.outcome,
                property_id=task.property_id,
                error_signature=probe_result.error_signature,
            )

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
    attempted_any = False

    for tier in ladder:
        if escalations > MAX_ESCALATIONS_PER_RUN:
            emit(EventKind.FETCH_LADDER_BUDGET_EXHAUSTED, task.property_id,
                 escalations=escalations, max=MAX_ESCALATIONS_PER_RUN)
            break

        # Run-level circuit breaker (2026-07-27 RCA). The escalator's providers
        # build their proxy from BrightDataProvider.get_config(), never from
        # ProxyPool — so ProxyPool's health/quarantine logic has never applied
        # to this path. Without this check a dead proxy behind DC_PROXY or
        # RESIDENTIAL is retried for every property of a 5k run, forever.
        # DIRECT is never guarded (see tier_health.UNGUARDED_TIERS).
        if tier_health.is_tier_disabled(tier.name):
            log.debug(
                "Skipping tier %s for %s — disabled for this run by the health guard",
                tier.name, task.property_id,
            )
            continue

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
        attempted_any = True

        outcome = result.outcome
        # Feed the run-level breaker. Only TRANSIENT/PROXY_ERROR count as tier
        # faults — a BOT_BLOCKED or a 404 proves the tier reached the origin.
        tier_health.note_result(
            tier.name,
            outcome,
            property_id=task.property_id,
            error_signature=result.error_signature,
        )
        if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED):
            emit(EventKind.FETCH_TIER_PROBE_SUCCESS, task.property_id,
                 tier=tier.name, escalations=escalations)
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.HARD_FAIL:
            return _merge_attempts(result, all_attempts)

        # DEAD_URL (HTTP 404/410/451, soft-404, parked) is terminal — a 404 is a
        # 404 from every tier, so escalating it through residential → Web-Unlocker
        # (120s) → render is pure waste (155-191s per 404 guess-path — the dominant
        # driver of the 600s per-property timeouts). Mirror the fetcher's inline
        # loop (fetcher.py) and return immediately. Flag-gated (default off) so a
        # canary can A/B the gain and catch soft-404 false-positives.
        if ENABLE_FAILFAST_TERMINAL_FETCH and outcome == FetchOutcome.DEAD_URL:
            emit(EventKind.FETCH_TIER_PROBE_FAILED, task.property_id,
                 tier=tier.name, block_sig="DEAD_URL_FAILFAST")
            return _merge_attempts(result, all_attempts)

        if outcome == FetchOutcome.BOT_BLOCKED:
            emit(EventKind.FETCH_TIER_PROBE_FAILED, task.property_id,
                 tier=tier.name, block_sig=result.block_signature)
            escalations += 1
            continue

        # TRANSIENT / RATE_LIMITED / PROXY_ERROR — escalate
        escalations += 1

    # Every tier in the ladder was disabled by the health guard and nothing was
    # tried. Fall back to DIRECT rather than synthesising a failure: DIRECT is
    # free, unproxied, and never guarded, so it is always a safe floor. Without
    # this a run whose tier_floor sits above DIRECT would return LADDER_EMPTY
    # for every remaining property once the breaker tripped — trading a silent
    # outage for a loud one, which is not the trade we want.
    if not attempted_any and ladder:
        from ma_poc.fetch.providers.direct import DirectProvider
        log.warning(
            "All tiers in the ladder are disabled for this run; falling back to "
            "DIRECT for %s", task.property_id,
        )
        return await DirectProvider().fetch(task, profile)

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
