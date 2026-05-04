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

from ma_poc.config.feature_flags import ENABLE_TIER_ESCALATION  # E0: wired; used in E3
from ma_poc.models.scrape_profile import (
    ApiEndpoint,
    BlockedEndpoint,
    FieldPatch,
    FieldSelectorMap,
    LlmFieldMapping,
    ProfileMaturity,
    ScrapeProfile,
)
from ma_poc.services.profile_store import ProfileStore

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
    "TIER_4_LLM_API": 4,  # Phase 3: targeted analyze_api_with_llm
    "TIER_4_LLM_DOM": 4,  # Phase 3: targeted analyze_dom_with_llm
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
    profile.api_hints.blocked_endpoints.append(BlockedEndpoint(url_pattern=api_url, reason=reason))
    # Trim oldest entries if over cap
    if len(profile.api_hints.blocked_endpoints) > _MAX_BLOCKED_ENDPOINTS:
        profile.api_hints.blocked_endpoints = profile.api_hints.blocked_endpoints[-_MAX_BLOCKED_ENDPOINTS:]


def save_llm_field_mapping(
    profile: ScrapeProfile,
    mapping_dict: dict,
    source_envelope_hash: str = "",
    expected_unit_count: int | None = None,
    body_for_validation: Any = None,
    multi_source: bool = False,
) -> bool:
    """Save an LLM-generated field mapping to the profile for future replay.

    Returns True on save/upsert, False on rejection. Never raises.

    Phase 1: silent early-returns are now logged. success_count is NOT
    incremented here — that belongs to the REPLAY path in generic.py.

    Phase 6: source_envelope_hash is recorded with the mapping for
    drift detection.

    Phase 10: when body_for_validation + expected_unit_count are provided,
    immediately replay the mapping; if it produces fewer than 80% of
    expected units, demote quality_score (which then multiplies replay
    confidence in the cascade).

    Fix 3: when multi_source=True (multiple API endpoints contributed to the
    total unit count), skip per-endpoint self-validation since expected_unit_count
    is not attributable to any single endpoint.
    """
    url_pattern = mapping_dict.get("api_url_pattern", "")
    json_paths = mapping_dict.get("json_paths") or {}
    if not url_pattern:
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty api_url_pattern (paths=%d)",
            len(json_paths),
        )
        return False
    if not json_paths:
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty json_paths for url=%s",
            url_pattern[:80],
        )
        return False

    # Phase 10: self-validation (skipped when multi_source — count is not per-endpoint)
    quality_score = 1.0
    if not multi_source and body_for_validation is not None and expected_unit_count is not None and expected_unit_count > 0:
        try:
            from ma_poc.services.llm_extractor import apply_saved_mapping
            replayed = apply_saved_mapping(
                body_for_validation,
                {
                    "response_envelope": mapping_dict.get("response_envelope", ""),
                    "json_paths": json_paths,
                },
            ) or []
        except Exception:
            replayed = []
        ratio = len(replayed) / expected_unit_count
        if ratio < 0.8:
            quality_score = max(0.4, ratio)
            log.warning(
                "Mapping for %s saved at quality_score=%.2f (replay produced %d/%d units)",
                url_pattern[:80], quality_score, len(replayed), expected_unit_count,
            )

    for existing in profile.api_hints.llm_field_mappings:
        if existing.api_url_pattern == url_pattern:
            existing.json_paths = json_paths
            existing.response_envelope = mapping_dict.get("response_envelope", existing.response_envelope)
            if source_envelope_hash:
                existing.source_envelope_hash = source_envelope_hash
            existing.quality_score = quality_score
            # NOTE: success_count is NOT incremented here. This is the
            # "re-save" path. success_count belongs to the REPLAY path.
            return True

    profile.api_hints.llm_field_mappings.append(
        LlmFieldMapping(
            api_url_pattern=url_pattern,
            json_paths=json_paths,
            response_envelope=mapping_dict.get("response_envelope", ""),
            source_envelope_hash=source_envelope_hash,
            quality_score=quality_score,
        )
    )
    if len(profile.api_hints.llm_field_mappings) > _MAX_LLM_FIELD_MAPPINGS:
        profile.api_hints.llm_field_mappings = profile.api_hints.llm_field_mappings[-_MAX_LLM_FIELD_MAPPINGS:]
    return True


