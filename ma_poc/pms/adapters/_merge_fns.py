"""Pure, side-effect-free unit-merge helpers extracted from generic.py (PR-2).

Contains the anchor-first merge cascade (R0 → R1f), field-category merge rules,
and unit-list discovery helpers. No I/O, no async, no Playwright — fully
unit-testable with stdlib only.

Observability calls (emit) are best-effort and wrapped in try/except so
failures never affect the merge result.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ── Field categories (H9) ──────────────────────────────────────────────────────

MERGE_MUTABLE_FIELDS: frozenset[str] = frozenset(
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

MERGE_PHYSICAL_FIELDS: frozenset[str] = frozenset(
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

MERGE_UNION_FIELDS: frozenset[str] = frozenset({"amenities"})

# Rank ladder — evaluated in priority order (R0 is strongest).
RANK_LADDER: tuple[str, ...] = ("R0", "R0a", "R1a", "R1b", "R1c", "R1d", "R1e", "R1f")

# Ranks where multiple candidates trigger H8 fail-closed treatment.
AMBIGUITY_RANKS: frozenset[str] = frozenset({"R1a", "R1b", "R1c", "R1d", "R1e", "R1f"})

# Keys used by _has_unit_signals to detect unit-shaped responses.
_UNIT_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        # ── Rent / price ───────────────────────────────────────────────────
        "rent", "minRent", "maxRent", "min_rent", "max_rent", "price",
        "askingRent", "monthlyRent", "baseRent",
        "minimumRent", "maximumRent", "minimumMarketRent", "maximumMarketRent",
        "rentRange", "depositAmount", "numberOfUnitsDisplay",
        # ── Bedrooms ───────────────────────────────────────────────────────
        "bedrooms", "beds", "bedRooms", "bed",
        "no_of_bedroom",                      # Entrata
        # ── Bathrooms ──────────────────────────────────────────────────────
        "bathrooms", "baths",
        "no_of_bathroom", "no_of_bath",       # Entrata
        # ── Area / sqft ────────────────────────────────────────────────────
        "sqft", "squareFeet", "square_footage", "sq_ft", "minimumSquareFeet",
        "area",                               # generic alias used by SightMap
        # ── Unit / floor-plan identity ─────────────────────────────────────
        "unitNumber", "unit_number", "unitId", "unit_id",
        "floorPlanName", "floor_plan_name", "floorplan_name", "floorplan-name",
        # ── Availability ───────────────────────────────────────────────────
        "availableDate", "available_date", "availableCount", "available_on",
    }
)

_LIST_KEYS: tuple[str, ...] = (
    "floorPlans", "floor_plans", "FloorPlans", "floorplans",
    "units", "apartments", "availabilities", "results", "items", "listings",
)


# ── Coercion helpers ───────────────────────────────────────────────────────────

def merge_norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def merge_int_or_none(v: Any) -> int | None:
    """Coerce to int; treats ``-1`` sentinel as None (H2)."""
    if v is None or v == "" or v == -1:
        return None
    try:
        return int(float(str(v).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def merge_float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def merge_field_present(unit: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = unit.get(k)
        if v not in (None, "", -1, "-1"):
            return v
    return None


def _normalise_fp_name(name: Any) -> str:
    """Strip stringified-JSON wrapper and lowercase the floor-plan name.

    D8 (2026-05-16): The B3 dict-unwrap fix in _api_parser.py corrected ONE
    code path that emitted ``floor_plan_name='{"name":"1x1Ac T1","provider_id":
    "6093286"}'`` but other paths (e.g. older saved profile data, legacy LLM
    output) may still emit the stringified-JSON shape. When the merge layer
    compares fp_name across cascade passes, the stringified-JSON form fails
    to match the unwrapped form → different rank-signature → records that
    describe the same unit are emitted twice.

    Two-pass normalisation:
      1. If the value looks like a JSON object string ('{...}'), parse it
         and extract the .name field.
      2. Lowercase + strip the result (existing merge_norm behaviour).
    """
    if name is None:
        return ""
    if not isinstance(name, str):
        return merge_norm(name)
    stripped = name.strip()
    # Cheap pre-check: only attempt JSON parse if it looks like a dict literal.
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            import json as _json
            obj = _json.loads(stripped)
            if isinstance(obj, dict):
                for k in ("name", "label", "title", "display_name", "displayName"):
                    v = obj.get(k)
                    if isinstance(v, str) and v:
                        return merge_norm(v)
        except (ValueError, TypeError):
            pass
    return merge_norm(name)


# ── Rank signature ─────────────────────────────────────────────────────────────

def merge_rank_signature(unit: dict[str, Any]) -> dict[str, Any]:
    """Extract the comparable identity fields from a unit (H2 strict sqft)."""
    fp_id = merge_field_present(unit, "floor_plan_id")
    uid = merge_field_present(unit, "unit_id", "unit_number", "_unit_number")
    # D8 (2026-05-16): unwrap stringified-JSON fp_name before comparison.
    fp_name = _normalise_fp_name(
        merge_field_present(unit, "floor_plan_name", "_floor_plan", "floorplan_name")
    )
    beds = merge_int_or_none(merge_field_present(unit, "beds", "bedrooms", "_bedrooms"))
    baths = merge_float_or_none(merge_field_present(unit, "baths", "bathrooms", "_bathrooms"))
    sqft = merge_int_or_none(merge_field_present(unit, "sqft", "area", "_sqft"))
    return {
        "fp_id": str(fp_id) if fp_id else "",
        "uid": merge_norm(uid),
        "fp_name": fp_name,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
    }


def rank_matches(rank: str, ex_sig: dict[str, Any], inc_sig: dict[str, Any]) -> bool:
    """Return True when the incoming signature matches existing at ``rank``."""
    if rank == "R0":
        # D7 follow-up (2026-05-16): R0 is the plan-level merge rank — used to
        # collapse two records describing the SAME PLAN. But when both records
        # are UNIT-LEVEL (each has its own uid), many units legitimately share
        # the same floor_plan_id (they're all units of plan "Baywood"), and
        # collapsing them at R0 destroys real units.  Require BOTH sides to
        # lack a uid before allowing the R0 fp_id-only match: that way R0
        # only fires for plan-aggregate records and unit-level dedup must
        # come through R0a (uid match) or R1* (physical-attr match).
        # Canary 2026-05-16 D6/D7/D8: pre-guard had Canyon Ridge collapse
        # 17 distinct units into 4, Olympic 49 into 3.
        if ex_sig["uid"] and inc_sig["uid"]:
            return False
        return bool(ex_sig["fp_id"]) and ex_sig["fp_id"] == inc_sig["fp_id"]
    if rank == "R0a":
        return bool(ex_sig["uid"]) and ex_sig["uid"] == inc_sig["uid"]
    if rank == "R1a":
        return (
            bool(ex_sig["fp_name"]) and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
            and ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is not None and inc_sig["sqft"] is not None
            and ex_sig["sqft"] == inc_sig["sqft"]
        )
    if rank == "R1b":
        return (
            ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is not None and inc_sig["sqft"] is not None
            and ex_sig["sqft"] == inc_sig["sqft"]
        )
    if rank == "R1c":
        return (
            ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and bool(ex_sig["fp_name"]) and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
        )
    if rank == "R1d":
        return (
            ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and ex_sig["baths"] is not None and inc_sig["baths"] is not None
            and ex_sig["baths"] == inc_sig["baths"]
            and ex_sig["sqft"] is None and inc_sig["sqft"] is None
            and not (ex_sig["fp_name"] and inc_sig["fp_name"])
        )
    if rank == "R1e":
        return (
            ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and (ex_sig["baths"] is None or inc_sig["baths"] is None)
            and (ex_sig["sqft"] is None or inc_sig["sqft"] is None)
            and not (ex_sig["fp_name"] and inc_sig["fp_name"])
        )
    if rank == "R1f":
        return (
            ex_sig["beds"] is not None and inc_sig["beds"] is not None
            and ex_sig["beds"] == inc_sig["beds"]
            and bool(ex_sig["fp_name"]) and bool(inc_sig["fp_name"])
            and ex_sig["fp_name"] == inc_sig["fp_name"]
            and (ex_sig["baths"] is None or inc_sig["baths"] is None)
            and (ex_sig["sqft"] is None or inc_sig["sqft"] is None)
        )
    return False


# ── Observability (best-effort) ────────────────────────────────────────────────

def emit_physical_conflicts(
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
        pass


def emit_ambiguous_fail_closed(property_id: str, rank: str, candidate_count: int) -> None:
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


# ── Merge functions ────────────────────────────────────────────────────────────

def merge_field_values(
    target: dict[str, Any],
    source: dict[str, Any],
    *,
    rank: str,
    conflict_log: list[dict[str, Any]],
) -> None:
    """Merge ``source`` into ``target`` per H9 field-category rules."""
    for k, v in source.items():
        if k.startswith("_"):
            continue
        if v in (None, "", -1, "-1"):
            continue

        existing_val = target.get(k)
        existing_present = existing_val not in (None, "", -1, "-1")

        if k in MERGE_UNION_FIELDS:
            if isinstance(existing_val, list) or isinstance(v, list):
                merged: list[Any] = list(existing_val) if isinstance(existing_val, list) else (
                    [existing_val] if existing_present else []
                )
                seen: set[Any] = set()
                for x in merged:
                    try:
                        seen.add(x)
                    except TypeError:
                        pass
                for item in (v if isinstance(v, list) else [v]):
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

        if k in MERGE_MUTABLE_FIELDS:
            target[k] = v
            continue

        if k in MERGE_PHYSICAL_FIELDS:
            if not existing_present:
                target[k] = v
            elif existing_val != v:
                conflict_log.append({"field": k, "existing": existing_val, "new": v, "rank_used": rank})
            continue

        if not existing_present:
            target[k] = v


def availability_count_aware_merge(
    target: dict[str, Any],
    source: dict[str, Any],
    rank: str,
) -> None:
    """Sum availability_count at R0/R0a/R1a; latest-wins below."""
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


def merge_into_result_units(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    property_id: str = "unknown",
) -> list[dict[str, Any]]:
    """Anchor-first merge cascade (R0 → R1f).

    For each incoming record, walk the rank ladder and merge into the first
    uniquely-matched existing record. Multiple candidates at an ambiguity rank
    (H8) causes fail-closed: append instead of merge.
    """
    if not existing:
        return list(incoming)
    if not incoming:
        return list(existing)

    ex_sigs: list[dict[str, Any]] = [merge_rank_signature(u) for u in existing]
    result: list[dict[str, Any]] = list(existing)

    for inc in incoming:
        inc_sig = merge_rank_signature(inc)
        match_idx: int | None = None
        match_rank: str | None = None
        ambiguous = False
        ambiguous_rank: str | None = None
        ambiguous_count = 0

        for rank in RANK_LADDER:
            candidates: list[int] = [
                i for i, ex_sig in enumerate(ex_sigs) if rank_matches(rank, ex_sig, inc_sig)
            ]
            if len(candidates) == 1:
                match_idx = candidates[0]
                match_rank = rank
                break
            if len(candidates) > 1:
                if rank in AMBIGUITY_RANKS:
                    ambiguous = True
                    ambiguous_rank = rank
                    ambiguous_count = len(candidates)
                    break
                match_idx = candidates[0]
                match_rank = rank
                break

        if match_idx is not None and match_rank is not None and not ambiguous:
            target = result[match_idx]
            conflict_log: list[dict[str, Any]] = []
            availability_count_aware_merge(target, inc, match_rank)
            merge_field_values(target, inc, rank=match_rank, conflict_log=conflict_log)
            emit_physical_conflicts(property_id, match_idx, match_rank, conflict_log)
            ex_sigs[match_idx] = merge_rank_signature(target)
        else:
            if ambiguous and ambiguous_rank is not None:
                emit_ambiguous_fail_closed(property_id, ambiguous_rank, candidate_count=ambiguous_count)
            result.append(dict(inc))
            ex_sigs.append(merge_rank_signature(inc))

    return result


def aggregate_quality(mappings: list[Any]) -> float:
    """Min quality_score across contributing mappings; defaults to 1.0."""
    if not mappings:
        return 1.0
    qs = [float(getattr(m, "quality_score", 1.0) or 1.0) for m in mappings]
    return min(qs) if qs else 1.0


# ── Unit-list discovery ────────────────────────────────────────────────────────

def find_unit_list(body: Any) -> list[dict[str, Any]]:
    """Find a list of unit/floorplan dicts in an API response body."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body

    if isinstance(body, dict):
        for k in _LIST_KEYS:
            v = body.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for outer in ("data", "response", "result", "body"):
            nested = body.get(outer)
            if isinstance(nested, dict):
                for k in _LIST_KEYS:
                    v = nested.get(k)
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v
            if isinstance(nested, list) and nested and isinstance(nested[0], dict):
                return nested

    return []


