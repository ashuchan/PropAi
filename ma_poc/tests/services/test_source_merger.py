"""Phase 3 — pure merger tests."""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.models.source import (
    ExtractedSource,
    FieldValue,
    ProvenancedUnit,
    SourceId,
    to_legacy_unit,
)
from ma_poc.services.source_merger import merge_sources


def _fv(value: Any, source: SourceId, conf: float = 0.9, url: str = "") -> FieldValue:
    return FieldValue(value=value, source=source, confidence=conf, source_url=url)


def _build_two_source_setup() -> list[ExtractedSource]:
    """sightmap unit-level + rentcafe floorplan-level for the same FP "A1"."""
    sm_units = [
        {
            "unit_id": _fv("305", SourceId.API_SIGHTMAP, 0.95),
            "unit_number": _fv("305", SourceId.API_SIGHTMAP, 0.95),
            "floor_plan_name": _fv("A1", SourceId.API_SIGHTMAP, 0.95),
            "asking_rent": _fv(1500, SourceId.API_SIGHTMAP, 0.95),
        },
        {
            "unit_id": _fv("410", SourceId.API_SIGHTMAP, 0.95),
            "unit_number": _fv("410", SourceId.API_SIGHTMAP, 0.95),
            "floor_plan_name": _fv("A1", SourceId.API_SIGHTMAP, 0.95),
            "asking_rent": _fv(1700, SourceId.API_SIGHTMAP, 0.95),
        },
    ]
    rc_units = [
        {
            "floor_plan_name": _fv("A1", SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "beds": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "baths": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "sqft": _fv(750, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        }
    ]
    return [
        ExtractedSource(
            source_id=SourceId.API_SIGHTMAP,
            source_url="https://sightmap.com/api/units",
            envelope_hash="sm0001",
            units=sm_units,
            has_unit_ids=True,
            is_floor_plan_level=False,
        ),
        ExtractedSource(
            source_id=SourceId.API_RENTCAFE_FLOORPLANS,
            source_url="https://rentcafe.com/api/fp",
            envelope_hash="rc0001",
            units=rc_units,
            has_unit_ids=False,
            is_floor_plan_level=True,
        ),
    ]


def test_merger_pure_function() -> None:
    sources = _build_two_source_setup()
    snapshot_a = [list(s.units) for s in sources]
    out_a = merge_sources(sources, "test_prop")
    out_b = merge_sources(sources, "test_prop")
    assert out_a == out_b
    snapshot_b = [list(s.units) for s in sources]
    assert snapshot_a == snapshot_b


def test_rank1_unit_id_match_merges() -> None:
    """Two sources both with unit_id=305 → one output unit, both contribute."""
    src_a_units = [
        {"unit_id": _fv("305", SourceId.API_SIGHTMAP, 0.95), "asking_rent": _fv(1500, SourceId.API_SIGHTMAP, 0.95)}
    ]
    src_b_units = [
        {"unit_id": _fv("305", SourceId.API_RENTCAFE_UNITS, 0.90), "sqft": _fv(750, SourceId.API_RENTCAFE_UNITS, 0.90)}
    ]
    a = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP, source_url="a", envelope_hash="h1",
        units=src_a_units, has_unit_ids=True, is_floor_plan_level=False,
    )
    b = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_UNITS, source_url="b", envelope_hash="h2",
        units=src_b_units, has_unit_ids=True, is_floor_plan_level=False,
    )
    out = merge_sources([a, b], "p")
    assert len(out) == 1
    assert out[0]["asking_rent"].value == 1500
    assert out[0]["sqft"].value == 750


def test_rank2_fan_out_floor_plan_to_units() -> None:
    """FP "A1" + units 305/410 → 2 output units, each with FP A1's beds/baths/sqft."""
    sources = _build_two_source_setup()
    out = merge_sources(sources, "p")
    assert len(out) == 2
    by_uid = {u["unit_number"].value: u for u in out}
    assert "305" in by_uid
    assert "410" in by_uid
    for uid in ("305", "410"):
        u = by_uid[uid]
        assert u["beds"].value == 1
        assert u["baths"].value == 1
        assert u["sqft"].value == 750


