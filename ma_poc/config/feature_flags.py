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

# 2026-07-12 (cost-minimization): when BOTH residential and unlocker are on,
# drop RESIDENTIAL from the ladder. Production run 2026-05-27 proved the
# residential tier is 100% wasted upstream of unlocker: 964/964 properties
# that escalated to RESIDENTIAL then ALSO escalated to UNLOCKER (residential
# recovers 0 — it fetches the ~27KB CF challenge with a no-JS client and
# fails), while UNLOCKER cleared 91% of the same blocks. So residential just
# adds per-GB spend + a 2s-RPS delay before the tier that actually works.
# Default True (skip the wasted tier); set false to keep DIRECT→…→RESIDENTIAL
# →UNLOCKER for a config that wants residential as a distinct fallback.
SKIP_RESIDENTIAL_WHEN_UNLOCKER: Final[bool] = (
    os.environ.get("SKIP_RESIDENTIAL_WHEN_UNLOCKER", "true").lower() == "true"
)

# FlareSolverr tier — local Docker service for CF JS-challenge bypass.
# Only useful for Tier-1 "Just a moment..." challenges, NOT WAF blocks.
# Enable with: ENABLE_FLARESOLVERR_TIER=true + docker run FlareSolverr on :8191
ENABLE_FLARESOLVERR_TIER: Final[bool] = (
    os.environ.get("ENABLE_FLARESOLVERR_TIER", "false").lower() == "true"
)

# Clean "2a" residential-render tier — a REAL (non-stealth) browser rendering
# through a residential proxy that passes JS challenges by WAITING and ABORTS
# on interactive captchas (never a solver). Positioned after RESIDENTIAL in
# the ladder. For the defensible "be a normal browser, don't defeat controls"
# posture, run it with the two solver tiers OFF:
#   ENABLE_RESIDENTIAL_RENDER_TIER=true
#   ENABLE_UNLOCKER_TIER=false  ENABLE_FLARESOLVERR_TIER=false
# See fetch/providers/residential_render.py. Default off.
ENABLE_RESIDENTIAL_RENDER_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_RESIDENTIAL_RENDER_TIER", "false").lower() == "true"
)

# Lever 3 (2026-07-16): render-on-empty escalation. When a routed adapter
# extracts 0 units from a fetch that SUCCEEDED (a 200 SSR/shell body) and did
# NOT already render, re-fetch that property ONCE via RenderMode.RENDER so the
# browser fires the client-side widget XHRs (OneSite OLL, Entrata/nestin SPAs,
# etc.) and re-run extraction. Bounded to one extra render per empty-GET
# property. Default OFF — a render run enables it and verifies recovery/cost.
ENABLE_RENDER_ON_EMPTY: Final[bool] = (
    os.environ.get("ENABLE_RENDER_ON_EMPTY", "false").lower() == "true"
)

# Entrata plan→unit render lever (2026-07-18, task #42). The Entrata
# prospect-portal per-apartment roster is a jd-fp widget
# (``a[data-jd-fp-selector="unit-card"]``) populated CLIENT-SIDE; a static
# probe_get sees only ``--preload`` skeletons, so ~146 props land at
# TIER_1_DOM_ENTRATA_PP_SSR *plan-level* (floorplan rows, unit_number="").
# render-on-empty never fires on them because they return >0 rows. This flag
# extends the same render+re-extract escalation to fire on an Entrata
# plan-level SUCCESS (units present but none carry a real unit_number): re-fetch
# once via RenderMode.RENDER and re-run extraction so parse_entrata_pp_jd_fp_cards
# populates the roster and the tier relabels to PP_UNIT_LEVEL. Entrata-scoped,
# one extra render/prop, accept only on a strict unit-level upgrade. Default OFF
# — a flag-on canary over the 172 plan cohort measures the PP_SSR→UNIT_LEVEL flip.
ENABLE_ENTRATA_PLAN_RENDER: Final[bool] = (
    os.environ.get("ENABLE_ENTRATA_PLAN_RENDER", "false").lower() == "true"
)

