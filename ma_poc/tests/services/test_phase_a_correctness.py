"""Phase A correctness tests — one test per bug fixed.

Each test verifies the corrected behaviour. The bug-state is described in
a comment so future readers understand what was wrong before the fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ma_poc.models.source import (
    ExtractedSource,
    FieldValue,
    ProvenancedUnit,
    SourceId,
    _ABSENT_VALUES,
    _ABSENT_UNLESS_FIELD_PERMITS_ZERO,
    _ZERO_IS_VALID_FIELDS,
    from_legacy_unit,
)
from ma_poc.services.source_merger import merge_sources
from ma_poc.services.source_planner import evaluate_completeness


# ── helpers ──────────────────────────────────────────────────────────────────

def _fv(value: Any, source: SourceId, conf: float = 0.95) -> FieldValue:
    return FieldValue(value=value, source=source, confidence=conf)


# ── A1: fan-out beds/baths conflation ────────────────────────────────────────

def test_a1_fan_out_respects_beds_baths() -> None:
    """Two floor plans both named 'A1' (1bd/1ba and 2bd/2ba). Merger must NOT
    cross the beds/baths boundary when absorbing floor-plan-level fan-out rows.

    Bug (before fix): fp_key[1] == fp_name matched on name only, ignoring
    beds/baths — so unit 305 (1bd) absorbed the 1100sqft 2bd plan and vice-versa.
    """
    sm_units = [
        {
            "unit_id": _fv("305", SourceId.API_SIGHTMAP, 0.95),
            "unit_number": _fv("305", SourceId.API_SIGHTMAP, 0.95),
            "floor_plan_name": _fv("A1", SourceId.API_SIGHTMAP, 0.95),
            "beds": _fv(1, SourceId.API_SIGHTMAP, 0.95),
            "baths": _fv(1, SourceId.API_SIGHTMAP, 0.95),
        },
        {
            "unit_id": _fv("410", SourceId.API_SIGHTMAP, 0.95),
            "unit_number": _fv("410", SourceId.API_SIGHTMAP, 0.95),
            "floor_plan_name": _fv("A1", SourceId.API_SIGHTMAP, 0.95),
            "beds": _fv(2, SourceId.API_SIGHTMAP, 0.95),
            "baths": _fv(2, SourceId.API_SIGHTMAP, 0.95),
        },
    ]
    rc_units = [
        {
            "floor_plan_name": _fv("A1", SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "beds": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "baths": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "sqft": _fv(750, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        },
        {
            "floor_plan_name": _fv("A1", SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "beds": _fv(2, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "baths": _fv(2, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            "sqft": _fv(1100, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        },
    ]
    src_sm = ExtractedSource(
        source_id=SourceId.API_SIGHTMAP,
        source_url="https://sightmap.com/api",
        envelope_hash="h1",
        units=sm_units,
        has_unit_ids=True,
        is_floor_plan_level=False,
    )
    src_rc = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_FLOORPLANS,
        source_url="https://rentcafe.com/api",
        envelope_hash="h2",
        units=rc_units,
        has_unit_ids=False,
        is_floor_plan_level=True,
    )
    out = merge_sources([src_sm, src_rc], "prop_a1")
    assert len(out) == 2, f"expected 2 units, got {len(out)}"
    by_uid = {u["unit_number"].value: u for u in out}
    # unit 305 (1bd/1ba A1) must absorb only the 750sqft entry
    assert by_uid["305"]["sqft"].value == 750, (
        f"unit 305 got sqft={by_uid['305']['sqft'].value}, expected 750"
    )
    # unit 410 (2bd/2ba A1) must absorb only the 1100sqft entry
    assert by_uid["410"]["sqft"].value == 1100, (
        f"unit 410 got sqft={by_uid['410']['sqft'].value}, expected 1100"
    )


# ── A2: _unit_has_group physical threshold ────────────────────────────────────

def test_a2_physical_requires_two_fields() -> None:
    """A unit with only floor_plan_name must NOT count as having physical.
    A unit with floor_plan_name + beds qualifies.

    Bug (before fix): _unit_has_group returned True on the first matching
    field, so a single floor_plan_name inflated pct_with_physical.
    """
    only_fp = ProvenancedUnit()
    only_fp["floor_plan_name"] = FieldValue(value="A1", source=SourceId.JSON_LD, confidence=0.85)

    fp_and_beds = ProvenancedUnit()
    fp_and_beds["floor_plan_name"] = FieldValue(value="A1", source=SourceId.JSON_LD, confidence=0.85)
    fp_and_beds["beds"] = FieldValue(value=1, source=SourceId.JSON_LD, confidence=0.85)

    r_one = evaluate_completeness([only_fp])
    r_two = evaluate_completeness([fp_and_beds])

    assert r_one.pct_with_physical == 0.0, (
        f"single-field physical should be 0.0, got {r_one.pct_with_physical}"
    )
    assert r_two.pct_with_physical == 1.0, (
        f"two-field physical should be 1.0, got {r_two.pct_with_physical}"
    )


# ── A3: _looks_field_rich requires physical ───────────────────────────────────

def _get_looks_field_rich():
    """Import _looks_field_rich, stubbing playwright if not installed."""
    import sys
    import types

    # Stub playwright and entrata so they can be imported in no-browser test env
    for mod_name in [
        "playwright",
        "playwright.async_api",
        "playwright.sync_api",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)
    # Stub Page and BrowserContext so entrata.py can be imported
    pw_mod = sys.modules["playwright.async_api"]
    for attr in ("Page", "BrowserContext", "async_playwright"):
        if not hasattr(pw_mod, attr):
            setattr(pw_mod, attr, object)

    from ma_poc.pms.adapters.generic import _looks_field_rich
    return _looks_field_rich


def test_a3_looks_field_rich_requires_physical() -> None:
    """Units with only identity+rent (no physical) must not be field-rich.
    Units with identity+physical+rent qualify.

    Bug (before fix): _looks_field_rich only checked identity+transactional,
    so mappings that learned rent but not beds/sqft short-circuited the cascade
    before physical fields were collected.
    """
    _looks_field_rich = _get_looks_field_rich()

    only_id_and_rent = [{"unit_id": "U1", "asking_rent": 1500}]
    id_and_rent_and_physical = [
        {"unit_id": "U1", "asking_rent": 1500, "beds": 1, "sqft": 750}
    ]
    assert _looks_field_rich(only_id_and_rent) is False, (
        "identity+rent without physical should NOT be field-rich"
    )
    assert _looks_field_rich(id_and_rent_and_physical) is True, (
        "identity+physical+rent should be field-rich"
    )
    assert _looks_field_rich([]) is False


def test_a3_looks_field_rich_needs_two_physical_fields() -> None:
    """Only one physical field (e.g., just beds) is insufficient."""
    _looks_field_rich = _get_looks_field_rich()

    one_physical = [{"unit_id": "U1", "asking_rent": 1500, "beds": 1}]
    two_physical = [{"unit_id": "U1", "asking_rent": 1500, "beds": 1, "baths": 1}]

    assert _looks_field_rich(one_physical) is False
    assert _looks_field_rich(two_physical) is True


# ── A4: from_legacy_unit zero-valid bypass ────────────────────────────────────

def test_a4_zero_valid_carveout_does_not_preserve_empty_string() -> None:
    """baths='' and floor=None are absent and must be dropped.
    baths=0 is a valid zero and must be preserved.

    Bug (before fix): the single-test `value in _ABSENT_VALUES` (which included
    0/"0") fed a carve-out that skipped even empty-string/None for baths/floor
    fields, so those got wrapped as FieldValue(value="") which is wrong.
    """
    unit = {
        "unit_number": "S1",
        "bathrooms": "",   # absent — must be dropped
        "floor": None,     # absent — must be dropped
        "baths": 0,        # valid zero — must be preserved
    }
    pu = from_legacy_unit(unit, SourceId.API_GENERIC_NARROW, "", "", 0.8)

    assert "bathrooms" not in pu, "empty-string bathrooms should be dropped"
    assert "floor" not in pu, "None floor should be dropped"
    assert "baths" in pu, "baths=0 should be preserved as valid zero"
    assert pu["baths"].value == 0


def test_a4_absent_values_set_does_not_contain_zero() -> None:
    """_ABSENT_VALUES must not include 0/'0' — those live in
    _ABSENT_UNLESS_FIELD_PERMITS_ZERO now."""
    assert 0 not in _ABSENT_VALUES
    assert "0" not in _ABSENT_VALUES
    assert 0 in _ABSENT_UNLESS_FIELD_PERMITS_ZERO
    assert "0" in _ABSENT_UNLESS_FIELD_PERMITS_ZERO


def test_a4_zero_preserved_for_all_carveout_fields() -> None:
    """All fields in _ZERO_IS_VALID_FIELDS preserve integer 0."""
    for field in _ZERO_IS_VALID_FIELDS:
        unit = {"unit_number": "X", field: 0}
        pu = from_legacy_unit(unit, SourceId.API_GENERIC_NARROW, "", "", 0.8)
        assert field in pu, f"{field}=0 should be preserved"
        assert pu[field].value == 0


# ── A5: fallback source label ────────────────────────────────────────────────

def test_a5_fallback_source_label_correct() -> None:
    """When the merger synthesises a unit_id via compute_fallback_unit_id,
    the FieldValue.source must be IDENTITY_FALLBACK, not API_GENERIC_NARROW.

    Bug (before fix): FieldValue(source=SourceId.API_GENERIC_NARROW) was
    used, mis-attributing the deterministic hash to a real API source.
    """
    # A floor-plan-only source with no unit_id forces the fallback path
    src = ExtractedSource(
        source_id=SourceId.API_RENTCAFE_FLOORPLANS,
        source_url="https://rc.com/api",
        envelope_hash="h",
        units=[
            {
                "floor_plan_name": _fv("A1", SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
                "beds": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
                "baths": _fv(1, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
                "sqft": _fv(750, SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
            }
        ],
        has_unit_ids=False,
        is_floor_plan_level=True,
    )
    out = merge_sources([src], "prop_a5")
    assert len(out) == 1
    uid_fv = out[0].get("unit_id")
    if uid_fv is not None:
        assert uid_fv.source == SourceId.IDENTITY_FALLBACK, (
            f"fallback unit_id source should be IDENTITY_FALLBACK, got {uid_fv.source}"
        )


def test_a5_identity_fallback_in_enum() -> None:
    """IDENTITY_FALLBACK must be a valid SourceId enum value."""
    assert SourceId.IDENTITY_FALLBACK == "identity_fallback"
    assert SourceId("identity_fallback") == SourceId.IDENTITY_FALLBACK


# ── A6: atomic write in migration script ─────────────────────────────────────

def test_a6_migration_atomic_write_creates_backup(tmp_path: Path) -> None:
    """First migrate_one call creates a .pre_xsource.bak; second run does not
    overwrite it (so ops can diff original vs migrated).
    """
    from ma_poc.scripts.migrations.profiles_xsource import migrate_one

    p = tmp_path / "test.json"
    original_content = json.dumps({"canonical_id": "x", "version": 1})
    p.write_text(original_content, encoding="utf-8")

    result = migrate_one(p, dry_run=False)
    assert result["status"] in ("UPGRADED", "NO_CHANGE"), result

    bak = p.with_suffix(".json.pre_xsource.bak")
    assert bak.exists(), ".pre_xsource.bak must be created on first write"
    first_bak = bak.read_text(encoding="utf-8")

    # Modify the profile so migrate_one would want to write again
    p.write_text(json.dumps({"canonical_id": "x", "version": 2}), encoding="utf-8")
    migrate_one(p, dry_run=False)

    # Backup must not change after first snapshot
    assert bak.read_text(encoding="utf-8") == first_bak, (
        "Backup must not be overwritten on subsequent runs"
    )


def test_a6_migration_dry_run_does_not_write(tmp_path: Path) -> None:
    """dry_run=True must not write or create backup."""
    from ma_poc.scripts.migrations.profiles_xsource import migrate_one

    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"canonical_id": "y", "version": 1}), encoding="utf-8")
    migrate_one(p, dry_run=True)

    bak = p.with_suffix(".json.pre_xsource.bak")
    assert not bak.exists(), "dry_run must not create backup"


# ── Import standardisation: no bare imports in targeted files ─────────────────

def test_a_no_bare_imports_in_generic() -> None:
    """generic.py must not have bare 'from models.' or 'from services.' imports."""
    import re
    text = Path("ma_poc/pms/adapters/generic.py").read_text(encoding="utf-8")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports found in generic.py: {bare}"


def test_a_no_bare_imports_in_scraper() -> None:
    """scraper.py must not have bare 'from models.' or 'from services.' imports."""
    import re
    text = Path("ma_poc/pms/scraper.py").read_text(encoding="utf-8")
    bare = re.findall(r"^\s*from\s+(models|services)\.", text, re.MULTILINE)
    assert not bare, f"bare imports found in scraper.py: {bare}"
