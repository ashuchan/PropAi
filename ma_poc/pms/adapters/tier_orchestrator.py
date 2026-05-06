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

import re as _re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._daily_runner_parsers import (
    parse_api_responses as _dr_parse_api_responses,
)
from ma_poc.pms.adapters._daily_runner_parsers import (
    parse_sightmap_payload as _dr_parse_sightmap,
)
from ma_poc.pms.adapters._html_extract import (
    extract_embedded_blobs_from_html,
    extract_jsonld_from_html,
    extract_units_from_dom,
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

if TYPE_CHECKING:
    from playwright.async_api import Page

# Used by the Option C relaxed-LLM gate to sanity-check HTML has enough
# rent-ish content to be worth an LLM call even when the detected PMS
# adapter already returned empty.
_re_strip_script = _re.compile(r"<script.*?</script>|<style.*?</style>", _re.IGNORECASE | _re.DOTALL)
_re_strip_tag = _re.compile(r"<[^>]+>")
_re_rent = _re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?", _re.IGNORECASE)

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
        from ma_poc.services.source_planner import evaluate_completeness, plan_next_action
        from ma_poc.models.source import SourceId, from_legacy_unit
        from ma_poc.models.scrape_profile import ProfileMaturity
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


# ── Field categories used by the anchor-first merge (H9) ──────────────────
# Mutable: latest extracted value wins on conflict.
_MERGE_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "market_rent_low",
        "market_rent_high",
        "asking_rent",
        "effective_rent",
        "rent_low",
        "rent_high",
        "rent_range",
        "available_date",
        "availability_status",
        "lease_term",
        "concession_text",
        "concession_value",
        "concession_source",
        "days_on_market",
    }
)
# Physical / identity: existing wins on conflict; conflict is logged.
_MERGE_PHYSICAL_FIELDS: frozenset[str] = frozenset(
    {
        "floor_plan_id",
        "floor_plan_name",
        "floor_plan_name_extracted",
        "floor_plan_source",
        "beds",
        "bedrooms",
        "_bedrooms",
        "baths",
        "bathrooms",
        "_bathrooms",
        "sqft",
        "area",
        "_sqft",
        "unit_number",
        "_unit_number",
        "unit_id",
        "apartmentid",
        "floorplannumber",
    }
)
# Multi-source: union/dedup. Insertion-order is preserved at this layer;
# the run-end report sorts the aggregated property_amenities for stable
# diffing.
_MERGE_UNION_FIELDS: frozenset[str] = frozenset({"amenities"})


def _merge_field_values(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    rank: str,
    conflict_log: list[dict[str, Any]],
) -> None:
    """Merge ``source`` into ``target`` per the H9 field-category rules.

    Mutable fields take the latest extracted value when both sides carry one.
    Physical fields keep the existing value and emit a structured conflict
    record. Multi-source fields union+dedupe. Anything else falls through
    to fill-when-missing semantics.
    """
    for k, v in source.items():
        if k.startswith("_"):
            # Telemetry keys (e.g. _provenance) flow through the source
            # merger, not this function. Skip them so they never collide.
            continue
        if v in (None, "", -1, "-1"):
            continue

        existing_val = target.get(k)
        existing_present = existing_val not in (None, "", -1, "-1")

        if k in _MERGE_UNION_FIELDS:
            if isinstance(existing_val, list) or isinstance(v, list):
                # Seed from existing — list values verbatim, scalars wrapped
                # so a previous tier's single string isn't dropped when the
                # next tier emits a list.
                if isinstance(existing_val, list):
                    merged: list[Any] = list(existing_val)
                elif existing_present:
                    merged = [existing_val]
                else:
                    merged = []
                seen: set[Any] = set()
                for x in merged:
                    try:
                        seen.add(x)
                    except TypeError:
                        # Non-hashable items (rare — e.g. dicts) can't seed
                        # the dedup set. Fall back to value-equality check.
                        pass
                for item in v if isinstance(v, list) else [v]:
                    try:
                        if item in seen:
                            continue
                        seen.add(item)
                    except TypeError:
                        if any(item == m for m in merged):
                            continue
                    merged.append(item)
                target[k] = merged
            else:
                if not existing_present:
                    target[k] = v
            continue

        if k in _MERGE_MUTABLE_FIELDS:
            target[k] = v  # latest wins
            continue

        if k in _MERGE_PHYSICAL_FIELDS:
            if not existing_present:
                target[k] = v
            elif existing_val != v:
                conflict_log.append(
                    {
                        "field": k,
                        "existing": existing_val,
                        "new": v,
                        "rank_used": rank,
                    }
                )
            continue

        # Default: fill-when-missing (legacy behaviour for anything not
        # explicitly categorised).
        if not existing_present:
            target[k] = v


