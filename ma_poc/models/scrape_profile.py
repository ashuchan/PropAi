"""
Self-learning scrape profile model.

Per-property profile that learns CSS selectors, API endpoints, and JSON paths
from LLM extraction (Tier 4). On subsequent runs the profile drives deterministic
extraction without LLM calls.

Phase: claude-scrapper-arch.md Step 1.1
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.fetch_tier import FetchTier


class ProfileMaturity(StrEnum):
    COLD = "COLD"
    WARM = "WARM"
    HOT = "HOT"


class BlockedEndpoint(BaseModel):
    """API endpoint analyzed and found to contain no unit data."""

    model_config = ConfigDict(extra="ignore")

    url_pattern: str
    reason: str = ""  # "chatbot_config", "analytics", "no_unit_data", etc.
    blocked_at: datetime = Field(default_factory=datetime.utcnow)
    attempts: int = 1  # incremented on each re-encounter


class LlmFieldMapping(BaseModel):
    """LLM-generated JSON path mapping for deterministic replay on future runs."""

    model_config = ConfigDict(extra="ignore")

    api_url_pattern: str
    json_paths: dict[str, str] = Field(default_factory=dict)  # field -> key name in API response
    response_envelope: str = ""  # e.g., "data.results.units" — path to the unit list
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    success_count: int = 0


class ApiEndpoint(BaseModel):
    """A discovered API endpoint that returns unit/floor-plan data."""

    model_config = ConfigDict(extra="ignore")

    url_pattern: str
    json_paths: dict[str, str] = Field(default_factory=dict)
    provider: str | None = None  # "sightmap", "knock", "entrata_api", etc.


class FieldSelectorMap(BaseModel):
    """CSS selectors for extracting unit fields from the DOM."""

    model_config = ConfigDict(extra="ignore")

    container: str | None = None
    unit_id: str | None = None
    rent: str | None = None
    sqft: str | None = None
    bedrooms: str | None = None
    bathrooms: str | None = None
    availability_status: str | None = None
    availability_date: str | None = None
    floor_plan_name: str | None = None


class ExpanderAction(BaseModel):
    """A click-to-expand action needed before DOM parsing."""

    model_config = ConfigDict(extra="ignore")

    selector: str
    action: str = "click"  # "click" or "scroll_into_view"


class NavigationConfig(BaseModel):
    """How to navigate to the property's availability page."""

    model_config = ConfigDict(extra="ignore")

    entry_url: str | None = None
    availability_page_path: str | None = None
    winning_page_url: str | None = None  # Exact URL that produced units last time
    requires_interaction: list[ExpanderAction] = Field(default_factory=list)
    timeout_ms: int = 60000
    block_resource_domains: list[str] = Field(default_factory=list)
    availability_links: list[str] = Field(default_factory=list)  # All links that led to availability data
    explored_links: list[str] = Field(default_factory=list)  # Links explored that had no data (skip next run)

    @field_validator("explored_links", mode="before")
    @classmethod
    def cap_explored_links(cls, v: list[str]) -> list[str]:
        """Cap explored_links at 50 entries to prevent unbounded growth."""
        if isinstance(v, list) and len(v) > 50:
            return v[:50]
        return v


class ApiHints(BaseModel):
    """Learned API interception hints."""

    model_config = ConfigDict(extra="ignore")

    known_endpoints: list[ApiEndpoint] = Field(default_factory=list)
    widget_endpoints: list[str] = Field(default_factory=list)  # Entrata widget URLs with data
    api_provider: str | None = "unknown"
    client_account_id: str | None = None
    wait_for_url_pattern: str | None = None
    blocked_endpoints: list[BlockedEndpoint] = Field(default_factory=list)  # Per-property noise blocklist
    llm_field_mappings: list[LlmFieldMapping] = Field(default_factory=list)  # Saved mappings for replay

    @field_validator("blocked_endpoints", mode="before")
    @classmethod
    def cap_blocked_endpoints(cls, v: list[object]) -> list[object]:
        """Cap blocked_endpoints at 50 entries."""
        if isinstance(v, list) and len(v) > 50:
            return v[:50]
        return v

    @field_validator("llm_field_mappings", mode="before")
    @classmethod
    def cap_llm_field_mappings(cls, v: list[object]) -> list[object]:
        """Cap llm_field_mappings at 20 entries."""
        if isinstance(v, list) and len(v) > 20:
            return v[:20]
        return v