def save_field_patch(profile: ScrapeProfile, patch_dict: dict) -> bool:
    """Phase 7 — upsert a FieldPatch by (api_url_pattern, field_name).

    Returns True on save/upsert, False on rejection. Never raises.
    """
    try:
        url = patch_dict.get("api_url_pattern", "") or ""
        field_name = patch_dict.get("field_name", "") or ""
        if not url or not field_name:
            log.warning("save_field_patch: dropped url=%s field=%s", url[:60], field_name)
            return False
        json_path = (patch_dict.get("json_path", "") or "").lstrip("$").lstrip(".")
        for existing in profile.api_hints.field_patches:
            if existing.api_url_pattern == url and existing.field_name == field_name:
                existing.json_path = json_path or existing.json_path
                existing.confidence = patch_dict.get("confidence", existing.confidence)
                existing.parser_fix = patch_dict.get("parser_fix", existing.parser_fix)
                if patch_dict.get("_envelope_hash"):
                    existing.source_envelope_hash = patch_dict["_envelope_hash"]
                return True
        profile.api_hints.field_patches.append(FieldPatch(
            api_url_pattern=url,
            field_name=field_name,
            json_path=json_path,
            confidence=patch_dict.get("confidence", 0.85),
            parser_fix=patch_dict.get("parser_fix"),
            source_envelope_hash=patch_dict.get("_envelope_hash", ""),
        ))
        if len(profile.api_hints.field_patches) > 50:
            profile.api_hints.field_patches = profile.api_hints.field_patches[-50:]
        return True
    except Exception as exc:
        log.warning("save_field_patch failed: %s", exc)
        return False


