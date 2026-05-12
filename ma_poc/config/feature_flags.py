"""Central feature-flag registry for the fetch-tier escalation ladder.

All flags are read from environment variables at import time.
Reload the module (importlib.reload) to pick up env changes in tests.
"""

from __future__ import annotations

import os
from typing import Final

ENABLE_TIER_ESCALATION: Final[bool] = (
    os.environ.get("ENABLE_TIER_ESCALATION", "false").lower() == "true"
)

# Provider-tier flags — keyed off the master flag. If master is off, all are off.
ENABLE_DC_PROXY_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_DC_PROXY_TIER", "true").lower() == "true"
)
ENABLE_RESIDENTIAL_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_RESIDENTIAL_TIER", "false").lower() == "true"
)
ENABLE_UNLOCKER_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_UNLOCKER_TIER", "false").lower() == "true"
)

# FlareSolverr tier — local Docker service for CF JS-challenge bypass.
# Only useful for Tier-1 "Just a moment..." challenges, NOT WAF blocks.
# Enable with: ENABLE_FLARESOLVERR_TIER=true + docker run FlareSolverr on :8191
ENABLE_FLARESOLVERR_TIER: Final[bool] = (
    os.environ.get("ENABLE_FLARESOLVERR_TIER", "false").lower() == "true"
)


def enable_degraded_mapping_persist() -> bool:
    """PR 1 (2026-05-10): degraded LlmFieldMapping persistence kill switch.

    When True (default), `save_llm_field_mapping` persists mappings that
    have a non-empty ``response_envelope`` even when ``json_paths`` is
    empty (LLM extracted units via semantic understanding without
    articulating per-field paths). Replay on these is a no-op (the cascade
    falls through to other tiers), but the URL itself is now known to the
    profile and can be prioritised by the cascade — and the entry is the
    foundation for an offline LLM-pass that fills in ``json_paths`` later.

    Read each call (not at import) so a flip via env var doesn't require
    a process restart — the next ``save_llm_field_mapping`` call sees
    the new value.
    """
    return os.environ.get("ENABLE_DEGRADED_MAPPING_PERSIST", "true").lower() == "true"


def enable_source_tiered_budget() -> bool:
    """Tighten the LLM budget when the profile has high-confidence saved
    sources, freeing budget that would otherwise re-pay for what's
    already known.

    When True, ``services.source_planner.compute_budget`` reduces
    per-source LLM call caps by 1 for any source whose
    ``avg_confidence_when_won >= 0.85`` over at least 5 contributions.
    The reduction is per-source so a property with high-confidence API
    extraction but flapping DOM extraction still gets the full DOM
    budget.

    Floor: ``llm_api_calls`` and ``llm_dom_calls`` never drop below 1 —
    we always keep one fresh probe to detect drift. Default OFF for
    canary-first rollout: switch on after observing the per-source-
    observation distribution in production.

    Read each call so a flip via env doesn't require process restart.
    """
    return os.environ.get("ENABLE_SOURCE_TIERED_BUDGET", "false").lower() == "true"


def enable_promote_on_hint() -> bool:
    """PR 9 sub-2 (2026-05-10): promote LlmFieldMapping.quality_score and
    dom_hints.field_selectors_quality after each successful replay hit.

    When True (default), every replay hit bumps quality_score by +0.05
    clamped at 1.0. This lets PR-6 degraded saves (start at 0.4) rise
    toward full quality (1.0) over ~12 successful replays. Combined with
    PR 8's quality-tiered eviction (1-strike for <0.8, 3-strike for >=0.8),
    a degraded hint that proves itself eventually graduates to the
    high-quality bucket.

    Read each call so a flip via env var doesn't require process restart.
    """
    return os.environ.get("ENABLE_PROMOTE_ON_HINT", "true").lower() == "true"


def enable_degraded_dom_persist() -> bool:
    """PR 6 (2026-05-10): degraded DOM-hint (css_selectors) persistence kill switch.

    When True (default), the LLM_DOM_TARGETED save site in
    ``pms.adapters.generic`` persists css_selectors even when their
    self-validation against the source HTML produced ``quality_score < 0.4``.
    The save clamps to 0.4 so the replay-side gate admits them on the
    next run.

    Why default True: low self-validation often means the LLM analysed
    the API/JSON envelope (with more units than the DOM shows on first
    paint) or stale loading-state nodes interfered with the selector
    match — neither of which kills the selectors' usefulness on a fresh
    page tomorrow. Pre-PR-6 these were silently dropped so the Channel 5
    cache stayed empty in production despite daily LLM_DOM wins.

    Read each call (not at import) so a flip via env var doesn't require
    a process restart.
    """
    return os.environ.get("ENABLE_DEGRADED_DOM_PERSIST", "true").lower() == "true"
