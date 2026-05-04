"""Unit tests for the deploy-time validator.

Each test patches the module-level constants so the validator runs
against a synthetic config tree, then asserts on the failure list. The
real ``main()`` returns a process exit code; we exercise the inner
helpers directly so the tests don't depend on stdout layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.scripts import validate_deployment as vd


def _seed_csv_pair(dst: Path, *, rows: int = 1500, chosen: str | None = "apartmentid") -> None:
    """Drop a synthetic CSV/mapping pair under ``dst`` so the catalog loads."""
    fp = dst / "Floorplan- comparisons.csv"
    fp.write_text(
        "apartmentid,floorplannumber,description,bed,bath,area\n"
        + "\n".join(f"{i},1,Plan{i},1,1,{600 + i}" for i in range(rows)),
        encoding="utf-8",
    )
    map_path = dst / "csv_floorplan_mapping.json"
    map_path.write_text(json.dumps({"chosen_field": chosen}), encoding="utf-8")


def _seed_prompts(prompt_dir: Path) -> None:
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for name in vd._REQUIRED_PROMPTS:
        ph = "{{known_floor_plans}}" if name == "api_rescue.txt" else "{known_floor_plans}"
        (prompt_dir / name).write_text(f"DUMMY {ph}\n", encoding="utf-8")


@pytest.fixture
def patched_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the validator at a tmp_path-rooted config tree.

    Also reroute the FloorplanCatalog defaults so its module-level
    singleton picks up the synthetic CSV instead of the production one.
    """
    config = tmp_path / "config"
    config.mkdir()
    prompts = config / "prompts"
    monkeypatch.setattr(vd, "_CONFIG_DIR", config)
    monkeypatch.setattr(vd, "_MAPPING_PATH", config / "csv_floorplan_mapping.json")
    monkeypatch.setattr(vd, "_PROMPT_DIR", prompts)

    from ma_poc.services import floorplan_catalog as fc

    monkeypatch.setattr(fc, "_FLOORPLAN_CSV", config / "Floorplan- comparisons.csv")
    monkeypatch.setattr(fc, "_MAPPING_PATH", config / "csv_floorplan_mapping.json")
    fc.reset_default_catalog()
    yield config
    fc.reset_default_catalog()


def test_validator_passes_with_full_config(patched_paths: Path) -> None:
    _seed_csv_pair(patched_paths)
    _seed_prompts(patched_paths / "prompts")
    failures: list[str] = []
    vd._check_mapping(failures)
    vd._check_catalog(failures)
    vd._check_prompts(failures)
    vd._check_imports(failures)
    assert failures == []


def test_validator_fails_when_chosen_field_null(patched_paths: Path) -> None:
    _seed_csv_pair(patched_paths, chosen=None)
    failures: list[str] = []
    vd._check_mapping(failures)
    assert any("chosen_field is null" in f for f in failures)


def test_validator_fails_on_missing_mapping(patched_paths: Path) -> None:
    failures: list[str] = []
    vd._check_mapping(failures)
    assert any("mapping file missing" in f for f in failures)


def test_validator_fails_on_low_index_count(patched_paths: Path) -> None:
    _seed_csv_pair(patched_paths, rows=10)  # well below MIN_INDEX_PROPERTIES
    failures: list[str] = []
    vd._check_catalog(failures)
    assert any("indexed only" in f for f in failures)


def test_validator_fails_when_prompt_missing(patched_paths: Path) -> None:
    _seed_prompts(patched_paths / "prompts")
    (patched_paths / "prompts" / "tier4_extraction.txt").unlink()
    failures: list[str] = []
    vd._check_prompts(failures)
    assert any("tier4_extraction.txt" in f for f in failures)


def test_validator_fails_when_placeholder_dropped(patched_paths: Path) -> None:
    prompts = patched_paths / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    # Three good single-brace prompts...
    for name in ("tier4_extraction.txt", "api_analysis.txt", "dom_analysis.txt"):
        (prompts / name).write_text("DUMMY {known_floor_plans}\n", encoding="utf-8")
    # ...but rescue prompt missing the double-brace placeholder.
    (prompts / "api_rescue.txt").write_text("DUMMY single {known_floor_plans}\n", encoding="utf-8")
    failures: list[str] = []
    vd._check_prompts(failures)
    assert any("api_rescue.txt" in f for f in failures)