class DomHints(BaseModel):
    """Learned DOM parsing hints."""

    model_config = ConfigDict(extra="ignore")

    platform_detected: str | None = None  # "entrata" | "rentcafe" | ... — set by bootstrap_from_meta
    field_selectors: FieldSelectorMap = Field(default_factory=FieldSelectorMap)
    jsonld_present: bool = False
    availability_page_sections: list[str] = Field(default_factory=list)  # CSS selectors for unit sections


class ExtractionConfidence(BaseModel):
    """Track extraction success/failure history to drive maturity promotion."""

    model_config = ConfigDict(extra="ignore")

    preferred_tier: int | None = None  # 1-5
    last_success_tier: int | None = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    last_unit_count: int = 0
    maturity: ProfileMaturity = ProfileMaturity.COLD
    last_success_detection: Any = None  # Stores DetectedPMS dict from ma_poc.pms.detector
    consecutive_unreachable: int = 0


class LlmArtifacts(BaseModel):
    """Artifacts from LLM extraction calls, used for drift detection."""

    model_config = ConfigDict(extra="ignore")

    extraction_prompt_hash: str | None = None
    field_mapping_notes: str | None = None
    api_schema_signature: str | None = None
    dom_structure_hash: str | None = None
    last_api_analysis_results: dict[str, str] = Field(default_factory=dict)  # API URL -> "has_units"|"noise"


class ProfileStats(BaseModel):
    """Aggregate statistics for a scrape profile."""

    model_config = ConfigDict(extra="ignore")

    total_scrapes: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_llm_calls: int = 0
    total_llm_cost_usd: float = 0.0
    last_tier_used: str | None = None
    last_unit_count: int = 0
    p50_scrape_duration_ms: int | None = None
    p95_scrape_duration_ms: int | None = None
    # F2: tracks consecutive LLM rescue failures to skip expensive calls on dead endpoints
    consecutive_llm_rescue_failures: int = 0


class FetchProfile(BaseModel):
    """Persisted fetch-tier state for a property.

    Lives at ScrapeProfile.fetch. Updated by services/profile_updater.py
    after every fetch (success or failure).
    """

    model_config = ConfigDict(extra="ignore")

    tier_floor: FetchTier = FetchTier.DIRECT
    last_success_tier: FetchTier | None = None
    consecutive_successes_at_floor: int = 0
    consecutive_failures_at_floor: int = 0
    last_block_signature: str | None = None
    last_demotion_probe_at: datetime | None = None
    promoted_at: datetime | None = None
    total_escalations: int = 0
    daily_unlocker_count: int = 0
    daily_unlocker_count_date: str | None = None  # ISO date of last reset


class ScrapeProfile(BaseModel):
    """Per-property scraping profile that learns optimal extraction strategy."""

    model_config = ConfigDict(extra="ignore")

    canonical_id: str
    version: int = 2
    schema_version: str = "v2"  # Jugnu explicit marker
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = "BOOTSTRAP"  # BOOTSTRAP | LLM_EXTRACTION | LLM_VISION | HUMAN

    navigation: NavigationConfig = Field(default_factory=NavigationConfig)
    api_hints: ApiHints = Field(default_factory=ApiHints)
    dom_hints: DomHints = Field(default_factory=DomHints)
    confidence: ExtractionConfidence = Field(default_factory=ExtractionConfidence)
    llm_artifacts: LlmArtifacts = Field(default_factory=LlmArtifacts)
    stats: ProfileStats = Field(default_factory=ProfileStats)
    fetch: FetchProfile = Field(default_factory=FetchProfile)


def detect_platform(url: str) -> str | None:
    """Detect PMS platform from URL patterns.

    Returns platform slug or None if unknown.
    """
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""

    if "rentcafe.com" in host or ("/apartments/" in path and "/default.aspx" in path):
        return "rentcafe"
    if "entrata" in host:
        return "entrata"
    if "appfolio" in host:
        return "appfolio"
    if "sightmap" in host:
        return "sightmap"
    if "realpage" in host:
        return "realpage"
    return None