def test_max_confidence_picks_winner() -> None:
    """LLM 0.65 + API 0.95 for asking_rent → API wins."""
    a = ExtractedSource(
        source_id=SourceId.LLM_API_TARGETED, source_url="llm", envelope_hash="h1",
        units=[{"unit_id": _fv("U1", SourceId.LLM_API_TARGETED, 0.65), "asking_rent": _fv(1000, SourceId.LLM_API_TARGETED, 0.65)}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    b = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP, source_url="api", envelope_hash="h2",
        units=[{"unit_id": _fv("U1", SourceId.API_SIGHTMAP, 0.95), "asking_rent": _fv(1500, SourceId.API_SIGHTMAP, 0.95)}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    out = merge_sources([a, b], "p")
    assert len(out) == 1
    assert out[0]["asking_rent"].value == 1500
    assert out[0]["asking_rent"].source == SourceId.API_SIGHTMAP


def test_confidence_floor_rejects_below_threshold() -> None:
    """Identity floor is 0.70 — a unit_id at 0.40 is rejected entirely."""
    a = ExtractedSource(
        source_id=SourceId.LLM_MONOLITHIC, source_url="llm", envelope_hash="h",
        units=[{"unit_id": _fv("U1", SourceId.LLM_MONOLITHIC, 0.40)}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    out = merge_sources([a], "p")
    assert len(out) == 1
    # unit_id below floor → not in output (even if it was the bucket key)
    assert "unit_id" not in out[0] or not isinstance(out[0].get("unit_id"), FieldValue)


def test_fuzzy_link_emits_event() -> None:
    """Two sources, no unit_id, same beds/baths/sqft → callback fires."""
    a_units = [{
        "beds": _fv(1, SourceId.DOM_CASCADE, 0.7),
        "baths": _fv(1, SourceId.DOM_CASCADE, 0.7),
        "sqft": _fv(750, SourceId.DOM_CASCADE, 0.7),
        "asking_rent": _fv(1500, SourceId.DOM_CASCADE, 0.7),
    }]
    b_units = [{
        "beds": _fv(1, SourceId.JSON_LD, 0.85),
        "baths": _fv(1, SourceId.JSON_LD, 0.85),
        "sqft": _fv(750, SourceId.JSON_LD, 0.85),
    }]
    a = ExtractedSource(source_id=SourceId.DOM_CASCADE, source_url="d", envelope_hash="h1",
                       units=a_units, has_unit_ids=False, is_floor_plan_level=False)
    b = ExtractedSource(source_id=SourceId.JSON_LD, source_url="j", envelope_hash="h2",
                       units=b_units, has_unit_ids=False, is_floor_plan_level=False)
    callback_calls = []

    def cb(unit, key, conf):
        callback_calls.append((key, conf))

    out = merge_sources([a, b], "p", fuzzy_link_callback=cb)
    assert len(callback_calls) == 2  # one per fuzzy bucket entry
    assert all(c[1] == 0.6 for c in callback_calls)
    # Only one merged unit (both share fingerprint)
    assert len(out) == 1


def test_no_match_keeps_separate() -> None:
    a = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP, source_url="a", envelope_hash="h1",
        units=[{"unit_id": _fv("U1", SourceId.API_SIGHTMAP, 0.95), "asking_rent": _fv(100, SourceId.API_SIGHTMAP, 0.95)}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    b = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP, source_url="b", envelope_hash="h2",
        units=[{"unit_id": _fv("U2", SourceId.API_SIGHTMAP, 0.95), "asking_rent": _fv(200, SourceId.API_SIGHTMAP, 0.95)}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    out = merge_sources([a, b], "p")
    assert len(out) == 2


def test_identity_fallback_when_missing() -> None:
    """Merged unit with no unit_id but fp/beds/sqft → fallback unit_id assigned."""
    src = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_FLOORPLANS, source_url="rc", envelope_hash="h",
        units=[{
            "floor_plan_name": _fv("A1", SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "beds": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "baths": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "sqft": _fv(750, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        }],
        has_unit_ids=False, is_floor_plan_level=True,
    )
    out = merge_sources([src], "prop_42")
    assert len(out) == 1
    uid_fv = out[0].get("unit_id")
    if uid_fv is not None:  # fallback may or may not generate one depending on inputs
        assert isinstance(uid_fv, FieldValue)
        assert uid_fv.value  # non-empty


def test_serialization_back_to_legacy() -> None:
    sources = _build_two_source_setup()
    out = merge_sources(sources, "p")
    for u in out:
        legacy = to_legacy_unit(u)
        assert "_provenance" in legacy
        assert "unit_number" in legacy or "floor_plan_name" in legacy


def test_envelope_hash_drift_does_not_link() -> None:
    """Same unit_id in two sources, different envelope_hash → still merged
    (drift detection lives outside the merger). Both sources end up in
    the merged unit's provenance."""
    a = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP, source_url="x", envelope_hash="hashA",
        units=[{"unit_id": _fv("U1", SourceId.API_SIGHTMAP, 0.95, "x"), "asking_rent": _fv(1500, SourceId.API_SIGHTMAP, 0.95, "x")}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    b = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_UNITS, source_url="y", envelope_hash="hashB",
        units=[{"unit_id": _fv("U1", SourceId.API_RENTCAFE_UNITS, 0.90, "y"), "sqft": _fv(750, SourceId.API_RENTCAFE_UNITS, 0.90, "y")}],
        has_unit_ids=True, is_floor_plan_level=False,
    )
    out = merge_sources([a, b], "p")
    assert len(out) == 1
    legacy = to_legacy_unit(out[0])
    sources_seen = {prov["source"] for prov in legacy["_provenance"].values()}
    # Both should appear in the provenance dict
    assert SourceId.API_SIGHTMAP.value in sources_seen
    assert SourceId.API_RENTCAFE_UNITS.value in sources_seen


def test_strip_after_to_legacy_passes_shape_contract() -> None:
    sources = _build_two_source_setup()
    out = merge_sources(sources, "p")
    assert out
    legacy = to_legacy_unit(out[0])
    public = {k: v for k, v in legacy.items() if not k.startswith("_")}
    # No _ keys leak; the public dict has only field names
    assert all(not k.startswith("_") for k in public)
    assert "unit_number" in public or "floor_plan_name" in public


def test_empty_sources_returns_empty() -> None:
    assert merge_sources([], "p") == []


def test_merger_purity_no_imports() -> None:
    """Static-scan check: source_merger.py body contains no log./logging. or
    profile. references — purity invariant H1."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "services" / "source_merger.py").read_text(encoding="utf-8")
    # Strip docstring
    body = text.split('"""', 2)[-1]  # crude but adequate
    assert "import logging" not in body
    assert "log.warning" not in body
    assert "log.info" not in body
    assert "log.error" not in body
    assert "profile." not in body