# Empty-exit → marketing-subpage plan-text fallback (2026-07-18, task #41).
# When a CONFIRMED PMS adapter empty-exits with 0 units (e.g. the AppFolio
# contamination filter demoted a whole-PMC dump to [] → TIER_1_API_APPFOLIO_EMPTY),
# the property's OWN plan-level rents still sit on its marketing /floor-plans page,
# which the emptied adapter never read and the detector-driven Path-B retry can't
# reach. This fires the SAME cheap probe_get(unlocker=False) + parse_generic_plan_text
# subpage pass F1.5 already uses, ADOPTS the rows (F1.5 only merges into existing
# units), and emits SUCCESS_PLAN_LEVEL. Validated 7/11 demote candidates publish
# static plan rent (livefountainplace $1,050-1,335, westwatervillage, arendal, ...).
# Default OFF — a flag-on run measures how many empty-exits recover plan-level.
ENABLE_EMPTY_EXIT_PLAN_TEXT: Final[bool] = (
    os.environ.get("ENABLE_EMPTY_EXIT_PLAN_TEXT", "false").lower() == "true"
)

# Fail-fast on terminal DEAD_URL in the tier escalator (2026-07-18, timeout lever).
# BUG: tier_escalator's cascade loop early-returns on OK/NOT_MODIFIED/HARD_FAIL and
# escalates BOT_BLOCKED, but DEAD_URL (HTTP 404/410/451, soft-404, parked) falls
# through to the implicit "escalate" branch — so a 404 walks the whole paid ladder
# (residential → Web-Unlocker 120s → render) even though a 404 is a 404 from every
# tier. Measured: 155-191s burned per 404 guess-path in the link-hop crawl, the
# dominant driver of the 600s per-property timeouts (615 props). The fetcher's own
# INLINE loop already treats DEAD_URL terminal (fetcher.py:413-418); this makes the
# escalator mirror it. Default OFF — a flag-on canary measures the throughput/gold
# gain and guards against soft-404 false-positives (a DIRECT block misread as
# DEAD_URL that residential would have cleared).
ENABLE_FAILFAST_TERMINAL_FETCH: Final[bool] = (
    os.environ.get("ENABLE_FAILFAST_TERMINAL_FETCH", "false").lower() == "true"
)

# Link-hop crawl cheap-GET-gate (2026-07-18, timeout lever part 2). The link-hop
# crawl fetches each guessed subpath (/floorplans, /availability, …) with
# RenderMode.RENDER — so a guessed path that 404s still runs a full browser
# render (~20-30s) + curl_cffi/Web-Unlocker fallback (~120s) on the 404 before
# the crawl advances. Measured: 155-191s per 404 guess-path, the dominant driver
# of the 600s per-property timeouts. This gates each subpath with a single cheap
# probe_get (curl_cffi, no escalation, no unlocker) FIRST and skips the expensive
# RENDER only for a GENUINE empty 404 (HTTP 404/410 with body < 10KB) — soft-404s
# that carry a substantive unit-roster body (≥10KB, the ~9.5% ten68west-style
# pages) are preserved and still rendered/extracted, as are 200s and walled
# pages. Default OFF — a flag-on canary measures the timeout/throughput win.
ENABLE_CRAWL_GET_GATE: Final[bool] = (
    os.environ.get("ENABLE_CRAWL_GET_GATE", "false").lower() == "true"
)


def enable_camden_adapter() -> bool:
    """Camden Property Trust REIT unit-level lever (2026-07-19, gap #14).

    When True, the detector routes ``camdenliving.com`` (a Next.js REIT site with
    no adapter) to the ``camden`` adapter, which parses the
    ``props.pageProps.suggestedFloorPlans`` array from the page's ``__NEXT_DATA__``
    island — per-floorplan objects each carrying a representative available unit
    (unitNumber, monthlyRent, squareFeet, bedrooms/bathrooms, moveInDate,
    realPageUnitId). Static, no render; present on both the landing page and
    ``/availability``.

    Live-verified across 6 Camden props (fallsgrove/south-charlotte/gallery/
    southline/buckhead/noma); generalizes portfolio-wide (~170 properties).
    Fully-leased props carry no suggestedFloorPlans and fall through cleanly.

    Default OFF: a new REIT route, canary-measured. Read each call.
    """
    return os.environ.get("ENABLE_CAMDEN_ADAPTER", "false").lower() == "true"


