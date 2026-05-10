"""
Profile updater — updates profile after every extraction.

Analyses what worked (tier, API URLs, LLM hints) and writes it into the profile.
Promotes/demotes maturity based on consecutive success/failure streaks.

Phase: claude-scrapper-arch.md Step 3.1
"""

from __future__ import annotations

import logging
import re
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


# ── DOM-hint quality thresholds ────────────────────────────────────────────
# Single source of truth for the 0.4 / 0.8 / 0.05 magic numbers that wire
# together (a) the LLM_DOM save-time self-validation gate in
# pms/adapters/generic, (b) the replay-time admission gate also in
# generic, (c) the quality-tiered eviction policy below, and (d) the
# promote-on-hit increment.
#
# These four numbers form a contract — changing any one in isolation
# silently breaks one of the policies. Constants here are the
# authoritative values; both modules read them.

# Below this, the replay matcher refuses the saved hint AND the save
# site (with the degraded-persist flag off) drops the hint.
DOM_HINT_QUALITY_REPLAY_FLOOR = 0.4

# At/above this, eviction uses the 3-strike resilience policy (transient
# misses don't destroy a validated hint). Below it, eviction is 1-strike.
# Mirror of the comment at pms/adapters/generic.py:1867
# ("ratio >= 0.8 → high quality, persist as-is").
DOM_HINT_QUALITY_RESILIENT_FLOOR = 0.8

# Bump applied to quality_score on every successful replay hit.
# 0.05 means a degraded save (starts at 0.4) graduates to the resilient
# tier (0.8) after exactly 8 hits.
DOM_HINT_QUALITY_PROMOTE_STEP = 0.05

# Maximum quality (saturation point of promote-on-hit).
DOM_HINT_QUALITY_MAX = 1.0


# PR 2 (2026-05-10) Gap 3 — shared JSONPath normaliser + walker.
#
# Pre-PR: writer (save_field_patch) stripped only the leading "$." prefix;
# reader (_apply_field_patches::_get_path in generic.py) walked dot-segments
# and could only resolve numeric tokens as list indices. Bracket-notation
# paths the LLM canonically emits — `$.units[*].pricing.amount`,
# `units[0].rent` — became unresolvable strings the reader silently failed
# on. Saved patches stayed in the DB but never replayed.
#
# Post-PR: both writer and reader call _normalize_json_path, which converts
# all path styles to a single canonical token list. _walk_json_path
# resolves it, including [N] (numeric index) and [*] (each-element).
#
# Sentinel for wildcard. Using a private object so user-supplied strings
# can't collide with it. Comparison is identity-only.
_JSON_PATH_WILDCARD = object()

# Token-extracting regex: matches either "name" or "[123]" or "[*]".
# JSONPath grammar in the wild also has `..descendant`, filters, and
# slicing — none of which the LLM emits and none of which the existing
# replay layer needs. We deliberately keep this narrow.
_JSON_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")


def _normalize_json_path(path: str) -> list[Any]:
    """Convert a JSONPath string to a token list of (str | int | sentinel).

    Accepted inputs (all canonicalise to the same token list when equivalent):
      ``$.units[*].rent`` → ``["units", _WILDCARD, "rent"]``
      ``$.units[0].rent`` → ``["units", 0, "rent"]``
      ``units[0].rent``   → ``["units", 0, "rent"]``
      ``units.0.rent``    → ``["units", 0, "rent"]``  (legacy dot-only form)
      ``uuid``            → ``["uuid"]``
      ``""`` / ``None``   → ``[]``

    Idempotent: call on a path that's already been processed and you get
    the same list back as the path string. Re-call on the resulting list
    and you get the same list (after string-rejoining and re-parsing).
    """
    if not path:
        return []
    s = path.strip().lstrip("$").lstrip(".")
    if not s:
        return []
    tokens: list[Any] = []
    for m in _JSON_PATH_TOKEN_RE.finditer(s):
        name, bracket = m.group(1), m.group(2)
        if name is not None:
            # A bare numeric segment in dot notation is a list index too.
            if name.isdigit():
                tokens.append(int(name))
            else:
                tokens.append(name)
        elif bracket is not None:
            if bracket == "*":
                tokens.append(_JSON_PATH_WILDCARD)
            else:
                tokens.append(int(bracket))
    return tokens


