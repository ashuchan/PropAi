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
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("universal-recovery chain raised err=%s", exc)

    mark_attempted(ctx, "")
    return [], "", ""
