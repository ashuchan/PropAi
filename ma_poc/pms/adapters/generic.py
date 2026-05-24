"""
Generic fallback adapter.

This adapter contains what remains of the current cascade from the main scraper
after PMS-specific branches (widget filter, map parser, API probe) are moved
into their respective adapters.

Research log
------------
Web sources consulted:
  - Internal: scripts main scraper parse_api_responses() (lines 503-664)
  - Internal: scripts main scraper extract_embedded_json() (lines 1229-1363)
  - Internal: scripts main scraper parse_jsonld() (lines 926-1008)
  - Internal: scripts main scraper parse_dom() (lines 1012-1176)
Real payloads inspected (from data/runs/*/raw_api/):
  - Multiple properties with various API shapes (Yardi /api/v1/, /api/v3/,
    Knock doorway-api, custom REST endpoints)
  - 12617 (Stoney Brook) — community_info endpoint (community-level only)
  - 254976 (San Artes) — gounion property status endpoint (property metadata)
Key findings:
  - Generic parser must handle 50+ key name variants for unit fields
  - Response envelopes vary: direct list[], {objects: [...]}, {data: {units: [...]}},
    {response: {floorplans: [...]}}, {results: [...]}
  - LLM/Vision tiers only run for pms=="unknown"; detected PMS failures skip LLM
  - Ported from parse_api_responses() with PMS-specific branches removed
"""

from __future__ import annotations

import logging
import re as _re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ma_poc.models.scrape_profile import FieldSelectorMap as _FieldSelectorMap
from ma_poc.pms.adapters._air_communities import (
    derive_plan_context_from_url as _air_url_ctx,
)
from ma_poc.pms.adapters._air_communities import (
    detect_air_communities as _detect_air,
)
from ma_poc.pms.adapters._air_communities import (
    parse_per_plan_html as _air_parse_per_plan,
)
from ma_poc.pms.adapters._air_communities import (
    parse_residences_html as _air_parse_residences,
)
from ma_poc.pms.adapters._amli import (
    detect_amli_trpc_blob as _detect_amli,
)
from ma_poc.pms.adapters._amli import (
    parse_amli_trpc_blob as _parse_amli,
)
from ma_poc.pms.adapters._apts247 import (
    build_floorplans_url as _apts247_build_url,
)
from ma_poc.pms.adapters._apts247 import (
    detect_apts247 as _detect_apts247,
)
from ma_poc.pms.adapters._apts247 import (
    extract_api_key as _apts247_api_key,
)
from ma_poc.pms.adapters._apts247 import (
    parse_apts247_floorplans as _parse_apts247,
)
from ma_poc.pms.adapters._daily_runner_parsers import (
    parse_api_responses as _dr_parse_api_responses,
)
from ma_poc.pms.adapters._daily_runner_parsers import (
    parse_sightmap_payload as _dr_parse_sightmap,
)
from ma_poc.pms.adapters._funnel import (
    build_availability_api_url as _funnel_api_url,
)
from ma_poc.pms.adapters._funnel import (
    detect_funnel as _detect_funnel,
)
from ma_poc.pms.adapters._funnel import (
    find_property_id as _funnel_property_id,
)
from ma_poc.pms.adapters._funnel import (
    parse_funnel_api_response as _parse_funnel,
)
from ma_poc.pms.adapters._html_extract import (
    extract_embedded_blobs_from_html,
    extract_jsonld_from_html,
    extract_units_from_data_attr_cards,
    extract_units_from_dom,
    extract_units_from_html_tables,
    extract_with_hints,
)
from ma_poc.pms.adapters._jetengine_repeater import (
    detect_jetengine_repeater as _detect_jetengine,
)
from ma_poc.pms.adapters._jetengine_repeater import (
    parse_jetengine_rows as _parse_jetengine,
)
from ma_poc.pms.adapters._mark_taylor import (
    derive_floor_plans_url as _mt_derive_fp_url,
)
from ma_poc.pms.adapters._mark_taylor import (
    detect_mark_taylor as _detect_mt,
)
from ma_poc.pms.adapters._mark_taylor import (
    parse_mark_taylor_html as _parse_mt,
)
from ma_poc.pms.adapters._merge_fns import (
    aggregate_quality as _aggregate_quality,
)
from ma_poc.pms.adapters._merge_fns import (
    find_unit_list as _find_unit_list,
)
from ma_poc.pms.adapters._merge_fns import (
    has_unit_signals as _has_unit_signals,
)
from ma_poc.pms.adapters._merge_fns import (
    merge_into_result_units as _merge_into_result_units,
)
from ma_poc.pms.adapters._nestio_widget import (
    detect_widget_rendered as _detect_nestio_widget_rendered,
)
from ma_poc.pms.adapters._nestio_widget import (
    parse_widget_dom as _parse_nestio_widget,
)
from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    format_rent_range,
    get_field,
    is_junk_floor_plan,
    is_junk_unit_number,
    make_unit_dict,
    money_to_int,
    rent_in_sanity_range,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.g5 import (
    is_g5_graphql_body as _is_g5_graphql_body,
)
from ma_poc.pms.adapters.g5 import (
    is_g5_graphql_url as _is_g5_graphql_url,
)
from ma_poc.pms.adapters.g5 import (
    parse_g5_response as _parse_g5_response,
)

# F0.1: module-level logger. Previously the cost-cap-exceeded branch
# referenced a bare ``log`` symbol that didn't exist, so a property that
# legitimately blew the cap raised NameError instead of logging — the
# error was masked by an outer try/except and the branch silently
# behaved as if the cap had not been enforced (the property failed with a
# NameError instead of stopping further LLM calls).
log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from playwright.async_api import Page

# Used by the Option C relaxed-LLM gate to sanity-check HTML has enough
# rent-ish content to be worth an LLM call even when the detected PMS
# adapter already returned empty.
_re_strip_script = _re.compile(r"<script.*?</script>|<style.*?</style>", _re.IGNORECASE | _re.DOTALL)
_re_strip_tag = _re.compile(r"<[^>]+>")
_re_rent = _re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?", _re.IGNORECASE)

# RC4: media-type hard gate — content types and URL suffixes that can never
# contain unit data. Defined at module level so the check is not re-created
# on every extract() call. Used before the LLM budget is consumed to prevent
# feeding JS/CSS/font files from CDNs (e.g. cdngeneralmvc.rentcafe.com) to
# the LLM, which correctly returns no units but wastes $0.005 of budget.
_NON_DATA_CT_PREFIXES: tuple[str, ...] = (
    "text/javascript",
    "text/css",
    "font/",
    "image/",
    "application/font",
    "application/x-font",
)
_NON_DATA_URL_SUFFIXES: tuple[str, ...] = (
    ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
)


def _is_non_data_response(resp: dict[str, Any]) -> bool:
    """Return True when the API response is a non-data media type (JS/CSS/font/image).

    Checks both the content_type field and the URL suffix. Query-string
    components are stripped before suffix matching.
    """
    ct = (resp.get("content_type") or
          (resp.get("headers") or {}).get("content-type") or "").lower()
    if any(ct.startswith(p) for p in _NON_DATA_CT_PREFIXES):
        return True
    url_lower = (resp.get("url") or "").lower().split("?")[0]
    return any(url_lower.endswith(sfx) for sfx in _NON_DATA_URL_SUFFIXES)


# Signal engine — module-level singletons (R1-H1 fix).
# All signal engine imports are in ONE try/except so they succeed or fail
# together. When any import fails the fallback path (_has_unit_signals,
# inline RC1 checks) is used throughout. Graceful degradation — never raises.
try:
    from ma_poc.pms.signal_engine.decider import (
        ActionDecider as _SEActionDecider,
    )
    from ma_poc.pms.signal_engine.decider import (
        ActionType as _SEActionType,
    )
    from ma_poc.pms.signal_engine.decider import (
        DecisionContext as _SEDecisionContext,
    )
    from ma_poc.pms.signal_engine.decider import (
        DOMAnalysisResult as _SEDOMAnalysisResult,
    )
    from ma_poc.pms.signal_engine.defaults import (
        create_default_qualifier as _create_sq,
    )
    from ma_poc.pms.signal_engine.defaults import (
        create_default_ranker as _create_sr,
    )
    from ma_poc.pms.signal_engine.models import (
        SourceKind as _SESourceKind,
    )
    from ma_poc.pms.signal_engine.models import (
        SourceSignal as _SESourceSignal,
    )
    _source_qualifier = _create_sq()
    _source_ranker = _create_sr()
    _action_decider = _SEActionDecider()   # stateless — safe to share across calls
except Exception:
    _source_qualifier = None  # type: ignore[assignment]
    _source_ranker = None  # type: ignore[assignment]
    _SESourceKind = None  # type: ignore[assignment]
    _SESourceSignal = None  # type: ignore[assignment]
    _action_decider = None  # type: ignore[assignment]
    _SEActionDecider = None  # type: ignore[assignment]
    _SEActionType = None  # type: ignore[assignment]
    _SEDecisionContext = None  # type: ignore[assignment]
    _SEDOMAnalysisResult = None  # type: ignore[assignment]

# RC1: TTL + minimum-verdicts thresholds — hoisted to module level for testability.
_BLOCK_TTL_DAYS: int = 14
_MIN_NOISE_VERDICTS: int = 2


def _should_block_endpoint(
    be: Any,
    now: datetime,
    ttl_days: int = _BLOCK_TTL_DAYS,
    min_verdicts: int = _MIN_NOISE_VERDICTS,
) -> bool:
    """Return True when a blocked-endpoint record should still suppress the URL.

    RC1: Re-admits when noise_verdicts < min_verdicts (insufficient evidence to
    sustain a permanent block) or when blocked_at is older than ttl_days (TTL
    expired — endpoint may have been redesigned).
    """
    attempts = int(getattr(be, "attempts", 1) or 1)
    if attempts < min_verdicts:
        return False
    blocked_at = getattr(be, "blocked_at", None)
    if blocked_at is not None:
        try:
            _ba = blocked_at
            if hasattr(_ba, "tzinfo") and _ba.tzinfo is not None:
                _ba = _ba.astimezone(UTC).replace(tzinfo=None)
            age_days = (now - _ba).days
            if age_days >= ttl_days:
                return False
        except Exception:
            pass
    return True


def _api_signal_qualifies(resp: dict[str, Any], items: list[Any]) -> bool:
    """Return True when the API response body matches a recognisable unit-data shape.

    Phase 4: SourceQualifier is the single gate, replacing the dual-run of
    _has_unit_signals() + qualifier from Phase 1. Falls back to has_unit_signals()
    when the signal engine is unavailable (import failure at startup).
    """
    if not items:
        return False
    if _source_qualifier is not None:
        first = items[0] if items else {}
        fkeys = frozenset(first.keys()) if isinstance(first, dict) else frozenset()
        sig = _SESourceSignal(
            kind=_SESourceKind.API_RESPONSE,
            url=resp.get("url"),
            content_type=resp.get("content_type"),
            field_keys=fkeys,
        )
        return _source_qualifier.qualify(sig).qualifies
    return _has_unit_signals(items)


# Broader rent-shaped pattern: matches rent values that don't lead with $.
# Examples: "Starting at 1,500", "1,500/mo", "from $1500/month", "1500 - 2000",
# "Rent: 1500", "Lease: $1,500/month". Used as a SECONDARY signal in
# ``_extract_rent_dom_section`` when the strict ``$NNN`` regex finds nothing
# — common on marketing-CMS sites (Jonah Digital, Hyly templates, etc.) that
# strip the dollar sign in display text.
_re_rent_loose = _re.compile(
    r"(?:starting\s+(?:at|from)|from|rent|lease|monthly|priced\s+at)\s*[:\-]?\s*"
    r"\$?\s?\d{3,4}(?:[,.]\d{3})?(?:\s*[/\-]\s*\$?\s?\d{3,4}(?:[,.]\d{3})?)?"
    r"(?:\s*/?\s*(?:mo|month|monthly))?",
    _re.IGNORECASE,
)

def _jsonld_gate_decision(units: list[dict[str, Any]], html: str) -> str:
    """Decide whether JSON-LD extracted ``units`` should win this scrape.

    Returns ``"accept"`` if the JSON-LD result should stand (current
    cascade will stop here), or a short reason string describing why
    the cascade should keep going — caller logs the reason and treats
    the units as empty so subsequent sub-tiers run.

    Rejection rules (any one fires):

    1. **name-only**: no rent, no sqft, AND no (floor_plan + beds/
       baths combo). Pure plan labels with nothing measurable. Falls
       back to LLM/DOM/etc.

    2. **no-rent + richer PMS present** *(2026-05-23 fix)*: JSON-LD
       got plan-level data but no rent, AND the page carries signals
       of a richer PMS source (SecureCafe, RentCafe XHR, Entrata,
       SightMap, MAAC API). Live-verified on
       mainstreetsquareapartments.com: JSON-LD won with 27 plan rows
       (no rent), but SecureCafe drill has 22 units WITH real rent.
       Without this rule, ~16 area-but-no-rent properties stamp
       TIER_2_JSONLD and downgrade to SUCCESS_PLAN_LEVEL when their
       PMS-specific adapter would have produced honest SUCCESS.

    Otherwise → ``"accept"``. (The original logic accepts any output
    carrying sqft OR a floor_plan+beds_or_baths combo. The new rule is
    additive — never accepts MORE than the original, only declines
    more aggressively when a PMS-specific path is likely to do better.)
    """
    if not units:
        return "no_units"
    has_rent = any(
        u.get("market_rent_low") or u.get("market_rent_high") or u.get("rent_range")
        for u in units
    )
    has_size = any(u.get("sqft") for u in units)
    has_beds_or_baths = any(
        u.get("bedrooms") or u.get("bathrooms") for u in units
    )
    has_floor_plan = any(u.get("floor_plan_name") for u in units)
    is_name_only = (
        not has_rent
        and not has_size
        and not (has_floor_plan and has_beds_or_baths)
    )
    if is_name_only:
        return (
            "JSON-LD had floor-plan names only (no rent/sqft/beds) — "
            "falling through"
        )
    # 2026-05-23: reject when JSON-LD has neither rent nor sqft, even
    # when beds+baths+floor_plan are present. Without this, 73 canary
    # properties stamped TIER_2_JSONLD with `fp+beds+baths` only and
    # never reached the deeper plan-text / embedded-JSON / subpage
    # tiers that often DO carry rent+sqft on a /floor-plans/ subpage.
    # Probe sample: 4/6 of these properties have full rent+sqft data
    # one click deeper (frginc /apartments/, larsonapts /floorplans/,
    # theclubsapt /floorplans/, rentchesapeakevillage /floor-plans/).
    # At worst the cascade finds nothing more and the property ends
    # up in the same partial bucket; at best we recover real rent+sqft.
    if not has_rent and not has_size:
        return (
            "JSON-LD has beds/baths but neither rent nor sqft — "
            "falling through to deeper tiers"
        )
    if not has_rent and html:
        h = html.lower()
        # Markers of a PMS-specific adapter that ships rent. Anchored
        # phrases (e.g. ``rentcafe.com``, ``maac.com/api/``) to avoid
        # incidental matches on logos or favicon paths.
        richer_pms = (
            "securecafe" in h
            or "rentcafe.com" in h
            or "entrata.com" in h
            or "sightmap.com" in h
            or "maac.com/api/" in h
        )
        if richer_pms:
            return (
                "JSON-LD has no rent and page has a richer PMS source "
                "(securecafe/rentcafe/entrata/sightmap/maac) — "
                "falling through"
            )
    return "accept"


def _looks_field_rich(units: list[dict[str, Any]]) -> bool:
    """Return True when units carry identity + physical (≥2 fields) + transactional.

    Uses the SAME absent-value semantics as services.source_planner.has_field_group
    so planner completeness scoring and cascade short-circuit decisions agree.
    """
    if not units:
        return False
    try:
        from ma_poc.services.source_planner import has_field_group
    except Exception:
        return False  # safety: never short-circuit if planner is unavailable
    return (
        any(has_field_group(u, "identity") for u in units)
        and any(has_field_group(u, "physical") for u in units)
        and any(has_field_group(u, "transactional") for u in units)
    )


def _has_rent_sqft_pair(units: list[dict[str, Any]]) -> bool:
    """True when at least one unit in *units* has BOTH rent and sqft.

    2026-05-23 partial-cohort fix: the dominant failure mode at the
    PARTIAL bucket level is a tier that accepted with rent OR sqft but
    not both. Without this guard, the cascade STOPs at the first tier
    that produced units, even when those units can't satisfy the
    strict success bar (≥1 unit with rent+sqft).

    With this guard wired into TIER_2_JSONLD / TIER_1_5_EMBEDDED /
    TIER_3_DOM acceptance sites, the cascade continues to deeper
    tiers when the current tier's output lacks the pair — often
    flipping a property from FAILED/PARTIAL to SUCCESS once a deeper
    tier (plan_text / subpage / dom_scan) finds the missing field.

    Defensively tolerant: returns False on empty list / non-list input.
    Tolerates the v2 schema (``market_rent_low`` + ``sqft``) and
    legacy variants (``rent_range`` + ``area``).
    """
    if not units:
        return False
    for u in units:
        if not isinstance(u, dict):
            continue
        rent = (
            u.get("market_rent_low")
            or u.get("market_rent_high")
            or u.get("rent_low")
            or u.get("rent_high")
            or u.get("asking_rent")
        )
        # rent_range is a string like "$1,500 - $2,000" — only count
        # when it contains an actual digit to avoid empty strings.
        if not rent:
            rr = u.get("rent_range") or ""
            if isinstance(rr, str) and any(c.isdigit() for c in rr):
                rent = rr
        if not rent:
            continue
        sqft = u.get("sqft") or u.get("area") or u.get("_sqft")
        if not sqft:
            continue
        # sqft can be -1 sentinel for "missing"
        try:
            if isinstance(sqft, str):
                sqft_num = int("".join(c for c in sqft if c.isdigit()) or "0")
            else:
                sqft_num = int(sqft)
        except (ValueError, TypeError):
            continue
        if sqft_num <= 0:
            continue
        return True
    return False


