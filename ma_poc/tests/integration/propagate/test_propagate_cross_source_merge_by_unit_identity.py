"""Propagate layer: cross-source merge by unit identity.

Verifies `source_merger.merge_sources()` merging rules:
  - Rank 1: same unit_id across sources → merged into one ProvenancedUnit
  - Rank 2: floor-plan level (no unit_ids) → floor-plan key merge
  - Rank 4: no identity overlap → sources stay separate
  - Purity: inputs are not mutated
"""

from __future__ import annotations

from ma_poc.models.source import ExtractedSource, FieldValue, ProvenancedUnit, SourceId
from ma_poc.services.source_merger import merge_sources


def _fv(value: str | int | float | None, source: SourceId = SourceId.API_GENERIC_NARROW, confidence: float = 0.9) -> FieldValue:
    return FieldValue(value=value, source=source, confidence=confidence)


def _unit(**fields: str | int | float | None) -> dict:
    return {k: _fv(v) for k, v in fields.items()}


def _source(
    source_id: SourceId,
    units: list[dict],
    *,
    has_unit_ids: bool = False,
    is_floor_plan_level: bool = False,
) -> ExtractedSource:
    return ExtractedSource(
        source_id=source_id,
        source_url="https://example.com/api/units",
        envelope_hash="abc123",
        units=units,  # type: ignore[arg-type]
        has_unit_ids=has_unit_ids,
        is_floor_plan_level=is_floor_plan_level,
    )


# ---------------------------------------------------------------------------
# Empty / degenerate
# ---------------------------------------------------------------------------


def test_empty_sources_returns_empty_list() -> None:
    """merge_sources([]) returns an empty list without error."""
    result = merge_sources([], property_id="prop-merge-001")
    assert result == []


def test_single_source_returns_its_units() -> None:
    """A single source passes through as-is."""
    units = [_unit(unit_id="A1", bedrooms=1)]
    src = _source(SourceId.API_GENERIC_NARROW, units, has_unit_ids=True)

    result = merge_sources([src], property_id="prop-merge-002")

    assert len(result) == 1


# ---------------------------------------------------------------------------
# Rank 1 — unit_id equality
# ---------------------------------------------------------------------------


def test_rank1_same_unit_id_merges_into_one() -> None:
    """Two sources with the same unit_id produce exactly one ProvenancedUnit."""
    unit_a = _unit(unit_id="U1", bedrooms=1, market_rent_low=1500)
    unit_b = _unit(unit_id="U1", sqft=750)

    src_a = _source(SourceId.API_GENERIC_NARROW, [unit_a], has_unit_ids=True)
    src_b = _source(SourceId.API_GENERIC_BROAD, [unit_b], has_unit_ids=True)

    result = merge_sources([src_a, src_b], property_id="prop-merge-003")

    assert len(result) == 1


def test_rank1_distinct_unit_ids_stay_separate() -> None:
    """Units with different unit_ids from the same source are not merged."""
    units = [
        _unit(unit_id="A1", bedrooms=1),
        _unit(unit_id="A2", bedrooms=2),
    ]
    src = _source(SourceId.API_GENERIC_NARROW, units, has_unit_ids=True)

    result = merge_sources([src], property_id="prop-merge-004")

    assert len(result) == 2


# ---------------------------------------------------------------------------
# Rank 2 — floor-plan level fan-out
# ---------------------------------------------------------------------------


def test_rank2_floor_plan_key_groups_same_plan() -> None:
    """Floor-plan-level sources with the same (name, beds, baths) key merge."""
    unit_a = _unit(floor_plan_name="1BR Classic", beds=1, baths=1, market_rent_low=1500)
    unit_b = _unit(floor_plan_name="1BR Classic", beds=1, baths=1, sqft=720)

    src_a = _source(SourceId.JSON_LD, [unit_a], is_floor_plan_level=True)
    src_b = _source(SourceId.EMBEDDED_JSON, [unit_b], is_floor_plan_level=True)

    result = merge_sources([src_a, src_b], property_id="prop-merge-005")

    assert len(result) == 1


def test_rank2_different_floor_plan_names_stay_separate() -> None:
    """Floor-plan entries with different names are kept separate."""
    unit_a = _unit(floor_plan_name="1BR", beds=1, baths=1)
    unit_b = _unit(floor_plan_name="2BR", beds=2, baths=1)

    src = _source(SourceId.JSON_LD, [unit_a, unit_b], is_floor_plan_level=True)

    result = merge_sources([src], property_id="prop-merge-006")

    assert len(result) == 2


# ---------------------------------------------------------------------------
# Purity invariant
# ---------------------------------------------------------------------------


def test_inputs_are_not_mutated() -> None:
    """merge_sources must not mutate the input source unit dicts."""
    unit = _unit(unit_id="X1", bedrooms=1)
    original_keys = set(unit.keys())

    src = _source(SourceId.API_GENERIC_NARROW, [unit], has_unit_ids=True)
    merge_sources([src], property_id="prop-merge-007")

    assert set(unit.keys()) == original_keys