def _walk_json_path(obj: Any, tokens: list[Any]) -> Any:
    """Resolve a normalised path token list against a JSON-shaped value.

    Returns:
      - The scalar / object / list at the path, OR
      - When a ``_JSON_PATH_WILDCARD`` token is encountered against a list,
        recurses for each element with the remaining tokens and returns a
        flat ``list`` of resolved values (None entries dropped).
      - ``None`` if any traversal step finds nothing.
    """
    if obj is None:
        return None
    if not tokens:
        return obj
    head, rest = tokens[0], tokens[1:]
    if head is _JSON_PATH_WILDCARD:
        if not isinstance(obj, list):
            return None
        out = []
        for item in obj:
            v = _walk_json_path(item, rest)
            if v is not None:
                out.append(v)
        return out
    if isinstance(head, int):
        if not isinstance(obj, list):
            return None
        if head < 0 or head >= len(obj):
            return None
        return _walk_json_path(obj[head], rest)
    # head is a string (dict key)
    if isinstance(obj, dict):
        return _walk_json_path(obj.get(head), rest)
    return None


def normalize_url_pattern(url: str) -> str:
    """Reduce a URL to the stable substring used for replay matching.

    PR 5 (2026-05-10): the replay matcher in ``pms.adapters.generic``
    does ``saved_pattern in incoming_url``, so any drift in query params,
    fragments, or scheme/case between runs makes the cache miss.
    Normalising both sides to ``host/path`` (lowercase host, no scheme,
    no query, no fragment) lets the substring compare survive rotated
    api_keys, session tokens, and timestamp params — which is what the
    Sweetwater FL profile saw in production (api_key rotation broke
    every replay attempt).

    Pre-existing un-normalised stored patterns continue to match because
    the matcher normalises the saved pattern at read time too — the
    full URL collapses to the same ``host/path`` form.

    Returns the input unchanged when normalisation would lose
    information (empty string, no path).
    """
    if not url:
        return ""
    raw = url.strip()
    # If the input lacks a scheme, urlparse won't extract a host. Synthesise
    # one so the parse is consistent — but remember to drop it on output if
    # it was synthesised (caller didn't ask for one).
    had_scheme = "://" in raw
    try:
        parsed = urllib.parse.urlparse(raw if had_scheme else "https://" + raw)
    except Exception:
        return raw
    host = (parsed.hostname or "").lower()
    # urllib.parse.urlparse keeps the port separately; drop it (replay
    # treats default and explicit-default as the same endpoint).
    path = parsed.path or ""
    if not host and not path:
        return raw
    if not host:
        # Path-only inputs ("/api/units") collapse to the path only.
        return path
    return f"{host}{path}"


def _emit_mapping_save_dropped(canonical_id: str, reason: str) -> None:
    """Emit MAPPING_SAVE_DROPPED so the daily analyser surfaces persistence drops.

    Never raises. Lazy-imports the events module so test contexts that
    don't initialise the ledger don't fail at import time.
    """
    try:
        from ma_poc.observability.events import EventKind, emit
        emit(EventKind.MAPPING_SAVE_DROPPED, canonical_id or "unknown", reason=reason)
    except Exception:
        pass

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