def _merge_norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _merge_int_or_none(v: Any) -> int | None:
    """Coerce to int or return None. Treats `-1` sentinel as None (H2)."""
    if v is None or v == "" or v == -1:
        return None
    try:
        return int(float(str(v).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def _merge_float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _merge_field_present(unit: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = unit.get(k)
        if v not in (None, "", -1, "-1"):
            return v
    return None


def _merge_rank_signature(unit: dict[str, Any]) -> dict[str, Any]:
    """Extract the comparable identity fields from a unit (H2 strict sqft)."""
    fp_id = _merge_field_present(unit, "floor_plan_id")
    uid = _merge_field_present(unit, "unit_id", "unit_number", "_unit_number")
    fp_name = _merge_norm(_merge_field_present(unit, "floor_plan_name", "_floor_plan", "floorplan_name"))
    beds = _merge_int_or_none(_merge_field_present(unit, "beds", "bedrooms", "_bedrooms"))
    baths = _merge_float_or_none(_merge_field_present(unit, "baths", "bathrooms", "_bathrooms"))
    sqft = _merge_int_or_none(_merge_field_present(unit, "sqft", "area", "_sqft"))
    return {
        "fp_id": str(fp_id) if fp_id else "",
        "uid": _merge_norm(uid),
        "fp_name": fp_name,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
    }


# Rank predicate: each function returns True when the candidate matches at
# this rank. The order of rank evaluation IS the priority ladder (R0 → R1f).
def _rank_matches(rank: str, ex_sig: dict[str, Any], inc_sig: dict[str, Any]) -> bool:
    if rank == "R0":
        return bool(ex_sig["fp_id"]) and ex_sig["fp_id"] == inc_sig["fp_id"]
    if rank == "R0a":
        return bool(ex_sig["uid"]) and ex_sig["uid"] == inc_sig["uid"]
    if rank == "R1a":
        return (
            bool(ex_sig["fp_name"])
            and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
            and ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None
            and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is not None
            and inc_sig["sqft"] is not None
            and ex_sig["sqft"] == inc_sig["sqft"]  # H2 strict
        )
    if rank == "R1b":
        return (
            ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None
            and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is not None
            and inc_sig["sqft"] is not None
            and ex_sig["sqft"] == inc_sig["sqft"]  # H2 strict
        )
    if rank == "R1c":
        return (
            ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None
            and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and bool(ex_sig["fp_name"])
            and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
        )
    if rank == "R1d":
        return (
            ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None
            and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is None
            and inc_sig["sqft"] is None
            and not (ex_sig["fp_name"] and inc_sig["fp_name"])
        )
    if rank == "R1e":
        return (
            ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and (ex_sig["baths"] is None or inc_sig["baths"] is None)
            and (ex_sig["sqft"] is None or inc_sig["sqft"] is None)
            and not (ex_sig["fp_name"] and inc_sig["fp_name"])
        )
    if rank == "R1f":
        return (
            ex_sig["beds"] is not None
            and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and bool(ex_sig["fp_name"])
            and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
            and (ex_sig["baths"] is None or inc_sig["baths"] is None)
            and (ex_sig["sqft"] is None or inc_sig["sqft"] is None)
        )
    return False


_RANK_LADDER: tuple[str, ...] = ("R0", "R0a", "R1a", "R1b", "R1c", "R1d", "R1e", "R1f")
# Ranks that allow the H8 fail-closed treatment for ambiguous matches (every
# rank below R0 / R0a — the strong-identity rungs cannot be ambiguous given
# fp_id and unit_id are unique).
_AMBIGUITY_RANKS: frozenset[str] = frozenset({"R1a", "R1b", "R1c", "R1d", "R1e", "R1f"})


def _emit_physical_conflicts(
    property_id: str,
    unit_index: int,
    rank: str,
    conflicts: list[dict[str, Any]],
) -> None:
    if not conflicts:
        return
    try:
        from ma_poc.observability.events import EventKind, emit

        for c in conflicts:
            emit(
                EventKind.EXTRACT_PHYSICAL_ATTRIBUTE_CONFLICT,
                property_id,
                unit_index=unit_index,
                field=c["field"],
                existing=c["existing"],
                new=c["new"],
                rank_used=rank,
            )
    except Exception:
        pass  # observability is best-effort


def _emit_ambiguous_fail_closed(property_id: str, rank: str, candidate_count: int) -> None:
    try:
        from ma_poc.observability.events import EventKind, emit

        emit(
            EventKind.EXTRACT_AMBIGUOUS_MERGE_FAIL_CLOSED,
            property_id,
            rank=rank,
            candidate_count=candidate_count,
        )
    except Exception:
        pass


def _availability_count_aware_merge(
    target: dict[str, Any],
    source: dict[str, Any],
    rank: str,
) -> None:
    """Sum availability_count when merging at R0 or R1a; latest-wins below."""
    inc_count = source.get("availability_count")
    if inc_count is None:
        return
    if rank in ("R0", "R0a", "R1a"):
        ex_count = target.get("availability_count")
        try:
            ex_int = int(ex_count) if ex_count is not None else 0
            inc_int = int(inc_count)
        except (TypeError, ValueError):
            target["availability_count"] = inc_count
            return
        target["availability_count"] = ex_int + inc_int
    else:
        target["availability_count"] = inc_count


def _merge_into_result_units(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    property_id: str = "unknown",
) -> list[dict[str, Any]]:
    """Anchor-first merge cascade (Phase 4 of CLAUDE_PROMPTS_MERGE_RESILIENCE).

    Walks the rank ladder R0 → R1f for each incoming record. The first rank
    with a unique winner among the existing list takes the merge. Multiple
    candidates at the same rank below R0/R0a fail closed (H8): the new
    record is appended rather than silently merged.

    Field-level merge follows H9 — mutable fields take latest, physical
    fields keep existing and emit a conflict event.

    H10: this function never merges across properties; callers are
    responsible for grouping by property_id, and the property_id is
    forwarded only for telemetry.
    """
    if not existing:
        return list(incoming)
    if not incoming:
        return list(existing)

    # Pre-compute signatures for the existing list so we don't recompute
    # them on every incoming row.
    ex_sigs: list[dict[str, Any]] = [_merge_rank_signature(u) for u in existing]
    result: list[dict[str, Any]] = list(existing)

    for inc_idx, inc in enumerate(incoming):
        inc_sig = _merge_rank_signature(inc)
        match_idx: int | None = None
        match_rank: str | None = None
        ambiguous = False
        ambiguous_rank: str | None = None
        ambiguous_count = 0

        for rank in _RANK_LADDER:
            candidates: list[int] = []
            for i, ex_sig in enumerate(ex_sigs):
                if _rank_matches(rank, ex_sig, inc_sig):
                    candidates.append(i)
            if len(candidates) == 1:
                match_idx = candidates[0]
                match_rank = rank
                break
            if len(candidates) > 1:
                # H8: ambiguous below the strong-identity rungs → fail closed.
                if rank in _AMBIGUITY_RANKS:
                    ambiguous = True
                    ambiguous_rank = rank
                    ambiguous_count = len(candidates)
                    break
                # R0/R0a should never be ambiguous (fp_id and unit_id unique
                # within a property). If they are, prefer the first hit.
                match_idx = candidates[0]
                match_rank = rank
                break

        if match_idx is not None and match_rank is not None and not ambiguous:
            target = result[match_idx]
            conflict_log: list[dict[str, Any]] = []
            _availability_count_aware_merge(target, inc, match_rank)
            _merge_field_values(target, inc, rank=match_rank, conflict_log=conflict_log)
            _emit_physical_conflicts(property_id, match_idx, match_rank, conflict_log)
            # Refresh signature in case the merge added identity fields.
            ex_sigs[match_idx] = _merge_rank_signature(target)
        else:
            if ambiguous and ambiguous_rank is not None:
                _emit_ambiguous_fail_closed(
                    property_id, ambiguous_rank, candidate_count=ambiguous_count
                )
            # Append as a fresh record — the original (existing) and the
            # new record both stand on their own (H8).
            result.append(dict(inc))
            ex_sigs.append(_merge_rank_signature(inc))

    return result


def _aggregate_quality(mappings: list[Any]) -> float:
    """Min quality_score across contributing mappings; defaults to 1.0."""
    if not mappings:
        return 1.0
    qs = [float(getattr(m, "quality_score", 1.0) or 1.0) for m in mappings]
    return min(qs) if qs else 1.0


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
    """
    if not units or not api_responses or not field_patches:
        return units

    def _get_path(obj: Any, path: str) -> Any:
        for part in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if idx < len(obj) else None
            else:
                return None
            if obj is None:
                return None
        return obj

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


def _find_unit_list(body: Any) -> list[dict[str, Any]]:
    """Attempt to find a list of unit/floorplan dicts in an API response body.

    Searches multiple envelope shapes: direct list, dict with known keys,
    one level of nesting (data.units, response.floorplans, etc.).
    """
    _LIST_KEYS = (
        "floorPlans",
        "floor_plans",
        "FloorPlans",
        "floorplans",
        "units",
        "apartments",
        "availabilities",
        "results",
        "items",
        "listings",
    )

    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body

    if isinstance(body, dict):
        for k in _LIST_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # One level deeper
        for outer in ("data", "response", "result", "body"):
            nested = body.get(outer)
            if isinstance(nested, dict):
                for k in _LIST_KEYS:
                    v = nested.get(k)
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v
            # response might be a list directly
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                return nested

    return []


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


def _has_unit_signals(items: list[dict[str, Any]]) -> bool:
    """Check if a list of dicts has enough unit/floorplan signals to be worth parsing."""
    if not items:
        return False
    _SIGNAL_KEYS = {
        "rent",
        "minRent",
        "maxRent",
        "min_rent",
        "max_rent",
        "price",
        "askingRent",
        "monthlyRent",
        "baseRent",
        "bedrooms",
        "beds",
        "bedRooms",
        "bed",
        "sqft",
        "squareFeet",
        "square_footage",
        "sq_ft",
        "minimumSquareFeet",
        "no_of_bedroom",
        "unitNumber",
        "unit_number",
        "unitId",
        "unit_id",
        "floorPlanName",
        "floor_plan_name",
        "floorplan_name",
        "floorplan-name",
        "availableDate",
        "available_date",
        "availableCount",
        "minimumRent",
        "maximumRent",
        "minimumMarketRent",
        "maximumMarketRent",
        "rentRange",
        "depositAmount",
        "numberOfUnitsDisplay",
    }
    sample_keys = set(items[0].keys())
    return len(sample_keys & _SIGNAL_KEYS) >= 2


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
        """
        result = await self._extract_inner(page, ctx)
        try:
            self._phase5_post_merge(result, ctx)
        except Exception:
            # Never break the run on a merge-side error (H10 best-effort)
            pass
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
                from ma_poc.observability.events import EventKind as _EK, emit as _ev
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
            from ma_poc.observability.events import EventKind as _EK2, emit as _ev2
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
        profile = getattr(ctx, "profile", None)
        blocked_urls: set[str] = set()
        if profile is not None:
            try:
                for be in getattr(profile.api_hints, "blocked_endpoints", []) or []:
                    pat = getattr(be, "url_pattern", None)
                    if pat:
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

                    for mapping in saved:
                        try:
                            pat = getattr(mapping, "api_url_pattern", None) or (
                                mapping.get("api_url_pattern") if isinstance(mapping, dict) else None
                            )
                        except Exception:
                            pat = None
                        if not pat:
                            continue
                        for resp in api_responses:
                            if pat in resp.get("url", ""):
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

        # Sub-tier 1: narrow generic API parser -----------------------------
        t0 = _time.monotonic()
        for resp in api_responses:
            body = resp.get("body")
            items = _find_unit_list(body)
            if items and _has_unit_signals(items):
                url = resp.get("url", "")
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
            # Sub-tier 3: JSON-LD
            t0 = _time.monotonic()
            jsonld_units = extract_jsonld_from_html(html, ctx.base_url)
            # Phase 5: reject JSON-LD success when the extraction is
            # plan-name-only with no Offer prices. That shape was gating
            # the LLM sub-tiers from running on 8 properties in the
            # 2026-04-19 run — marking them "SUCCESS" with a list of
            # floor-plan labels but no rent/sqft. Better to fall through.
            if jsonld_units:
                has_rent = any(
                    u.get("market_rent_low") or u.get("market_rent_high") or u.get("rent_range")
                    for u in jsonld_units
                )
                has_size = any(u.get("sqft") for u in jsonld_units)
                if not has_rent and not has_size:
                    _log_attempt(
                        "generic:jsonld",
                        "ran_empty",
                        units=len(jsonld_units),
                        reason="JSON-LD had floor-plan names only (no rent/sqft) — falling through",
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
            if embedded:
                try:
                    embedded_units = _dr_parse_api_responses(embedded) or []
                except Exception as exc:
                    embedded_units = []
                    result.errors.append(f"embedded-parse-error: {exc}")
            else:
                embedded_units = []
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
                if _ed is None or _ed.action == "STOP":
                    result._decision_log = decision_log  # type: ignore[attr-defined]
                    return result
                skip_llm = False  # planner escalated — allow LLM

            # Sub-tier 5: DOM selector cascade ------------------------------
            # Scans container elements (.unit, .floor-plan, .pricing-card, …)
            # for visible rent + structural signals. Catches static HTML sites
            # where unit data lives in the markup, not in any JSON envelope.
            # Phase E: pass saved field_selectors as hints when available.
            t0 = _time.monotonic()
            dom_hints = None
            if ctx.profile is not None:
                fs = ctx.profile.dom_hints.field_selectors
                if fs and getattr(fs, "container", None):
                    dom_hints = fs
            hints_attempted = dom_hints is not None
            try:
                dom_units, _dom_hit_mode = extract_units_from_dom(html, ctx.base_url, hints=dom_hints)
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
                if _dd is None or _dd.action == "STOP":
                    result._decision_log = decision_log  # type: ignore[attr-defined]
                    return result
                skip_llm = False  # planner escalated — allow LLM
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
                result.tier_used = decision.reason.split(":")[0]
                result.errors.append(decision.reason)
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
        _budget = getattr(ctx, "budget", None) or {"llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1, "link_hop": 3}
        api_llm_budget = int(_budget.get("llm_api_calls", 3))
        dom_llm_budget = int(_budget.get("llm_dom_calls", 1))
        llm_interactions: list[dict[str, Any]] = getattr(result, "_llm_interactions", []) or []
        # Self-learning payload surfaced to scraper.py. Shape matches what
        # services.profile_updater.update_profile_after_extraction expects:
        #   - dict (with api_url_pattern) => save as LlmFieldMapping
        #   - "noise:<reason>" string     => add to blocked_endpoints
        llm_analysis_results: dict[str, Any] = {}
        llm_field_mappings: list[dict[str, Any]] = []
        llm_css_selectors: dict[str, Any] | None = None
        llm_navigation_hints: list[str] = []

        # Sub-tier 6a: targeted API analysis ------------------------------
        # For each captured API response with unit-like signals that the
        # deterministic parsers couldn't unwrap, ask the LLM to both
        # extract units AND return json_paths + response_envelope that we
        # can replay deterministically on the next run (zero LLM cost).
        targeted_units: list[dict[str, Any]] = []
        if api_responses and api_llm_budget > 0:
            t0 = _time.monotonic()
            api_calls_made = 0
            for resp in api_responses:
                if api_calls_made >= api_llm_budget:
                    break
                body = resp.get("body")
                items = _find_unit_list(body)
                # Only spend budget on responses where something looks like a
                # unit list — avoids feeding analytics payloads to the LLM.
                if not items or not _has_unit_signals(items):
                    continue
                url = resp.get("url", "")
                try:
                    units, mapping, is_noise, interaction = await analyze_api_with_llm(
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
                if is_noise:
                    # Feed profile_updater the colon-prefixed format it
                    # already recognises so this URL ends up in
                    # profile.api_hints.blocked_endpoints on next run.
                    llm_analysis_results[url] = "noise:no_unit_data"
                    continue
                if units:
                    targeted_units.extend(units)
                    if mapping:
                        # Emit the mapping dict at its URL key — matches
                        # the shape profile_updater reads via
                        # save_llm_field_mapping().
                        llm_field_mappings.append(mapping)
                        llm_analysis_results[url] = mapping
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
            return result

        # Sub-tier 6b: targeted DOM analysis ------------------------------
        # Extract the rent-bearing DOM section (not the full page) and ask
        # the LLM to return units AND CSS selectors we can replay next run.
        dom_units = []
        dom_section_html = _extract_rent_dom_section(html) if html else None
        if dom_section_html and dom_llm_budget > 0:
            t0 = _time.monotonic()
            try:
                dom_units, selectors, interaction = await analyze_dom_with_llm(
                    dom_section_html,
                    ctx.base_url,
                    property_context,
                    ctx.property_id or "unknown",
                )
            except Exception as exc:
                result.errors.append(f"dom-analysis-error: {exc}")
                dom_units, selectors, interaction = [], None, None
            if interaction:
                llm_interactions.append(interaction)
            if selectors:
                llm_css_selectors = selectors
            _log_attempt(
                "generic:llm_dom_targeted",
                "ran_units" if dom_units else "ran_empty",
                units=len(dom_units or []),
                reason="" if dom_units else "targeted DOM LLM returned no units",
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )

        if dom_units:
            result.units = dom_units
            result.tier_used = "TIER_4_LLM_DOM"
            result.winning_url = ctx.base_url
            result.confidence = min(0.80, 0.55 + 0.04 * len(dom_units))
            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
            if llm_css_selectors:
                result._llm_hints = {"css_selectors": llm_css_selectors}  # type: ignore[attr-defined]
            return result

        # Sub-tier 6c: monolithic fallback --------------------------------
        # Only fires when 6a + 6b both returned empty. This is the legacy
        # "send full HTML + top-3 APIs" prompt — broadest coverage, highest
        # token cost, so it runs last.
        t0 = _time.monotonic()
        try:
            llm_input = prepare_llm_input(html, api_responses, property_context)
            llm_units, hints, _raw, interaction = await extract_with_llm(
                llm_input,
                property_id=ctx.property_id or "unknown",
            )
            if interaction:
                llm_interactions.append(interaction)
            # Capture navigation_hint (Phase 3 + Phase 5) so scrape_jugnu's
            # link-hop can prioritise the URL the LLM just told us about.
            if isinstance(hints, dict):
                nav = hints.get("navigation_hint") or ""
                if nav:
                    llm_navigation_hints.append(str(nav))
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
                if hints:
                    result._llm_hints = hints  # type: ignore[attr-defined]
                return result
        except Exception as exc:
            _log_attempt(
                "generic:llm",
                "errored",
                reason=str(exc)[:200],
                duration_ms=int((_time.monotonic() - t0) * 1000),
            )
            result.errors.append(f"llm-tier-error: {exc}")

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
            if vision_enabled and page_obj is not None and has_rent_kw and hasattr(page_obj, "screenshot"):
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
                        if vision_units:
                            result.units = vision_units
                            result.tier_used = "TIER_5_VISION"
                            result.winning_url = ctx.base_url
                            result.confidence = min(0.70, 0.5 + 0.03 * len(vision_units))
                            result._llm_interactions = llm_interactions  # type: ignore[attr-defined]
                            result._llm_field_mappings = llm_field_mappings  # type: ignore[attr-defined]
                            result._llm_analysis_results = llm_analysis_results  # type: ignore[attr-defined]
                            if vision_hints:
                                result._llm_hints = vision_hints  # type: ignore[attr-defined]
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
        result._decision_log = decision_log  # type: ignore[attr-defined]
        result.confidence = 0.0
        result.errors.append("Generic parser found no units in captured API responses")
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
