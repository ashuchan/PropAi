"""Fail-closed scoping for verified multi-community inventory collections."""

from __future__ import annotations

import copy
import re
from dataclasses import FrozenInstanceError, dataclass, replace
from typing import Any


@dataclass(frozen=True)
class CollectionScopeRule:
    property_id: str
    property_name: str
    allowed_plan_prefixes: tuple[str, ...]


_RULES: dict[str, CollectionScopeRule] = {
    rule.property_id: rule
    for rule in (
        CollectionScopeRule("264077", "Novi Flats", ("Flats",)),
        CollectionScopeRule("78783", "Link 480", ("Link",)),
        CollectionScopeRule("98191", "Timber", ("Timber",)),
    )
}


def collection_scope_rule(property_id: Any) -> CollectionScopeRule | None:
    return _RULES.get(str(property_id or "").strip())


def _plan_label(row: dict[str, Any]) -> str:
    for key in (
        "floor_plan_name",
        "floorplan_name",
        "floor_plan_type",
        "floorplan",
        "plan_name",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _matches_prefix(label: str, prefixes: tuple[str, ...]) -> bool:
    normalised = " ".join(label.casefold().split())
    return any(
        re.match(rf"^{re.escape(prefix.casefold())}(?:\s|[-|:/])", normalised)
        or normalised == prefix.casefold()
        for prefix in prefixes
    )


def apply_collection_scope(
    rows: list[dict[str, Any]],
    *,
    property_id: Any,
    tier: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Keep only the configured subcommunity on verified collection routes.

    The rule applies only to RentCafe/SecureCafe output.  If a configured
    collection contains rows but none match, it fails closed by returning an
    empty list instead of silently shipping a sibling community.
    """

    rule = collection_scope_rule(property_id)
    if rule is None or not rows:
        return rows, 0
    tier_upper = str(tier or "").upper()
    if "RENTCAFE" not in tier_upper and "SECURECAFE" not in tier_upper:
        return rows, 0

    kept = [
        copy.deepcopy(row) for row in rows if _matches_prefix(_plan_label(row), rule.allowed_plan_prefixes)
    ]
    if not kept:
        return [], len(rows)
    return kept, len(rows) - len(kept)


def _physical_unit_key(row: dict[str, Any]) -> str:
    """Return the public apartment label used to collapse cross-feed aliases."""

    if row.get("is_floor_plan_level") is True:
        return ""
    for key in ("unit_number", "unit_name"):
        value = " ".join(str(row.get(key) or "").casefold().split())
        if value:
            return value
    return ""


def _row_quality(row: dict[str, Any]) -> tuple[int, int]:
    """Prefer the richer marketing record while preserving stable input order."""

    rent_present = any(
        row.get(key) not in (None, "")
        for key in ("market_rent_low", "rent_low", "market_rent_high", "rent_high", "rent_range")
    )
    source_ids = row.get("source_ids")
    return (
        int(rent_present) * 8
        + int(isinstance(source_ids, dict) and bool(source_ids)) * 4
        + int(bool(row.get("source_property_id"))) * 2
        + int(not row.get("_inferred")),
        len([value for value in row.values() if value not in (None, "", [], {})]),
    )


def _merge_scoped_aliases(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge rows known to describe one apartment on a scoped collection.

    Collection sites can publish the same apartment through SecureCafe and a
    marketing-site feed with different provider IDs.  The marketing row is
    normally richer (notably rent and provider-native identifiers), while the
    SecureCafe available-units response is authoritative for current status
    and move-in date.  Keep both pieces of evidence in one physical row.
    """

    best_index = max(range(len(rows)), key=lambda index: (_row_quality(rows[index]), -index))
    merged = copy.deepcopy(rows[best_index])

    for row in rows:
        for key, value in row.items():
            if merged.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                merged[key] = copy.deepcopy(value)

    # SecureCafe is the live available-units authority for volatile fields.
    securecafe_rows = [
        row
        for row in rows
        if any(token in str(row.get("extraction_tier") or "").upper() for token in ("RENTCAFE", "SECURECAFE"))
    ]
    for key in ("availability_status", "available_date", "availability_date", "move_in_date"):
        for row in securecafe_rows:
            value = row.get(key)
            if value not in (None, ""):
                merged[key] = copy.deepcopy(value)
                break

    combined_source_ids: dict[str, Any] = {}
    aliases: list[str] = []
    for row in rows:
        source_ids = row.get("source_ids")
        if isinstance(source_ids, dict):
            for key, value in source_ids.items():
                if key not in combined_source_ids and value not in (None, ""):
                    combined_source_ids[key] = copy.deepcopy(value)
        for candidate in (row.get("unit_id"), row.get("source_unit_id")):
            value = str(candidate or "").strip()
            if value and value != str(merged.get("unit_id") or "").strip() and value not in aliases:
                aliases.append(value)
    if combined_source_ids:
        merged["source_ids"] = combined_source_ids
    if aliases:
        merged["unit_id_aliases"] = aliases
    return merged


def _dedupe_scoped_units(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    output: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        key = _physical_unit_key(row)
        if not key:
            output.append((index, copy.deepcopy(row)))
            continue
        grouped.setdefault(key, []).append(row)

    seen: set[str] = set()
    for index, row in enumerate(rows):
        key = _physical_unit_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append((index, _merge_scoped_aliases(grouped[key])))

    output.sort(key=lambda item: item[0])
    deduped = [row for _index, row in output]
    return deduped, len(rows) - len(deduped)


def _sync_extract_records(result: dict[str, Any], units: list[dict[str, Any]]) -> None:
    extract_result = result.get("_extract_result")
    if extract_result is None or not hasattr(extract_result, "records"):
        return
    try:
        extract_result.records = units
    except FrozenInstanceError:
        result["_extract_result"] = replace(extract_result, records=units)


def apply_collection_scope_to_result(
    result: dict[str, Any],
    *,
    property_id: Any,
) -> dict[str, int]:
    """Apply the audited boundary at the final, shared result boundary.

    This is deliberately downstream of direct shortcuts and link-hop roster
    reconciliation.  Earlier adapter-level filtering remains useful, but it
    cannot protect paths that bypass the adapter or rows reintroduced by a
    later cross-page merge.
    """

    stats = {"units_dropped": 0, "plans_dropped": 0, "aliases_deduped": 0}
    rule = collection_scope_rule(property_id)
    if rule is None:
        return stats

    tier = str(result.get("extraction_tier_used") or "")
    tier_upper = tier.upper()
    if "RENTCAFE" not in tier_upper and "SECURECAFE" not in tier_upper:
        return stats

    try:
        units, stats["units_dropped"] = apply_collection_scope(
            [row for row in (result.get("units") or []) if isinstance(row, dict)],
            property_id=property_id,
            tier=tier,
        )
        plans, stats["plans_dropped"] = apply_collection_scope(
            [row for row in (result.get("plan_summaries") or []) if isinstance(row, dict)],
            property_id=property_id,
            tier=tier,
        )
        units, stats["aliases_deduped"] = _dedupe_scoped_units(units)
        result["units"] = units
        if "plan_summaries" in result or plans:
            result["plan_summaries"] = plans

        _sync_extract_records(result, units)

        if any(stats.values()):
            errors = result.setdefault("errors", [])
            if isinstance(errors, list):
                errors.append(
                    "COLLECTION_PROPERTY_SCOPE_FINAL_APPLIED: "
                    f"units_dropped={stats['units_dropped']} "
                    f"plans_dropped={stats['plans_dropped']} "
                    f"aliases_deduped={stats['aliases_deduped']}"
                )
    except Exception as exc:
        # A configured RentCafe boundary is a release invariant. Unexpected
        # row shapes fail closed instead of shipping another community.
        result["units"] = []
        result["plan_summaries"] = []
        _sync_extract_records(result, [])
        errors = result.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append(f"COLLECTION_PROPERTY_SCOPE_FINAL_ERROR: {type(exc).__name__}: {str(exc)[:120]}")
    return stats
