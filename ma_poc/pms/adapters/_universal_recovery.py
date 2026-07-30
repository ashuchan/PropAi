"""Universal embed-recovery chain.

Wraps the four individual recovery paths added 2026-05-19 in a single
priority-ordered call:

  1. ``recover_appfolio_embed``      — Wix/Squarespace shell → AppFolio
                                       listings iframe ({tenant}.appfolio.com/listings)
  2. ``recover_leaseleads_embed``    — Squarespace shell → embed.leaseleads.co
                                       iframe + public api.leaseleads.co JSON
  3. ``recover_pms_portal``          — marketing-shell links to ResMan
                                       Implicity / RentCafe SecureCafe
  4. ``recover_generic_floorplans``  — repeated SSR plan-card containers at
                                       /floor[-]plans (long-tail catchall)

Used by:

  * ``squarespace_nopms`` and ``wix_nopms`` — these run the chain *before*
    declaring SYNDICATION_ONLY (the fast path; saves a downstream Tier-4
    LLM call).
  * ``GenericAdapter.extract()`` — as the *final* fallback after all
    internal tiers. Closes the **cross-vendor misroute** gap: if the
    detector picked the wrong PMS (e.g. "entrata" on a site that's
    actually an AppFolio embed), the primary adapter returns 0, scraper
    Step 8 falls back to generic, and *now* generic also tries these
    recoveries.

Idempotency: sets ``ctx._embed_recovery_attempted = True`` after the
chain runs (with the recovery name that produced units, if any). The
GenericAdapter caller checks this attr first and skips a re-run when
the syndication adapter already covered it for the same property.

Each recovery function is independently safe to call (returns ``[]`` on
non-applicable pages); the chain stops at the first one that yields
units after the caller's ``post_process`` admission gate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)


_RECOVERY_FLAG_ATTR = "_embed_recovery_attempted"
_RECOVERY_BLOCKS_ATTR = "_embed_recovery_blocks"
_RECOVERY_NOTES_ATTR = "_embed_recovery_notes"

# HTTP status codes that indicate the response was intercepted by a bot-wall
# (DataDome, Akamai, Cloudflare, IIS bot-protection) rather than a genuine
# "no resource" / "no data" outcome. Recording these separately from "empty
# body" lets downstream telemetry distinguish a misroute (no embed anywhere)
# from a routing-correct-but-blocked recovery (residential proxy + Camoufox
# may flip the same probe to a HIT in production).
_BOT_BLOCK_STATUSES: frozenset[int] = frozenset({401, 403, 429, 503})


def is_bot_block(status: int) -> bool:
    """True when *status* indicates a bot-wall intercept (not a genuine empty)."""
    return status in _BOT_BLOCK_STATUSES


def already_attempted(ctx: AdapterContext) -> bool:
    """True if a prior caller in this scrape already ran the recovery
    chain on the same context. Used by GenericAdapter to skip a double
    run for sites that came through a syndication adapter.
    """
    return bool(getattr(ctx, _RECOVERY_FLAG_ATTR, False))


def mark_attempted(ctx: AdapterContext, winning_recovery: str = "") -> None:
    """Record that the chain ran; optionally name the winner."""
    try:
        setattr(ctx, _RECOVERY_FLAG_ATTR, True)
        if winning_recovery:
            ctx._embed_recovery_winner = winning_recovery  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — defensive
        pass


def mark_blocked(
    ctx: AdapterContext, recovery: str, url: str, status: int
) -> None:
    """Record that a recovery sub-fetch hit a bot-wall (HTTP 401/403/429/503).
    The scraper reads ``ctx._embed_recovery_blocks`` after the chain runs and
    appends ``universal_recovery_blocked:<recovery>:<status>`` entries to
    ``fallback_chain`` so DLQ/triage can distinguish bot-walled-but-routing-
    correct cases (worth retrying with proxy/Camoufox) from genuine no-signal
    misses.
    """
    if not is_bot_block(status):
        return
    try:
        existing = getattr(ctx, _RECOVERY_BLOCKS_ATTR, None)
        if not isinstance(existing, list):
            existing = []
            setattr(ctx, _RECOVERY_BLOCKS_ATTR, existing)
        existing.append({"recovery": recovery, "url": url, "status": int(status)})
    except Exception:  # pragma: no cover — defensive
        pass


def note_recovery(
    ctx: AdapterContext, recovery: str, reason: str, detail: str = ""
) -> None:
    """Record WHY a recovery declined to emit the rows it had in hand.

    A recovery that finds a data surface and then refuses it (2026-07-28:
    AppFolio account rosters that could not be scoped to the property) must
    not look identical to one that found nothing — otherwise "we declined to
    guess" is indistinguishable from "there is nothing here", which is the
    failure mode that produced the contamination in the first place. The
    scraper appends these to ``fallback_chain`` so triage can see the
    property is unresolved on purpose.
    """
    if not recovery or not reason:
        return
    try:
        existing = getattr(ctx, _RECOVERY_NOTES_ATTR, None)
        if not isinstance(existing, list):
            existing = []
            setattr(ctx, _RECOVERY_NOTES_ATTR, existing)
        existing.append(
            {"recovery": recovery, "reason": reason, "detail": detail}
        )
        log.info(
            "recovery declined recovery=%s reason=%s detail=%s",
            recovery,
            reason,
            detail,
        )
    except Exception:  # pragma: no cover — defensive
        pass


def get_notes(ctx: AdapterContext) -> list[dict[str, object]]:
    """Return the decline notes recorded on *ctx* during the recovery chain."""
    notes = getattr(ctx, _RECOVERY_NOTES_ATTR, None)
    if not isinstance(notes, list):
        return []
    return list(notes)


def get_blocks(ctx: AdapterContext) -> list[dict[str, object]]:
    """Return the list of bot-block observations recorded on *ctx* during
    the recovery chain. Empty list when nothing was blocked.
    """
    blocks = getattr(ctx, _RECOVERY_BLOCKS_ATTR, None)
    if not isinstance(blocks, list):
        return []
    return list(blocks)


async def recover_universal_embed(
    page: Page,
    ctx: AdapterContext,
) -> tuple[list[dict[str, str]], str, str]:
    """Run the four recoveries in priority order. Returns
    ``(units, tier_used, recovery_name)``. ``recovery_name`` is the
    string identifier of the recovery that produced the units, or ``""``
    when all four returned empty.

    This function does NOT call ``post_process`` — the caller decides
    whether to admit. Marking ``ctx._embed_recovery_attempted`` is
    handled here on every invocation, irrespective of outcome.
    """
    # Late imports to avoid a circular import at module load time
    # (these recovery modules each import from ``adapters._parsing``).
    from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
    from ma_poc.pms.adapters._generic_dom_floorplans import (
        recover_generic_floorplans,
    )
    from ma_poc.pms.adapters._leaseleads_embed import recover_leaseleads_embed
    from ma_poc.pms.adapters._pms_portal_hop import recover_pms_portal
    from ma_poc.pms.adapters._g5_recovery import recover_g5
    from ma_poc.pms.adapters._sightmap_subpage_recovery import (
        recover_sightmap_subpage,
    )

    try:
        units = await recover_appfolio_embed(page, ctx)
        if units:
            mark_attempted(ctx, "appfolio_embed")
            return units, "TIER_1_DOM_APPFOLIO_SSR", "appfolio_embed"

        ll_units = await recover_leaseleads_embed(page, ctx)
        if ll_units:
            mark_attempted(ctx, "leaseleads_embed")
            return ll_units, "TIER_1_API_LEASELEADS", "leaseleads_embed"

        portal_units = await recover_pms_portal(page, ctx)
        if portal_units:
            mark_attempted(ctx, "pms_portal_hop")
            # The portal-hop recovery sets ``extraction_tier`` on its
            # rows. Prefer that label when present (carries the specific
            # backend) over a generic ``TIER_1_PMS_PORTAL_HOP``.
            tier = "TIER_1_PMS_PORTAL_HOP"
            if isinstance(portal_units[0], dict):
                t = str(portal_units[0].get("extraction_tier") or "").strip()
                if t:
                    tier = t
            return portal_units, tier, "pms_portal_hop"

        generic_units, _ = await recover_generic_floorplans(page, ctx)
        if generic_units:
            mark_attempted(ctx, "generic_dom")
            return generic_units, "TIER_3_DOM_GENERIC", "generic_dom"

        # SightMap subpage recovery (2026-05-24): closes the
        # TIER_1_API_SIGHTMAP P1 cohort (131 props) where prod scored
        # SUCCESS via SightMap but canary's detector misrouted to
        # RentCafe/Funnel/etc. because the embed only lives at
        # /floorplans/ one nav-hop deep. Probes that family of
        # subpaths, splices a matching body into ctx, and lets
        # SightMapAdapter discover the embed code + canonical API URL.
        # Live-verified 8/10 in the cohort sample.
        sm_units = await recover_sightmap_subpage(page, ctx)
        if sm_units:
            mark_attempted(ctx, "sightmap_subpage")
            # The recovery stamps its own extraction_tier; prefer that
            # over a generic label to keep cohort reporting accurate.
            tier = "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY"
            if isinstance(sm_units[0], dict):
                t = str(sm_units[0].get("extraction_tier") or "").strip()
                if t:
                    tier = t
            return sm_units, tier, "sightmap_subpage"

        # Rently recovery (2026-07-30, #89): a property whose own site
        # redirects to ``u{ID}.rently.com`` (scattered single-family / BTR)
        # detects as generic/plan-text with no data. Extract the managerID
        # from the resolved host / body and fetch the searchQuery JSON
        # endpoint code-only. Address = scattered-site identity (#29).
        from ma_poc.pms.adapters.rently import recover_rently

        rently_units = await recover_rently(ctx)
        if rently_units:
            mark_attempted(ctx, "rently")
            return rently_units, "TIER_1_API_RENTLY", "rently"

        # G5 recovery (2026-05-24): closes the TIER_1_API generic /
        # Knock-misroute sub-cohort where the property has a g5-cl-*
        # URN in its body but the detector picked a different PMS
        # adapter that returned 0 units. Pairs with the G5 adapter's
        # own curl_cffi + URN-candidate retry (commit 642c41b) — this
        # wrapper just makes G5 reachable from the misroute path.
        g5_units = await recover_g5(page, ctx)
        if g5_units:
            mark_attempted(ctx, "g5_recovery")
            tier = "TIER_1_API_G5_RECOVERY"
            if isinstance(g5_units[0], dict):
                t = str(g5_units[0].get("extraction_tier") or "").strip()
                if t:
                    tier = t
            return g5_units, tier, "g5_recovery"
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("universal-recovery chain raised err=%s", exc)

    mark_attempted(ctx, "")
    return [], "", ""
