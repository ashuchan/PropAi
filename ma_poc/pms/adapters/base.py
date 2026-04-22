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


@dataclass
class AdapterResult:
    units: list[dict[str, Any]] = field(default_factory=list)
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