def has_unit_signals(items: list[dict[str, Any]]) -> bool:
    """Return True when a sample of items has ≥2 signal keys with non-null values.

    E: field keys are normalised via the signal-engine alias table before
    intersection with ``_UNIT_SIGNAL_KEYS``.  This allows Entrata-specific
    names (no_of_bedroom, no_of_bathroom, square_footage) and camelCase
    variants (squareFeet, bedRooms) to match without every variant needing
    an explicit entry in ``_UNIT_SIGNAL_KEYS``.

    Previous check only verified key *names* on items[0]. A response like
    ``[{"name": "Hoboken", "minRent": null, "bedrooms": null}, ...]`` has 3
    matching key names but zero real data — the Nestiolistings locations
    endpoint that polluted 52 properties in the 2026-05-11 run.
    """
    if not items:
        return False
    sample = items[: min(5, len(items))]
    quorum = max(1, len(sample) // 2)  # majority of the sample

    try:
        from ma_poc.pms.signal_engine.floor_plan_signals import normalize_field_key as _nfk
    except Exception as _nfk_err:
        log.warning(
            "has_unit_signals: floor_plan_signals import failed — "
            "field-key normalisation disabled (Entrata/camelCase keys may miss): %s",
            _nfk_err,
        )
        _nfk = None  # type: ignore[assignment]

    items_with_values = 0
    for item in sample:
        if not isinstance(item, dict):
            continue
        if _nfk is not None:
            normalised: dict[str, Any] = {_nfk(k): v for k, v in item.items()}
        else:
            normalised = item
        valued_signal_keys = sum(
            1
            for k in (normalised.keys() & _UNIT_SIGNAL_KEYS)
            if normalised[k] not in (None, "", 0)
        )
        if valued_signal_keys >= 2:
            items_with_values += 1

    return items_with_values >= quorum
