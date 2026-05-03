"""
Pure merger — combines multiple ExtractedSources into provenanced units.

INVARIANTS (test-enforced — see Phase 3 gate):
  - Pure function: same inputs always produce same output
  - No I/O, no logging side-effects, no profile mutation
  - No source can trigger another source — input set is fixed before call
  - Inputs are not mutated

Identity-link rules (strict precedence):
  1. unit_id equal across sources
  2. (floor_plan_name, beds, baths) equal AND one side has no unit_id
  3. (beds, baths, sqft_rounded_to_10) equal — fuzzy, callback-gated
  4. None — keep records separate
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any, Callable

from ma_poc.models.source import (
    ExtractedSource,
    FieldValue,
    ProvenancedUnit,
    SourceId,
)
from ma_poc.services.source_planner import CONFIDENCE_FLOORS, FIELD_GROUP


def merge_sources(
    sources: Iterable[ExtractedSource],
    property_id: str,
    fuzzy_link_callback: Callable[[ProvenancedUnit, tuple, float], None] | None = None,
) -> list[ProvenancedUnit]:
    """Merge sources into provenanced units. See module docstring for rules."""
    sources_list = list(sources)
    if not sources_list:
        return []

    # Defensive deep-copy of source units so mutations during merging never
    # leak back to callers. Inputs must remain untouched (purity invariant H1).
    src_units_snapshot: list[tuple[ExtractedSource, list[ProvenancedUnit]]] = []
    for src in sources_list:
        unit_copies: list[ProvenancedUnit] = []
        for unit in src.units:
            pu = ProvenancedUnit()
            for k, v in unit.items():
                if isinstance(v, FieldValue):
                    pu[k] = v.model_copy(deep=True)
                else:
                    pu[k] = copy.deepcopy(v)
            unit_copies.append(pu)
        src_units_snapshot.append((src, unit_copies))

    # buckets: bucket_key -> list of (SourceId, ProvenancedUnit, rank)
    buckets: dict[tuple, list[tuple[SourceId, ProvenancedUnit, int]]] = {}
    alone_counter = 0

    for src, unit_copies in src_units_snapshot:
        for unit in unit_copies:
            uid_fv = unit.get("unit_id") or unit.get("unit_number")
            # Rank 1: unit_id equality
            if src.has_unit_ids and isinstance(uid_fv, FieldValue) and uid_fv.value:
                key = ("uid", str(uid_fv.value).strip())
                buckets.setdefault(key, []).append((src.source_id, unit, 1))
                continue
            # Rank 2: floor-plan-level fan-out
            if src.is_floor_plan_level:
                fp_fv = unit.get("floor_plan_name")
                beds_fv = unit.get("beds") or unit.get("bedrooms")
                baths_fv = unit.get("baths") or unit.get("bathrooms")
                if isinstance(fp_fv, FieldValue) and fp_fv.value:
                    key = (
                        "fp",
                        str(fp_fv.value).strip().lower(),
                        _fv_str(beds_fv),
                        _fv_str(baths_fv),
                    )
                    buckets.setdefault(key, []).append((src.source_id, unit, 2))
                    continue
            # Rank 3: fuzzy by (beds, baths, sqft_bucket) — only if caller opts in
            if fuzzy_link_callback is not None:
                beds_fv = unit.get("beds") or unit.get("bedrooms")
                baths_fv = unit.get("baths") or unit.get("bathrooms")
                sqft_fv = unit.get("sqft")
                if all(
                    isinstance(x, FieldValue) and x.value not in (None, "", -1, "-1")
                    for x in (beds_fv, baths_fv, sqft_fv)
                ):
                    try:
                        sqft_int = int(float(str(sqft_fv.value)))
                    except (TypeError, ValueError):
                        sqft_int = 0
                    sqft_bucket = (sqft_int // 10) * 10
                    key = (
                        "fuzzy",
                        _fv_str(beds_fv),
                        _fv_str(baths_fv),
                        str(sqft_bucket),
                    )
                    buckets.setdefault(key, []).append((src.source_id, unit, 3))
                    fuzzy_link_callback(unit, key, 0.6)
                    continue
            # No identity → keep alone
            alone_key = ("alone", alone_counter)
            alone_counter += 1
            buckets[alone_key] = [(src.source_id, unit, 4)]

    # Group buckets by key category for ordered processing
    uid_buckets = {k: v for k, v in buckets.items() if k[0] == "uid"}
    fp_buckets = {k: v for k, v in buckets.items() if k[0] == "fp"}
    other_buckets = {k: v for k, v in buckets.items() if k[0] in ("fuzzy", "alone")}

    out_units: list[ProvenancedUnit] = []
    # absorbed_fps tracks (fp_name_lower, beds_str, baths_str) triples that
    # were pulled into a rank-1 unit bucket so Pass 2 doesn't emit them again.
    absorbed_fps: set[tuple[str, str, str]] = set()

    # Pass 1: rank-1 (unit_id) buckets, optionally pulling in matching rank-2 (floor-plan) entries
    for _key, entries in uid_buckets.items():
        fp_name = _extract_fp_name(entries)
        merged_entries = list(entries)
        if fp_name:
            beds_s, baths_s = _unit_beds_baths(entries)
            for fp_key, fp_entries in fp_buckets.items():
                # fp_key = ("fp", fp_name_lower, beds_str, baths_str)
                if fp_key[1] != fp_name:
                    continue
                # Absorb only when beds/baths agree (or rank-1 didn't supply them)
                if beds_s and fp_key[2] and beds_s != fp_key[2]:
                    continue
                if baths_s and fp_key[3] and baths_s != fp_key[3]:
                    continue
                merged_entries.extend(fp_entries)
                absorbed_fps.add((fp_key[1], fp_key[2], fp_key[3]))
        out_units.append(_merge_field_max_confidence(merged_entries))

    # Pass 2: rank-2 floor-plan-only buckets that weren't absorbed
    for fp_key, fp_entries in fp_buckets.items():
        if (fp_key[1], fp_key[2], fp_key[3]) in absorbed_fps:
            continue
        out_units.append(_merge_field_max_confidence(fp_entries))

    # Pass 3: fuzzy + alone buckets
    for _key, entries in other_buckets.items():
        out_units.append(_merge_field_max_confidence(entries))

    # Pass 4: identity fallback for units missing unit_id
    for unit in out_units:
        uid_fv = unit.get("unit_id")
        has_uid = isinstance(uid_fv, FieldValue) and uid_fv.value not in (None, "", -1, "-1")
        if has_uid:
            continue
        legacy = {
            k: v.value for k, v in unit.items() if isinstance(v, FieldValue)
        }
        try:
            from scripts.identity_fallback import compute_fallback_unit_id
            fb = compute_fallback_unit_id(legacy, property_id)
        except Exception:
            fb = None
        if fb:
            unit["unit_id"] = FieldValue(
                value=fb,
                source=SourceId.IDENTITY_FALLBACK,
                confidence=0.65,
            )

    out_units.sort(key=_unit_sort_key)
    return out_units


def _fv_str(fv: Any) -> str:
    """Stringify a FieldValue's value (or empty string for non-FV)."""
    if isinstance(fv, FieldValue) and fv.value is not None:
        return str(fv.value).strip().lower()
    return ""


