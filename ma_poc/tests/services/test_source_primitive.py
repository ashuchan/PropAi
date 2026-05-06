"""Phase 2 — field-provenance primitive tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from models.source import (
    ExtractedSource,
    FieldValue,
    ProvenancedUnit,
    SourceId,
    envelope_hash_of,
    from_legacy_unit,
    to_legacy_unit,
)


def test_field_value_validation() -> None:
    fv = FieldValue(value=1500, source=SourceId.API_SIGHTMAP, confidence=0.5)
    assert fv.confidence == 0.5
    with pytest.raises(Exception):  # pydantic ValidationError
        FieldValue(value=1, source=SourceId.API_SIGHTMAP, confidence=1.5)
    with pytest.raises(Exception):
        FieldValue(value=1, source=SourceId.API_SIGHTMAP, confidence=-0.1)


def test_envelope_hash_is_stable() -> None:
    body_a = {"a": 1, "b": [1, 2, 3]}
    body_b = {"b": [1, 2, 3], "a": 1}  # reordered keys
    body_c = {"a": 1, "b": [3, 2, 1]}  # reordered list — must differ
    assert envelope_hash_of(body_a) == envelope_hash_of(body_b)
    assert envelope_hash_of(body_a) != envelope_hash_of(body_c)
    # Length 16 prefix
    assert len(envelope_hash_of(body_a)) == 16


def test_to_legacy_round_trip() -> None:
    unit = {
        "unit_number": "305",
        "asking_rent": 1500,
        "sqft": 750,
        "availability_status": "AVAILABLE",
    }
    pu = from_legacy_unit(
        unit,
        source=SourceId.API_SIGHTMAP,
        source_url="https://example.com/api",
        envelope_hash="abc123",
        confidence=0.95,
    )
    legacy = to_legacy_unit(pu)
    for k, v in unit.items():
        if v == "AVAILABLE":  # this is the make_unit_dict default — skipped on import
            continue
        assert legacy[k] == v


def test_to_legacy_drops_provenance_at_v1_boundary() -> None:
    pu = ProvenancedUnit()
    pu["unit_number"] = FieldValue(
        value="101",
        source=SourceId.API_SIGHTMAP,
        confidence=0.95,
    )
    legacy = to_legacy_unit(pu)
    assert "_provenance" in legacy
    # simulate the v1/v2 boundary strip
    public = {k: v for k, v in legacy.items() if not k.startswith("_")}
    assert "_provenance" not in public
    assert public["unit_number"] == "101"


def test_from_legacy_skips_default_fields() -> None:
    # availability_status="AVAILABLE" is the make_unit_dict default; floor_plan_name=""
    # is empty; sqft="-1" is the absent marker.
    unit = {
        "unit_number": "101",
        "floor_plan_name": "",
        "sqft": "-1",
        "asking_rent": 1500,
    }
    pu = from_legacy_unit(
        unit,
        source=SourceId.API_GENERIC_NARROW,
        source_url="",
        envelope_hash="",
        confidence=0.8,
    )
    assert "unit_number" in pu
    assert "asking_rent" in pu
    assert "floor_plan_name" not in pu
    assert "sqft" not in pu


def test_zero_is_valid_for_bath_count() -> None:
    """baths=0 should be preserved (it means no bathroom in the unit row)."""
    unit = {"unit_number": "S1", "bathrooms": 0, "asking_rent": 900}
    pu = from_legacy_unit(
        unit,
        source=SourceId.API_GENERIC_NARROW,
        source_url="",
        envelope_hash="",
        confidence=0.8,
    )
    assert "bathrooms" in pu
    assert pu["bathrooms"].value == 0


def test_extracted_source_validates() -> None:
    src = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_FLOORPLANS,
        source_url="https://rentcafe.com/api/foo",
        envelope_hash="deadbeef00000000",
        units=[
            {
                "unit_number": FieldValue(
                    value="A1",
                    source=SourceId.API_RENTCAFE_FLOORPLANS,
                    confidence=0.95,
                )
            }
        ],
        has_unit_ids=False,
        is_floor_plan_level=True,
    )
    assert src.is_floor_plan_level is True
    assert len(src.units) == 1


def test_source_id_enum_is_closed() -> None:
    """Static-scan style check: enum has all source IDs the spec mandates."""
    expected = {
        "api_rentcafe_floorplans",
        "api_rentcafe_units",
        "api_entrata_widget",
        "api_appfolio_listings",
        "api_sightmap",
        "api_onesite",
        "api_avalonbay",
        "api_generic_narrow",
        "api_generic_broad",
        "json_ld",
        "embedded_json",
        "dom_cascade",
        "dom_profile_hints",
        "mapping_replay",
        "field_patch",
        "cluster_mapping_replay",
        "identity_fallback",
        "llm_api_targeted",
        "llm_dom_targeted",
        "llm_monolithic",
        "llm_field_recovery",
        "default_availability",
    }
    actual = {s.value for s in SourceId}
    assert actual == expected


def test_source_id_enum_static_scan_no_string_literals() -> None:
    """No string literals for SourceId values appear in services/ or pms/
    outside this module and source_planner.py (which we haven't created yet)."""
    # Cover the most-likely literals; closed enum is enforced by Phase 4 gate.
    suspect_literals = [
        '"mapping_replay"',
        '"field_patch"',
        '"cluster_mapping_replay"',
        '"llm_monolithic"',
        '"dom_profile_hints"',
    ]
    repo_root = Path(__file__).resolve().parents[3]
    scan_dirs = [
        repo_root / "ma_poc" / "services",
        repo_root / "ma_poc" / "pms",
    ]
    allowed_files = {
        repo_root / "ma_poc" / "models" / "source.py",
        repo_root / "ma_poc" / "services" / "source_planner.py",
        repo_root / "ma_poc" / "services" / "source_merger.py",
        repo_root / "ma_poc" / "services" / "source_observer.py",
        # Phase H: these files use "llm_targeted"/"llm_monolithic" as budget
        # dict keys — same spelling as SourceId values but not SourceId usage.
        repo_root / "ma_poc" / "pms" / "scraper.py",
        repo_root / "ma_poc" / "pms" / "adapters" / "base.py",
        repo_root / "ma_poc" / "pms" / "adapters" / "generic.py",
        # PR-4: tier_orchestrator.py took over generic.py's cascade logic
        # (including the budget dict that uses "llm_monolithic" as a key).
        repo_root / "ma_poc" / "pms" / "adapters" / "tier_orchestrator.py",
    }
    hits: list[str] = []
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for py in scan_dir.rglob("*.py"):
            if py in allowed_files:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            for lit in suspect_literals:
                if lit in text:
                    hits.append(f"{py}: {lit}")
    assert hits == [], f"unexpected source-id string literals: {hits}"
