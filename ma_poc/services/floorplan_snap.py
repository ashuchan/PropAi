"""Post-extraction CSV snap.

Resolves an extracted unit's identity to a canonical row in
``Floorplan-comparisons.csv`` whenever possible. Records that snap
successfully receive a deterministic ``floor_plan_id`` and have their
``floor_plan_name`` overwritten with the canonical description (the
LLM-emitted name is preserved for audit in
``floor_plan_name_extracted``).

Hard invariants this module enforces (see CLAUDE_PROMPTS_MERGE_RESILIENCE.md):
    H2  sqft equality is strict — strict-sqft tolerance is enforced inside
        FloorplanCatalog.snap and never weakened here.
    H4  Snap runs *after* LLM extraction. Records that fail to snap are
        annotated and returned untouched — never dropped.
    H8  The catalog returns ``multiple_candidates_fail_closed`` when the
        ambiguity cannot be resolved; this module respects that and leaves
        the record un-snapped.
"""

from __future__ import annotations

import logging
from typing import Any

from ma_poc.services.floorplan_catalog import (
    SNAP_REASON_LOW_COVERAGE,
    SNAP_REASON_MULTI_FAIL_CLOSED,
    SNAP_REASON_NO_MATCH,
    FloorplanCatalog,
    SnapResult,
)

log = logging.getLogger(__name__)

_NOOP_REASONS = frozenset(
    {
        SNAP_REASON_LOW_COVERAGE,
        SNAP_REASON_MULTI_FAIL_CLOSED,
        SNAP_REASON_NO_MATCH,
    }
)


def _coerce_optional_int(v: Any) -> int | None:
    if v is None or v == "" or v == -1 or (isinstance(v, str) and v.strip() in ("", "-1")):
        return None
    try:
        out = int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None
    # Treat the area-sentinel value as null in either int or string form (H2).
    return None if out == -1 else out


def _coerce_optional_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _read_unit_field(unit: dict[str, Any], *keys: str) -> Any:
    """Return the first non-empty value across ``keys``."""
    for k in keys:
        v = unit.get(k)
        if v is not None and v != "":
            return v
    return None


def snap_units(
    units: list[dict[str, Any]],
    property_id: str,
    catalog: FloorplanCatalog | None = None,
) -> list[dict[str, Any]]:
    """Apply CSV snap to every unit. Returns mutated copies.

    For each unit:
      - Coerce extracted beds/baths/sqft into the types ``FloorplanCatalog`` expects.
      - Call ``catalog.snap(...)``.
      - On hit: overwrite floor_plan_name with the canonical description,
        preserve the LLM's name in ``floor_plan_name_extracted``, set
        ``floor_plan_id``, and tag the snap reason / confidence.
      - On miss / fail-closed / bypass: leave fields untouched but tag
        ``floor_plan_snap_reason`` for downstream observability (H4).

    The returned list is a fresh list of fresh dicts — the input list and
    its dicts are never mutated in place. This keeps callers that hold the
    pre-snap reference (e.g. for source-merger provenance) safe.
    """
    if not units:
        return list(units)

    if catalog is None:
        from ma_poc.services.floorplan_catalog import get_default_catalog

        catalog = get_default_catalog()

    out: list[dict[str, Any]] = []
    for unit in units:
        out.append(_snap_one(unit, property_id, catalog))
    return out


def _snap_one(
    unit: dict[str, Any],
    property_id: str,
    catalog: FloorplanCatalog,
) -> dict[str, Any]:
    """Snap a single unit. Returns a fresh dict; never mutates input."""
    new_unit = dict(unit)

    # Read identity fields from any of the v1/v2 alias slots that
    # adapters and the LLM normalizer use.
    beds = _coerce_optional_int(_read_unit_field(unit, "beds", "bedrooms", "_bedrooms"))
    baths = _coerce_optional_float(_read_unit_field(unit, "baths", "bathrooms", "_bathrooms"))
    sqft = _coerce_optional_int(_read_unit_field(unit, "sqft", "area", "_sqft"))
    hint = _read_unit_field(unit, "floor_plan_name", "_floor_plan", "floorplan_name")

    result: SnapResult = catalog.snap(
        property_id=property_id,
        beds=beds,
        baths=baths,
        sqft=sqft,
        floor_plan_name_hint=str(hint) if hint else None,
    )

    new_unit["floor_plan_snap_reason"] = result.reason
    new_unit["floor_plan_snap_confidence"] = result.confidence

    if result.plan is None or result.reason in _NOOP_REASONS:
        # H4: leave the record alone; it falls through to the merge cascade.
        return new_unit

    plan = result.plan
    # Preserve the LLM/page-emitted name verbatim for audit.
    extracted_name = unit.get("floor_plan_name") or unit.get("_floor_plan") or unit.get("floorplan_name")
    if extracted_name is not None:
        new_unit["floor_plan_name_extracted"] = extracted_name

    # 2026-07-25 — THE OPERATOR'S MARKETING NAME WINS.
    #
    # This line used to be an unconditional `= plan.description`, overwriting
    # the name the operator publishes with the `description` column of
    # config/Floorplan-comparisons.csv. Measured on the 2026-07-25 5k run:
    # 65,169 units (73.3%) shipped a CSV description instead of the site's own
    # name, and 8,135 of the 10,523 "bad" plan names were this overwrite rather
    # than a capture gap — including every one of the 5,910 "NBD/NBR-N" labels,
    # a format nothing in this repo generates.
    #
    # It is not merely cosmetic: it MERGES DISTINCT PRODUCTS. Laurel Oaks
    # publishes "2x2 Upstairs Granite", "2x2 D Brown Cabinets" and "2x2 Down
    # End White Cabinets" as separate plans; all three snapped onto the single
    # CSV row "2BD/2BR-2", so plan-level rent comparison silently averaged
    # non-equivalent inventory. The CSV is also 11.7% unusable (blank, generic,
    # or NBD/NBR-N rows), so it is a weaker source than the live page.
    #
    # The catalog KEYS (floor_plan_id / floorplannumber / apartmentid) are
    # still adopted — that join is the point of the snap and is unaffected.
    # Only the human-facing NAME now prefers what the operator published.
    _extracted_str = str(extracted_name).strip() if extracted_name is not None else ""
    gaps = unit.get("data_gaps")
    provider_declares_name_gap = isinstance(gaps, (list, tuple, set)) and any(
        str(gap).strip().casefold() == "floor_plan_name" for gap in gaps
    )
    if _extracted_str:
        new_unit["floor_plan_name"] = _extracted_str
        new_unit["floor_plan_name_catalog"] = plan.description
    elif provider_declares_name_gap:
        # A provider-specific adapter can prove that its native unit roster
        # publishes no floor-plan name.  The CSV match is still useful for a
        # stable floor_plan_id, but exposing its description as though the
        # operator published it launders a dimension-based inference into
        # source data.  Keep the catalog label explicitly catalog-scoped.
        new_unit["floor_plan_name"] = ""
        new_unit["floor_plan_name_catalog"] = plan.description
        new_unit["floor_plan_snap_name_suppressed"] = True
    else:
        new_unit["floor_plan_name"] = plan.description
    new_unit["floor_plan_id"] = plan.floor_plan_id
    new_unit["floorplannumber"] = plan.floorplannumber
    new_unit["apartmentid"] = plan.apartmentid

    log.debug(
        "floorplan_snap: property=%s reason=%s plan_id=%s",
        property_id,
        result.reason,
        plan.floor_plan_id,
    )
    return new_unit