def update_rescue_counter(profile: ScrapeProfile, rescue_succeeded: bool) -> ScrapeProfile:
    """After an LLM rescue attempt, increment or reset the consecutive-failure counter.

    When the counter reaches 3, the orchestrator skips future rescue attempts
    for this property (cost guard).
    """
    if rescue_succeeded:
        profile.stats.consecutive_llm_rescue_failures = 0
    else:
        profile.stats.consecutive_llm_rescue_failures += 1
    return profile


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

    # Phase 1: monotonic stats — never go backward
    profile.stats.total_scrapes += 1
    profile.stats.last_tier_used = tier or None
    profile.stats.last_unit_count = units_extracted

    if units_extracted > 0 and tier and tier != "FAILED":
        profile.stats.total_successes += 1
    else:
        profile.stats.total_failures += 1

    # Phase 1: LLM cost accounting (the AdapterResult-to-dict translator
    # already attaches _llm_interactions in scraper.py; sum once here).
    llm_interactions = scrape_result.get("_llm_interactions") or []
    if llm_interactions:
        profile.stats.total_llm_calls += len(llm_interactions)
        profile.stats.total_llm_cost_usd += sum(
            (i.get("cost_usd", 0.0) or 0.0) for i in llm_interactions
        )

    # Record success/failure streak
    if units_extracted > 0 and tier and tier != "FAILED":
        profile.confidence.consecutive_successes += 1
        profile.confidence.consecutive_failures = 0
        tier_num = _TIER_MAP.get(tier)
        if tier_num:
            profile.confidence.last_success_tier = tier_num
            if profile.confidence.preferred_tier is None or tier_num < profile.confidence.preferred_tier:
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
    if tier in (
        "TIER_1_API",
        "TIER_1_PROFILE_MAPPING",
        "TIER_1_5_EMBEDDED",
        "TIER_1_WIDGET",
        "TIER_5_5_EXPLORATORY",
    ):
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
                if any(
                    k in path.lower()
                    for k in [
                        "floor",
                        "plan",
                        "avail",
                        "rent",
                        "unit",
                        "conventional",
                    ]
                ):
                    profile.navigation.availability_page_path = path
                    break

    # ── Record LLM API analysis results (new workflow) ─────────
    # Build a url→body lookup from the raw captured responses once
    raw_apis = scrape_result.get("_raw_api_responses", []) or []
    url_to_body: dict[str, Any] = {
        r.get("url", ""): r.get("body")
        for r in raw_apis
        if isinstance(r, dict)
    }
    # Use actual unit count from this run as the validation target
    expected_n = max(len(scrape_result.get("units", []) or []), 1)

    try:
        from ma_poc.models.source import envelope_hash_of as _envelope_hash_of
    except Exception:
        _envelope_hash_of = None  # type: ignore[assignment]

    llm_analysis = scrape_result.get("_llm_analysis_results", {})
    is_multi_source = len(llm_analysis) > 1
    for api_url, result in llm_analysis.items():
        if isinstance(result, dict) and result.get("api_url_pattern"):
            # Phase B: match the captured body by api_url_pattern substring
            pattern = result.get("api_url_pattern", "")
            matched_body = None
            for url, body in url_to_body.items():
                if pattern in url:
                    matched_body = body
                    break
            env_hash = ""
            if matched_body is not None and _envelope_hash_of is not None:
                try:
                    env_hash = _envelope_hash_of(matched_body)
                except Exception:
                    pass
            save_llm_field_mapping(
                profile,
                result,
                source_envelope_hash=env_hash,
                body_for_validation=matched_body,
                expected_unit_count=expected_n,
                multi_source=is_multi_source,
            )
        elif result == "blocked" or (isinstance(result, str) and result.startswith("noise:")):
            reason = result.replace("noise:", "").strip() if isinstance(result, str) else "no_unit_data"
            update_profile_blocklist(profile, api_url, reason)

    # ── Record explored links ────────────────────────────────
    explored = scrape_result.get("_explored_links", {})
    for link, had_data in explored.items():
        record_explored_link(profile, link, had_data)

    # ── Phase 7: persist field patches from null_field_recovery ──
    patches_payload = scrape_result.get("_field_patches", []) or []
    for patch_dict in patches_payload:
        if isinstance(patch_dict, dict):
            save_field_patch(profile, patch_dict)

    # Phase 8: DOM hints miss tracking + eviction
    if scrape_result.get("_dom_hints_attempted"):
        if scrape_result.get("_dom_hints_hit"):
            profile.dom_hints.consecutive_misses = 0
        else:
            profile.dom_hints.consecutive_misses = getattr(
                profile.dom_hints, "consecutive_misses", 0
            ) + 1
            try:
                from ma_poc.observability.events import EventKind, emit
                emit(
                    EventKind.DOM_HINTS_MISS,
                    profile.canonical_id,
                    count=profile.dom_hints.consecutive_misses,
                )
            except Exception:
                pass
            if profile.dom_hints.consecutive_misses >= 3:
                log.info(
                    "Evicting DOM field selectors for %s after 3 consecutive misses",
                    profile.canonical_id,
                )
                profile.dom_hints.field_selectors = FieldSelectorMap()
                profile.dom_hints.consecutive_misses = 0
                try:
                    from ma_poc.observability.events import EventKind, emit
                    emit(EventKind.DOM_HINTS_EVICTED, profile.canonical_id)
                except Exception:
                    pass

    # Phase 6: evict stale LlmFieldMappings after 3 consecutive replay failures
    _EVICTION_THRESHOLD = 3
    before_count = len(profile.api_hints.llm_field_mappings)
    profile.api_hints.llm_field_mappings = [
        m for m in profile.api_hints.llm_field_mappings
        if getattr(m, "consecutive_replay_failures", 0) < _EVICTION_THRESHOLD
    ]
    evicted = before_count - len(profile.api_hints.llm_field_mappings)
    if evicted:
        log.info("Evicted %d stale mapping(s) for %s", evicted, profile.canonical_id)
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(EventKind.MAPPING_EVICTED, profile.canonical_id, count=evicted)
        except Exception:
            pass

    # Phase 7: evict stale FieldPatches after 3 consecutive replay failures
    fp_before = len(profile.api_hints.field_patches)
    profile.api_hints.field_patches = [
        p for p in profile.api_hints.field_patches
        if getattr(p, "consecutive_replay_failures", 0) < _EVICTION_THRESHOLD
    ]
    fp_evicted = fp_before - len(profile.api_hints.field_patches)
    if fp_evicted:
        log.info("Evicted %d stale field patch(es) for %s", fp_evicted, profile.canonical_id)
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(EventKind.FIELD_PATCH_EVICTED, profile.canonical_id, count=fp_evicted)
        except Exception:
            pass

    # Phase 11: record source contribution telemetry
    merged_units = scrape_result.get("_merged_units", []) or []
    if merged_units:
        try:
            from ma_poc.services.source_observer import record_source_observations
            record_source_observations(profile, merged_units)
        except Exception as exc:
            log.warning("record_source_observations call failed: %s", exc)

    # Update last_sources_run for next-run failure-streak detection
    sources_payload = scrape_result.get("_sources", []) or []
    if sources_payload:
        try:
            profile.confidence.last_sources_run = [
                s.source_id.value if hasattr(s.source_id, "value") else str(s.source_id)
                for s in sources_payload
            ][:20]
        except Exception:
            pass

    # Phase 10: cold-run rotation counter
    if profile.confidence.maturity == ProfileMaturity.COLD:
        profile.confidence.cold_run_count += 1
    else:
        profile.confidence.cold_run_count = 0

    # F6 — persist a successfully-used RentCafe propertyId so the next
    # run skips resolver. Best-effort (H12); never raises. Only writes
    # when the direct path actually produced units (H13: a failed direct
    # fetch must NOT overwrite a previously-good id). The runner
    # signals success by stashing the id under
    # ``scrape_result["_rentcafe_property_id"]`` and stamping the tier
    # as one of the two success codes.
    try:
        pid = scrape_result.get("_rentcafe_property_id")
        rc_tier = scrape_result.get("extraction_tier_used", "")
        if (
            pid
            and rc_tier
            in (
                "TIER_1_API_RENTCAFE_DIRECT",
                "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
            )
        ):
            profile.api_hints.rentcafe_property_id = str(pid)
    except Exception as exc:
        log.warning("failed to persist rentcafe_property_id: %s", exc)

    profile.updated_at = datetime.utcnow()
    profile.version += 1
    store.save(profile)
    return profile


