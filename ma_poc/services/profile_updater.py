"""
Profile updater — updates profile after every extraction.

Analyses what worked (tier, API URLs, LLM hints) and writes it into the profile.
Promotes/demotes maturity based on consecutive success/failure streaks.

Phase: claude-scrapper-arch.md Step 3.1
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime
from typing import Any

from models.scrape_profile import (
    ApiEndpoint,
    BlockedEndpoint,
    FieldSelectorMap,
    LlmFieldMapping,
    ProfileMaturity,
    ScrapeProfile,
)
from services.profile_store import ProfileStore

log = logging.getLogger(__name__)

# Tier name → tier number mapping
_TIER_MAP: dict[str, int] = {
    "TIER_1_API": 1,
    "TIER_1_PROFILE_MAPPING": 1,
    "TIER_1_5_EMBEDDED": 1,
    "TIER_1_SIGHTMAP": 1,
    "TIER_1_WIDGET": 1,
    "TIER_2_JSONLD": 2,
    "TIER_3_DOM": 3,
    "TIER_3_DOM_LLM": 3,
    "TIER_4_LLM": 4,
    "TIER_4_LLM_API": 4,   # Phase 3: targeted analyze_api_with_llm
    "TIER_4_LLM_DOM": 4,   # Phase 3: targeted analyze_dom_with_llm
    "TIER_4_ENTRATA_API": 4,
    "TIER_5_PORTAL": 5,
    "TIER_5_5_EXPLORATORY": 5,
    "TIER_5_VISION": 5,
}


def _response_looks_like_units(body: Any) -> bool:
    """Quick check if an API response body looks like it contains unit data."""
    if not body:
        return False
    text = str(body).lower()
    return any(k in text for k in ("unit", "floor", "plan", "rent", "price", "sqft"))


_MAX_BLOCKED_ENDPOINTS = 50
_MAX_LLM_FIELD_MAPPINGS = 20
_MAX_EXPLORED_LINKS = 30


def update_profile_blocklist(
    profile: ScrapeProfile,
    api_url: str,
    reason: str = "no_unit_data",
) -> None:
    """Add or update a blocked endpoint in the profile.

    If the URL already exists, increments the attempt count.
    Caps the list at _MAX_BLOCKED_ENDPOINTS (oldest removed first).
    """
    for ep in profile.api_hints.blocked_endpoints:
        if ep.url_pattern == api_url:
            ep.attempts += 1
            ep.blocked_at = datetime.utcnow()
            return
    profile.api_hints.blocked_endpoints.append(
        BlockedEndpoint(url_pattern=api_url, reason=reason)
    )
    # Trim oldest entries if over cap
    if len(profile.api_hints.blocked_endpoints) > _MAX_BLOCKED_ENDPOINTS:
        profile.api_hints.blocked_endpoints = profile.api_hints.blocked_endpoints[-_MAX_BLOCKED_ENDPOINTS:]


def save_llm_field_mapping(
    profile: ScrapeProfile,
    mapping_dict: dict,
) -> None:
    """Save an LLM-generated field mapping to the profile for future replay.

    If a mapping for the same URL pattern already exists, updates it and
    increments success_count. Caps at _MAX_LLM_FIELD_MAPPINGS.
    """
    url_pattern = mapping_dict.get("api_url_pattern", "")
    if not url_pattern:
        return

    for existing in profile.api_hints.llm_field_mappings:
        if existing.api_url_pattern == url_pattern:
            existing.json_paths = mapping_dict.get("json_paths", existing.json_paths)
            existing.response_envelope = mapping_dict.get("response_envelope", existing.response_envelope)
            existing.success_count += 1
            return

    profile.api_hints.llm_field_mappings.append(
        LlmFieldMapping(
            api_url_pattern=url_pattern,
            json_paths=mapping_dict.get("json_paths", {}),
            response_envelope=mapping_dict.get("response_envelope", ""),
        )
    )
    if len(profile.api_hints.llm_field_mappings) > _MAX_LLM_FIELD_MAPPINGS:
        profile.api_hints.llm_field_mappings = profile.api_hints.llm_field_mappings[-_MAX_LLM_FIELD_MAPPINGS:]


def record_explored_link(
    profile: ScrapeProfile,
    link: str,
    had_data: bool,
) -> None:
    """Record a link as having data (availability_links) or no data (explored_links)."""
    if had_data:
        if link not in profile.navigation.availability_links:
            profile.navigation.availability_links.append(link)
    else:
        if link not in profile.navigation.explored_links:
            profile.navigation.explored_links.append(link)
        if len(profile.navigation.explored_links) > _MAX_EXPLORED_LINKS:
            profile.navigation.explored_links = profile.navigation.explored_links[-_MAX_EXPLORED_LINKS:]


def update_profile_after_extraction(
    profile: ScrapeProfile,
    scrape_result: dict,
    units_extracted: int,
    store: ProfileStore,
) -> ScrapeProfile:
    """Update profile based on what worked during this scrape."""
    tier = scrape_result.get("extraction_tier_used")

    # Record success/failure streak
    if units_extracted > 0 and tier and tier != "FAILED":
        profile.confidence.consecutive_successes += 1
        profile.confidence.consecutive_failures = 0
        tier_num = _TIER_MAP.get(tier)
        if tier_num:
            profile.confidence.last_success_tier = tier_num
            if (
                profile.confidence.preferred_tier is None
                or tier_num < profile.confidence.preferred_tier
            ):
                profile.confidence.preferred_tier = tier_num
        profile.confidence.last_unit_count = units_extracted
    else:
        profile.confidence.consecutive_failures += 1
        profile.confidence.consecutive_successes = 0

    # Promote/demote maturity
    if profile.confidence.consecutive_successes >= 3:
        profile.confidence.maturity = ProfileMaturity.HOT
    elif profile.confidence.consecutive_successes >= 1:
        profile.confidence.maturity = ProfileMaturity.WARM
    elif profile.confidence.consecutive_failures >= 3:
        profile.confidence.maturity = ProfileMaturity.COLD

    # ── Record the winning page URL ────────────────────────────────────
    # This is the actual URL (or widget endpoint) that produced unit data.
    # On subsequent runs the scraper can prioritise this URL.
    winning_url = scrape_result.get("_winning_page_url")
    if winning_url and units_extracted > 0:
        profile.navigation.winning_page_url = winning_url
        path = urllib.parse.urlparse(winning_url).path
        if path and path != "/":
            profile.navigation.availability_page_path = path

    # ── Record API URLs that had data (Tier 1 / widget) ──────────────
    if tier in ("TIER_1_API", "TIER_1_PROFILE_MAPPING", "TIER_1_5_EMBEDDED",
                "TIER_1_WIDGET", "TIER_5_5_EXPLORATORY"):
        raw_apis = scrape_result.get("_raw_api_responses", [])
        for api in raw_apis:
            url = api.get("url", "")
            if _response_looks_like_units(api.get("body")):
                # Track widget endpoints separately (they need special handling)
                if "/apartments/module/widgets/" in url.lower():
                    if url not in profile.api_hints.widget_endpoints:
                        profile.api_hints.widget_endpoints.append(url)
                elif not any(ep.url_pattern == url for ep in profile.api_hints.known_endpoints):
                    profile.api_hints.known_endpoints.append(ApiEndpoint(url_pattern=url))

    # ── Record LLM-generated hints (Tier 4 / Tier 5) ────────────────
    # Phase 3 added TIER_4_LLM_API (targeted per-API analysis) and
    # TIER_4_LLM_DOM (targeted per-DOM-section analysis). Both carry
    # learnable hints — json_paths for the former, css_selectors for the
    # latter — so they're treated the same as the monolithic TIER_4_LLM.
    llm_hints = scrape_result.get("_llm_hints")
    llm_tiers = ("TIER_4_LLM", "TIER_4_LLM_API", "TIER_4_LLM_DOM", "TIER_5_VISION")
    if llm_hints and tier in llm_tiers:
        profile.updated_by = "LLM_VISION" if tier == "TIER_5_VISION" else "LLM_EXTRACTION"

        # API hints from LLM
        for api_url in llm_hints.get("api_urls_with_data") or []:
            if not any(ep.url_pattern == api_url for ep in profile.api_hints.known_endpoints):
                profile.api_hints.known_endpoints.append(
                    ApiEndpoint(
                        url_pattern=api_url,
                        json_paths=llm_hints.get("json_paths", {}),
                    )
                )

        # DOM hints from LLM
        css = llm_hints.get("css_selectors") or {}
        if css.get("container"):
            profile.dom_hints.field_selectors = FieldSelectorMap(
                container=css.get("container"),
                rent=css.get("rent"),
                sqft=css.get("sqft"),
                bedrooms=css.get("bedrooms"),
                bathrooms=css.get("bathrooms"),
                availability_date=css.get("availability_date"),
                unit_id=css.get("unit_id"),
            )

        if llm_hints.get("platform_guess"):
            profile.dom_hints.platform_detected = llm_hints["platform_guess"]
            profile.api_hints.api_provider = llm_hints["platform_guess"]

        if llm_hints.get("field_mapping_notes"):
            profile.llm_artifacts.field_mapping_notes = llm_hints["field_mapping_notes"]

    # ── Navigation hints from the actual crawl ───────────────────────
    # Always update availability_page_path if we found units via crawling
    # (even if a previous path was stored — the site may have changed).
    if not winning_url:
        crawled = scrape_result.get("property_links_crawled", [])
        if crawled and units_extracted > 0:
            for url in crawled:
                path = urllib.parse.urlparse(url).path
                if any(k in path.lower() for k in [
                    "floor", "plan", "avail", "rent", "unit", "conventional",
                ]):
                    profile.navigation.availability_page_path = path
                    break

    # ── Record LLM API analysis results (new workflow) ─────────
    llm_analysis = scrape_result.get("_llm_analysis_results", {})
    for api_url, result in llm_analysis.items():
        if isinstance(result, dict) and result.get("api_url_pattern"):
            # This is an LlmFieldMapping — API had unit data
            save_llm_field_mapping(profile, result)
        elif result == "blocked" or (isinstance(result, str) and result.startswith("noise:")):
            reason = result.replace("noise:", "").strip() if isinstance(result, str) else "no_unit_data"
            update_profile_blocklist(profile, api_url, reason)

    # ── Record explored links ────────────────────────────────
    explored = scrape_result.get("_explored_links", {})
    for link, had_data in explored.items():
        record_explored_link(profile, link, had_data)

    profile.updated_at = datetime.utcnow()
    profile.version += 1
    store.save(profile)
    return profile
