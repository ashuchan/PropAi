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
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ProfileMaturity(str, Enum):
    COLD = "COLD"
    WARM = "WARM"
    HOT = "HOT"


class BlockedEndpoint(BaseModel):
    """API endpoint analyzed and found to contain no unit data."""

    url_pattern: str
    reason: str = ""  # "chatbot_config", "analytics", "no_unit_data", etc.
    blocked_at: datetime = Field(default_factory=datetime.utcnow)
    attempts: int = 1  # incremented on each re-encounter


class LlmFieldMapping(BaseModel):
    """LLM-generated JSON path mapping for deterministic replay on future runs."""

    api_url_pattern: str
    json_paths: dict[str, str] = Field(default_factory=dict)  # field -> key name in API response
    response_envelope: str = ""  # e.g., "data.results.units" — path to the unit list
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    success_count: int = 0


class ApiEndpoint(BaseModel):
    """A discovered API endpoint that returns unit/floor-plan data."""

    url_pattern: str
    json_paths: dict[str, str] = Field(default_factory=dict)
    provider: Optional[str] = None  # "sightmap", "knock", "entrata_api", etc.


class FieldSelectorMap(BaseModel):
    """CSS selectors for extracting unit fields from the DOM."""

    container: Optional[str] = None
    unit_id: Optional[str] = None
    rent: Optional[str] = None
    sqft: Optional[str] = None
    bedrooms: Optional[str] = None
    bathrooms: Optional[str] = None
    availability_status: Optional[str] = None
    availability_date: Optional[str] = None
    floor_plan_name: Optional[str] = None


class ExpanderAction(BaseModel):
    """A click-to-expand action needed before DOM parsing."""

    selector: str
    action: str = "click"  # "click" or "scroll_into_view"


class NavigationConfig(BaseModel):
    """How to navigate to the property's availability page."""

    entry_url: Optional[str] = None
    availability_page_path: Optional[str] = None
    winning_page_url: Optional[str] = None  # Exact URL that produced units last time
    requires_interaction: list[ExpanderAction] = Field(default_factory=list)
    timeout_ms: int = 60000
    block_resource_domains: list[str] = Field(default_factory=list)
    availability_links: list[str] = Field(default_factory=list)  # All links that led to availability data
    explored_links: list[str] = Field(default_factory=list)  # Links explored that had no data (skip next run)


class ApiHints(BaseModel):
    """Learned API interception hints."""

    known_endpoints: list[ApiEndpoint] = Field(default_factory=list)
    widget_endpoints: list[str] = Field(default_factory=list)  # Entrata widget URLs with data
    api_provider: Optional[str] = None
    wait_for_url_pattern: Optional[str] = None
    blocked_endpoints: list[BlockedEndpoint] = Field(default_factory=list)  # Per-property noise blocklist
    llm_field_mappings: list[LlmFieldMapping] = Field(default_factory=list)  # Saved mappings for replay


class DomHints(BaseModel):
    """Learned DOM parsing hints."""

    platform_detected: Optional[str] = None
    field_selectors: FieldSelectorMap = Field(default_factory=FieldSelectorMap)
    jsonld_present: bool = False
    availability_page_sections: list[str] = Field(default_factory=list)  # CSS selectors for unit sections


class ExtractionConfidence(BaseModel):
    """Track extraction success/failure history to drive maturity promotion."""

    preferred_tier: Optional[int] = None  # 1-5
    last_success_tier: Optional[int] = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    last_unit_count: int = 0
    maturity: ProfileMaturity = ProfileMaturity.COLD


class LlmArtifacts(BaseModel):
    """Artifacts from LLM extraction calls, used for drift detection."""

    extraction_prompt_hash: Optional[str] = None
    field_mapping_notes: Optional[str] = None
    api_schema_signature: Optional[str] = None
    dom_structure_hash: Optional[str] = None
    last_api_analysis_results: dict[str, str] = Field(default_factory=dict)  # API URL -> "has_units"|"noise"


class ScrapeProfile(BaseModel):
    """Per-property scraping profile that learns optimal extraction strategy."""

    canonical_id: str
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = "BOOTSTRAP"  # BOOTSTRAP | LLM_EXTRACTION | LLM_VISION | HUMAN

    navigation: NavigationConfig = Field(default_factory=NavigationConfig)
    api_hints: ApiHints = Field(default_factory=ApiHints)
    dom_hints: DomHints = Field(default_factory=DomHints)
    confidence: ExtractionConfidence = Field(default_factory=ExtractionConfidence)
    llm_artifacts: LlmArtifacts = Field(default_factory=LlmArtifacts)
    cluster_id: Optional[str] = None


def detect_platform(url: str) -> Optional[str]:
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