def update_fetch_profile_after_fetch(
    profile: Any,
    result: Any,
) -> Any:
    """Update profile.fetch based on a completed fetch. Never raises.

    Called after every fetch when ENABLE_TIER_ESCALATION is True.
    Handles promotion, demotion, and failure tracking.

    Args:
        profile: ScrapeProfile instance.
        result: FetchResult instance.

    Returns:
        Updated ScrapeProfile (same object, mutated in place).
    """
    if not ENABLE_TIER_ESCALATION:
        return profile
    try:
        from datetime import UTC
        from ma_poc.models.fetch_tier import FetchTier
        from ma_poc.observability.events import EventKind, emit

        fp = profile.fetch
        outcome = result.outcome if hasattr(result.outcome, "value") else str(result.outcome)
        outcome_str = outcome.value if hasattr(outcome, "value") else str(outcome)
        tier_used = int(result.fetch_tier_used)

        if outcome_str in ("OK", "NOT_MODIFIED"):
            fp.last_success_tier = FetchTier(tier_used)

            if tier_used > int(fp.tier_floor):
                # Promotion: property needed a higher tier today
                fp.tier_floor = FetchTier(tier_used)
                fp.promoted_at = datetime.now(UTC)
                fp.total_escalations += 1
                fp.consecutive_successes_at_floor = 1
                fp.consecutive_failures_at_floor = 0
                emit(EventKind.FETCH_TIER_PERSISTED, profile.canonical_id,
                     new_floor=fp.tier_floor.name, reason="promotion")
            elif tier_used == int(fp.tier_floor):
                fp.consecutive_successes_at_floor += 1
                fp.consecutive_failures_at_floor = 0
            else:
                # Demotion: probe at lower tier succeeded
                fp.tier_floor = FetchTier(tier_used)
                fp.consecutive_successes_at_floor = 1
                fp.consecutive_failures_at_floor = 0
                emit(EventKind.FETCH_TIER_DEMOTED, profile.canonical_id,
                     new_floor=fp.tier_floor.name)
        else:
            fp.consecutive_failures_at_floor += 1
            if outcome_str == "BOT_BLOCKED":
                fp.last_block_signature = result.block_signature
    except Exception as e:
        log.warning("fetch profile update failed: %s", e)
    return profile