# Bug 4.4 — typed normalisation of the dual-shape ``_llm_analysis_results``
# value space. Historically the value at each URL key could be:
#   - a dict with "api_url_pattern" → save as LlmFieldMapping
#   - the literal string "blocked" → blocklist with reason "no_unit_data"
#   - a string starting with "noise:" → blocklist with the trailing reason
#   - anything else → silently ignored
# The silent-ignore branch made every refactor that changed the shape
# (e.g. introducing a typed verdict) lose data without any visible
# signal. The helper below makes the verdict explicit and logs anything
# unrecognised so a future shape drift surfaces immediately.
def _classify_llm_analysis_verdict(value: Any) -> tuple[str, dict | None, str | None]:
    """Return ``(kind, mapping, noise_reason)``.

    - ``kind="mapping"`` → ``mapping`` is the persistable dict
      (``api_url_pattern`` + ``json_paths`` + optional ``response_envelope``).
    - ``kind="noise"``   → ``noise_reason`` carries the LLM's free-text
      reason (or ``"no_unit_data"`` when not provided).
    - ``kind="ignored"`` → unrecognised shape; caller should log + skip.
    """
    if isinstance(value, dict) and value.get("api_url_pattern"):
        return "mapping", value, None
    if value == "blocked":
        return "noise", None, "no_unit_data"
    if isinstance(value, str) and value.startswith("noise:"):
        reason = value[len("noise:"):].strip() or "no_unit_data"
        return "noise", None, reason
    return "ignored", None, None


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

    PR 1 (2026-05-10): degraded persistence. When ``json_paths`` is empty
    BUT ``response_envelope`` is set, persist the mapping with
    ``quality_score=0.5`` (instead of the prior silent drop) so the URL is
    known to the profile for next-run prioritisation. Replay falls through
    to the cascade. Wrapped behind ``ENABLE_DEGRADED_MAPPING_PERSIST``
    (default on) for kill-switch ability if degraded mappings start
    spamming the replay tier with empty-replay attempts.

    On every drop, emit ``MAPPING_SAVE_DROPPED`` with the reason so the
    daily analyser's named-fix table surfaces the count day 1.
    """
    from ma_poc.config.feature_flags import enable_degraded_mapping_persist

    # PR 5 (2026-05-10): normalise URL at write time so the matcher's
    # substring compare survives query-param drift between runs.
    raw_url = mapping_dict.get("api_url_pattern", "")
    url_pattern = normalize_url_pattern(raw_url)
    json_paths = mapping_dict.get("json_paths") or {}
    response_envelope = mapping_dict.get("response_envelope") or ""

    if not url_pattern:
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty api_url_pattern (paths=%d)",
            len(json_paths),
        )
        _emit_mapping_save_dropped(profile.canonical_id, "empty_pattern")
        return False

    is_degraded = not json_paths
    if is_degraded and not response_envelope:
        # Nothing learnable — drop. (Empty paths AND empty envelope means
        # the LLM gave us back only an URL we already know.)
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty paths AND envelope for url=%s",
            url_pattern[:80],
        )
        _emit_mapping_save_dropped(profile.canonical_id, "empty_paths_and_envelope")
        return False

    if is_degraded and not enable_degraded_mapping_persist():
        log.info(
            "save_llm_field_mapping: dropped degraded mapping for url=%s (flag off)",
            url_pattern[:80],
        )
        _emit_mapping_save_dropped(profile.canonical_id, "disabled_by_flag")
        return False

    if is_degraded:
        log.info(
            "save_llm_field_mapping: persisting degraded mapping (envelope-only) for %s",
            url_pattern[:80],
        )

    # Phase 10: self-validation (skipped when multi_source — count is not per-endpoint)
    # Degraded mappings (no json_paths) cannot self-validate by definition (the replay
    # would always produce 0 units), so we pin quality_score=0.5 and skip the Phase 10
    # check rather than letting it demote a known-degraded mapping to its 0.4 floor.
    quality_score = 0.5 if is_degraded else 1.0
    if not is_degraded and not multi_source and body_for_validation is not None and expected_unit_count is not None and expected_unit_count > 0:
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
        if ratio < DOM_HINT_QUALITY_RESILIENT_FLOOR:
            quality_score = max(DOM_HINT_QUALITY_REPLAY_FLOOR, ratio)
            log.warning(
                "Mapping for %s saved at quality_score=%.2f (replay produced %d/%d units)",
                url_pattern[:80], quality_score, len(replayed), expected_unit_count,
            )

    for existing in profile.api_hints.llm_field_mappings:
        # PR 5 (2026-05-10): existing entry may have been stored pre-PR-5
        # with a raw URL. Normalise both sides of the equality check so the
        # upsert correctly collapses old + new entries for the same endpoint.
        # On match, also REWRITE existing.api_url_pattern to the normalised
        # form so subsequent saves and the matcher see the canonical key.
        if normalize_url_pattern(existing.api_url_pattern) == url_pattern:
            if existing.api_url_pattern != url_pattern:
                existing.api_url_pattern = url_pattern
            # PR 1 (2026-05-10) self-review HIGH fix: don't let a degraded
            # follow-up overwrite a previously-good mapping. If the
            # existing entry has non-empty json_paths AND the new entry is
            # degraded (json_paths empty), keep the existing fields and
            # only refresh metadata. Otherwise overwrite with the new
            # mapping as before.
            existing_full = bool(existing.json_paths)
            new_degraded = is_degraded
            if existing_full and new_degraded:
                # Preserve existing json_paths and quality. The new envelope
                # may be different (rare — same URL, different shape) so
                # accept it only if existing didn't have one.
                if not existing.response_envelope and response_envelope:
                    existing.response_envelope = response_envelope
                if source_envelope_hash and not existing.source_envelope_hash:
                    existing.source_envelope_hash = source_envelope_hash
                # quality_score stays at the existing value — don't downgrade.
                # Don't emit MAPPING_SAVE_DROPPED here: the call returns
                # True (the existing better mapping is what we want to keep).
                # Telemetry-wise this is a preservation win, not a drop.
                log.info(
                    "save_llm_field_mapping: upsert preserved existing full mapping for %s "
                    "(new mapping was degraded, would have downgraded quality_score)",
                    url_pattern[:80],
                )
                return True
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
        # PR 2 (2026-05-10) Gap 3: store the canonicalised path string (no "$." prefix,
        # bracket notation preserved) so the reader's _walk_json_path can resolve it
        # via _normalize_json_path. Pre-PR did only ``lstrip("$").lstrip(".")`` which
        # left "units[*].rent" alone — fine for the writer, but the reader's
        # dot-only walker silently failed on the bracket. Now both sides parse
        # through the same helper.
        raw_path = patch_dict.get("json_path", "") or ""
        # Preserve the canonical string form (whatever the LLM emitted) but
        # strip the "$." prefix once so we don't double-prefix on round trips.
        json_path = raw_path.strip().lstrip("$").lstrip(".")
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
    #
    # NOTE: previously the entire hint-capture block was gated on
    # ``tier in ("TIER_4_LLM", "TIER_4_LLM_API", "TIER_4_LLM_DOM",
    # "TIER_5_VISION")``. That gate silently discarded everything the
    # LLM learned whenever a deterministic tier ultimately won (e.g.
    # ``TIER_MERGED_CROSS_PAGE`` after a hop, or ``TIER_3_DOM`` after the
    # cascade). The fix is to gate on the *presence* of ``_llm_hints``
    # — if LLM ran and produced anything, persist it regardless of who
    # got the units credit. The ``updated_by`` label stays tier-gated
    # since that flag describes the WINNING tier, not the
    # learning surface.
    llm_hints = scrape_result.get("_llm_hints")
    llm_tiers = ("TIER_4_LLM", "TIER_4_LLM_API", "TIER_4_LLM_DOM", "TIER_5_VISION")
    if llm_hints:
        if tier in llm_tiers:
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
            # Persist every selector slot the FieldSelectorMap model
            # exposes. Earlier this dropped floor_plan_name + availability_status
            # (the LLM returns them, the model has the fields, the writer
            # forgot to pass them through). amenities + concession are new
            # slots — DOM analysis prompts ask for them, so save them too.
            profile.dom_hints.field_selectors = FieldSelectorMap(
                container=css.get("container"),
                rent=css.get("rent"),
                sqft=css.get("sqft"),
                bedrooms=css.get("bedrooms"),
                bathrooms=css.get("bathrooms"),
                availability_date=css.get("availability_date"),
                availability_status=css.get("availability_status"),
                unit_id=css.get("unit_id"),
                floor_plan_name=css.get("floor_plan_name"),
                amenities=css.get("amenities"),
                concession=css.get("concession"),
            )
            # Persist the save-time replay quality score the adapter
            # computed (1.0 by default for selectors that perfectly
            # reproduce their own LLM-extracted units; <0.8 means the
            # cascade should soft-trust them; <0.4 should never reach
            # this path because the adapter drops the hints entirely).
            # Without this score, consecutive-miss eviction is the only
            # safety net, which spends a wasted DOM cascade on every
            # broken selector for three runs before clearing — exactly
            # the daily LLM-tax pattern this fix targets.
            quality_raw = llm_hints.get("css_selectors_quality")
            if isinstance(quality_raw, (int, float)):
                profile.dom_hints.field_selectors_quality = max(
                    0.0, min(1.0, float(quality_raw))
                )
            else:
                profile.dom_hints.field_selectors_quality = 1.0

        if llm_hints.get("platform_guess"):
            profile.dom_hints.platform_detected = llm_hints["platform_guess"]
            profile.api_hints.api_provider = llm_hints["platform_guess"]

        if llm_hints.get("field_mapping_notes"):
            profile.llm_artifacts.field_mapping_notes = llm_hints["field_mapping_notes"]

        # Drift-detection hashes — populated by the extractors at call
        # time (sha-256[:16] of the prompt template / dom section /
        # api envelope). Stored on llm_artifacts so a future run can
        # compare and decide whether cached mappings still apply.
        if llm_hints.get("extraction_prompt_hash"):
            profile.llm_artifacts.extraction_prompt_hash = llm_hints["extraction_prompt_hash"]
        if llm_hints.get("dom_structure_hash"):
            profile.llm_artifacts.dom_structure_hash = llm_hints["dom_structure_hash"]
        if llm_hints.get("api_schema_signature"):
            profile.llm_artifacts.api_schema_signature = llm_hints["api_schema_signature"]

        # Raw navigation hints emitted by the LLM (e.g. "/floorplans"),
        # captured even when no link-hop has consumed them yet so the
        # next run can prioritise the same URL without re-paying for
        # the LLM diagnostic. Existing entries refresh in place; we
        # keep the most recent 10 (validator caps).
        #
        # PR 7 (2026-05-10): also merge ``scrape_result["_llm_navigation_hints"]``
        # — the LIST captured by ``_merge_hint_extras`` across every LLM
        # sub-tier in this run. The singular ``llm_hints["navigation_hint"]``
        # is overwritten by the latest tier; the list preserves all hints.
        # Without this, when LLM_DOM emits ``/floorplans`` and the monolithic
        # tier emits ``/availability`` only the latest one persists, and
        # the next run only retries ``/availability``.
        nav_candidates: list[str] = []
        nav_hint_str = llm_hints.get("navigation_hint")
        if isinstance(nav_hint_str, str) and nav_hint_str.strip():
            nav_candidates.append(nav_hint_str.strip())
    else:
        nav_candidates = []

    raw_nav_list = scrape_result.get("_llm_navigation_hints") or []
    if isinstance(raw_nav_list, list):
        for v in raw_nav_list:
            if isinstance(v, str) and v.strip():
                nav_candidates.append(v.strip())

    if nav_candidates:
        existing_navs = list(profile.navigation.last_navigation_hints or [])
        for cleaned in nav_candidates:
            if cleaned in existing_navs:
                existing_navs.remove(cleaned)
            existing_navs.append(cleaned)
        profile.navigation.last_navigation_hints = existing_navs[-10:]

    # ── Property-level amenities (Bug 2.1) ───────────────────────────
    # Surfaced from any LLM tier via ``result["property_amenities"]``
    # (set in scraper.py from ``adapter_result._property_amenities``).
    # Lowercased + deduplicated; merged with prior runs so amenities
    # observed once stick across runs (a brief outage that drops the
    # amenities section shouldn't flush a property's known facilities).
    amen_raw = scrape_result.get("property_amenities")
    if isinstance(amen_raw, list) and amen_raw:
        existing_amen = {a.lower().strip() for a in (profile.property_amenities or []) if isinstance(a, str)}
        for a in amen_raw:
            if isinstance(a, str) and a.strip():
                existing_amen.add(a.strip().lower())
        profile.property_amenities = sorted(existing_amen)

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
    # Per-URL verdict cache the LLM produced this run. Maintains the
    # ``llm_artifacts.last_api_analysis_results`` field so the cascade
    # can future-skip re-analysing the same URL when the verdict is
    # still cached. URLs that resolved to mappings → "has_units";
    # URLs marked noise → "noise:<reason>".
    verdicts_this_run: dict[str, str] = {}
    for api_url, result in llm_analysis.items():
        kind, mapping_dict, noise_reason = _classify_llm_analysis_verdict(result)
        if kind == "mapping" and mapping_dict is not None:
            # Phase B: match the captured body by api_url_pattern substring
            pattern = mapping_dict.get("api_url_pattern", "")
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
                mapping_dict,
                source_envelope_hash=env_hash,
                body_for_validation=matched_body,
                expected_unit_count=expected_n,
                multi_source=is_multi_source,
            )
            verdicts_this_run[str(api_url)] = "has_units"
        elif kind == "noise":
            reason = noise_reason or "no_unit_data"
            update_profile_blocklist(profile, api_url, reason)
            verdicts_this_run[str(api_url)] = f"noise:{reason}"
        else:
            # Unknown verdict shape — log so a future shape drift is
            # caught loudly instead of silently dropped. Don't raise:
            # the property's other learning still completes.
            log.warning(
                "Unrecognised _llm_analysis_results value for %s on %s (type=%s) — skipped",
                api_url, profile.canonical_id, type(result).__name__,
            )

    # Merge into prior verdicts (newer entries supersede; cap at 50 to
    # bound profile growth — cluster-property profiles can otherwise
    # accumulate hundreds of one-shot URLs over time).
    if verdicts_this_run:
        prior = dict(profile.llm_artifacts.last_api_analysis_results or {})
        prior.update(verdicts_this_run)
        if len(prior) > 50:
            # Keep only the most-recently-seen 50 (dict ordering is
            # insertion-ordered as of Python 3.7+).
            prior = dict(list(prior.items())[-50:])
        profile.llm_artifacts.last_api_analysis_results = prior

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
    #
    # PR 8 (2026-05-10): quality-tiered eviction threshold.
    #   - Validated selectors (quality >= 0.8): 3-strike resilience —
    #     transient page issues shouldn't prematurely lose a hint that
    #     proved itself.
    #   - Soft selectors (quality < 0.8 — incl. PR 6's degraded saves at
    #     quality=0.4): 1-strike. They already failed self-validation OR
    #     were labelled "flaky" by the adapter; the first miss is the
    #     second strike. Cutting losses faster avoids 2 more days of
    #     wasted DOM cascade attempts before eviction.
    if scrape_result.get("_dom_hints_attempted"):
        if scrape_result.get("_dom_hints_hit"):
            profile.dom_hints.consecutive_misses = 0
            # PR 9 sub-2 (2026-05-10): promote-on-hint. Bump
            # field_selectors_quality by +0.05 (clamped at 1.0) so PR-6
            # degraded DOM saves at 0.4 can rise to 1.0 after consistent
            # replay success. Combined with PR 8's tiered eviction, a
            # degraded selector that proves itself moves into the 3-strike
            # bucket once it crosses 0.8.
            try:
                from ma_poc.config.feature_flags import enable_promote_on_hint
                if enable_promote_on_hint():
                    cur_q = float(getattr(profile.dom_hints, "field_selectors_quality", DOM_HINT_QUALITY_MAX) or DOM_HINT_QUALITY_MAX)
                    # round(.., 2) avoids float drift (see DOM_HINT_QUALITY_PROMOTE_STEP comment)
                    profile.dom_hints.field_selectors_quality = min(
                        DOM_HINT_QUALITY_MAX,
                        round(cur_q + DOM_HINT_QUALITY_PROMOTE_STEP, 2),
                    )
            except Exception:
                pass
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
            fs_quality = float(
                getattr(profile.dom_hints, "field_selectors_quality", DOM_HINT_QUALITY_MAX) or DOM_HINT_QUALITY_MAX
            )
            eviction_threshold = 3 if fs_quality >= DOM_HINT_QUALITY_RESILIENT_FLOOR else 1
            if profile.dom_hints.consecutive_misses >= eviction_threshold:
                log.info(
                    "Evicting DOM field selectors for %s after %d miss(es) (quality=%.2f, threshold=%d)",
                    profile.canonical_id,
                    profile.dom_hints.consecutive_misses,
                    fs_quality,
                    eviction_threshold,
                )
                profile.dom_hints.field_selectors = FieldSelectorMap()
                profile.dom_hints.consecutive_misses = 0
                try:
                    from ma_poc.observability.events import EventKind, emit
                    emit(
                        EventKind.DOM_HINTS_EVICTED,
                        profile.canonical_id,
                        threshold=eviction_threshold,
                        quality=round(fs_quality, 3),
                    )
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
