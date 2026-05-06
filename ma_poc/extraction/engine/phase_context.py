"""Shared mutable context passed between extraction phases.

Each phase reads from and writes to a single PhaseContext instance, avoiding
parameter explosion and making phase contracts explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PhaseContext:
    """Mutable state bag shared across all 7 extraction phases."""

    base_url: str
    profile: Any | None = None
    expected_total_units: int | None = None
    property_city: str | None = None

    # Set by BrowserSession before phases run
    page: Any | None = None

    # Network capture — populated by Phase 1, consumed by Phase 2+
    api_responses: list[dict] = field(default_factory=list)

    # Phase 2 output: profile-filtered subset of api_responses
    filtered_responses: list[dict] = field(default_factory=list)

    # Phase 1 link discovery
    internal_links: set[str] = field(default_factory=set)
    anchor_text_links: list[str] = field(default_factory=list)

    # Property metadata from Phase 1
    property_metadata: dict = field(default_factory=dict)
    property_name: str = ""
    page_unreachable: bool = False
    errors: list[str] = field(default_factory=list)

    # Extraction outputs
    units: list[dict] = field(default_factory=list)
    extraction_tier_used: str | None = None

    # Phase 5/6 LLM outputs
    llm_analysis_results: dict = field(default_factory=dict)
    llm_interactions: list[dict] = field(default_factory=list)

    # Phase 4 exploration tracking
    explored_links_status: dict[str, bool] = field(default_factory=dict)
    winning_page_url: str | None = None
    llm_candidates: list[dict] = field(default_factory=list)
    property_links_crawled: list[str] = field(default_factory=list)

    # Profile routing (set by scrape() before phases run)
    skip_to_tier: int | None = None
    run_full_cascade: bool = True

    # Property ID for cost accounting
    property_id: str = "unknown"

    def __post_init__(self) -> None:
        if self.base_url.startswith("http://"):
            self.base_url = "https://" + self.base_url[len("http://"):]
