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
#
# ENABLE_DC_PROXY_TIER defaults **OFF** as of 2026-07-25. It previously defaulted
# ON, which made ``PROXY_POOL_URLS`` opt-OUT: any job that did not explicitly
# disable it had a datacentre proxy attached to every fetch, RENDER included.
# When the pool's single entry (a rented DC proxy) silently disappeared — every
# port black-holing, not even refusing — Chromium failed EVERY navigation and a
# whole 5k run produced 0 usable rendered bodies out of 10,677 fetch attempts,
# while still reporting 71% "success" off the static-HTML fallback. The run that
# survived did so only because someone had set the flag false by hand.
#
# The rung is also redundant in the current architecture: DIRECT static first,
# then Hyperbrowser (free on the RealPage account, full browser + CF clearance),
# then BrightData residential as backup. The DC rung's niche — a cheap non-GCP
# exit IP for hosts that block GCP but need no rendering — is covered by HB, and
# this codebase has repeatedly found blocks to be browser-solvable rather than
# IP-solvable (the RESIDENTIAL tier was previously dropped as 100% wasted; the
# 2026-07-12 audit found 87% of blocks CF-managed and browser-solvable).
#
# The code path is deliberately KEPT, not deleted: zero firings with the flag off
# is not evidence it was useless when on, and no historical measurement exists
# either way. Set ENABLE_DC_PROXY_TIER=1 to re-enable if a need is measured —
# but point PROXY_POOL_URLS at a proxy that is verified live first.
ENABLE_DC_PROXY_TIER: Final[bool] = (
    ENABLE_TIER_ESCALATION
    and os.environ.get("ENABLE_DC_PROXY_TIER", "false").lower() == "true"
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
# property. Default ON (2026-07-19, user-approved): this is the generic
# self-heal for the shape-mismatch / needs-render class (~262 confirmed props
# where the roster is client-rendered) — a per-vendor fingerprint can't cover
# it. Cost note: adds at most ONE local render per zero-unit property (no paid
# tier — jugnu_fetch ladder still governs escalation). Env "false" disables.
ENABLE_RENDER_ON_EMPTY: Final[bool] = (
    os.environ.get("ENABLE_RENDER_ON_EMPTY", "true").lower() == "true"
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
# escalator mirror it. Default ON (2026-07-19, user-approved): DEAD_URL is keyed on
# genuine HTTP 404/410/451 (soft-blocks are BOT_BLOCKED/403/503 and still escalate),
# so the soft-404 false-positive risk is low. Set the env var to "false" to disable.
ENABLE_FAILFAST_TERMINAL_FETCH: Final[bool] = (
    os.environ.get("ENABLE_FAILFAST_TERMINAL_FETCH", "true").lower() == "true"
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
# pages. Default ON (2026-07-19, user-approved) — set env "false" to disable.
ENABLE_CRAWL_GET_GATE: Final[bool] = (
    os.environ.get("ENABLE_CRAWL_GET_GATE", "true").lower() == "true"
)

# Generalized plan→unit render lever (2026-07-19, task #45). Widens the
# Entrata-scoped #42 trigger to ANY plan-level success (rows present, none
# with a real unit_number) — non-Entrata plan-level properties previously got
# no unit-level upgrade attempt at all. Same single render block, same
# one-render/property bound, same strict unit-level-improvement accept guard.
# Two cost guards make the generalization safe where the Entrata scoping used
# to be the guard: (1) a per-property CIRCUIT-BREAKER on
# profile.quality.plan_render_attempts_held — after PLAN_RENDER_MAX_STREAK
# renders that were attempted and HELD (no upgrade), the property is treated
# as verified plan-only and the render stops firing (re-armed after
# PLAN_RENDER_REARM_DAYS so genuine re-listings recover); (2) an elapsed-budget
# guard — the generic trigger only fires when less than
# PLAN_RENDER_BUDGET_GUARD_S of the 600s per-property budget is spent, so a
# walled slow property never converts a plan-level SUCCESS into a
# per_property_timeout FAILED. Default OFF (repo convention for new
# cost-surface routes) — flag-on canary measures the PLAN→UNIT flip rate.
ENABLE_PLAN_UNIT_RENDER: Final[bool] = (
    os.environ.get("ENABLE_PLAN_UNIT_RENDER", "false").lower() == "true"
)


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to *default* on unset/garbage."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


# Circuit-breaker cap: render on attempts_held 0..N-1, stop at N. 3 matches
# the repo's 3-strike convention (HOT at 3 successes, COLD demotion at 3
# failures, drift at 3 timeouts).
PLAN_RENDER_MAX_STREAK: Final[int] = _int_env("PLAN_RENDER_MAX_STREAK", 3)
# Re-arm window: allow one fresh render when the last attempt is older than
# this many days, so the circuit is never permanently latched open (mirrors
# PR-02's days_since_full_scrape>=7 forced scrape).
PLAN_RENDER_REARM_DAYS: Final[int] = _int_env("PLAN_RENDER_REARM_DAYS", 7)
# Elapsed-budget guard (seconds): the generic plan→unit trigger only fires
# when the property has consumed less than this much of its 600s budget.
PLAN_RENDER_BUDGET_GUARD_S: Final[int] = _int_env("PLAN_RENDER_BUDGET_GUARD_S", 300)

# Knock direct-GET shortcut (task #21, warm=fast). Knock's doorway-api /units
# endpoint is a PUBLIC GET (no auth/CF/cookies) — proven 2026-07-24, 6/6 warm
# props returned the full gold roster (unit+rent) via plain GET. A WARM Knock
# profile stores that endpoint in known_endpoints, so subsequent runs can skip
# the ~10-45s render and GET it directly (<1s), feeding the JSON to the existing
# TIER_1_KNOCK_API parser via network_log. Default OFF — flag-on canary measures
# the render-skip rate + confirms unit parity vs the render path. Never-fail:
# any GET failure falls through to the normal render.
ENABLE_KNOCK_DIRECT_GET: Final[bool] = os.environ.get(
    "ENABLE_KNOCK_DIRECT_GET", "false"
).strip().lower() in ("1", "true", "yes", "on")

# RentCafe/SecureCafe direct raw-GET shortcut (task #21, warm=fast). The
# unit-level gold lives on the securecafe availableunits.aspx page — CF-fronted +
# the parser needs RAW HTML (a render mutates the DOM → parser returns 0). A WARM
# profile stores that URL in navigation.winning_page_url, so subsequent runs can
# skip the marketing-page render + link-hop and fetch it via hb_raw_get (HB
# in-page same-origin fetch — residential proxy clears CF, returns raw HTML, no
# BrightData). Proven 2026-07-24: grantparkvillage → 25 gold units via HB.
# Default OFF — flag-on canary measures the render-skip rate + unit parity.
ENABLE_RENTCAFE_DIRECT_GET: Final[bool] = os.environ.get(
    "ENABLE_RENTCAFE_DIRECT_GET", "false"
).strip().lower() in ("1", "true", "yes", "on")

# SightMap direct raw-GET shortcut (task #21, warm=fast). The SightMap unit API
# (sightmap.com/app/api/v1/{key}/sightmaps/{id}) is CF-fronted + content-
# negotiates (browser nav → HTML embed, not JSON), so a WARM profile's stored API
# URL is fetched via hb_raw_get (HB in-page same-origin fetch → raw JSON, CF
# cleared by residential proxy, no BrightData), then parse_sightmap_payload +
# json.loads. Content-guarded (≥1 rent-bearing unit) so Entrata-hint props fall
# through to render. Default OFF — flag-on canary measures render-skip + parity.
ENABLE_SIGHTMAP_DIRECT_GET: Final[bool] = os.environ.get(
    "ENABLE_SIGHTMAP_DIRECT_GET", "false"
).strip().lower() in ("1", "true", "yes", "on")

# RealPage CWS/LeaseStar direct raw-GET shortcut (task #21, warm=fast). The
# api.ws.realpage.com floorplans+units API is render-free — auth-gated by
# x-ws-authkey, a PUBLIC per-property key in the property page's static HTML
# (RPFP_config). For a WARM CWS profile: fetch the property page via hb_raw_get
# (HB clears any CF, no BrightData) → reuse generic._probe_realpage_cws (regex
# key + GET /units via httpx; the API is Akamai auth-gated, not CF-IP-blocked, so
# no proxy). EXCLUDES OLL (leasing.realpage.com — render-required). Default OFF.
ENABLE_REALPAGE_DIRECT_GET: Final[bool] = os.environ.get(
    "ENABLE_REALPAGE_DIRECT_GET", "false"
).strip().lower() in ("1", "true", "yes", "on")

# Agentic router SHADOW mode (Phase 2). When on, _process_property computes what
# the deterministic router (services/route_policy.py) WOULD decide from the fetch
# signals and logs it to a per-run route_shadow.jsonl next to what the pipeline
# actually did (tier/verdict/units) — OBSERVE-ONLY, the decision is never acted
# on, zero behavior change. The divergence measures whether wiring the agent
# (Phase 3) to act is worth it. Default OFF (pure measurement instrument).
ENABLE_ROUTE_SHADOW: Final[bool] = os.environ.get(
    "ENABLE_ROUTE_SHADOW", "false"
).strip().lower() in ("1", "true", "yes", "on")

# Marketing-page floor-plan TEXT parser (task #21 plan-level recovery). Many
# small/independent sites publish floor plans as free text ("Unit Layouts:
# Studio | 288 sq ft | from $1125") that the CSS-selector dom_scan misses → they
# mis-verdict FAILED_NO_DATA despite publishing PLAN-LEVEL data. This runs
# parse_marketing_plan_text as the last deterministic tier before the LLM, when
# the cascade extracted 0 units. Emits plan-level records (unit_number="") →
# SUCCESS_PLAN_LEVEL (coverage, not gold). Default OFF — flag-on canary measures
# the FAILED_NO_DATA → SUCCESS_PLAN_LEVEL flip rate. Proven: cottagesatsanford → 4 plans.
ENABLE_PLAN_TEXT: Final[bool] = os.environ.get(
    "ENABLE_PLAN_TEXT", "false"
).strip().lower() in ("1", "true", "yes", "on")

# Link-hop wall-clock budget (seconds). _try_link_hop does up to ~14 sequential
# RENDER sub-fetches (max_hops + dynamic appends), each ~35s+ — historically the
# DOMINANT driver of the 600s per-property timeouts, because the hop loop was
# bounded only by a page COUNT (max_hops), never by elapsed time. When a host
# tarpits (slow-walls under load, e.g. throttle-off), every hop render stretches
# and the property blows the whole budget. This cap stops STARTING new hops once
# the budget is spent, so a slow property fails-fast and frees its pool slot
# instead of monopolising it for 10 minutes. 150s ≈ 3-4 healthy hops and leaves
# room under the 600s ceiling for the escalation ladder that ran before it.
LINK_HOP_BUDGET_S: Final[int] = _int_env("LINK_HOP_BUDGET_S", 150)

# Encore per-plan render fan-out (2026-07-19, task #37 Track 2b). The
# encoreskyline (Jonah Digital) unit roster lives on N per-plan
# /floorplans/{slug}/ pages and appears only after a "Check Availability" JS
# click. At page=None the adapter discovers the plan URLs from the body but
# can't fetch+click them → 0 units. This renders each plan URL and — composed
# with INTERACTION_REVEAL, which fires the Check-Availability reveal during the
# render — parses the post-click roster. Bounded by ENCORE_PLAN_RENDER_MAX_PLANS
# and the shared PLAN_RENDER_BUDGET_GUARD_S elapsed guard; never-fail. Small
# cohort (single-digit-to-low-tens). Default OFF — REQUIRES INTERACTION_REVEAL=1
# for the click; a flag-on canary measures the roster recovery + validates the
# innerText-from-render-HTML parse approximation.
ENABLE_ENCORE_PLAN_RENDER: Final[bool] = (
    os.environ.get("ENABLE_ENCORE_PLAN_RENDER", "false").lower() == "true"
)
ENCORE_PLAN_RENDER_MAX_PLANS: Final[int] = _int_env("ENCORE_PLAN_RENDER_MAX_PLANS", 6)

# Body-capable resolver (2026-07-19, task #37 Track 1). Production dispatches L3
# with page=None, so the live resolver (CTA-hop / iframe scan / redirect follow)
# is skipped entirely — the confirmed root of the SightMap-iframe / leasing-portal
# / JS-injected-marker misroute mass (the large body-recoverable pool from the
# page=None scope). This runs the SAME resolver scoring over the already-fetched
# RENDER body's anchors + iframes + final_url — no live page, no new render, no
# memory/timeout cost (unlike holding a page across L3, which was rejected).
# Never-fail: degrades to today's fetch_only no-hop on any miss, so it can only
# ADD correct hops, never regress a working property. Default OFF (repo
# convention for a new routing surface) — a flag-on canary measures the
# misroute-recovery before default-on.
ENABLE_BODY_RESOLVER: Final[bool] = (
    os.environ.get("ENABLE_BODY_RESOLVER", "false").lower() == "true"
)

# Cluster/template cross-property warm-start (2026-07-19, arch-hardening #1).
# The self-learning profile is per-property: every property in a shared PMS
# client-account cluster (same RentCafe/Yardi account, same template & API
# shape — 1,678 rentcafe props, etc.) learns its winning API mapping
# INDEPENDENTLY, re-paying the LLM discovery cost that a sibling already paid.
# When a COLD property (never cracked, or demoted after failures) shares a
# cluster_key with ≥1 HOT sibling, this borrows the siblings' top-proven
# llm_field_mappings and injects them into the COLD profile's in-memory
# api_hints BEFORE dispatch, so the existing deterministic replay tier
# (generic:profile_replay, $0) can attempt them ahead of any LLM call.
# SAFE-BY-CONSTRUCTION: the replay path independently validates every mapping
# against THIS property's captured responses (normalized-URL substring match +
# apply_saved_mapping must yield real units), so a borrowed mapping that
# doesn't fit simply no-ops. Borrows carry a cleared source_envelope_hash
# (two properties never share a body hash, so keeping the mate's hash would
# make the drift-guard skip every borrow) and success_count=0 (unproven here).
# Purely additive to a COLD profile → default ON. Env "false" disables.
ENABLE_CLUSTER_WARM_START: Final[bool] = (
    os.environ.get("ENABLE_CLUSTER_WARM_START", "true").lower() == "true"
)


def enable_resurface_maintenance() -> bool:
    """Post-daily hot-URL migration-detector stage (2026-07-19).

    When True, the Jugnu runner runs ``scripts/resurface_profiles.py`` over the
    warm-started profile set at end-of-run (before the GCS push): re-probes each
    cached ``winning_page_url`` / ``known_endpoints`` and invalidates the ones
    that have MIGRATED (404/410/451 or a 200 empty-shell) so the pipeline
    re-discovers them, flagging each to ``{run_dir}/stale_surfaces.jsonl``. The
    sweep is strided (``RESURFACE_STRIDE`` days, default 7) so daily probe volume
    is ~1/stride of the cache. Non-fatal; a maintenance error never fails the run.

    Default OFF — enable in the daily job once the profile set is GCS-backed
    (needs ``PROFILE_GCS_PREFIX``). Read each call so an env flip needs no restart.
    """
    return os.environ.get("ENABLE_RESURFACE_MAINTENANCE", "false").lower() == "true"


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


# ── Hyperbrowser fetch-backend switch (2026-07-20) ──────────────────────────
# A vendor switch that swaps the Hyperbrowser cloud browser in BEHIND the
# existing RESIDENTIAL_RENDER / UNLOCKER rungs — NOT a new cost tier. Default
# ``brightdata`` so the code path is never constructed unless explicitly opted
# in. Function-form (read env each call) so an ops flip needs no restart, and
# because the seams (_make_provider / _try_unlocker_fallback) are hot.
# "HB for the CF-walled cohort only" recipe:
#   FETCH_BACKEND=hyperbrowser ENABLE_TIER_ESCALATION=true ENABLE_UNLOCKER_TIER=true
# — HB then bites ONLY on rungs a blocked property escalates into; a property
# that succeeds cheap (DIRECT/DC/residential/patchright-render) never reaches it.


def fetch_backend() -> str:
    """``FETCH_BACKEND`` = ``brightdata`` (default) | ``hyperbrowser``.

    Master vendor switch. Read each call so a flip needs no process restart.
    """
    return os.environ.get("FETCH_BACKEND", "brightdata").strip().lower()


def hb_enabled() -> bool:
    """True when the Hyperbrowser backend is selected (``FETCH_BACKEND=hyperbrowser``)."""
    return fetch_backend() == "hyperbrowser"


def hb_tiers() -> frozenset[str]:
    """Which ladder rungs HB replaces (``HYPERBROWSER_TIERS``, default ``UNLOCKER``).

    Comma list of ``FetchTier`` names. Default ``UNLOCKER`` — HB replaces only
    the solver rung; DIRECT/DC/residential/patchright-render stay on the cheap
    BrightData/httpx path. Add ``RESIDENTIAL_RENDER`` only when the compliance-
    posture change (2a is non-solver by design) is intended.
    """
    raw = os.environ.get("HYPERBROWSER_TIERS", "UNLOCKER").strip().upper()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def compliance_mode() -> bool:
    """``COMPLIANCE_MODE=1`` forces OFF every challenge-solver / bot-detection-
    defeating path — Web Unlocker, FlareSolverr, CAPTCHA-solving — regardless of
    their individual flags.

    RealPage legal (2026-07-22) ruled these a "no-go / no-fly zone" for this
    scrape (see feedback_realpage_scraping_legal_constraints memory). The code
    is KEPT, not deleted — this switch just holds it OFF, so it stays available
    for any separately-authorized context. **Set COMPLIANCE_MODE=1 on every
    RealPage run.** Read each call so an ops flip needs no restart.
    """
    return os.environ.get("COMPLIANCE_MODE", "false").strip().lower() in ("1", "true", "yes", "on")


def web_unlocker_allowed() -> bool:
    """False under compliance mode — the Web Unlocker (BrightData) is a no-fly zone."""
    return not compliance_mode()


def flaresolverr_allowed() -> bool:
    """False under compliance mode — FlareSolverr is a CF-challenge solver (no-go)."""
    return not compliance_mode()


def hb_covers_render_primary() -> bool:
    """Aggressive mode: HB as the PRIMARY renderer for every RENDER task
    (``RENDER_BACKEND=hyperbrowser``), not just the blocked-render fallback.

    Default off — much bigger spend (HB renders every RENDER task, not only
    the walled cohort a blocked render escalates into).
    """
    return os.environ.get("RENDER_BACKEND", "").strip().lower() == "hyperbrowser"
