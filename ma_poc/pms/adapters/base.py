"""Adapter protocol + shared dataclasses. See claude_refactor.md Phase 2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ma_poc.pms.detector import DetectedPMS

if TYPE_CHECKING:
    # Playwright is a heavy import and is unavailable in unit-test environments
    # that don't have browsers installed. The Protocol needs the type only at
    # type-check time; adapter implementations import it directly.
    from playwright.async_api import Page


@dataclass
class AdapterContext:
    base_url: str
    detected: DetectedPMS
    profile: Any | None  # ScrapeProfile; typed Any to avoid a hard dep cycle here
    expected_total_units: int | None
    property_id: str
    # Jugnu: L1 fetch result — the adapter does not re-fetch. For adapters
    # that work from network_log, the page argument can be a stub.
    fetch_result: Any | None = None  # FetchResult; typed Any to avoid import cycle
    # Property metadata from the CSV row. Threaded through so LLM prompts
    # and any adapter that wants property-aware behavior (e.g. validating
    # extracted city against CSV city) have the context. Before Phase 2
    # these were hard-coded to "" in the generic adapter's LLM call.
    property_name: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    pmc: str = ""  # Management company
    # Phase H: per-property LLM budget for this run (computed once in scraper.py)
    budget: dict = field(default_factory=lambda: {
        "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3,
    })
    # F12: count of units the PMS-specific adapter produced before the
    # generic-fallback handoff. When 0, the generic LLM gate stays open
    # even on detected (non-unknown) PMS hosts.
    adapter_unit_count: int = 0
    # B1: structural floor-plan signal count from _characterize_html (0-4).
    # Used by page_has_content_signals to suppress RC3 deferral when the
    # entry page already has genuine unit-data structure.
    floor_plan_signal_count: int = 0
    # Hop depth: 0 on the entry page, 1+ when scrape() recurses from
    # _try_link_hop. Read by signal_engine.decider to gate RC3 monolithic
    # deferral — Rule 2 only fires on the entry page (depth=0). Without
    # this field, getattr(ctx, "hop_depth", 0) always returned 0 and the
    # decider deferred the LLM on every hop too, exhausting budget on
    # nothing. Observed 2026-05-14 on PIDs 290347, 246710.
    hop_depth: int = 0
    # Bug #5 fix (2026-05-16): harvested portal-iframe URLs from
    # _extract_portal_iframe_hints. The Entrata adapter consumes these as
    # additional probe targets so candidate-derived ``comms.entrata.com/
    # widget?website_token=...`` URLs from the entry page can be fetched
    # via ``page.evaluate(fetch ...)`` with the CF clearance cookies the
    # entry-page load established. Without this, the candidate is queued
    # only for the link-hop scheduler — which fetches as top-level
    # navigation and is rejected by CF (HARD_FAIL 400) since the cookie
    # didn't carry. PID 40867 gardenparkinfo.com is the canonical case;
    # 13+ Entrata properties on 2026-05-16 cloud run carry comms.entrata
    # iframe markers that landed here.
    candidate_portal_urls: list[str] = field(default_factory=list)
    # ── Proxy budget (2026-05-23) ──────────────────────────────────────────
    # Per-property accounting for paid-egress hops (residential proxy +
    # Web Unlocker combined). Read and bumped by ``fetch.proxy_gate.decide``
    # / ``proxy_gate.record_use``. Hard cap at ``proxy_max_hops`` keeps a
    # single property from monopolising the proxy budget. Bytes counter
    # is belt-and-braces against a runaway multi-MB response loop.
    #
    # NEVER read PROBE_PROXY_URL directly off this struct or anywhere
    # else — go through ``fetch.proxy_gate.decide`` to keep the strict-
    # allow invariant.
    proxy_hops_used: int = 0
    proxy_bytes_used: int = 0
    proxy_max_hops: int = 3        # see fetch.proxy_gate.DEFAULT_MAX_PROXY_HOPS
    proxy_max_bytes: int = 1_500_000   # 1.5 MB / property


@dataclass
class AdapterResult:
    units: list[dict[str, Any]] = field(default_factory=list)
    #: Stage 2 (2026-05-12): floor-plan-level summaries surfaced separately
    #: from per-apartment ``units``. Adapters populate this from
    #: ``PostProcessResult.plan_summaries``; the runner surfaces them as
    #: ``floor_plans[]`` on the V2 property record. See
    #: docs/2026_05_11_regressions_fix_design.md (Stage 2 — plan-level
    #: routing). Additive field — adapters that don't yet populate this
    #: continue to return an empty list, matching pre-Stage-2 behaviour.
    plan_summaries: list[dict[str, Any]] = field(default_factory=list)
    #: D16 (2026-05-16): cross-page unit-number dedup + inverse-B4 telemetry
    #: from ``PostProcessResult.to_meta()``. Keys:
    #: ``cross_page_dedup_collapses``, ``inverse_b4_rerouted``,
    #: ``n_unit_level``, ``n_plan_level``. ``None`` means the adapter
    #: didn't run ``post_process`` (rare — only on early-exit paths). The
    #: scraper threads this into ``result["_post_process_meta"]`` for the
    #: per-property markdown and run-level aggregations.
    post_process_meta: dict[str, int] | None = None
    tier_used: str = ""
    winning_url: str | None = None
    api_responses: list[dict[str, Any]] = field(default_factory=list)
    blocked_endpoints: list[tuple[str, str]] = field(default_factory=list)
    llm_field_mappings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    confidence: float = 0.0


@runtime_checkable
class PmsAdapter(Protocol):
    pms_name: str

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult: ...

    def static_fingerprints(self) -> list[str]: ...

    # Optional — intentionally NOT in the Protocol body so that `runtime_checkable`
    # isinstance() checks remain backward-compatible with adapters that predate
    # Change 2. Adapters that opt in implement the following signature:
    #
    #     def matches_response_body(self, body: Any) -> bool:
    #         """Cheap body-shape check used by detector.confirm_detection to
    #         demote a URL-based detection when no captured network body
    #         matches the adapter's expected envelope.
    #
    #         Adapters that omit this method are treated as "no body-shape
    #         check available"; confirm_detection keeps the URL detection
    #         intact for them (this is the correct behaviour for DOM-only
    #         adapters like TouchTour where inventory is server-rendered
    #         rather than fetched via XHR)."""
    #         ...
