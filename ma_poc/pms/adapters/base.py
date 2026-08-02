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


# Adapter-row marker for a bounded source whose entire public inventory surface
# has been parsed and proven to publish plans only.  This is deliberately opt-in:
# ordinary plan rows must not bypass the publish-ceiling rent-token guard merely
# because an extractor found some floor-plan cards.
VERIFIED_PLAN_ONLY_SURFACE_KEY = "_verified_plan_only_surface"


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
    # 2026-05-25 (canary 1ef1060 regr#11b): street address from CSV,
    # threaded through so the AppFolio adapter can post-fetch-filter
    # multi-property PMC vanity responses (Academy Place / riedman cohort).
    address: str = ""
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
    # 2026-05-27 brochure/pre-leasing classifier observability: paths
    # the resolver/link-hop attempted vs. those that returned a real
    # inventory page (200 + floor-plan keywords). Pure observability —
    # adapters that don't populate these are ignored by the classifier.
    inventory_paths_attempted: list[str] = field(default_factory=list)
    inventory_pages_reachable: list[str] = field(default_factory=list)


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
    tier_used: str = ""
    winning_url: str | None = None
    api_responses: list[dict[str, Any]] = field(default_factory=list)
    blocked_endpoints: list[tuple[str, str]] = field(default_factory=list)
    llm_field_mappings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # Exact response(s) that produced the admitted units.  Bodies are never
    # persisted here: only a hash, sanitised URL, count and identity verdict.
    unit_source_provenance: list[dict[str, Any]] = field(default_factory=list)


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
