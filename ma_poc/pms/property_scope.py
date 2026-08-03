"""Fail-closed scoping for verified multi-community inventory collections."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
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

    kept = [copy.deepcopy(row) for row in rows if _matches_prefix(_plan_label(row), rule.allowed_plan_prefixes)]
    if not kept:
        return [], len(rows)
    return kept, len(rows) - len(kept)