def _assess_and_decide(
    units_so_far: list[dict[str, Any]],
    sources_already_run: set,
    ctx: Any,
    decision_log: list,
) -> Any | None:
    """Consult the planner after a sub-tier produces units.

    Returns the Decision if planner fires; None if units are empty or import
    fails. Appends every non-None decision to decision_log.
    """
    if not units_so_far:
        return None
    try:
        from ma_poc.models.scrape_profile import ProfileMaturity
        from ma_poc.models.source import SourceId, from_legacy_unit
        from ma_poc.services.source_planner import evaluate_completeness, plan_next_action
    except Exception:
        return None
    try:
        pu_units = [
            from_legacy_unit(u, SourceId.API_GENERIC_NARROW, getattr(ctx, "base_url", ""), "", 0.85)
            for u in units_so_far
        ]
        report = evaluate_completeness(pu_units)
        floor: dict = {"complete": 0.85, "transactional": 0.70}
        profile = getattr(ctx, "profile", None)
        if profile is not None:
            try:
                if profile.confidence.maturity == ProfileMaturity.HOT:
                    floor = {"complete": 0.90, "transactional": 0.80}
            except Exception:
                pass
        decision = plan_next_action(
            report,
            sources_already_run=sources_already_run,
            budget_remaining=dict(getattr(ctx, "budget", {"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3})),
            pms_name=getattr(getattr(ctx, "detected", None), "pms", "unknown"),
            profile_completeness_floor=floor,
            profile_preferences=(
                list(profile.api_hints.source_observations) if profile is not None else []
            ),
        )
        decision_log.append(decision)
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(
                EventKind.PLANNER_DECISION,
                getattr(ctx, "property_id", "unknown"),
                action=decision.action,
                target_field_group=decision.target_field_group or "",
                rationale=decision.rationale[:120],
            )
        except Exception:
            pass
        return decision
    except Exception:
        return None




def _apply_field_patches(
    units: list[dict[str, Any]],
    api_responses: list[dict[str, Any]],
    field_patches: list[Any],
) -> list[dict[str, Any]]:
    """Sub-tier 0b: apply saved FieldPatches to replayed units via positional join.

    For each patch, find the matching API response, navigate to the list of values
    at json_path, then fill units[i][field_name] = values[i] where the field is
    currently absent/null. Never overwrites non-null values.
    Returns the (mutated) units list.

    PR 2 (2026-05-10) Gap 3: path resolution now uses
    ``services.profile_updater._normalize_json_path`` +
    ``_walk_json_path`` which support bracket notation (``units[*].rent``,
    ``data.items[0].price``). Pre-PR's dot-only walker silently failed on
    every LLM-emitted path with brackets — most paths beyond single-segment
    aliases like ``uuid``.
    """
    if not units or not api_responses or not field_patches:
        return units

    # Lazy import to keep the cross-module dependency localised. The
    # services package already imports ma_poc.pms in some flows, so an
    # eager import would risk a circular dep at module load time.
    from ma_poc.services.profile_updater import (
        _normalize_json_path,
        _walk_json_path,
    )

    def _get_path(obj: Any, path: str) -> Any:
        return _walk_json_path(obj, _normalize_json_path(path))

    for patch in field_patches:
        try:
            url_pat = getattr(patch, "api_url_pattern", None) or ""
            field_name = getattr(patch, "field_name", None) or ""
            json_path = getattr(patch, "json_path", None) or ""
        except Exception:
            continue
        if not (url_pat and field_name and json_path):
            continue
        # Find matching API response
        matched_body = None
        for resp in api_responses:
            if url_pat in resp.get("url", ""):
                matched_body = resp.get("body")
                break
        if matched_body is None:
            _mark_patch_miss(patch, reason="no_url_match")
            continue
        # Extract the list of patch values from the body
        values = _get_path(matched_body, json_path)
        if not isinstance(values, list):
            # Maybe it's a scalar — wrap it for single-unit case
            if values is not None:
                values = [values]
            else:
                _mark_patch_miss(patch, reason="path_returned_none")
                continue
        # Positional fill: units[i] gets values[i] if field is absent
        any_filled = False
        for i, unit in enumerate(units):
            if i >= len(values):
                break
            if unit.get(field_name) not in (None, "", -1, "-1"):
                continue
            v = values[i]
            if v not in (None, ""):
                unit[field_name] = v
                any_filled = True

        if any_filled:
            _mark_patch_hit(patch)
        else:
            _mark_patch_miss(patch, reason="no_unit_filled")
    return units


def _mark_patch_hit(patch: Any) -> None:
    """B2: bump success_count, reset consecutive_replay_failures, emit event."""
    try:
        if hasattr(patch, "success_count"):
            patch.success_count += 1
        if hasattr(patch, "consecutive_replay_failures"):
            patch.consecutive_replay_failures = 0
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(
                EventKind.FIELD_PATCH_HIT,
                "unknown",
                field=getattr(patch, "field_name", "") or "",
                url=str(getattr(patch, "api_url_pattern", ""))[:80],
            )
        except Exception:
            pass
    except Exception:
        pass


def _mark_patch_miss(patch: Any, reason: str) -> None:
    """B2: bump consecutive_replay_failures, emit drift event."""
    try:
        if hasattr(patch, "consecutive_replay_failures"):
            patch.consecutive_replay_failures += 1
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(
                EventKind.FIELD_PATCH_DRIFT,
                "unknown",
                field=getattr(patch, "field_name", "") or "",
                url=str(getattr(patch, "api_url_pattern", ""))[:80],
                reason=reason,
            )
        except Exception:
            pass
    except Exception:
        pass


def _extract_rent_dom_section(html: str, max_bytes: int = 20_000) -> str | None:
    """Return the smallest HTML chunk that contains the site's rent signals.

    We pick the tightest ancestor around rent-looking text rather than
    sending the entire page to the DOM-analysis LLM. This keeps the prompt
    small enough to meet per-call token limits and biases the model toward
    the unit/floor-plan container instead of global layout chrome.

    Strategy:
      1. Prefer ``<main>`` when present — apartment sites almost always put
         availability content there.
      2. Otherwise find the smallest container with 2+ rent-pattern matches.
      3. Cap at ``max_bytes`` so oversized ``<main>`` tags don't blow the
         per-call token budget.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_bytes]
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return html[:max_bytes]

    # Strip noise tags.
    for tag in soup.find_all(["script", "style", "svg", "noscript", "nav", "footer", "header", "iframe"]):
        tag.decompose()

    main = soup.find("main")
    if main:
        block = str(main)
        if len(block) > max_bytes:
            block = block[:max_bytes] + "<!-- truncated -->"
        return block

    # Find smallest ancestor holding 2+ rent patterns.
    best: Any = None
    best_len = 10**9
    for el in soup.find_all(True):
        try:
            text = el.get_text(" ", strip=True)
        except Exception:
            continue
        if len(_re_rent.findall(text)) < 2:
            continue
        s = str(el)
        if 500 <= len(s) <= max_bytes and len(s) < best_len:
            best, best_len = el, len(s)

    if best is not None:
        return str(best)

    # 2026-05 batch-3 broadened: try the loose rent-pattern (matches
    # "Starting at 1,500", "1,500/mo" etc. without `$`). This catches
    # marketing-CMS sites (Jonah Digital, Hyly templates) where rent
    # is displayed without dollar signs and the strict regex misses.
    best = None
    best_len = 10**9
    for el in soup.find_all(True):
        try:
            text = el.get_text(" ", strip=True)
        except Exception:
            continue
        if len(_re_rent_loose.findall(text)) < 2:
            continue
        s = str(el)
        if 500 <= len(s) <= max_bytes and len(s) < best_len:
            best, best_len = el, len(s)

    if best is not None:
        return str(best)

    # Last resort: strip to body, then truncate.
    body = soup.find("body")
    fallback = str(body) if body else str(soup)
    return fallback[:max_bytes]


async def _get_page_html(page: Any, ctx: AdapterContext) -> str | None:
    """Extract raw HTML from either a live Playwright page or fetch_result.body.

    Jugnu adapters may receive either a real Page (legacy ``scrape()`` path)
    or ``page=None`` with ``ctx.fetch_result.body`` populated by L1. Both
    should be usable; prefer the live page (post-JS-render content) and
    fall back to the fetch body (raw server HTML).
    """
    # Prefer live page content — it reflects post-render DOM.
    if page is not None and hasattr(page, "content"):
        try:
            content: Any = await page.content()
            if content and isinstance(content, str):
                return str(content)
        except Exception:
            pass

    # Fall back to the raw fetch body (bytes or str).
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return None
    body = getattr(fr, "body", None)
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(body, str):
        return body
    return None


def parse_generic_api(items: list[dict[str, Any]], url: str) -> list[dict[str, str]]:
    """Parse a generic list of unit/floorplan dicts using broad key name matching.

    Ported from the main scraper parse_api_responses() with PMS-specific branches
    removed (all PMS parsers moved to their own adapters).
    """
    units: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        name = get_field(
            item,
            "floorPlanName",
            "floor_plan_name",
            "floorplan_name",
            "floorplan-name",
            "name",
            "unitType",
            "planName",
        )
        beds_str = get_field(
            item, "bedrooms", "beds", "bedroom_count", "bedRooms", "numBedrooms", "no_of_bedroom", "bd", "bed"
        )
        baths_str = get_field(
            item,
            "bathrooms",
            "baths",
            "bathroom_count",
            "bathRooms",
            "numBathrooms",
            "no_of_bathroom",
            "ba",
            "bath",
        )
        sqft_str = get_field(
            item,
            "sqft",
            "squareFeet",
            "square_feet",
            "minSqft",
            "minimumSquareFeet",
            "size",
            "area",
            "square_footage",
            "sq_ft",
            "maximumSquareFeet",
        )
        unit_num = get_field(
            item,
            "unitNumber",
            "unit_number",
            "unitId",
            "unit_id",
            "label",
            "display_unit_number",
            "id",
            "unit_name",
        )
        rent_lo_str = get_field(
            item,
            "minRent",
            "rent_min",
            "min_rent",
            "startingFrom",
            "askingRent",
            "price",
            "rent",
            "minimumRent",
            "minimumMarketRent",
            "baseRent",
            "display_price",
            "monthlyRent",
            "startingPrice",
        )
        rent_hi_str = get_field(
            item,
            "maxRent",
            "rent_max",
            "max_rent",
            "maxAskingRent",
            "endingAt",
            "maximumRent",
            "maximumMarketRent",
        )
        avail_str = get_field(
            item,
            "availableCount",
            "available_count",
            "numAvailable",
            "unitsAvailable",
            "units_available",
            "availableUnitsCount",
        )
        avail_dt = get_field(
            item, "availableDate", "available_date", "moveInDate", "moveInReady", "availableOn", "readyDate"
        )
        floor_str = get_field(item, "floor", "floorNumber", "floor_id", "floorId")
        building_str = get_field(item, "building", "buildingName", "building_name")
        deposit_str = get_field(item, "deposit", "securityDeposit", "security_deposit", "depositAmount")
        concession_str = get_field(
            item, "concession", "special", "promotion", "specials_description", "specialsDescription"
        )
        plan_type = get_field(item, "floorPlanType", "type", "bedBath", "BedBath")
        status_str = get_field(item, "status", "availability_status", "leaseStatus", "unit_status")

        # Dedup gate: skip if missing ALL of [name, beds, sqft, rent_lo]
        if not any([name, beds_str, sqft_str, rent_lo_str]):
            continue

        # Phase 5: junk deny-list. Drops CMS-widget names like
        # "MODULE_CONCESSIONMANAGER" and "[Riedman] Lease Magnet - Pop-Up"
        # that the 2026-04-19 run surfaced as fake units. Also drops
        # unit_number stop-words ("Left", "s", etc).
        if is_junk_floor_plan(name):
            continue
        if is_junk_unit_number(unit_num):
            # Prefer to clear the unit number rather than drop the whole
            # record — the rent/sqft may still be valid floor-plan data.
            unit_num = ""

        # Dedup key
        dedup = unit_num or f"{name}|{beds_str}|{sqft_str}|{rent_lo_str}"
        if dedup in seen:
            continue
        seen.add(dedup)

        beds = int(float(beds_str)) if beds_str else None
        baths = int(float(baths_str)) if baths_str else None
        rent_lo = money_to_int(rent_lo_str)
        rent_hi = money_to_int(rent_hi_str)

        # Rent sanity check
        if not rent_in_sanity_range(rent_lo) or not rent_in_sanity_range(rent_hi):
            continue

        bl = bed_label_from(beds, name)
        if not bl and plan_type:
            bl = plan_type

        units.append(
            make_unit_dict(
                floor_plan_name=name,
                bed_label=bl,
                bedrooms=str(beds) if beds is not None else "",
                bathrooms=str(baths) if baths is not None else "",
                sqft=sqft_str,
                unit_number=unit_num,
                floor=floor_str,
                building=building_str,
                rent_range=format_rent_range(rent_lo, rent_hi),
                rent_low=rent_lo,
                rent_high=rent_hi or rent_lo,
                deposit=deposit_str,
                concession=concession_str,
                availability_status=status_str.upper() if status_str else "AVAILABLE",
                available_units=avail_str,
                availability_date=avail_dt,
                source_api_url=url,
                extraction_tier="TIER_1_API",
            )
        )

    return units


# ── RealPage CWS credential probe ────────────────────────────────────────────
# RealPage LeaseStar CWS sites embed RPFP_config in their HTML with a numeric
# propertyId and a UUID apiKey.  The units API requires x-ws-authkey but the
# key is NOT secret — it is embedded in the publicly served HTML to authenticate
# the browser-side JS widget.  We extract it and fire the call directly.

_RPFP_PROPERTY_ID = _re.compile(
    r'(?:var\s+propertyId|propertyId\s*:)\s*[=:]\s*["\']?(\d+)["\']?',
    _re.IGNORECASE,
)
_RPFP_API_KEY = _re.compile(
    r'apiKey\s*:\s*["\']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["\']',
    _re.IGNORECASE,
)
# Unit field mappings from the RealPage units API response
_RPFP_UNIT_MAP: dict[str, str] = {
    "unitNumber": "unit_number",
    "numberOfBeds": "bedrooms",
    "numberOfBaths": "bathrooms",
    "squareFeet": "sqft",
    "rent": "market_rent_low",
    "totalRent": "market_rent_high",
    "internalAvailableDate": "available_date",
    "floorNumber": "floor",
    "buildingName": "building",
    "floorplanId": "floor_plan_id",
}


async def _probe_realpage_cws(html: str) -> list[dict[str, Any]]:
    """Extract units from a RealPage CWS page by reading credentials from HTML.

    Args:
        html: Full page HTML containing RPFP_config.

    Returns:
        Parsed unit dicts, empty list on any failure.
    """
    pid_m = _RPFP_PROPERTY_ID.search(html)
    key_m = _RPFP_API_KEY.search(html)
    if not pid_m or not key_m:
        return []

    property_id = pid_m.group(1)
    api_key = key_m.group(1)

    url = (
        f"https://api.ws.realpage.com/v2/property/{property_id}/units"
        f"?available=true&honordisplayorder=true&siteid={property_id}"
        "&bestprice=true&leaseterm=3,4,5,6,7,8,9,10,11,12,13,14,15&baseRent=true"
    )
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"x-ws-authkey": api_key})
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    raw_units = (data.get("response") or {}).get("units") or []
    units: list[dict[str, Any]] = []
    for u in raw_units:
        if not isinstance(u, dict):
            continue
        record: dict[str, Any] = {}
        for src_key, dst_key in _RPFP_UNIT_MAP.items():
            val = u.get(src_key)
            if val is not None:
                record[dst_key] = val
        # Normalise date string to YYYY-MM-DD (strips time + TZ offset)
        avail = record.get("available_date")
        if isinstance(avail, str):
            record["available_date"] = avail[:10]
        # Lease status
        if u.get("leaseStatus") in ("Available", "available"):
            record["availability_status"] = "AVAILABLE"
        elif u.get("leaseStatus"):
            record["availability_status"] = "UNAVAILABLE"
        if record.get("unit_number"):
            units.append(record)
    return units


class GenericAdapter:
    """Generic fallback adapter.

    Contains the generic API parser and (when pms=="unknown") the full cascade
    including LLM/Vision tiers. When invoked for a detected PMS that failed,
    LLM/Vision tiers are skipped (controlled by ctx or skip_llm flag).
    """

    pms_name: str = "generic"
    _fingerprints: list[str] = []

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Run generic extraction cascade. Phase 5 wrapper: post-merge sidecar.

        Calls _extract_inner (the legacy cascade) and, if sub-tier 0 stashed
        partial replay units that need filling-in by other sub-tiers, runs
        merge_sources on the combined source list and rewrites result.units.

        Stage 1 validity gate runs **after** ``_phase5_post_merge`` (so the
        cross-source merger has a chance to combine partial records before
        filtering) but **before** ``_stash_provenanced_units`` (so the
        observability snapshot reflects the same set the run output ships).
        """
        result = await self._extract_inner(page, ctx)
        try:
            self._phase5_post_merge(result, ctx)
        except Exception:
            # Never break the run on a merge-side error (H10 best-effort)
            pass

        # Floor-plan sqft backfill — runs after merge, before validity gate.
        #
        # Fills sqft on units where it is absent by scanning ALL captured API
        # responses for floor-plan-aggregate bodies (short lists, has sqft +
        # beds but no per-unit IDs). When a (beds, baths) key maps
        # unambiguously to a single sqft value across all catalog APIs, that
        # sqft is written onto matching units. Never overwrites a real value;
        # never fills when the mapping is ambiguous.
        #
        # Real-world case: repli360.com get_apartmentsync_data_for_floorplan_*
        # endpoints have sqft ranges per floor plan but are tagged as LLM noise
        # because they don't carry per-unit identifiers. Units extracted from a
        # companion unit-level API (which has rent + beds but no sqft) get the
        # floor-plan sqft filled in here.
        if result.units:
            try:
                from ma_poc.observability.events import EventKind
                from ma_poc.observability.events import emit as _emit_ev
                from ma_poc.pms.adapters._sqft_backfill import run_sqft_backfill

                _api_responses: list[dict] = getattr(ctx, "_api_responses", []) or []
                _ctx_keys, _filled = run_sqft_backfill(result.units, _api_responses)
                if _filled > 0:
                    try:
                        _emit_ev(
                            EventKind.TIER_ATTEMPTED,
                            getattr(ctx, "property_id", ""),
                            tier_key="generic:floorplan_sqft_backfill",
                            outcome="ran_units",
                            units_found=_filled,
                            reason=f"{_ctx_keys} context keys, {_filled} units filled",
                            duration_ms=0,
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        # Stage 1 unified validity gate. Drops the rows that surveyed gates
        # (parse_generic_api / parse_api_responses per-item / has_unit_signals
        # / _container_yields_unit / _offer_to_unit / schema_gate.is_substantive)
        # would each have caught with different bars. See
        # docs/2026_05_11_regressions_fix_design.md (Stage 1).
        if result.units:
            try:
                from ma_poc.extraction.post_process import post_process

                _pp_parsed = len(result.units)
                _pp = post_process(
                    result.units, property_id=getattr(ctx, "property_id", None)
                )
                if _pp.n_admitted > 0:
                    result.units = _pp.admitted
                    result.plan_summaries = _pp.plan_summaries
                    # confidence already set by the winning sub-tier; only
                    # rescale if the validity-drop changes its denominator
                    # materially. Leave alone to preserve sub-tier scoring.
                else:
                    result.units = []
                    result.tier_used = "GENERIC_VALIDITY_REJECTED"
                    result.errors.append(
                        f"GENERIC_VALIDITY_REJECTED: {_pp_parsed} rows from "
                        f"{result.tier_used} failed unit_validity "
                        f"(no numeric dimension)"
                    )
            except Exception as _pp_exc:
                # post_process raised unexpectedly. Fail-safe: clear units
                # so no pre-gate junk ships, and log so the failure is
                # visible. Silently falling through was the original
                # behaviour but caused Sagestone Village-style rows (unit
                # number + floor_plan_name only, no dims, no rent) to reach
                # properties.json when the gate crashed.
                log.warning(
                    "post_process raised for property %s (%d units) — "
                    "clearing units to prevent pre-gate junk: %s",
                    getattr(ctx, "property_id", "?"),
                    len(result.units),
                    _pp_exc,
                    exc_info=True,
                )
                result.units = []
                result.tier_used = "GENERIC_VALIDITY_EXCEPTION"
                result.errors.append(
                    f"GENERIC_VALIDITY_EXCEPTION: post_process raised — {_pp_exc!r}"
                )

        # Fix 4: stash provenanced units for the Phase 11 self-learning loop
        # when the merge step didn't already populate _merged_units (single-source path).
        if not getattr(result, "_merged_units", None) and result.units:
            try:
                self._stash_provenanced_units(result, ctx)
            except Exception:
                pass
        return result

    @staticmethod
    def _stash_provenanced_units(result: AdapterResult, ctx: AdapterContext) -> None:
        """Wrap result.units as ProvenancedUnits for the Phase 11 self-learning loop.

        Populates result._merged_units so downstream observers (e.g. the
        cluster-store writer in jugnu_runner) have a consistent access path
        regardless of whether a multi-source merge occurred.
        """
        try:
            from ma_poc.models.source import SourceId, envelope_hash_of, from_legacy_unit
        except Exception:
            return
        winning_url = result.winning_url or getattr(ctx, "base_url", "") or ""
        tier_to_source = {
            "TIER_1_PROFILE_MAPPING": SourceId.MAPPING_REPLAY,
            "TIER_1_API": SourceId.API_GENERIC_NARROW,
            "TIER_1_5_EMBEDDED": SourceId.EMBEDDED_JSON,
            "TIER_2_JSONLD": SourceId.JSON_LD,
            "TIER_3_DOM": SourceId.DOM_CASCADE,
            "TIER_4_LLM_API": SourceId.LLM_API_TARGETED,
            "TIER_4_LLM_DOM": SourceId.LLM_DOM_TARGETED,
            "TIER_4_LLM": SourceId.LLM_MONOLITHIC,
        }
        source_id = tier_to_source.get(result.tier_used, SourceId.API_GENERIC_NARROW)
        env_hash = envelope_hash_of(result.units)
        conf = result.confidence if result.confidence > 0 else 0.80
        provenanced = [
            from_legacy_unit(u, source_id, winning_url, env_hash, conf)
            for u in result.units
        ]
        result._merged_units = provenanced  # type: ignore[attr-defined]

    @staticmethod
    def _phase5_post_merge(result: AdapterResult, ctx: AdapterContext) -> None:
        """If sub-tier 0 stashed `_phase5_replay_units` AND a later cascade
        sub-tier produced its own units, merge them via source_merger so
        partial mappings can't shadow native cascade fields. No-op when
        only one source produced units."""
        sidecar = getattr(result, "_phase5_replay_units", None)
        if not sidecar:
            return
        agg_q = float(getattr(result, "_phase5_replay_quality", 1.0) or 1.0)
        if not result.units:
            # Cascade produced nothing — replay is the sole contributor.
            result.units = list(sidecar)
            if not result.tier_used or result.tier_used == "TIER_1_API":
                result.tier_used = "TIER_1_PROFILE_MAPPING"
            result.confidence = min(0.90, 0.7 + 0.03 * len(sidecar)) * agg_q
            return
        # Both sources produced units — merge by identity, max-confidence per field.
        # NOTE: keep imports relative to the same root the merger uses internally
        # (models.source) so that FieldValue identity checks (isinstance) hold.
        try:
            from ma_poc.models.source import (
                ExtractedSource,
                SourceId,
                envelope_hash_of,
                from_legacy_unit,
                to_legacy_unit,
            )
            from ma_poc.services.source_merger import merge_sources
        except Exception:
            return
        winning_url = result.winning_url or ctx.base_url or ""
        replay_h = envelope_hash_of(sidecar)
        cascade_h = envelope_hash_of(result.units)
        # PR-FUTURE-WORK: when a FieldPatch is derived from a parent LlmFieldMapping,
        # the patch should inherit the mapping's quality_score demotion so that
        # patch-replay units also carry the reduced per-field confidence here.
        replay_src = ExtractedSource(
            source_id=SourceId.MAPPING_REPLAY,
            source_url=winning_url,
            envelope_hash=replay_h,
            units=[
                from_legacy_unit(u, SourceId.MAPPING_REPLAY, winning_url, replay_h, 0.85 * agg_q)
                for u in sidecar
            ],
            has_unit_ids=any(u.get("unit_number") or u.get("unit_id") for u in sidecar),
            is_floor_plan_level=False,
        )
        # Map the cascade's tier_used to the closest SourceId for provenance.
        cascade_source = {
            "TIER_1_API": SourceId.API_GENERIC_NARROW,
            "TIER_1_5_EMBEDDED": SourceId.EMBEDDED_JSON,
            "TIER_2_JSONLD": SourceId.JSON_LD,
            "TIER_3_DOM": SourceId.DOM_CASCADE,
            "TIER_3_DOM_LLM": SourceId.LLM_DOM_TARGETED,
            "TIER_4_LLM": SourceId.LLM_MONOLITHIC,
            "TIER_4_LLM_API": SourceId.LLM_API_TARGETED,
            "TIER_4_LLM_DOM": SourceId.LLM_DOM_TARGETED,
        }.get(result.tier_used, SourceId.API_GENERIC_NARROW)
        cascade_src = ExtractedSource(
            source_id=cascade_source,
            source_url=winning_url,
            envelope_hash=cascade_h,
            units=[
                from_legacy_unit(u, cascade_source, winning_url, cascade_h, 0.90)
                for u in result.units
            ],
            has_unit_ids=any(u.get("unit_number") or u.get("unit_id") for u in result.units),
            is_floor_plan_level=False,
        )
        # Phase I: emit IDENTITY_FUZZY_LINK events via callback; merger stays pure
        def _emit_fuzzy(unit: Any, key: Any, conf: float) -> None:
            try:
                from ma_poc.observability.events import EventKind as _EK
                from ma_poc.observability.events import emit as _ev
                _ev(_EK.IDENTITY_FUZZY_LINK, ctx.property_id, bucket_key=str(key)[:80], confidence=conf)
            except Exception:
                pass

        merged = merge_sources([replay_src, cascade_src], ctx.property_id, fuzzy_link_callback=_emit_fuzzy)
        if not merged:
            return
        legacy = [to_legacy_unit(u) for u in merged]
        # Strip _provenance before handing to legacy serializer; provenance
        # lives on result._sources for downstream observers.
        for u in legacy:
            u.pop("_provenance", None)
        result.units = legacy
        result._sources = [replay_src, cascade_src]  # type: ignore[attr-defined]
        # Phase D: stash the provenanced merge output for downstream observers
        result._merged_units = list(merged)  # type: ignore[attr-defined]
        # Phase I: emit SOURCES_MERGED telemetry
        try:
            from ma_poc.observability.events import EventKind as _EK2
            from ma_poc.observability.events import emit as _ev2
            _ev2(
                _EK2.SOURCES_MERGED,
                ctx.property_id,
                source_count=2,
                merged_unit_count=len(merged),
                tier=result.tier_used,
            )
        except Exception:
            pass
        # Tier label: deterministic merge unless an LLM source contributed.
        if cascade_source in (
            SourceId.LLM_API_TARGETED,
            SourceId.LLM_DOM_TARGETED,
            SourceId.LLM_MONOLITHIC,
        ):
            result.tier_used = "TIER_MERGED_HYBRID"
        else:
            result.tier_used = "TIER_MERGED_DETERMINISTIC"

    async def _extract_inner(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Legacy cascade — unchanged from pre-Phase-5 except sub-tier 0 may
        stash `_phase5_replay_units` instead of preempting the adapter when
        the replayed units are field-incomplete. See _phase5_post_merge."""
        import time as _time

        try:
            from ma_poc.observability.events import EventKind
            from ma_poc.observability.events import emit as _emit
        except Exception:
            _emit, EventKind = None, None  # type: ignore[assignment, misc]

        attempts: list[dict[str, Any]] = []

        def _log_attempt(
            key: str, outcome: str, units: int = 0, reason: str = "", duration_ms: int = 0
        ) -> None:
            entry = {
                "tier_key": key,
                "outcome": outcome,
                "units_found": units,
                "reason": reason,
                "duration_ms": duration_ms,
            }
            attempts.append(entry)
            if _emit is not None and EventKind is not None:
                try:
                    _emit(EventKind.TIER_ATTEMPTED, ctx.property_id, **entry)
                except Exception:
                    pass

        result = AdapterResult(tier_used="TIER_1_API")
        result._tier_attempts = attempts  # type: ignore[attr-defined]
        # Phase H: track which SourceIds have already run for the planner
        sources_already_run: set = set()
        # Phase H: decision log for telemetry / debugging
        decision_log: list = []
        all_units: list[dict[str, str]] = []
        # Option C gate: default is skip LLM when the detected PMS is not
        # "unknown" (GenericAdapter runs as a fallback for a failed PMS
        # adapter — spending LLM budget on those was originally gated OFF).
        # However the 10-property validation showed 2 FAILED_NO_DATA cases
        # (SightMap, RentCafe) where the detected adapter found nothing but
        # the HTML had visible text + rent signals. The relaxation below
        # re-enables LLM for those — evaluated after we have ``html`` and
        # can inspect its shape.
        #
        # F12 (2026-05-05): the gate-off was firing on ~100 AppFolio /
        # Entrata properties per run where the detected adapter returned
        # zero units. Those cases ended FAILED_NO_DATA with no LLM tier
        # to recover them. The condition now also requires that the
        # PMS-specific adapter actually produced units before we trust it
        # enough to skip the LLM. ``adapter_unit_count`` is the count of
        # units the upstream PMS adapter handed in via ctx; when 0, the
        # gate stays open regardless of detection confidence.
        adapter_unit_count = int(getattr(ctx, "adapter_unit_count", 0) or 0)
        skip_llm = ctx.detected.pms != "unknown" and adapter_unit_count > 0

        api_responses: list[dict[str, Any]] = getattr(ctx, "_api_responses", [])

        # Phase 4: filter out API URLs the profile previously classified as
        # noise. Saves token spend on known-bad endpoints (chatbot configs,
        # analytics pixels, CMS widgets). New noise discoveries also flow
        # back into this list via _llm_analysis_results.
        #
        # RC1: apply a 14-day TTL and a minimum-noise-verdicts guard before
        # treating a blocked endpoint as still-blocked. A URL that was
        # misclassified once (noise_verdicts < 2) should be re-admitted so
        # it can be re-evaluated, rather than blocked forever. A URL that
        # was correctly classified but whose block is >14 days old is also
        # re-admitted in case the endpoint has been redesigned.
        profile = getattr(ctx, "profile", None)
        blocked_urls: set[str] = set()
        if profile is not None:
            try:
                _now = datetime.now(UTC).replace(tzinfo=None)
                for be in getattr(profile.api_hints, "blocked_endpoints", []) or []:
                    pat = getattr(be, "url_pattern", None)
                    if not pat:
                        continue
                    if _should_block_endpoint(be, _now):
                        blocked_urls.add(str(pat))
            except Exception:
                blocked_urls = set()
        if blocked_urls:
            before = len(api_responses)
            api_responses = [r for r in api_responses if r.get("url", "") not in blocked_urls]
            dropped = before - len(api_responses)
            if dropped:
                _log_attempt(
                    "generic:blocked_filter",
                    "ran_units",
                    units=0,
                    reason=f"dropped {dropped} API(s) from profile.blocked_endpoints",
                )

        # Sub-tier 0a: deterministic probe of profile-recorded endpoints
        # (Bug 3.2 / 3.6). The profile remembers every URL that
        # previously yielded unit data — ``known_endpoints`` for general
        # APIs, ``widget_endpoints`` for Entrata-style XHR widgets that
        # render via iframe and don't always re-fire on entry-page
        # navigation. Without this probe those URLs were passively
        # captured only when the page happened to call them; pages that
        # render differently across runs (or that need a click to fire
        # the XHR) lose their cached endpoint and the property has to
        # rediscover it via LLM. Probing through the existing
        # Playwright session preserves cookies + same-origin headers.
        page_obj = getattr(ctx, "_page", None) or getattr(ctx, "page", None)
        if profile is not None and page_obj is not None:
            evaluate = getattr(page_obj, "evaluate", None)
            probe_urls: list[str] = []
            try:
                for ep in getattr(profile.api_hints, "known_endpoints", []) or []:
                    pat = getattr(ep, "url_pattern", None)
                    if isinstance(pat, str) and pat.startswith(("http://", "https://")):
                        probe_urls.append(pat)
                for w in getattr(profile.api_hints, "widget_endpoints", []) or []:
                    if isinstance(w, str) and w.startswith(("http://", "https://")):
                        probe_urls.append(w)
            except Exception:
                probe_urls = []
            # Already-captured URLs don't need re-probing — the existing
            # network log carries them. Also bound the probe count so a
            # cluster property with dozens of cached endpoints can't
            # blow the per-property time budget.
            seen_urls = {r.get("url", "") for r in api_responses}
            probe_urls = [u for u in probe_urls if u not in seen_urls][:5]
            if probe_urls and callable(evaluate):
                t0 = _time.monotonic()
                probed_added = 0
                for url in probe_urls:
                    try:
                        body = await evaluate(
                            "(u) => fetch(u, {credentials: 'include'})"
                            ".then(r => r.ok ? r.json() : null).catch(() => null)",
                            url,
                        )
                    except Exception:
                        continue
                    if body is not None:
                        api_responses.append({"url": url, "body": body})
                        probed_added += 1
                if probed_added:
                    _log_attempt(
                        "generic:profile_probe",
                        "ran_units",
                        units=0,
                        reason=f"probed {probed_added}/{len(probe_urls)} profile endpoint(s)",
                        duration_ms=int((_time.monotonic() - t0) * 1000),
                    )

        # Phase 4 sub-tier 0: deterministic replay of saved LLM mappings.
        # Runs before ANY parser: if a prior run's LLM told us exactly how
        # to extract units from a specific API shape, we can reuse it with
        # zero LLM cost. Falls through to the generic cascade on miss.
        if profile is not None and api_responses:
            t0 = _time.monotonic()
            replayed_units: list[dict[str, Any]] = []
            replayed_mappings: list[Any] = []
            try:
                saved = list(getattr(profile.api_hints, "llm_field_mappings", []) or [])
            except Exception:
                saved = []
            if saved:
                try:
                    from ma_poc.services.llm_extractor import apply_saved_mapping
                except ImportError:
                    apply_saved_mapping = None
                if apply_saved_mapping is not None:
                    # Phase 6: drift detection helper
                    try:
                        from ma_poc.models.source import envelope_hash_of as _env_hash
                    except ImportError:
                        _env_hash = None  # type: ignore[assignment]
                    from datetime import datetime as _dt

                    # Normalise both saved pattern and incoming URL so
                    # query-param drift (api_key rotation, session tokens)
                    # doesn't kill the substring match.
                    try:
                        from ma_poc.services.profile_updater import normalize_url_pattern as _norm_url
                    except ImportError:
                        # If the writer module is not importable in this
                        # context (test isolation, etc.) fall back to
                        # identity so the matcher still works on
                        # non-drifted URLs.
                        def _norm_url(x: str) -> str:
                            return x

                    for mapping in saved:
                        try:
                            pat = getattr(mapping, "api_url_pattern", None) or (
                                mapping.get("api_url_pattern") if isinstance(mapping, dict) else None
                            )
                        except Exception:
                            pat = None
                        if not pat:
                            continue
                        norm_pat = _norm_url(pat)
                        if not norm_pat:
                            continue
                        for resp in api_responses:
                            norm_url = _norm_url(resp.get("url", ""))
                            if norm_pat in norm_url:
                                # Phase 6: drift check
                                body = resp.get("body")
                                saved_hash = getattr(mapping, "source_envelope_hash", "") or ""
                                if saved_hash and _env_hash is not None:
                                    current_hash = _env_hash(body)
                                    if current_hash != saved_hash:
                                        # Drift — skip replay, count failure
                                        if hasattr(mapping, "consecutive_replay_failures"):
                                            try:
                                                mapping.consecutive_replay_failures += 1
                                            except Exception:
                                                pass
                                        try:
                                            from ma_poc.observability.events import EventKind, emit
                                            emit(
                                                EventKind.MAPPING_DRIFT_DETECTED,
                                                getattr(ctx, "property_id", "unknown"),
                                                url=str(resp.get("url", ""))[:80],
                                                saved_hash=saved_hash[:8],
                                                current_hash=current_hash[:8],
                                            )
                                        except Exception:
                                            pass
                                        continue
                                mdict = (
                                    mapping
                                    if isinstance(mapping, dict)
                                    else {
                                        "api_url_pattern": pat,
                                        "json_paths": getattr(mapping, "json_paths", {}) or {},
                                        "response_envelope": getattr(mapping, "response_envelope", "") or "",
                                    }
                                )
                                try:
                                    units = apply_saved_mapping(body, mdict) or []
                                except Exception:
                                    units = []
                                if hasattr(mapping, "last_replayed_at"):
                                    try:
                                        mapping.last_replayed_at = _dt.utcnow()
                                    except Exception:
                                        pass
                                if units:
                                    replayed_units.extend(units)
                                    result.api_responses.append(resp)
                                    replayed_mappings.append(mapping)
                                    # Phase 1: increment success_count
                                    if hasattr(mapping, "success_count"):
                                        try:
                                            mapping.success_count += 1
                                        except Exception:
                                            pass
                                    # Phase 6: reset failure streak on success
                                    if hasattr(mapping, "consecutive_replay_failures"):
                                        try:
                                            mapping.consecutive_replay_failures = 0
                                        except Exception:
                                            pass
                                    # PR 9 sub-2 (2026-05-10): promote-on-hint.
                                    # Bump quality_score by +0.05 (clamped at
                                    # 1.0) so PR-6 degraded saves at 0.4 can
                                    # rise toward full quality after consistent
                                    # replay success. Behind ENABLE_PROMOTE_ON_HINT.
                                    try:
                                        from ma_poc.config.feature_flags import enable_promote_on_hint
                                        from ma_poc.services.profile_updater import (
                                            DOM_HINT_QUALITY_MAX as _Q_MAX,
                                        )
                                        from ma_poc.services.profile_updater import (
                                            DOM_HINT_QUALITY_PROMOTE_STEP as _Q_STEP,
                                        )
                                        if enable_promote_on_hint() and hasattr(mapping, "quality_score"):
                                            # round(.., 2) avoids float drift (see _Q_STEP comment)
                                            mapping.quality_score = min(
                                                _Q_MAX,
                                                round(float(mapping.quality_score) + _Q_STEP, 2),
                                            )
                                    except Exception:
                                        pass
                                    break
                                else:
                                    # Empty replay — count failure
                                    if hasattr(mapping, "consecutive_replay_failures"):
                                        try:
                                            mapping.consecutive_replay_failures += 1
                                        except Exception:
                                            pass
                                    try:
                                        from ma_poc.observability.events import EventKind, emit
                                        emit(
                                            EventKind.MAPPING_REPLAY_EMPTY,
                                            getattr(ctx, "property_id", "unknown"),
                                            url=str(resp.get("url", ""))[:80],
                                        )
                                    except Exception:
                                        pass
            if replayed_units:
                # Sub-tier 0b: apply saved FieldPatches positionally to augment replay units.
                try:
                    _fp_list = list(getattr(profile.api_hints, "field_patches", []) or [])
                    if _fp_list:
                        replayed_units = _apply_field_patches(replayed_units, api_responses, _fp_list)
                except Exception:
                    pass

                # B1: compute aggregate quality across all mappings that contributed.
                agg_q = _aggregate_quality(replayed_mappings)

                # Phase 5: short-circuit when aggregate mapping quality is sufficient
                # (≥0.7). Low-quality mappings must not short-circuit — let the
                # cascade run so the merger can prefer a higher-quality source.
                if agg_q >= 0.7:
                    _log_attempt(
                        "generic:profile_replay",
                        "ran_units",
                        units=len(replayed_units),
                        reason="replayed saved LlmFieldMapping (quality ok)",
                        duration_ms=int((_time.monotonic() - t0) * 1000),
                    )
                    # PROFILE_REPLAY_HIT — the self-learning loop produced
                    # a unit set without paying any LLM cost. This is THE
                    # avoidance signal we monitor: aggregating these per
                    # run divided by total successful properties is the
                    # "LLM tax avoidance rate". A drop in this rate means
                    # the loop has regressed (mappings stopped persisting,
                    # the data layer wiped them, etc.).
                    try:
                        from ma_poc.observability.events import EventKind, emit
                        emit(
                            EventKind.PROFILE_REPLAY_HIT,
                            ctx.property_id or "unknown",
                            units=len(replayed_units),
                            quality=round(float(agg_q), 3),
                            mappings_used=len(replayed_mappings),
                        )
                    except Exception:
                        pass
                    result.units = replayed_units
                    result.tier_used = "TIER_1_PROFILE_MAPPING"
                    result.winning_url = (
                        result.api_responses[0].get("url") if result.api_responses else ctx.base_url
                    )
                    result.confidence = min(0.90, 0.7 + 0.03 * len(replayed_units)) * agg_q
                    return result
                # Field-incomplete or low quality: stash for post-merge and continue cascade.
                _log_attempt(
                    "generic:profile_replay",
                    "ran_units",
                    units=len(replayed_units),
                    reason="replayed mapping field-incomplete or low quality; cascading + merging",
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                result._phase5_replay_units = list(replayed_units)  # type: ignore[attr-defined]
                result._phase5_replay_quality = agg_q  # type: ignore[attr-defined]
                # Keep api_responses populated so downstream sub-tiers see them
                # too — they may produce additional fields the merger fills in.
            _log_attempt(
                "generic:profile_replay",
                "skipped" if not saved else "ran_empty",
                reason=("no saved mappings" if not saved else "saved mappings didn't match any captured API"),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            # PROFILE_REPLAY_MISS_WITH_SAVED — the property had saved
            # mappings but none of them matched a captured API on this
            # run. Distinct from "skipped" (no saved at all): a miss
            # despite having saved mappings is the early-warning sign
            # for environment/URL drift between runs. If this fires for
            # the same property 3+ days running, the eviction path will
            # clear the entry, but until then the property pays LLM tax.
            if saved and not replayed_units:
                try:
                    from ma_poc.observability.events import EventKind, emit
                    emit(
                        EventKind.PROFILE_REPLAY_MISS_WITH_SAVED,
                        ctx.property_id or "unknown",
                        saved_count=len(saved),
                        captured_apis=len(api_responses),
                    )
                except Exception:
                    pass

        # Sub-tier 1: narrow generic API parser -----------------------------
        # Phase 4: _api_signal_qualifies() is the single gate — SourceQualifier
        # when the signal engine is available, has_unit_signals() otherwise.
        # This replaces the Phase 1 dual-run and removes the lazy import.
        t0 = _time.monotonic()
        for resp in api_responses:
            body = resp.get("body")
            items = _find_unit_list(body)
            url = resp.get("url", "")
            if not _api_signal_qualifies(resp, items):
                continue
            units = parse_generic_api(items, url)
            if units:
                all_units.extend(units)
                result.api_responses.append(resp)
        _narrow_ms = int((_time.monotonic() - t0) * 1000)
        if api_responses:
            _log_attempt(
                "generic:api_narrow",
                "ran_units" if all_units else "ran_empty",
                units=len(all_units),
                reason="" if all_units else "no items matched unit-signal heuristic",
                duration_ms=_narrow_ms,
            )
        else:
            _log_attempt("generic:api_narrow", "skipped", reason="no captured API responses", duration_ms=0)

        # Sub-tier 2: broad parser + host-specific (SightMap/RealPage) -----
        if not all_units and api_responses:
            t0 = _time.monotonic()
            for resp in api_responses:
                url = resp.get("url") or ""
                body = resp.get("body")
                host_units: list[dict[str, str]] = []
                if body is not None and "sightmap.com" in url.lower():
                    try:
                        host_units = _dr_parse_sightmap(body, url) or []
                    except Exception as exc:  # defensive — never break the run
                        result.errors.append(f"sightmap-parse-error: {exc}")
                # Patch #11 — G5 Marketing Cloud GraphQL. Parse a captured
                # inventory.g5marketingcloud.com/graphql body even when
                # detection didn't route to G5Adapter (defense in depth).
                elif body is not None and _is_g5_graphql_url(url) and _is_g5_graphql_body(body):
                    try:
                        host_units = _parse_g5_response(body, url) or []
                    except Exception as exc:  # defensive — never break the run
                        result.errors.append(f"g5-graphql-parse-error: {exc}")
                if host_units:
                    all_units.extend(host_units)
                    result.api_responses.append(resp)
            if not all_units:
                try:
                    broad = _dr_parse_api_responses(list(api_responses)) or []
                except Exception as exc:
                    broad = []
                    result.errors.append(f"daily-runner-parser-error: {exc}")
                if broad:
                    # Pre-filter: reject rows with no physical dimension
                    # (beds/baths/sqft). CMS config objects and API noise rows
                    # typically have only string fields. Emitting them causes
                    # the planner to see units_found=N with pct=0.00 and
                    # escalate to expensive LLM hop chains.
                    from ma_poc.pms.signal_engine.unit_filter import filter_by_physical_dimension as _fpd
                    broad = _fpd(broad)
                if broad:
                    all_units.extend(broad)
                    # parse_api_responses tags each unit with source_api_url;
                    # surface the first as winning_url if we don't have one.
                    if not result.api_responses:
                        first_url = next(
                            (u.get("source_api_url") for u in broad if u.get("source_api_url")),
                            None,
                        )
                        if first_url:
                            for resp in api_responses:
                                if resp.get("url") == first_url:
                                    result.api_responses.append(resp)
                                    break
            _log_attempt(
                "generic:api_broad",
                "ran_units" if all_units else "ran_empty",
                units=len(all_units),
                reason="" if all_units else "broad parser + host-specific found no units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if all_units:
            # Phase H: consult the planner before short-circuiting.
            # If planner says STOP, return early. Otherwise fall through so
            # LLM sub-tiers can fill completeness gaps — but only if budget allows.
            from ma_poc.models.source import SourceId as _SI
            sources_already_run.add(_SI.API_GENERIC_NARROW)
            _decision = _assess_and_decide(all_units, sources_already_run, ctx, decision_log)
            if _decision is None or _decision.action == "STOP":
                result.units = all_units
                result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
                result.confidence = min(0.85, 0.6 + 0.05 * len(all_units))
                result._decision_log = decision_log  # type: ignore[attr-defined]
                return result
            # Planner says escalate — promote all_units into result so HTML tiers
            # can merge into them rather than replacing them (Fix 2 / C2).
            result.units = list(all_units)
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else None
            skip_llm = False  # override: planner explicitly requested escalation

        # ── HTML-based tiers ──────────────────────────────────────────────
        # If neither narrow nor broad API parsers produced units, fall through
        # to the HTML extractors. These run on the raw page HTML (either from
        # a live Playwright page or from fetch_result.body) and cover the SSR
        # / static-site cases where no XHR fires during load.
        html = await _get_page_html(page, ctx)
        if html:
            # ── Sub-tier 2.5 (2026-05-21): AIR Communities AEM adapter ──
            # AIR runs the entire 76-community / 27K-unit portfolio on a
            # single Adobe Experience Manager stack with a deterministic
            # /residences.html → /floor-plan/{bed}/{slug}.html URL family.
            # We detect via the ``apartmentIncomeReit/clientlibs`` marker
            # and fan out to the right parser based on which page in the
            # family we received. Validated against 5 AIR properties
            # (laurelcrossing, adara, arcadia, 20thstreetstation,
            # 21fitzsimons, 15fifty5).
            if _detect_air(html):
                t0 = _time.monotonic()
                cur_url = (ctx.base_url or "").lower()
                air_units: list[dict[str, Any]] = []
                if "/floor-plan/" in cur_url:
                    # Per-plan page → unit-level records. Derive bedrooms
                    # + plan-slug from URL since we may not have parent
                    # residences.html context.
                    plan_ctx = _air_url_ctx(ctx.base_url)
                    try:
                        air_units = _air_parse_per_plan(
                            html, plan_context=plan_ctx, base_url=ctx.base_url
                        )
                    except Exception as _ae:
                        result.errors.append(f"air-per-plan-error: {_ae}")
                else:
                    # Default: residences.html → plan-level records +
                    # surface per-plan deep-links as floor-plan subpage
                    # hints for the orchestrator to follow.
                    try:
                        air_units = _air_parse_residences(html, base_url=ctx.base_url)
                    except Exception as _ae:
                        result.errors.append(f"air-residences-error: {_ae}")
                    if air_units:
                        # Emit details_urls as subpage hints so link-hop
                        # fetches each per-plan page on the next pass.
                        subpages = [
                            (u["details_url"], "air_per_plan")
                            for u in air_units
                            if u.get("details_url")
                        ]
                        if subpages:
                            existing_sp = (
                                getattr(result, "_embedded_floorplan_subpage_hints", None)
                                or []
                            )
                            result._embedded_floorplan_subpage_hints = (  # type: ignore[attr-defined]
                                existing_sp + subpages
                            )
                _log_attempt(
                    "generic:air_communities",
                    "ran_units" if air_units else "ran_empty",
                    units=len(air_units),
                    reason="" if air_units else (
                        "AIR marker present but no plan/unit signal extracted"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if air_units:
                    result.units = _merge_into_result_units(
                        result.units, air_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_3_DOM"
                    result.winning_url = ctx.base_url
                    result.confidence = min(0.85, 0.6 + 0.04 * len(result.units))
                    from ma_poc.models.source import SourceId as _SI_AIR
                    sources_already_run.add(_SI_AIR.DOM_CASCADE)
                    _aird = _assess_and_decide(
                        result.units, sources_already_run, ctx, decision_log
                    )
                    if _aird is None or _aird.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False

            # ── Sub-tier 2.6 (2026-05-21): Funnel Leasing (formerly Nestio) ─
            # Funnel powers Essex Property Trust (~247 properties),
            # plus parts of Cortland / UDR / RedPeak / Monument / Avanti /
            # Dermot portfolios. Server-side proxy at
            # ``{site}.com/api/properties/{prop_id}/availability``
            # returns clean ``result.floorplans[].units[]`` JSON.
            # Vercel-fronted endpoint passes via curl_cffi chrome120
            # impersonation. Validated 2026-05-21 against 3 distinct
            # Essex properties (Belcarra, Connolly Station, Allure at
            # Scripps Ranch). See project_nestio_funnel_discovery memo.
            if not result.units and _detect_funnel(html):
                t0 = _time.monotonic()
                funnel_units: list[dict[str, Any]] = []
                pid = _funnel_property_id(html)
                if pid:
                    api_url = _funnel_api_url(ctx.base_url, pid)
                    try:
                        from ma_poc.pms.adapters._probe import probe_get
                        resp = probe_get(api_url, headers={
                            "Accept": "application/json",
                            "Referer": ctx.base_url,
                        })
                        if resp is not None and getattr(resp, "status_code", 0) == 200:
                            import json as _json
                            try:
                                data = _json.loads(resp.text)
                                funnel_units = _parse_funnel(data, source_url=api_url)
                            except Exception as _fpe:
                                result.errors.append(f"funnel-parse-error: {_fpe}")
                        else:
                            _status = getattr(resp, "status_code", "?") if resp else "?"
                            result.errors.append(
                                f"funnel-api-error: status={_status}"
                            )
                    except Exception as _fe:
                        result.errors.append(
                            f"funnel-api-error: {type(_fe).__name__}: {str(_fe)[:120]}"
                        )
                _log_attempt(
                    "generic:funnel",
                    "ran_units" if funnel_units else "ran_empty",
                    units=len(funnel_units),
                    reason="" if funnel_units else (
                        "Funnel detected but property_id missing or API empty"
                        if pid else "Funnel marker but no data-communityid in HTML"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if funnel_units:
                    result.units = _merge_into_result_units(
                        result.units, funnel_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_1_API"
                    result.winning_url = _funnel_api_url(ctx.base_url, pid)
                    result.confidence = min(0.9, 0.65 + 0.04 * len(result.units))
                    from ma_poc.models.source import SourceId as _SI_FN
                    sources_already_run.add(_SI_FN.API_GENERIC_NARROW)
                    _fnd = _assess_and_decide(
                        result.units, sources_already_run, ctx, decision_log
                    )
                    if _fnd is None or _fnd.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False

            # ── Sub-tier 2.75 (2026-05-23): apts247 / Vergence Multifamily.
            # Yardi's small-property tenant exposes a public REST endpoint
            # at ``{host}/api/v1/floorplans/?api_key=<HEX40>`` that returns
            # full unit-level data (rent, sqft, available_date, unit_number)
            # for every floor plan. The api_key is published in plain JS as
            # ``api_key = "<HEX40>"``. Validated 2026-05-23 on
            # foxrundothan.com (Vergence portfolio) — 3 plans × 3 units, all
            # carrying rent + sqft + availability date.
            if not result.units and _detect_apts247(html):
                t0 = _time.monotonic()
                apts247_units: list[dict[str, Any]] = []
                api_key = _apts247_api_key(html)
                api_url = (
                    _apts247_build_url(ctx.base_url, api_key) if api_key else None
                )
                if api_url:
                    try:
                        from ma_poc.pms.adapters._probe import probe_get
                        resp = probe_get(api_url, headers={
                            "Accept": "application/json",
                            "Referer": ctx.base_url,
                        })
                        if resp is not None and getattr(resp, "status_code", 0) == 200:
                            import json as _json
                            try:
                                data = _json.loads(resp.text)
                                apts247_units = _parse_apts247(
                                    data, source_url=api_url
                                )
                            except Exception as _ape:
                                result.errors.append(
                                    f"apts247-parse-error: {_ape}"
                                )
                        else:
                            _status = (
                                getattr(resp, "status_code", "?") if resp else "?"
                            )
                            result.errors.append(
                                f"apts247-api-error: status={_status}"
                            )
                    except Exception as _ae:
                        result.errors.append(
                            f"apts247-api-error: {type(_ae).__name__}: "
                            f"{str(_ae)[:120]}"
                        )
                _log_attempt(
                    "generic:apts247",
                    "ran_units" if apts247_units else "ran_empty",
                    units=len(apts247_units),
                    reason="" if apts247_units else (
                        "apts247 detected but api_key extract / API fetch failed"
                        if api_url else "apts247 marker but no api_key in HTML"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if apts247_units:
                    result.units = _merge_into_result_units(
                        result.units, apts247_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_1_API_APTS247"
                    result.winning_url = api_url
                    result.confidence = min(
                        0.92, 0.7 + 0.03 * len(result.units)
                    )
                    from ma_poc.models.source import SourceId as _SI_A247
                    sources_already_run.add(_SI_A247.API_GENERIC_NARROW)
                    _a247d = _assess_and_decide(
                        result.units, sources_already_run, ctx, decision_log
                    )
                    if _a247d is None or _a247d.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False

            # ── Sub-tier 2.7 (2026-05-21): Nestio contact-widget rendered DOM
            # Some Funnel/Nestio customers (Dermot Company; some non-Essex
            # customers) embed the contact widget
            # (``integrations.nestio.com/contact-widget/v1/integration.js``)
            # which client-side renders unit detail into a fixed DOM
            # template — distinct from the Essex-style server-side proxy
            # the Funnel sub-tier above targets. The widget's direct API
            # call (``nestiolistings.com/api/v2/...``) is 503-prone, but
            # the rendered DOM is deterministic. ``_get_page_html`` already
            # prefers Playwright-rendered content over the L1 shell when
            # available, so the parser fires only when the rendered DOM
            # is actually present (detector requires ≥2 ``apt-*`` IDs).
            # One unit per page (the URL is per-apartment), so this
            # contributes a single record per scrape.
            if not result.units and _detect_nestio_widget_rendered(html):
                t0 = _time.monotonic()
                try:
                    widget_units = _parse_nestio_widget(html, source_url=ctx.base_url)
                except Exception as _nwe:
                    widget_units = []
                    result.errors.append(f"nestio-widget-error: {_nwe}")
                _log_attempt(
                    "generic:nestio_widget",
                    "ran_units" if widget_units else "ran_empty",
                    units=len(widget_units),
                    reason="" if widget_units else (
                        "Nestio widget rendered but no unit fields extracted"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if widget_units:
                    result.units = _merge_into_result_units(
                        result.units, widget_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_3_DOM"
                    result.winning_url = ctx.base_url
                    result.confidence = min(0.85, 0.6 + 0.05 * len(result.units))
                    from ma_poc.models.source import SourceId as _SI_NW
                    sources_already_run.add(_SI_NW.DOM_CASCADE)
                    _nwd = _assess_and_decide(
                        result.units, sources_already_run, ctx, decision_log
                    )
                    if _nwd is None or _nwd.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False

            # Sub-tier 3: JSON-LD
            t0 = _time.monotonic()
            jsonld_units = extract_jsonld_from_html(html, ctx.base_url)
            # Phase 5: reject JSON-LD success when the extraction is
            # plan-name-only with no Offer prices. That shape was gating
            # the LLM sub-tiers from running on properties that ship only
            # floor-plan labels. Extended: floor_plan_name + beds/baths is
            # a valid partial record — the rent may load dynamically from
            # an iframe/portal (e.g. AMLI ProspectPortal).
            if jsonld_units:
                _gate_outcome = _jsonld_gate_decision(jsonld_units, html or "")
                if _gate_outcome != "accept":
                    _log_attempt(
                        "generic:jsonld",
                        "ran_empty",
                        units=len(jsonld_units),
                        reason=_gate_outcome,
                        duration_ms=int((_time.monotonic() - t0) * 1000),
                    )
                    jsonld_units = []
            if jsonld_units:
                _log_attempt(
                    "generic:jsonld",
                    "ran_units",
                    units=len(jsonld_units),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
            elif "generic:jsonld" not in {a["tier_key"] for a in attempts}:
                _log_attempt(
                    "generic:jsonld",
                    "ran_empty",
                    reason="no Apartment/Offer schema in HTML",
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
            if jsonld_units:
                result.units = _merge_into_result_units(
                    result.units, jsonld_units, property_id=ctx.property_id
                )
                result.tier_used = "TIER_2_JSONLD"
                result.winning_url = ctx.base_url
                result.confidence = min(0.80, 0.55 + 0.05 * len(result.units))
                # Fix 13: planner gate — stop only if complete, else fall through to LLM
                from ma_poc.models.source import SourceId as _SI2
                sources_already_run.add(_SI2.JSON_LD)
                _jd = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                if _jd is None or _jd.action == "STOP":
                    result._decision_log = decision_log  # type: ignore[attr-defined]
                    return result
                skip_llm = False  # planner escalated — allow LLM

            # Sub-tier 4: Embedded JSON / SSR blobs -------------------------
            t0 = _time.monotonic()
            embedded = extract_embedded_blobs_from_html(html)
            embedded_units: list[dict[str, Any]] = []
            amli_units: list[dict[str, Any]] = []
            if embedded:
                # ── Sub-tier 4a (Phase 6 / AMLI, 2026-05-21): tRPC-aware
                # extraction. AMLI Residential (76+ Next.js properties)
                # embeds its full unit list inside __NEXT_DATA__ but
                # behind a tRPC envelope the generic parser only
                # partially walks. Detect first to avoid touching
                # non-AMLI Next.js blobs. When we have an AMLI match,
                # PREFER its units over the generic parser's output
                # (generic recovers ~17%; AMLI recovers 100%).
                for blob in embedded:
                    if _detect_amli(blob.get("body")):
                        try:
                            amli_units.extend(
                                _parse_amli(blob["body"], source_url=ctx.base_url)
                            )
                        except Exception as _ax:
                            result.errors.append(f"amli-parse-error: {_ax}")
                if amli_units:
                    embedded_units = amli_units
                else:
                    try:
                        embedded_units = _dr_parse_api_responses(embedded) or []
                    except Exception as exc:
                        embedded_units = []
                        result.errors.append(f"embedded-parse-error: {exc}")

            # Sub-tier 4b (2026-05-23): Mark-Taylor PRELOADED_STATE.
            # mark-taylor.com properties (~10 in registry, ~50 total in the
            # operator portfolio) embed their floor_plan_meta in a plain
            # window.PRELOADED_STATE assignment that the generic
            # embedded-JSON walker doesn't pick up (it scans for
            # __NEXT_DATA__ / __NUXT__ / type="application/json" script
            # tags only). Detect by host + global marker and emit one
            # plan-level row per bedroom count when found. Honors the
            # success bar (≥1 unit with rent+sqft) for the cohort
            # previously mis-flagged as operator-data-gap.
            if not embedded_units and _detect_mt(html, ctx.base_url):
                try:
                    mt_rows = _parse_mt(html, source_url=ctx.base_url)
                except Exception as _mtx:
                    mt_rows = []
                    result.errors.append(f"mark-taylor-parse-error: {_mtx}")
                if mt_rows:
                    embedded_units = mt_rows
                else:
                    # Homepage didn't carry PRELOADED_STATE — surface the
                    # /floor-plans/ subpage as a deterministic link-hop
                    # hint so the orchestrator re-probes there.
                    fp_url = _mt_derive_fp_url(ctx.base_url)
                    if fp_url:
                        existing_hints = (
                            getattr(result, "_subpage_hints", None) or []
                        )
                        if fp_url not in existing_hints:
                            result._subpage_hints = existing_hints + [fp_url]  # type: ignore[attr-defined]

            # Sub-tier 4c (2026-05-23): JetEngine + RealPage-OLL DOM.
            # Copperpoint-class WordPress sites render unit data inline
            # in jet-listing-dynamic-repeater__item rows, with the
            # RealPage propertyId embedded in
            # data-unit-application-url. The 3-signal detector keeps
            # us off plain JetEngine sites. Each row has rent + sqft +
            # unit_number — no further HTTP fetch needed. Fires before
            # the LLM tiers since we have a confident deterministic
            # parse path.
            if not embedded_units and _detect_jetengine(html, ctx.base_url):
                try:
                    jet_rows = _parse_jetengine(html, source_url=ctx.base_url)
                except Exception as _jx:
                    jet_rows = []
                    result.errors.append(f"jetengine-parse-error: {_jx}")
                if jet_rows:
                    embedded_units = jet_rows

            # Sub-tier 4d (2026-05-23): Wix HtmlComponent iframe →
            # AppFolio tenant resolver. Many Wix-built operator sites
            # (millenniumnw, liveallureva, etc.) embed an AppFolio
            # listing widget inside a Wix HtmlComponent iframe whose
            # src points at *.filesusr.com/html/<hex>.html. The vanity
            # HTML alone never mentions appfolio.com — only the
            # filesusr.com body does. We fetch each iframe, look for
            # Appfolio.Listing({hostUrl: 'TENANT.appfolio.com'}), and
            # surface https://{TENANT}.appfolio.com/listings as a
            # portal hint. The orchestrator's link-hop then probes
            # that URL and the existing _appfolio_embed parser handles
            # the SSR listing-id grid.
            if not embedded_units:
                try:
                    from ma_poc.pms.adapters._wix_iframe_walker import (
                        build_appfolio_listings_url as _wix_af_url,
                    )
                    from ma_poc.pms.adapters._wix_iframe_walker import (
                        detect_wix_html_iframes as _wix_iframes,
                    )
                    from ma_poc.pms.adapters._wix_iframe_walker import (
                        extract_appfolio_tenant as _wix_af_tenant,
                    )
                    wix_iframes = _wix_iframes(html)
                except Exception:
                    wix_iframes = []
                if wix_iframes:
                    from ma_poc.pms.adapters._probe import probe_get
                    af_hints: list[str] = []
                    for iframe_url in wix_iframes[:5]:  # cap at 5
                        try:
                            iframe_resp = probe_get(iframe_url, headers={
                                "Accept": "text/html,*/*",
                                "Referer": ctx.base_url,
                            })
                        except Exception:
                            iframe_resp = None
                        if iframe_resp is None or getattr(
                            iframe_resp, "status_code", 0
                        ) != 200:
                            continue
                        tenant = _wix_af_tenant(iframe_resp.text)
                        if not tenant:
                            continue
                        listings_url = _wix_af_url(tenant)
                        if listings_url and listings_url not in af_hints:
                            af_hints.append(listings_url)
                    if af_hints:
                        existing = (
                            getattr(result, "_subpage_hints", None) or []
                        )
                        for u in af_hints:
                            if u not in existing:
                                existing = existing + [u]
                        result._subpage_hints = existing  # type: ignore[attr-defined]
                        _log_attempt(
                            "generic:wix_iframe_appfolio",
                            "ran_hint",
                            units=0,
                            reason=(
                                f"resolved {len(af_hints)} AppFolio tenant(s) "
                                f"from Wix iframe walk: "
                                + ", ".join(af_hints[:3])
                            ),
                        )

            _log_attempt(
                "generic:embedded_json",
                "ran_units" if embedded_units else ("ran_empty" if embedded else "skipped"),
                units=len(embedded_units),
                reason=""
                if embedded_units
                else (
                    f"{len(embedded)} SSR blob(s) had no unit signals"
                    if embedded
                    else "no __NEXT_DATA__/__NUXT__/window globals in HTML"
                ),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

            # Leasing-portal detection. Even when SSR blobs have no unit
            # signals (Jonah Digital widget config, headless WordPress
            # marketing shells, custom React shells), they often point at
            # a third-party portal (SightMap, RealPage OLL, RentCafe,
            # FunnelLeasing, AppFolio) where the actual units live. We
            # surface those URLs as hints for the orchestrator's link-hop
            # to fetch — the URL host then triggers the matching PMS
            # fingerprint and the right adapter takes over.
            if embedded:
                from ma_poc.pms.adapters._html_extract import (
                    detect_embedded_portal_urls,
                )
                portal_hints = detect_embedded_portal_urls(embedded)
                if portal_hints:
                    existing = getattr(result, "_embedded_portal_hints", None) or []
                    result._embedded_portal_hints = existing + portal_hints  # type: ignore[attr-defined]
                    _log_attempt(
                        "generic:embedded_portal_detected",
                        "ran_hint",
                        units=0,
                        reason=(
                            f"detected {len(portal_hints)} leasing-portal URL(s) in "
                            f"embedded JSON: "
                            + ", ".join(f"{p}@{u[:60]}" for u, p in portal_hints[:3])
                        ),
                    )

                # Floor-plan sub-page detection: Jonah-style index pages
                # (renderable_endpoint + base_uri in embedded JSON) contain
                # per-plan sub-pages with the FULL unit list in their own
                # embedded JSON blobs. Surface those URLs so link-hop
                # fetches them and extracts units from the static HTML.
                from ma_poc.pms.adapters._html_extract import (
                    detect_floorplan_subpage_urls,
                )
                subpage_hints = detect_floorplan_subpage_urls(
                    embedded, html, ctx.base_url
                )
                if subpage_hints:
                    existing_sp = (
                        getattr(result, "_embedded_floorplan_subpage_hints", None) or []
                    )
                    result._embedded_floorplan_subpage_hints = (  # type: ignore[attr-defined]
                        existing_sp + subpage_hints
                    )
                    _log_attempt(
                        "generic:floorplan_subpages_detected",
                        "ran_hint",
                        units=0,
                        reason=(
                            f"detected {len(subpage_hints)} floor-plan sub-page(s) "
                            f"from embedded JSON config: "
                            + ", ".join(u[:50] for u, _ in subpage_hints[:3])
                        ),
                    )
            # Shape-B SecureCafe detection — runs on raw HTML, not JSON blobs.
            # Sites that route /onlineleasing/{slug} client-side and JS-redirect
            # to {subdomain}.securecafe.com never expose the final URL until the
            # React bundle fires.  We synthesise it from the href slug + domain.
            if html and not result.units:
                try:
                    from ma_poc.pms.adapters._html_extract import (
                        detect_securecafe_portal_url,
                    )
                    # Use final_url (post-redirect) when the server followed a
                    # cross-HOST redirect (e.g. affinity56.com → elevation56.com).
                    # Slug derivation from the original base_url would produce the
                    # wrong securecafe subdomain in those cases. Same-host redirects
                    # (path changes only) keep using base_url — the host is the same
                    # so the slug is unchanged.
                    _fr = ctx.fetch_result
                    _final_url = getattr(_fr, "final_url", None) or ctx.base_url
                    try:
                        import urllib.parse as _up
                        _base_host = (_up.urlparse(ctx.base_url).hostname or "").lower()
                        _final_host = (_up.urlparse(_final_url).hostname or "").lower()
                        _sc_base = _final_url if _final_host and _final_host != _base_host else ctx.base_url
                    except Exception:
                        _sc_base = ctx.base_url
                    _sc_url = detect_securecafe_portal_url(html, _sc_base)
                    if _sc_url:
                        # Generic session-level guard: if this URL already returned
                        # a network error (BOT_BLOCKED, HARD_FAIL, TRANSIENT) on a
                        # prior hop in this same scrape, don't re-queue it — doing
                        # so wastes 4–8s per hop page on a guaranteed CF_CHALLENGE.
                        # _session_blocked_urls lives on ctx.budget so it survives
                        # across the full extraction pass without extra plumbing.
                        _ctx_budget = getattr(ctx, "budget", None) or {}
                        _session_blocked: set[str] = _ctx_budget.get("_session_blocked_urls", set())
                        if _sc_url not in _session_blocked:
                            existing_ep = getattr(result, "_embedded_portal_hints", None) or []
                            result._embedded_portal_hints = existing_ep + [(_sc_url, "securecafe")]  # type: ignore[attr-defined]
                            _log_attempt(
                                "generic:securecafe_portal_detected",
                                "ran_hint",
                                units=0,
                                reason=f"synthesised securecafe URL from /onlineleasing/ href: {_sc_url[:70]}",
                            )
                        else:
                            _log_attempt(
                                "generic:securecafe_portal_detected",
                                "skipped",
                                units=0,
                                reason=f"securecafe URL already blocked in session: {_sc_url[:70]}",
                            )
                except Exception:
                    pass

            if embedded_units:
                result.units = _merge_into_result_units(
                    result.units, embedded_units, property_id=ctx.property_id
                )
                result.tier_used = "TIER_1_5_EMBEDDED"
                result.winning_url = ctx.base_url
                result.confidence = min(0.80, 0.55 + 0.05 * len(result.units))
                # Fix 13: planner gate — stop only if complete, else fall through to LLM
                from ma_poc.models.source import SourceId as _SI3
                sources_already_run.add(_SI3.EMBEDDED_JSON)
                _ed = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                # 2026-05-23: hard guard — never STOP at EMBEDDED tier
                # if the units lack a rent+sqft pair. The planner can
                # mis-classify "complete" when only one of the two
                # fields is present. Forcing fall-through here lets
                # plan-text / subpage / DOM tiers backfill the missing
                # side. 43 canary properties currently stamp
                # TIER_1_5_EMBEDDED with rent-only or sqft-only output.
                has_pair = _has_rent_sqft_pair(result.units)
                if has_pair and (_ed is None or _ed.action == "STOP"):
                    result._decision_log = decision_log  # type: ignore[attr-defined]
                    return result
                skip_llm = False  # planner escalated OR no rent+sqft pair

            # Sub-tier 4b: RealPage CWS credential probe ─────────────────────
            # RealPage LeaseStar CWS sites serve a JavaScript shell whose
            # static HTML contains an RPFP_config object with:
            #   propertyId — the numeric property identifier
            #   apiKey     — a UUID used as x-ws-authkey on the units API
            # The widget makes: GET api.ws.realpage.com/v2/property/{id}/units
            # Without x-ws-authkey the endpoint 401s; with it the full unit
            # list (rent, sqft, availability) is returned directly.
            # Extracting credentials from HTML and re-firing the call gives
            # deterministic results without waiting for the JS bundle to load.
            if not result.units and html and "rpfp_config" in html.lower():
                try:
                    _cws_units = await _probe_realpage_cws(html)
                    if _cws_units:
                        result.units = _merge_into_result_units(
                            result.units, _cws_units, property_id=ctx.property_id
                        )
                        result.tier_used = "TIER_1_API"
                        result.winning_url = ctx.base_url
                        result.confidence = min(0.90, 0.65 + 0.04 * len(result.units))
                        _log_attempt(
                            "generic:realpage_cws",
                            "ran_units",
                            units=len(_cws_units),
                            reason="RPFP_config credentials extracted from HTML",
                        )
                        from ma_poc.models.source import SourceId as _SI4b
                        sources_already_run.add(_SI4b.API_GENERIC_NARROW)
                        _cws_d = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                        if _cws_d is None or _cws_d.action == "STOP":
                            result._decision_log = decision_log  # type: ignore[attr-defined]
                            return result
                    else:
                        _log_attempt(
                            "generic:realpage_cws",
                            "ran_empty",
                            units=0,
                            reason="RPFP_config found but API returned no units",
                        )
                except Exception as _cws_exc:
                    _log_attempt(
                        "generic:realpage_cws",
                        "ran_empty",
                        units=0,
                        reason=f"probe error: {_cws_exc}",
                    )

            # Sub-tier 4.7 (Phase 6.2, 2026-05-21): HTML floor-plan tables --
            # Some properties ship unit data as a plain ``<table>`` with a
            # header row of column labels. The DOM cascade below misses
            # these because table rows don't carry ``.unit`` /
            # ``.floor-plan`` class containers — the column semantics live
            # in the ``<th>`` row. Run this between embedded-JSON and
            # DOM-scan so tables that match merge in before the DOM
            # scanner's looser heuristics fire.
            if not result.units:
                t0 = _time.monotonic()
                try:
                    table_units = extract_units_from_html_tables(html, ctx.base_url)
                except Exception as _tbl_exc:
                    table_units = []
                    result.errors.append(f"table-scan-error: {_tbl_exc}")
                _log_attempt(
                    "generic:html_tables",
                    "ran_units" if table_units else "ran_empty",
                    units=len(table_units),
                    reason="" if table_units else (
                        "no <table> with floor-plan headers + ≥2 qualifying rows"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if table_units:
                    result.units = _merge_into_result_units(
                        result.units, table_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_3_DOM"
                    result.winning_url = ctx.base_url
                    result.confidence = min(0.75, 0.5 + 0.04 * len(result.units))
                    from ma_poc.models.source import SourceId as _SI_T
                    sources_already_run.add(_SI_T.DOM_CASCADE)
                    _td = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                    if _td is None or _td.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False  # planner escalated — allow LLM

            # Sub-tier 4.8 (Phase 6.3, 2026-05-21): data-* attribute cards -
            # Some properties render floor-plan cards where the numeric
            # data is hidden behind ``data-*`` attributes (the JS hydrates
            # the visible text after page-load). Reads attributes directly,
            # so the DOM cascade's text-scrape doesn't miss them.
            if not result.units:
                t0 = _time.monotonic()
                try:
                    da_units = extract_units_from_data_attr_cards(html, ctx.base_url)
                except Exception as _da_exc:
                    da_units = []
                    result.errors.append(f"data-attr-scan-error: {_da_exc}")
                _log_attempt(
                    "generic:html_data_attr",
                    "ran_units" if da_units else "ran_empty",
                    units=len(da_units),
                    reason="" if da_units else (
                        "no ≥2 sibling elements with ≥3 unit-vocab data-* attributes"
                    ),
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if da_units:
                    result.units = _merge_into_result_units(
                        result.units, da_units, property_id=ctx.property_id
                    )
                    result.tier_used = "TIER_3_DOM"
                    result.winning_url = ctx.base_url
                    result.confidence = min(0.75, 0.5 + 0.04 * len(result.units))
                    from ma_poc.models.source import SourceId as _SI_D
                    sources_already_run.add(_SI_D.DOM_CASCADE)
                    _ad = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                    if _ad is None or _ad.action == "STOP":
                        result._decision_log = decision_log  # type: ignore[attr-defined]
                        return result
                    skip_llm = False

            # Sub-tier 5: DOM selector cascade ------------------------------
            # Scans container elements (.unit, .floor-plan, .pricing-card, …)
            # for visible rent + structural signals. Catches static HTML sites
            # where unit data lives in the markup, not in any JSON envelope.
            # Phase E: pass saved field_selectors as hints when available.
            t0 = _time.monotonic()
            _profile_dom_field_selectors = None
            if ctx.profile is not None:
                fs = ctx.profile.dom_hints.field_selectors
                if fs and getattr(fs, "container", None):
                    # Skip replay when the saved selectors were flagged at
                    # save time as low-quality (LLM produced selectors that
                    # didn't reproduce their own units). Letting them try
                    # anyway would produce ``consecutive_misses`` events
                    # that gradually evict the entry — but at the cost of
                    # one wasted DOM cascade per run. Faster to bypass them
                    # and let the LLM-DOM tier produce fresh selectors.
                    # Replay admission gate. Mirror of the same constant used
                    # at save time and in the eviction policy — see
                    # services/profile_updater.py constants.
                    from ma_poc.services.profile_updater import (
                        DOM_HINT_QUALITY_MAX,
                        DOM_HINT_QUALITY_REPLAY_FLOOR,
                    )
                    fs_quality = float(
                        getattr(ctx.profile.dom_hints, "field_selectors_quality", DOM_HINT_QUALITY_MAX)
                        or DOM_HINT_QUALITY_MAX
                    )
                    if fs_quality >= DOM_HINT_QUALITY_REPLAY_FLOOR:
                        _profile_dom_field_selectors = fs
            hints_attempted = _profile_dom_field_selectors is not None
            try:
                dom_units, _dom_hit_mode = extract_units_from_dom(html, ctx.base_url, hints=_profile_dom_field_selectors)
            except Exception as exc:
                dom_units, _dom_hit_mode = [], "none"
                result.errors.append(f"dom-scan-error: {exc}")
            hints_hit = _dom_hit_mode == "hints"
            result._dom_hints_attempted = hints_attempted  # type: ignore[attr-defined]
            result._dom_hints_hit = hints_hit  # type: ignore[attr-defined]
            _log_attempt(
                "generic:dom_scan",
                "ran_units" if dom_units else "ran_empty",
                units=len(dom_units),
                reason="" if dom_units else "no DOM container matched rent + structural signals",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            if dom_units:
                result.units = _merge_into_result_units(
                    result.units, dom_units, property_id=ctx.property_id
                )
                result.tier_used = "TIER_3_DOM"
                result.winning_url = ctx.base_url
                result.confidence = min(0.75, 0.5 + 0.04 * len(result.units))
                # Fix 13: planner gate — stop only if complete, else fall through to LLM
                from ma_poc.models.source import SourceId as _SI4
                sources_already_run.add(_SI4.DOM_CASCADE)
                _dd = _assess_and_decide(result.units, sources_already_run, ctx, decision_log)
                # 2026-05-23: hard guard — never STOP at DOM tier if
                # units lack a rent+sqft pair. 67 canary properties
                # currently stamp TIER_3_DOM with rent-only or sqft-
                # only units (operator splits the fields across
                # different page sections). Forcing fall-through lets
                # subpage / embedded-JSON tiers backfill the missing
                # side.
                has_pair = _has_rent_sqft_pair(result.units)
                if has_pair and (_dd is None or _dd.action == "STOP"):
                    result._decision_log = decision_log  # type: ignore[attr-defined]
                    return result
                skip_llm = False  # planner escalated OR no rent+sqft pair
        else:
            _log_attempt("generic:jsonld", "skipped", reason="no HTML body available")
            _log_attempt("generic:embedded_json", "skipped", reason="no HTML body available")
            _log_attempt("generic:dom_scan", "skipped", reason="no HTML body available")

        # Sub-tier 6: LLM extraction --------------------------------------
        # Originally gated ON only for ``pms=unknown``. Option C relaxes
        # that gate: if the detected adapter returned empty BUT the page
        # has enough visible text and rent signals for the LLM to have a
        # shot, we let it run. Rationale: a detected-but-failing adapter
        # means the site shape drifted (or the data lives on a sub-page);
        # the LLM can sometimes recover from the home-page HTML that's
        # right there in front of us.
        #
        # 2026-05 batch-3 relaxation (no deny-list, broader signal match):
        # The previous deny-list excluded rentcafe and the trigger required
        # ``$NNN`` formatted rent signals which many marketing sites don't
        # emit. Investigation of 924 still-failing properties showed ~110
        # entrata-detected sites where Tier-1 captured nothing AND LLM was
        # gated off — so those sites had no fallback. We now relax the gate
        # whenever:
        #   • known-PMS adapter returned empty (skip_llm is True), AND
        #   • page body has >= 3KB of visible text (any reasonable HTML page)
        # Cost is bounded by the per-property LLM budget (3 API + 1 DOM + 1
        # mono = 5 calls max), so worst-case impact is ~$0.05/property.
        _RENT_KEYWORDS = (
            "$",
            "rent",
            "/mo",
            "/month",
            "bedroom",
            "studio",
            "sqft",
            "sq. ft",
            "sq ft",
            "floor plan",
            "floorplan",
            "available",
        )

        if skip_llm and html:
            try:
                _text = _re_strip_script.sub("", html)
                _text = _re_strip_tag.sub(" ", _text)
                _text_lower = _text.lower()
                _text_bytes = len(_text.encode("utf-8", errors="ignore"))
                _rent_hits = len(_re_rent.findall(html))
                _kw_hits = sum(1 for kw in _RENT_KEYWORDS if kw in _text_lower)

                # Two relaxation triggers (any one suffices):
                #   strict — body >= 5KB AND >= 1 dollar-formatted rent signal
                #            (preserves original behavior — known-good cases)
                #   broad  — body >= 2KB AND >= 2 rent-related keywords
                #            (catches marketing sites with non-dollar pricing;
                #             threshold lowered from 3KB to 2KB in batch-3
                #             to catch smaller marketing-template homepages)
                strict_match = _text_bytes >= 5000 and _rent_hits >= 1
                broad_match = _text_bytes >= 2000 and _kw_hits >= 2
                # Fix 4: third trigger — for very small pages (< 2KB stripped
                # text) that look like AppFolio / Wix / SquareSpace marketing
                # shells, allow LLM if there's at least 1 rent keyword. These
                # are typically tiny landing pages that point to a hosted
                # portal; the LLM may extract a "schedule a tour" hint or
                # link the runner can follow. Threshold lowered to 1KB.
                tiny_marketing_match = (
                    _text_bytes >= 1000
                    and _kw_hits >= 1
                    and ctx.detected.pms in ("appfolio", "wix_nopms", "squarespace_nopms", "unknown")
                )

                if strict_match or broad_match or tiny_marketing_match:
                    try:
                        from ma_poc.observability.events import EventKind
                        from ma_poc.observability.events import emit as _gate_emit

                        _gate_emit(
                            EventKind.LLM_GATE_RELAXED,
                            ctx.property_id,
                            detected_pms=ctx.detected.pms,
                            text_bytes=_text_bytes,
                            rent_signals=_rent_hits,
                            keyword_hits=_kw_hits,
                            reason=(
                                "detected_adapter_empty_strict"
                                if strict_match
                                else "detected_adapter_empty_broad_keywords"
                                if broad_match
                                else "detected_adapter_empty_tiny_marketing"
                            ),
                        )
                    except Exception:
                        pass
                    skip_llm = False
            except Exception:
                pass

        if skip_llm:
            _log_attempt(
                "generic:llm", "skipped", reason=f"detected PMS '{ctx.detected.pms}' — LLM gated off"
            )
            result.errors.append(
                f"Generic fallback found no units for detected PMS '{ctx.detected.pms}'; "
                "LLM/Vision skipped for non-unknown PMS"
            )
            result.confidence = 0.0
            return result

        import os as _os

        if _os.getenv("ENABLE_TIER4_LLM", "true").lower() not in ("1", "true", "yes"):
            _log_attempt("generic:llm", "skipped", reason="ENABLE_TIER4_LLM=false")
            result.errors.append("Generic parser found no units in captured API responses")
            result.confidence = 0.0
            return result

        if not html:
            _log_attempt("generic:llm", "skipped", reason="no HTML body to send to LLM")
            result.errors.append("Generic parser found no units; no HTML for LLM")
            result.confidence = 0.0
            return result

        # Change 5: LLM escalation gate. The 04-20 run spent ~$0.94 across 13
        # TIER_1_API properties where the captured body had nothing for the
        # LLM to rescue. Gate *before* we burn any tokens. Gate failure must
        # never crash extraction — wrap in try/except so a buggy gate falls
        # back to the legacy behaviour (run the LLM).
        try:
            from ma_poc.services.llm_gate import should_escalate_to_llm

            tier1_proxy = {"api_responses": api_responses}
            decision = should_escalate_to_llm(
                html=html,
                tier1_result=tier1_proxy,
                tier2_units=None,
                tier3_units=None,
            )
            if not decision.escalate:
                _log_attempt("generic:llm", "skipped", reason=decision.reason)
                # Only overwrite tier_used if no prior tier already set it —
                # JSON-LD / embedded tiers may have succeeded and their tier
                # label must be preserved even when the LLM gate fires.
                if result.tier_used in ("TIER_1_API", "", None):
                    result.tier_used = decision.reason.split(":")[0]
                result.errors.append(decision.reason)
                if not result.units:
                    result.confidence = 0.0
                return result
        except Exception as exc:
            _log_attempt("generic:llm_gate", "errored", reason=str(exc)[:200])

        # Phase 2: shared property context for every LLM call below.
        # property_id is forwarded so the prompt can pull KNOWN FLOOR PLANS
        # for this property from the FloorplanCatalog.
        property_context = {
            "property_id": ctx.property_id or "",
            "property_name": getattr(ctx, "property_name", "") or "",
            "city": getattr(ctx, "city", "") or "",
            "state": getattr(ctx, "state", "") or "",
            "pmc": getattr(ctx, "pmc", "") or "",
            "total_units": ctx.expected_total_units or "",
            "website": ctx.base_url,
        }
        # Bug 3.7 — inject prior LLM observations as context. The
        # ``field_mapping_notes`` written by past runs are short prose
        # describing how the site organises its data (e.g. "rent lives
        # inside .pricing > .price"). Feeding them back as PRIOR
        # OBSERVATIONS lets the next LLM call skip rediscovery and
        # converge faster on the same selectors / paths. Only attached
        # when populated so cold profiles don't see an empty header.
        try:
            prior_notes = (
                getattr(getattr(ctx.profile, "llm_artifacts", None), "field_mapping_notes", None)
                if getattr(ctx, "profile", None) is not None else None
            )
        except Exception:
            prior_notes = None
        if isinstance(prior_notes, str) and prior_notes.strip():
            property_context["prior_observations"] = prior_notes.strip()[:500]

        # Import the targeted LLM helpers; fall through cleanly if unavailable
        # so the adapter degrades gracefully (monolithic call still runs).
        try:
            from ma_poc.services.llm_extractor import (
                analyze_api_with_llm,
                analyze_dom_with_llm,
                extract_with_llm,
                prepare_llm_input,
            )
        except ImportError as exc:
            _log_attempt("generic:llm", "errored", reason=f"llm_extractor import: {exc}")
            result.errors.append(f"llm-import-error: {exc}")
            result.confidence = 0.0
            return result

        # Phase H: use budget from ctx (computed per-property in scraper.py).
        # Falls back to safe defaults when ctx.budget is absent (e.g. old callers).
        # NOTE: this dict is the SAME reference held by scraper.py's
        # _jugnu_budget — link-hop reuses it across the entry page and each
        # sub-page so decrements compose. See scraper.py shared_budget
        # comment for the wedge bug this prevents.
        # F0.1: import the env-driven default lazily so a missing budget
        # dict still picks up PROPERTY_LLM_COST_CAP_USD overrides.
        try:
            from ma_poc.services.source_planner import (
                get_property_llm_cost_cap_usd as _get_cost_cap,
            )
            _env_cost_cap = _get_cost_cap()
        except Exception:
            _env_cost_cap = 1.50
        _budget = getattr(ctx, "budget", None) or {
            "llm_api_calls": 3,
            "llm_dom_calls": 1,
            "llm_monolithic": 1,
            "link_hop": 3,
            "_cost_cap_usd": _env_cost_cap,
        }
        # Per-property cumulative cost gate (Fix #3 + F0.1). Default is
        # ``PROPERTY_LLM_COST_CAP_USD`` (env-overridable, $1.50 baseline).
        # The 2026-05-09 cloud-run regression showed the prior $1.00 cap
        # starved link-hop pages of their LLM rescue path; F0.1 raises the
        # baseline and adds a one-time bonus per link-hop session in
        # ``pms/scraper.py:_refresh_cost_cap_for_hop``. Past the cap, no
        # further LLM calls fire even if call-count budget still allows
        # them. Override per-property via the budget dict's
        # ``_cost_cap_usd`` key — compute_budget already populates it.
        #
        # SOFT CAP: the gate checks _cost_usd_spent BEFORE each call, but
        # the in-flight call's cost is only recorded when it returns. So
        # actual spend can overshoot the cap by up to one call's worth
        # (~$0.05 for monolithic, ~$0.01 for targeted). This is intentional
        # — making it hard would require mid-call cancellation, which the
        # provider SDKs don't expose cleanly. Treat _cost_cap_usd as a
        # ceiling-plus-epsilon, not a hard quota.
        _cost_cap_usd = float(_budget.get("_cost_cap_usd", _env_cost_cap) or _env_cost_cap)

        def _llm_cost_exceeded() -> bool:
            spent = float(_budget.get("_cost_usd_spent", 0.0) or 0.0)
            if spent >= _cost_cap_usd:
                log.warning(
                    "Property %s LLM cost cap reached: $%.4f >= $%.2f — skipping further LLM calls",
                    ctx.property_id, spent, _cost_cap_usd,
                )
                return True
            return False

        def _record_interaction_cost(interaction: dict[str, Any] | None) -> None:
            if not interaction:
                return
            try:
                cost = float(interaction.get("cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return
            if cost > 0:
                _budget["_cost_usd_spent"] = float(_budget.get("_cost_usd_spent", 0.0) or 0.0) + cost

        llm_interactions: list[dict[str, Any]] = getattr(result, "_llm_interactions", []) or []
        # Self-learning payload surfaced to scraper.py. Shape matches what
        # services.profile_updater.update_profile_after_extraction expects:
        #   - dict (with api_url_pattern) => save as LlmFieldMapping
        #   - "noise:<reason>" string     => add to blocked_endpoints
        llm_analysis_results: dict[str, Any] = {}
        llm_field_mappings: list[dict[str, Any]] = []
        llm_css_selectors: dict[str, Any] | None = None
        llm_navigation_hints: list[str] = []

        # Cross-tier hint aggregator. Every LLM sub-tier (api / dom /
        # monolithic / vision) can volunteer ancillary signals
        # (navigation_hint, platform_guess, api_urls_with_data,
        # property_amenities, dom_structure_hash, api_schema_signature).
        # Previously the adapter only surfaced ``css_selectors`` from DOM
        # tier OR the full ``profile_hints`` from monolithic — every
        # other signal was discarded. This dict accumulates everything
        # learned across all tiers in this run; the winner-tier branch
        # merges it into ``result._llm_hints`` so profile_updater sees
        # the complete picture regardless of which tier ultimately won.
        aggregated_hints: dict[str, Any] = {}
        # Property-level amenities collected across tiers — folded into
        # ``aggregated_hints["property_amenities"]`` and surfaced on the
        # AdapterResult so scraper.py's aggregator at line ~1567 finally
        # has data to read.
        aggregated_property_amenities: list[str] = []

        def _merge_hint_extras(src: Any) -> None:
            """Fold ancillary LLM signals from any sub-tier into the aggregator.

            Safe to call with ``None`` or partial dicts — we only copy
            keys we recognise so an unexpected schema can't poison the
            aggregator. Lists (api_urls, property_amenities) are
            extended without duplicates; scalar keys overwrite (later
            tiers refine earlier guesses).
            """
            if not isinstance(src, dict):
                return
            nav = src.get("navigation_hint")
            if isinstance(nav, str) and nav.strip() and nav.strip() not in llm_navigation_hints:
                llm_navigation_hints.append(nav.strip())
                aggregated_hints["navigation_hint"] = nav.strip()
            plat = src.get("platform_guess")
            if isinstance(plat, str) and plat.strip() and plat.strip().lower() not in ("null", "none"):
                aggregated_hints["platform_guess"] = plat.strip()
            api_urls = src.get("api_urls_with_data")
            if isinstance(api_urls, list):
                existing = set(aggregated_hints.get("api_urls_with_data", []) or [])
                for u in api_urls:
                    if isinstance(u, str) and u.strip() and u.strip() not in existing:
                        existing.add(u.strip())
                if existing:
                    aggregated_hints["api_urls_with_data"] = sorted(existing)
            prop_amen = src.get("property_amenities")
            if isinstance(prop_amen, list):
                for a in prop_amen:
                    if isinstance(a, str) and a.strip() and a.strip() not in aggregated_property_amenities:
                        aggregated_property_amenities.append(a.strip())
            for k in ("dom_structure_hash", "api_schema_signature", "extraction_prompt_hash", "field_mapping_notes"):
                v = src.get(k)
                if isinstance(v, str) and v.strip() and not aggregated_hints.get(k):
                    aggregated_hints[k] = v.strip()

        # Sub-tier 6a: targeted API analysis ------------------------------
        # For each captured API response with unit-like signals that the
        # deterministic parsers couldn't unwrap, ask the LLM to both
        # extract units AND return json_paths + response_envelope that we
        # can replay deterministically on the next run (zero LLM cost).
        #
        # Budget is consumed against the shared dict, NOT a local counter,
        # so link-hop sub-pages see decrements made by the entry page.
        # Session-level dedup: track API URLs already analyzed by LLM so
        # successive hop pages don't repeat the same expensive call when the
        # same endpoint appears in multiple page network logs. The set lives in
        # _budget so it is shared across the entry page and all hop sub-pages.
        _analyzed_api_urls: set[str] = _budget.setdefault("_analyzed_api_urls", set())

        targeted_units: list[dict[str, Any]] = []
        if api_responses and int(_budget.get("llm_api_calls", 0)) > 0 and not _llm_cost_exceeded():
            t0 = _time.monotonic()
            api_calls_made = 0
            for resp in api_responses:
                if int(_budget.get("llm_api_calls", 0)) <= 0:
                    break
                if _llm_cost_exceeded():
                    break
                # RC4: skip non-data media types before LLM
                if _is_non_data_response(resp):
                    continue
                body = resp.get("body")
                items = _find_unit_list(body)
                # Only spend budget on responses where something looks like a
                # unit list — avoids feeding analytics payloads to the LLM.
                # _api_signal_qualifies() runs the full SourceQualifier gate
                # (including the RC4 MediaTypeFilter), so JS/CSS/font responses
                # are dropped here too, not just in the api_narrow path.
                if not _api_signal_qualifies(resp, items):
                    continue
                url = resp.get("url", "")
                # Skip if this API URL was already analyzed in this session
                # (same endpoint reappears across multiple hop pages). Prior
                # result is already in llm_analysis_results via shared_budget.
                if url and url in _analyzed_api_urls:
                    continue
                # Mark as analyzed before the await so concurrent hops see it.
                if url:
                    _analyzed_api_urls.add(url)
                # Atomic consume — decrement BEFORE the awaited call so a
                # concurrent recursive scrape() (link-hop) sees the lower
                # remaining budget.
                _budget["llm_api_calls"] = int(_budget.get("llm_api_calls", 0)) - 1
                try:
                    units, api_hints, is_noise, interaction = await analyze_api_with_llm(
                        resp,
                        property_context,
                        ctx.property_id or "unknown",
                    )
                except Exception as exc:
                    result.errors.append(f"api-analysis-error: {exc}")
                    continue
                api_calls_made += 1
                if interaction:
                    llm_interactions.append(interaction)
                    _record_interaction_cost(interaction)

                # Capture ancillary signals from this API analysis (nav
                # hint, platform_guess, property_amenities,
                # api_schema_signature) regardless of unit/noise outcome
                # — the LLM may have classified noise but still
                # diagnosed where the real data lives.
                _merge_hint_extras(api_hints)

                if is_noise:
                    # Feed profile_updater the colon-prefixed format it
                    # already recognises so this URL ends up in
                    # profile.api_hints.blocked_endpoints on next run.
                    # Use the LLM's free-text noise_reason when available
                    # so the blocklist entry carries WHY this URL was
                    # rejected (chatbot_config / analytics / gallery / …)
                    # instead of the generic ``no_unit_data``.
                    noise_reason = (
                        (api_hints or {}).get("noise_reason") or "no_unit_data"
                    )
                    llm_analysis_results[url] = f"noise:{noise_reason}"
                    try:
                        from ma_poc.observability.events import EventKind, emit
                        emit(
                            EventKind.LLM_API_NOISE,
                            ctx.property_id or "unknown",
                            url=url[:200],
                            reason=str(noise_reason)[:200],
                        )
                    except Exception:
                        pass
                    continue
                if units:
                    targeted_units.extend(units)
                    # Extract the persistable mapping subset (the keys
                    # ``save_llm_field_mapping`` needs) from the broader
                    # hints envelope. The other keys flow through the
                    # aggregator above.
                    #
                    # PR 1 (2026-05-10): gate on ``api_url_pattern`` rather
                    # than ``json_paths``. Producer (llm_extractor) now
                    # always sets api_url_pattern when units were
                    # extracted; the persistence layer
                    # (save_llm_field_mapping) gates degraded persistence
                    # on its own flag. Surfacing-site empty-paths gate
                    # used to drop 38 of 41 daily LLM-API winners before
                    # they could ever be persisted.
                    mapping_subset: dict[str, Any] = {}
                    if isinstance(api_hints, dict):
                        for k in ("api_url_pattern", "json_paths", "response_envelope"):
                            if k in api_hints:
                                mapping_subset[k] = api_hints[k]
                    if mapping_subset.get("api_url_pattern"):
                        llm_field_mappings.append(mapping_subset)
                        llm_analysis_results[url] = mapping_subset
                        if not result.api_responses:
                            result.api_responses.append(resp)

            _log_attempt(
                "generic:llm_api_targeted",
                "ran_units" if targeted_units else ("ran_empty" if api_calls_made else "skipped"),
                units=len(targeted_units),
                reason=""
                if targeted_units
                else (
                    f"{api_calls_made} API(s) analysed, no units"
                    if api_calls_made
                    else "no API responses with unit signals"
                ),
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if targeted_units:
            result.units = targeted_units
            result.tier_used = "TIER_4_LLM_API"
            result.winning_url = result.api_responses[0].get("url") if result.api_responses else ctx.base_url
            result.confidence = min(0.85, 0.6 + 0.04 * len(targeted_units))
            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
            # Surface the cross-tier hint aggregator + property amenities
            # so profile_updater persists every signal the API tier
            # collected (nav_hint, platform_guess, api_urls, schema_sig).
            if aggregated_hints:
                result._llm_hints = dict(aggregated_hints)  # type: ignore[attr-defined]
            if aggregated_property_amenities:
                result._property_amenities = list(aggregated_property_amenities)  # type: ignore[attr-defined]
            if llm_navigation_hints:
                result._llm_navigation_hints = list(llm_navigation_hints)  # type: ignore[attr-defined]
            return result

        # Sub-tier 6b: targeted DOM analysis ------------------------------
        # Extract the rent-bearing DOM section (not the full page) and ask
        # the LLM to return units AND CSS selectors we can replay next run.
        dom_units = []
        dom_hints: dict[str, Any] | None = None

        # Rule 2: check for CSS selectors cached from a prior sub-page in the
        # same floor-plan accumulation pass.  If the hop chain's first sub-page
        # LLM DOM call already discovered the selectors, we can replay them
        # against subsequent sub-pages without burning another LLM token.
        # The selectors are written into the shared budget dict by _try_link_hop.
        _fp_css_hint: dict[str, Any] | None = getattr(ctx, "budget", {}).get("_fp_css_hint")  # type: ignore[union-attr]
        # Guard: only replay cache when no units yet — profile selectors may
        # have been tried but produced 0 units (sub-page structure differs from
        # the index page where selectors were saved).  The old check
        # `not _profile_dom_field_selectors` skipped the replay entirely when
        # the profile had saved selectors, defeating the floor-plan accumulation
        # optimisation on WARM/HOT profiles.
        if _fp_css_hint and not dom_units and html:
            try:
                _hint_obj = _FieldSelectorMap(**_fp_css_hint)
                _fp_hint_units, _fp_mode = extract_units_from_dom(html, ctx.base_url, hints=_hint_obj)
                if _fp_hint_units:
                    dom_units = _fp_hint_units
                    _log_attempt(
                        "generic:llm_dom_targeted",
                        "ran_units",
                        units=len(dom_units),
                        reason="fp_css_hint_replay",
                        duration_ms=0,
                    )
            except Exception:
                pass  # fall through to LLM DOM call below

        dom_section_html = _extract_rent_dom_section(html) if html else None
        if not dom_units and dom_section_html and int(_budget.get("llm_dom_calls", 0)) > 0 and not _llm_cost_exceeded():
            t0 = _time.monotonic()
            _budget["llm_dom_calls"] = int(_budget.get("llm_dom_calls", 0)) - 1
            try:
                dom_units, dom_hints, interaction = await analyze_dom_with_llm(
                    dom_section_html,
                    ctx.base_url,
                    property_context,
                    ctx.property_id or "unknown",
                )
            except Exception as exc:
                result.errors.append(f"dom-analysis-error: {exc}")
                dom_units, dom_hints, interaction = [], None, None
            if interaction:
                llm_interactions.append(interaction)
                _record_interaction_cost(interaction)
            # The DOM-LLM now returns a richer envelope rather than a
            # bare css_selectors dict. Extract css_selectors for the
            # self-validation path below; fold every other signal
            # (nav_hint, platform_guess, api_urls_with_data,
            # property_amenities, dom_structure_hash) into the
            # cross-tier aggregator so winner-tier persistence sees them.
            if isinstance(dom_hints, dict):
                css_from_dom = dom_hints.get("css_selectors")
                if isinstance(css_from_dom, dict) and css_from_dom.get("container"):
                    llm_css_selectors = css_from_dom
                _merge_hint_extras(dom_hints)
            _log_attempt(
                "generic:llm_dom_targeted",
                "ran_units" if dom_units else "ran_empty",
                units=len(dom_units or []),
                reason="" if dom_units else "targeted DOM LLM returned no units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
        # RC3 tracking: capture the nav hint from DOM analysis specifically
        # (distinct from llm_navigation_hints which aggregates across all tiers).
        _dom_nav_hint: str | None = (
            dom_hints.get("navigation_hint")
            if isinstance(dom_hints, dict)
            else None
        )

        if dom_units:
            # Merge rather than replace — result.units may already hold units
            # from Tier 3 DOM scan when the planner escalated past it.
            # Replacing would silently drop those units; merging keeps them.
            result.units = _merge_into_result_units(
                result.units, dom_units, property_id=ctx.property_id
            )
            result.tier_used = "TIER_4_LLM_DOM"
            result.winning_url = ctx.base_url
            result.confidence = min(0.80, 0.55 + 0.04 * len(result.units))
            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
            if llm_css_selectors:
                # Self-validate the LLM's selectors before persisting them.
                # The LLM has just produced N units AND a CSS-selector dict
                # claimed to extract them deterministically. Replay the
                # selectors against the same dom_section_html and compare
                # the unit count to gate persistence:
                #
                #   ratio >= 0.8  → high quality, persist as-is.
                #   0.4 ≤ ratio < 0.8 → flaky, persist with a quality_score
                #                       so replay-side gating can soft-fail.
                #   ratio < 0.4   → broken, drop the selectors entirely so
                #                   tomorrow's run won't burn time on a
                #                   guaranteed miss before falling through
                #                   to LLM. Cheaper to re-LLM than to chase
                #                   a known-bad hint cycle.
                #
                # Why inline at extract-time and not in profile_updater:
                # the dom_section_html lives only here. Plumbing it through
                # to the updater would bloat the result dict and cost ledger
                # for every property; the validation needs ~1ms locally so
                # there's no reason to defer it.
                quality_score = 1.0
                try:
                    _hints_obj = _FieldSelectorMap(**llm_css_selectors) if isinstance(
                        llm_css_selectors, dict
                    ) else llm_css_selectors
                    _replayed = extract_with_hints(
                        dom_section_html or "", ctx.base_url, _hints_obj
                    )
                    expected = max(len(dom_units), 1)
                    quality_score = min(1.0, len(_replayed) / expected)
                except Exception:
                    # Replay raised — treat selectors as unverified rather
                    # than blocking the win. Quality 0.5 lets the cascade
                    # use them tomorrow but the replay path won't trust
                    # them for short-circuit.
                    quality_score = 0.5

                # The strict < REPLAY_FLOOR drop assumed any selector that
                # doesn't 1:1 reproduce its own source HTML is broken. In
                # practice low self-validation often means (a) the LLM
                # analysed the API/JSON envelope which has more units than
                # the DOM displays at first paint, or (b) stale
                # loading-state nodes interfered with selector match today
                # but won't be there tomorrow. Behind
                # ENABLE_DEGRADED_DOM_PERSIST (default ON) we persist with
                # quality clamped to the replay-gate floor so the matcher
                # admits the hint on the next run.
                from ma_poc.services.profile_updater import (
                    DOM_HINT_QUALITY_REPLAY_FLOOR as _Q_REPLAY_FLOOR,
                )
                if quality_score < _Q_REPLAY_FLOOR:
                    try:
                        from ma_poc.config.feature_flags import enable_degraded_dom_persist
                        _allow_degraded = enable_degraded_dom_persist()
                    except Exception:
                        _allow_degraded = False
                    try:
                        from ma_poc.observability.events import EventKind, emit
                    except ImportError:
                        EventKind = None  # type: ignore[assignment]
                        emit = None  # type: ignore[assignment]

                    if _allow_degraded:
                        # Clamp to the replay-gate floor so the matcher
                        # admits the entry. The reader still treats it as
                        # soft-fail but at least gets a chance.
                        if emit is not None and EventKind is not None:
                            try:
                                emit(
                                    EventKind.DOM_HINTS_DEGRADED_SAVED,
                                    ctx.property_id or "unknown",
                                    raw_quality=round(quality_score, 3),
                                )
                            except Exception:
                                pass
                        merged_hints: dict[str, Any] = dict(aggregated_hints)
                        merged_hints["css_selectors"] = llm_css_selectors
                        merged_hints["css_selectors_quality"] = _Q_REPLAY_FLOOR
                        result._llm_hints = merged_hints  # type: ignore[attr-defined]
                    else:
                        # Strict pre-PR-6 behaviour — drop. Skip persistence
                        # and emit telemetry so the saver regression doesn't
                        # reappear silently.
                        if emit is not None and EventKind is not None:
                            try:
                                emit(
                                    EventKind.DOM_HINTS_MISS,
                                    ctx.property_id or "unknown",
                                    count=0,
                                    reason=f"selectors_self_validation_failed:{quality_score:.2f}",
                                )
                            except Exception:
                                pass
                        # Selectors discarded but the rest of the DOM-LLM's
                        # output (nav hints, platform guess, etc.) is still
                        # useful — surface it so profile_updater can persist.
                        if aggregated_hints:
                            result._llm_hints = dict(aggregated_hints)  # type: ignore[attr-defined]
                else:
                    # Compose hints from the cross-tier aggregator and
                    # overlay the validated css_selectors + quality on
                    # top. Aggregator first so any later key the
                    # css_selectors block wants to set wins.
                    merged_hints: dict[str, Any] = dict(aggregated_hints)
                    merged_hints["css_selectors"] = llm_css_selectors
                    merged_hints["css_selectors_quality"] = quality_score
                    result._llm_hints = merged_hints  # type: ignore[attr-defined]
            else:
                # No selectors at all — still surface the aggregator so
                # nav hints / platform guess / property_amenities reach
                # profile_updater on a DOM-tier win.
                if aggregated_hints:
                    result._llm_hints = dict(aggregated_hints)  # type: ignore[attr-defined]
            if aggregated_property_amenities:
                result._property_amenities = list(aggregated_property_amenities)  # type: ignore[attr-defined]
            if llm_navigation_hints:
                result._llm_navigation_hints = list(llm_navigation_hints)  # type: ignore[attr-defined]
            return result

        # Sub-tier 6c: monolithic fallback --------------------------------
        # Only fires when 6a + 6b both returned empty. This is the legacy
        # "send full HTML + top-3 APIs" prompt — broadest coverage, highest
        # token cost, so it runs last.
        #
        # Previously this tier was unguarded: budget["llm_monolithic"] was
        # in the dict but never checked, so every link-hop sub-page that
        # reached it fired another monolithic call. Now consumes the
        # shared budget like 6a/6b. When the budget is exhausted (or the
        # cost cap is hit) we skip the call but DO NOT short-circuit the
        # Vision Tier 5 fallback below — the cost cap fires there too.
        #
        # RC3 guard: if DOM analysis (sub-tier 6b) found 0 units but
        # diagnosed a navigation_hint pointing elsewhere, AND there is
        # link_hop budget remaining, defer the monolithic to the hop page
        # rather than burning it on the current page. ActionDecider encodes
        # the full deferral rule including the high-confidence-hop threshold.
        _rc3_deferred = False
        if _dom_nav_hint and not dom_units and int(_budget.get("link_hop", 0)) > 0:
            if _source_ranker is not None and _action_decider is not None:
                try:
                    _dar = _SEDOMAnalysisResult(unit_count=0, navigation_hint=_dom_nav_hint)
                    _hint_sig = _SESourceSignal(kind=_SESourceKind.LLM_HINT, url=_dom_nav_hint)
                    # Use SourceRanker (single scoring authority) to produce the
                    # composite score — not a hardcoded 10_000.
                    _ranked = _source_ranker.rank([_hint_sig])
                    _dctx = _SEDecisionContext(
                        ranked_signals=_ranked,
                        current_unit_count=0,
                        budget=dict(_budget),
                        dom_analysis_result=_dar,
                        hop_depth=getattr(ctx, "hop_depth", 0),
                        # Suppress RC3 deferral when the current page already
                        # has rent or floor-plan signals — running the LLM here
                        # is more likely to produce data than deferring to a hop
                        # page that may be an equally empty SPA shell.
                        page_has_content_signals=(
                            (getattr(ctx, "rent_signal_count", 0) or 0) > 0
                        ),
                    )
                    _decision = _action_decider.decide(_dctx)
                    if _decision.action_type == _SEActionType.HOP_TO_URL:
                        _rc3_deferred = True
                        _log_attempt(
                            "generic:llm",
                            "skipped",
                            reason="rc3_defer_monolithic_to_hop",
                        )
                except Exception as _rc3_exc:
                    log.debug("rc3-decider-error: %s", _rc3_exc)
        if not _rc3_deferred and int(_budget.get("llm_monolithic", 0)) > 0 and not _llm_cost_exceeded():
            _budget["llm_monolithic"] = int(_budget.get("llm_monolithic", 0)) - 1
            t0 = _time.monotonic()
            try:
                llm_input = prepare_llm_input(html, api_responses, property_context)
                llm_units, hints, _raw, interaction = await extract_with_llm(
                    llm_input,
                    property_id=ctx.property_id or "unknown",
                )
                if interaction:
                    llm_interactions.append(interaction)
                    _record_interaction_cost(interaction)
                # Fold the monolithic LLM's hints into the cross-tier
                # aggregator so they survive even when the monolithic
                # returns no units (the LLM frequently produces a
                # navigation_hint or platform_guess on an empty page —
                # signal we want regardless of which tier ultimately wins).
                _merge_hint_extras(hints)
                _log_attempt(
                    "generic:llm",
                    "ran_units" if llm_units else "ran_empty",
                    units=len(llm_units or []),
                    reason="" if llm_units else "LLM returned no structured units",
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                if llm_units:
                    result.units = llm_units
                    result.tier_used = "TIER_4_LLM"
                    result.winning_url = ctx.base_url
                    result.confidence = min(0.75, 0.5 + 0.04 * len(llm_units))
                    result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
                    result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
                    result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
                    # Monolithic hints are the broadest envelope — they
                    # already include profile_hints (nav_hint /
                    # platform_guess / api_urls_with_data /
                    # field_mapping_notes / property_amenities /
                    # extraction_prompt_hash). Fold the aggregator in
                    # underneath so any DOM/API tier discoveries are
                    # preserved on collision (monolithic wins on shared keys).
                    merged_mono = dict(aggregated_hints)
                    if isinstance(hints, dict):
                        merged_mono.update(hints)
                    if merged_mono:
                        result._llm_hints = merged_mono  # type: ignore[attr-defined]
                    if aggregated_property_amenities:
                        result._property_amenities = list(aggregated_property_amenities)  # type: ignore[attr-defined]
                    if llm_navigation_hints:
                        result._llm_navigation_hints = list(llm_navigation_hints)  # type: ignore[attr-defined]
                    return result
            except Exception as exc:
                _log_attempt(
                    "generic:llm",
                    "errored",
                    reason=str(exc)[:200],
                    duration_ms=int((_time.monotonic() - t0) * 1000),
                )
                result.errors.append(f"llm-tier-error: {exc}")
        elif not _rc3_deferred:
            _log_attempt(
                "generic:llm",
                "skipped",
                reason="llm_monolithic budget exhausted or cost cap reached",
            )

        # Fix 6 — Vision LLM as Tier 5 last-resort fallback.
        # When all text-based LLM tiers (api_targeted, dom_targeted, monolithic)
        # have returned empty, AND the page has rent-context keywords visible
        # in the body, AND we have a live Playwright page, take a screenshot
        # and send it to a vision-capable model. Catches sites where rent
        # renders inside images, custom canvas elements, or DOM patterns the
        # text-LLM can't navigate.
        #
        # Bounded: only fires once per property, only when text-LLM has
        # exhausted, only when ENABLE_TIER5_VISION env allows it (default
        # on; can be disabled to control cost). Cost ~$0.05/property when
        # it fires; expected to fire on ~120 of 5000 properties per run
        # → ~$6 added per run.
        try:
            import os as _os_q
            vision_enabled = _os_q.getenv("ENABLE_TIER5_VISION", "true").lower() in ("1", "true", "yes")
            page_obj = getattr(ctx, "_page", None) or getattr(ctx, "page", None)
            # Cheap rent-keyword check on stripped html
            has_rent_kw = False
            if html:
                _hl = html.lower()
                has_rent_kw = any(kw in _hl for kw in ("rent", "bedroom", "studio", "sqft", "floor plan", "$"))
            if vision_enabled and page_obj is not None and has_rent_kw and hasattr(page_obj, "screenshot") and not _llm_cost_exceeded():
                t_v = _time.monotonic()
                try:
                    screenshot_bytes = await page_obj.screenshot(full_page=True, type="png", timeout=10000)
                except Exception as exc:
                    screenshot_bytes = None
                    _log_attempt("generic:vision_llm", "skipped", reason=f"screenshot-error: {str(exc)[:60]}")
                if screenshot_bytes:
                    from ma_poc.services.vision_extractor import extract_with_vision
                    try:
                        vision_units, vision_hints, _raw, vision_interaction = await extract_with_vision(
                            screenshot_bytes,
                            property_context,
                            cropped_sections=None,
                            property_id=ctx.property_id or "unknown",
                        )
                    except Exception as exc:
                        vision_units, vision_hints, vision_interaction = [], {}, None
                        _log_attempt("generic:vision_llm", "errored", reason=str(exc)[:120])
                    else:
                        _log_attempt(
                            "generic:vision_llm",
                            "ran_units" if vision_units else "ran_empty",
                            units=len(vision_units or []),
                            reason="" if vision_units else "vision LLM returned no units",
                            duration_ms=int((_time.monotonic() - t_v) * 1000),
                        )
                        if vision_interaction:
                            llm_interactions.append(vision_interaction)
                            _record_interaction_cost(vision_interaction)
                        # Vision LLM may also volunteer property_amenities
                        # / nav_hint — fold into the cross-tier aggregator.
                        _merge_hint_extras(vision_hints)
                        if vision_units:
                            result.units = vision_units
                            result.tier_used = "TIER_5_VISION"
                            result.winning_url = ctx.base_url
                            result.confidence = min(0.70, 0.5 + 0.03 * len(vision_units))
                            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
                            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
                            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
                            # Merge aggregator with vision hints — vision
                            # wins on shared keys (it's the most recent
                            # observation, equivalent to monolithic).
                            merged_vision = dict(aggregated_hints)
                            if isinstance(vision_hints, dict):
                                merged_vision.update(vision_hints)
                            if merged_vision:
                                result._llm_hints = merged_vision  # type: ignore[attr-defined]
                            if aggregated_property_amenities:
                                result._property_amenities = list(aggregated_property_amenities)  # type: ignore[attr-defined]
                            if llm_navigation_hints:
                                result._llm_navigation_hints = list(llm_navigation_hints)  # type: ignore[attr-defined]
                            return result
        except Exception as exc:
            # Vision tier must never crash the adapter; swallow and continue
            _log_attempt("generic:vision_llm", "errored", reason=str(exc)[:120])

        # All LLM sub-tiers empty — surface everything we learned so the
        # profile updater (Phase 4) can still record blocked endpoints and
        # link-hop (Phase 5) can follow navigation_hint on a second pass.
        result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
        result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
        result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
        if llm_navigation_hints:
            result._llm_navigation_hints = llm_navigation_hints  # type: ignore[attr-defined]
        # Even on a no-tier-win path, every learnable signal that any
        # sub-tier surfaced (nav_hint / platform_guess / api_urls /
        # property_amenities / drift hashes) flows to profile_updater.
        # Without this, the same property would re-pay for the same
        # diagnostic work tomorrow.
        if aggregated_hints:
            result._llm_hints = dict(aggregated_hints)  # type: ignore[attr-defined]
        if aggregated_property_amenities:
            result._property_amenities = list(aggregated_property_amenities)  # type: ignore[attr-defined]
        result._decision_log = decision_log  # type: ignore[attr-defined]

        # Last-resort SightMap iframe-fallback (2026-05-18 generalisation).
        # The JS-injected-embed Engrain/SightMap class (HubSpot/Wix —
        # springsapartments.com etc.) has no sightmap marker on the
        # property page, so the detector tags it ``unknown`` and this
        # generic adapter runs. But its per-property ``/floor-plans``
        # sub-page (which the link-hop fetches) DOES embed
        # ``<iframe src="sightmap.com/embed/{code}">`` on load. The
        # capture-based generic:sightmap path (line ~1490) is timing-
        # fragile because the SightMap data XHR fires only after the
        # embed SPA boots (often >12s). The iframe FALLBACK resolves it
        # deterministically from HTML alone — fetch the embed page, read
        # ``__APP_CONFIG__``, GET the API — exactly the path that already
        # makes IRT succeed (TIER_1_API_SIGHTMAP_IFRAME). Works on the
        # fetch_result snapshot (no live page), so it fits jugnu. Reuses
        # the proven sightmap.py helper unchanged. Cost-gated to the
        # genuine 0-unit tail; never raises.
        if not result.units:
            try:
                from ma_poc.pms.adapters.sightmap import (
                    _try_sightmap_iframe_fallback,
                )

                _sm_units = await _try_sightmap_iframe_fallback(ctx, result)
                if _sm_units:
                    from ma_poc.extraction.post_process import post_process

                    _pp = post_process(
                        _sm_units,
                        property_id=getattr(ctx, "property_id", None),
                    )
                    if _pp.n_admitted > 0:
                        result.units = _pp.admitted
                        result.plan_summaries = _pp.plan_summaries
                        result.tier_used = "TIER_1_API_SIGHTMAP_IFRAME"
                        result.confidence = min(
                            0.90, 0.7 + 0.05 * _pp.n_admitted
                        )
                        _log_attempt(
                            "generic:sightmap_iframe", "ran_units",
                            units_found=len(_pp.admitted),
                        )
                        return result
                _log_attempt(
                    "generic:sightmap_iframe", "ran_empty",
                    reason="no sightmap embed in HTML or fallback empty",
                )
            except Exception as _sm_exc:  # never crash the adapter
                _log_attempt(
                    "generic:sightmap_iframe", "errored",
                    reason=str(_sm_exc)[:120],
                )

        # 2026-05-23: operator-published "no availability now" detector.
        # Last resort BEFORE we declare the page extractionless. ~10
        # krcapartments-class properties publish an explicit
        # "Sorry, there are no available units at this time." string
        # (and ~9 sibling phrases — see _no_availability.py). That's a
        # genuine zero-inventory state, not a failure — the verdict
        # layer routes it to SUCCESS_NO_AVAILABILITY when this flag is
        # set. Fires ONLY when no other tier produced units, so it
        # never overrides a real extraction.
        if not result.units:
            try:
                from ma_poc.pms.adapters._no_availability import (
                    build_no_availability_placeholder,
                    detect_no_availability,
                    matched_phrase,
                )

                if detect_no_availability(html):
                    placeholder = build_no_availability_placeholder(
                        source_url=ctx.base_url,
                        property_name=getattr(ctx, "property_name", "")
                        or "",
                        matched_text=matched_phrase(html),
                    )
                    result.units = [placeholder]
                    result.tier_used = "TIER_1_DOM_NO_AVAILABILITY"
                    result.winning_url = ctx.base_url
                    result.confidence = 0.65
                    # Surface the flag for jugnu → compute_verdict.
                    result._operator_no_availability = True  # type: ignore[attr-defined]
                    _log_attempt(
                        "generic:no_availability",
                        "ran_units",
                        units=1,
                        reason="operator published explicit zero-availability state",
                    )
                    return result
            except Exception as _na_exc:
                # Detector must never crash the pipeline — log and
                # continue to the final no-units return below.
                result.errors.append(f"no-availability-error: {_na_exc}")

        result.confidence = 0.0
        result.errors.append("Generic parser found no units in captured API responses")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