def enable_venterra_adapter() -> bool:
    """Venterra in-house (eOnlineLease) unit-level lever (2026-07-19, gap #4).

    When True, the detector routes a page carrying Venterra's static
    ``var vt_units = [...]`` island (or an ``online.venterraliving.com/
    eOnlineLease`` marker) to the ``venterra`` adapter, which parses that island
    — a proper JSON array of unit records (unit_name / unit_rent_min-max /
    unit_sqft / unit_bedrooms / unit_available_on / unit_specials_message /
    stable unit_code) straight from the marketing page body. Static, no render,
    no cross-host API.

    The roster-confirmation sweep mis-routed these to SightMap + needs_render;
    the island is right there in the SSR body. Live-verified across forest-view
    (20 units) / canton-mill (19) / thomasglen (11). ~10 props.

    Default OFF: a new detector route that can win co-residence against a
    SightMap embed, so a flag-on canary measures it. Read each call.
    """
    return os.environ.get("ENABLE_VENTERRA_ADAPTER", "false").lower() == "true"


def enable_cws_getunits() -> bool:
    """RealPage CWS GetUnits unit-level lever (2026-07-19, roster-confirmation gap #3).

    When True, the ``realpage_cws`` adapter first tries the property-hosted
    ``/CmsSiteManager/callback.aspx?act=Proxy/GetUnits&available=true`` endpoint,
    which returns a clean ``{"units":[...]}`` roster (unitNumber/rent/squareFeet/
    numberOfBeds/floorplanName/internalAvailableDate/leaseStatus) — a static GET,
    no render. It falls back to the existing ``.rpfp-card`` DOM plan-level parse
    when GetUnits yields no available units.

    This refutes the adapter's own docstring ("RealPage CWS doesn't publish a
    per-unit roster publicly") — it DOES. Live-probed identical across 3+ CWS
    props (huntingtonwoods/keltonstation/thegarfield/capitalplace). Likely
    generalizes to the whole CWS portfolio (upgrades plan-level → unit-level).

    Default OFF: a new per-property HTTP call that changes the output tier, so a
    flag-on canary measures the plan→unit upgrade and guards edge cases. Read
    each call so an env flip needs no process restart.
    """
    return os.environ.get("ENABLE_CWS_GETUNITS", "false").lower() == "true"


def enable_onsite_apply_adapter() -> bool:
    """On-Site.com routing lever (2026-07-18, timeout-grind Surface C).

    When True, the detector emits ``onsite_apply`` for a page carrying an
    ``on-site.com/apply/property`` or ``on-site.com/web/online_app3`` portal
    link (or whose own host is ``on-site.com``), routing it to
    ``OnSiteApplyAdapter``. That adapter fetches the ``online_app3`` shell via
    a static ``probe_get`` and parses the embedded React props island into
    unit-level records (unit_number / per-unit rent / sqft / date / stable id).

    Live-probed 3/3 as a genuine Tier-1 unit-level surface (pullmansantarosa,
    sienavilla, tustin-view). ~24 timeout/generic-cohort props link out to it.
    RealPage CWS's docstring calling the on-site.com apply link "not a public
    unit roster" is WRONG for this surface.

    Default OFF: a new detector route that can win co-residence against
    knock/entrata at 0.91, so a flag-on canary measures the recovery and guards
    the default config against regression. Read each call so an env flip does
    not require a process restart (the detector reload trap).
    """
    return (
        os.environ.get("ENABLE_ONSITE_APPLY_ADAPTER", "false").lower() == "true"
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