def _unit_beds_baths(entries: list[tuple[SourceId, ProvenancedUnit, int]]) -> tuple[str, str]:
    """Extract (beds_str, baths_str) from rank-1 unit entries.
    Used to disambiguate which fan-out floor-plan bucket to absorb."""
    for _src, unit, _rank in entries:
        beds_fv = unit.get("beds") or unit.get("bedrooms")
        baths_fv = unit.get("baths") or unit.get("bathrooms")
        beds_s = _fv_str(beds_fv) if beds_fv else ""
        baths_s = _fv_str(baths_fv) if baths_fv else ""
        if beds_s or baths_s:
            return (beds_s, baths_s)
    return ("", "")


def _extract_fp_name(entries: list[tuple[SourceId, ProvenancedUnit, int]]) -> str | None:
    """If any entry has a floor_plan_name, return its lowercased value."""
    for _src, unit, _rank in entries:
        fp = unit.get("floor_plan_name")
        if isinstance(fp, FieldValue) and fp.value:
            return str(fp.value).strip().lower()
    return None


def _merge_field_max_confidence(
    entries: list[tuple[SourceId, ProvenancedUnit, int]],
) -> ProvenancedUnit:
    """Pick max-confidence FieldValue per field, respecting confidence floors."""
    out = ProvenancedUnit()
    all_fields: set[str] = set()
    for _src, unit, _rank in entries:
        all_fields.update(unit.keys())
    for field_name in all_fields:
        best: FieldValue | None = None
        for _src, unit, _rank in entries:
            fv = unit.get(field_name)
            if not isinstance(fv, FieldValue):
                continue
            group = FIELD_GROUP.get(field_name)
            if group is not None and fv.confidence < CONFIDENCE_FLOORS[group]:
                continue
            if best is None or fv.confidence > best.confidence:
                best = fv
        if best is not None:
            out[field_name] = best
    return out


def _unit_sort_key(unit: ProvenancedUnit) -> tuple:
    """Stable sort key: unit_id first, then floor_plan_name, then ''."""
    uid = unit.get("unit_id")
    fp = unit.get("floor_plan_name")
    uid_str = str(uid.value) if isinstance(uid, FieldValue) and uid.value else ""
    fp_str = str(fp.value) if isinstance(fp, FieldValue) and fp.value else ""
    return (uid_str, fp_str)
