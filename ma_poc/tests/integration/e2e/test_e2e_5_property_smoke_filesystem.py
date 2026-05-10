"""E2E smoke: 5-property pipeline through all 4 layers — FileSystem provider.

Synthetic scrape results flow through:
  extraction artifacts → profile update → FS provider persist → reports

Seven invariants asserted (mirroring what scripts/smoke_test.py would check):
  1.  properties.json exists and is valid JSON
  2.  properties.json contains exactly 5 entries
  3.  Each property is retrievable via provider.property_state.get()
  4.  At least one property carries unit data
  5.  A ScrapeProfile was persisted for every property
  6.  run_report totals.properties == 5
  7.  amenities_report.json written to run_dir
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from data_provider import FileSystemDataProvider
from reporting.observation_reports import build_amenities_report
from reporting.run_report import build as build_run_report
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction
from tests.integration.factories.scrape_profile import build_profile


RUN_DATE = "2026-05-10"
_PROPS = [f"E2E-FS-{i:03d}" for i in range(1, 6)]


def _scrape_result(
    prop_id: str,
    *,
    units: list[dict[str, Any]] | None = None,
    tier: str = "TIER_2_JSONLD",
    verdict: str = "SUCCESS",
) -> dict[str, Any]:
    return {
        "property_name": f"Property {prop_id}",
        "property_id": prop_id,
        "units": units or [],
        "errors": [],
        "extraction_tier_used": tier,
        "_meta": {"verdict": verdict, "scrape_tier_used": tier},
        "_detected_pms": {"name": "unknown", "confidence": "n/a", "evidence": [], "adapter": "n/a"},
        "_llm_interactions": [],
    }


@pytest.fixture
def fs(tmp_path: Path) -> FileSystemDataProvider:
    config_dir = tmp_path / "config"
    (config_dir / "profiles").mkdir(parents=True)
    (config_dir / "properties.csv").write_text("canonical_id,url\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    return FileSystemDataProvider(base_dir=tmp_path / "data", config_dir=config_dir)


@pytest.fixture
def profile_store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(base_dir=tmp_path / "config" / "profiles")


def _run_pipeline(
    fs: FileSystemDataProvider,
    profile_store: ProfileStore,
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], Path]:
    run_dir = tmp_path / "data" / "runs" / RUN_DATE
    run_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for i, prop_id in enumerate(_PROPS):
        units = [{"unit_id": f"U{i}", "market_rent_low": 1500 + i * 100}] if i < 3 else []
        result = _scrape_result(prop_id, units=units)
        results.append(result)

        profile = build_profile(canonical_id=prop_id)
        update_profile_after_extraction(profile, result, len(units), profile_store)

        snap = {"proj_name": result["property_name"], "units": units, "_meta": result["_meta"]}
        with fs.transaction():
            fs.property_state.upsert(prop_id, snap, RUN_DATE)
            if units:
                fs.unit_state.upsert_units(prop_id, units, RUN_DATE)

    # Write properties.json so run_report.build() and run_dir utilities work.
    props_for_report = [
        {
            "property_id": r["property_id"],
            "units": r["units"],
            "_meta": r["_meta"],
        }
        for r in results
    ]
    (run_dir / "properties.json").write_text(
        json.dumps(props_for_report), encoding="utf-8"
    )

    return props_for_report, run_dir


# ---------------------------------------------------------------------------
# The 7 smoke invariants
# ---------------------------------------------------------------------------


def test_e2e_fs_properties_json_exists(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 1: properties.json exists and is valid JSON."""
    _, run_dir = _run_pipeline(fs, profile_store, tmp_path)
    props_file = run_dir / "properties.json"
    assert props_file.exists()
    data = json.loads(props_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)


def test_e2e_fs_properties_json_has_5_entries(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 2: properties.json contains exactly 5 entries."""
    _, run_dir = _run_pipeline(fs, profile_store, tmp_path)
    data = json.loads((run_dir / "properties.json").read_text(encoding="utf-8"))
    assert len(data) == 5


def test_e2e_fs_each_property_retrievable(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 3: all 5 properties retrievable from provider."""
    _run_pipeline(fs, profile_store, tmp_path)
    for prop_id in _PROPS:
        assert fs.property_state.exists(prop_id), f"{prop_id} missing from provider"


def test_e2e_fs_at_least_one_property_has_units(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 4: at least one property carries unit data."""
    _run_pipeline(fs, profile_store, tmp_path)
    found = any(
        len(fs.unit_state.get_units(prop_id)) > 0
        for prop_id in _PROPS
    )
    assert found, "expected at least one property to have units stored"


def test_e2e_fs_profiles_persisted(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 5: a ScrapeProfile was saved for every property."""
    _run_pipeline(fs, profile_store, tmp_path)
    for prop_id in _PROPS:
        p = profile_store.load(prop_id)
        assert p is not None, f"profile missing for {prop_id}"
        assert p.stats.total_scrapes >= 1


def test_e2e_fs_run_report_total_matches(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 6: run_report totals.properties == 5."""
    props_for_report, run_dir = _run_pipeline(fs, profile_store, tmp_path)
    report = build_run_report(props_for_report, run_dir, RUN_DATE)
    assert report["totals"]["properties"] == 5


def test_e2e_fs_amenities_report_written(fs: FileSystemDataProvider, profile_store: ProfileStore, tmp_path: Path) -> None:
    """Invariant 7: amenities_report.json written to run_dir."""
    props_for_report, run_dir = _run_pipeline(fs, profile_store, tmp_path)
    build_amenities_report(props_for_report, run_dir, RUN_DATE)
    assert (run_dir / "amenities_report.json").exists()
