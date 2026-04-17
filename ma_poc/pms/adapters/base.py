"""Adapter protocol + shared dataclasses. See claude_refactor.md Phase 2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pms.detector import DetectedPMS

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
