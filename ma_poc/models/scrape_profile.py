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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ma_poc.models.fetch_tier import FetchTier


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

    # Phase 6: drift detection + stale-mapping eviction
    consecutive_replay_failures: int = 0
    last_replayed_at: datetime | None = None
    source_envelope_hash: str = ""  # sha256[:16] of body when mapping was learned
    quality_score: float = 1.0  # demoted by Phase 10 self-validation; multiplies confidence


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
    # Selectors for the per-unit/per-card amenity list and the concession
    # banner / strikethrough rent. The DOM analysis prompt asks the LLM
    # for both; without these slots they were silently dropped at the
    # profile-write boundary.
    amenities: str | None = None
    concession: str | None = None


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
    # Raw navigation hints emitted by the LLM (e.g. "/Marketing/FloorPlans").
    # Captured even when the link-hop didn't take them so the next run can
    # still prioritise them — and so a human reviewer can see what the LLM
    # diagnosed even when no hop succeeded.
    last_navigation_hints: list[str] = Field(default_factory=list)

    @field_validator("explored_links", mode="before")
    @classmethod
    def cap_explored_links(cls, v: list[str]) -> list[str]:
        """Cap explored_links at 50 entries to prevent unbounded growth.

        Keeps the **newest** 50 — older dead-end discoveries roll off so
        recent learning isn't drowned by historical noise.
        """
        if isinstance(v, list) and len(v) > 50:
            return v[-50:]
        return v

    @field_validator("last_navigation_hints", mode="before")
    @classmethod
    def cap_last_navigation_hints(cls, v: list[str]) -> list[str]:
        if isinstance(v, list) and len(v) > 10:
            return v[-10:]
        return v


class FieldPatch(BaseModel):
    """Phase 7 — a learned single-field completion hint from null_field_recovery.

    Patches replay deterministically against captured API responses on
    subsequent runs as a low-priority source. JSONPath stored without
    the leading $. (stripped at boundary).
    """

    model_config = ConfigDict(extra="ignore")

    api_url_pattern: str
    field_name: Literal[
        "rent_low",
        "rent_high",
        "asking_rent",
        "market_rent_low",
        "market_rent_high",
        "unit_id",
        "unit_number",
        "floor_plan_name",
        "beds",
        "bedrooms",
        "baths",
        "bathrooms",
        "sqft",
        "available_date",
        "availability_date",
    ]
    json_path: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.85)
    parser_fix: str | None = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    consecutive_replay_failures: int = 0
    success_count: int = 0
    source_envelope_hash: str = ""


class SourceObservation(BaseModel):
    """Phase 11 — per-source telemetry tracking how often a source contributed
    the winning field value, and at what confidence."""

    model_config = ConfigDict(extra="ignore")

    source_id: str  # SourceId.value (string form, since it's a closed enum)
    field_group: str  # "identity" | "physical" | "transactional"
    contribution_count: int = 0
    last_contributed_at: datetime | None = None
    avg_confidence_when_won: float = 0.0
    consecutive_failures: int = 0


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
    # Phase 7
    field_patches: list[FieldPatch] = Field(default_factory=list)
    # Phase 11
    source_observations: list[SourceObservation] = Field(default_factory=list)
    # F6 (rentcafe_direct) — cached RentCafe propertyId. Set by the
    # profile_updater after a successful direct-path fetch; read by
    # jugnu_runner to skip the resolver call on subsequent runs (H6
    # cache hit). ``None`` means "not yet resolved" — the runner will
    # call resolve_property_id once and persist the result.
    rentcafe_property_id: str | None = None

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

    @field_validator("field_patches", mode="before")
    @classmethod
    def cap_field_patches(cls, v: list[object]) -> list[object]:
        if isinstance(v, list) and len(v) > 50:
            return v[-50:]
        return v

    @field_validator("source_observations", mode="before")
    @classmethod
    def cap_source_observations(cls, v: list[object]) -> list[object]:
        if isinstance(v, list) and len(v) > 20:
            return v[-20:]
        return v


class DomHints(BaseModel):
    """Learned DOM parsing hints."""

    model_config = ConfigDict(extra="ignore")

    platform_detected: str | None = None  # "entrata" | "rentcafe" | ... — set by bootstrap_from_meta
    field_selectors: FieldSelectorMap = Field(default_factory=FieldSelectorMap)
    jsonld_present: bool = False
    availability_page_sections: list[str] = Field(default_factory=list)  # CSS selectors for unit sections
    # Phase 8: drift eviction — clear field_selectors after 3 consecutive misses
    consecutive_misses: int = 0
    # Save-time replay quality for ``field_selectors``. Recorded by
    # ``profile_updater`` from the value the GenericAdapter computed when
    # the LLM produced the selectors: replay the selectors against the
    # source HTML and divide by the LLM's own unit count. A score below
    # 0.4 prevents persistence; between 0.4 and 0.8 the selectors persist
    # but the replay path soft-fails (won't short-circuit on them); 1.0
    # means perfect reproduction. Default is 1.0 for backward compat
    # with profiles written before this field existed — they were saved
    # without validation, but the consecutive-misses eviction still
    # cleans them up.
    field_selectors_quality: float = 1.0


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
    # Phase 10: COLD-property LLM tier rotation counter (reset when promoted out)
    cold_run_count: int = 0
    # Phase 11: source observation telemetry — list of source_id strings that ran in last scrape
    last_sources_run: list[str] = Field(default_factory=list)


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
    # Phase 12: cluster bootstrap — populated from detector's pms_client_account_id
    cluster_key: str = ""
    # Aggregated property-level amenities. Populated either from the LLM's
    # top-level ``property_amenities`` output or from the union of per-unit
    # amenity arrays. Lowercased + deduplicated. Lives on the profile (not
    # the units table) because amenities are a property-of-property
    # observation: stable across runs, refreshed on each successful scrape.
    property_amenities: list[str] = Field(default_factory=list)


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
