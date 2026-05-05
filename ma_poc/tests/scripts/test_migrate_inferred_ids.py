"""Migration script: idempotency, dry-run safety, collision handling, and
correct dispatch across the three stores (F8 / H9)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ma_poc.scripts.migrate_inferred_ids_v1_to_v2 import (
    V1_ID_RE,
    migrate_postgres_units,
    migrate_run_properties,
    migrate_unit_index,
)


def _v1_id(record: dict[str, Any]) -> str:
    """Build a synthetic v1-shaped 12-char ID for fixtures."""
    h = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]
    return f"inferred_{h}"


# ---- H9: idempotency on unit_index.json ------------------------------------


def test_h9_unit_index_migration_idempotent(tmp_path: Path) -> None:
    """Running the migration twice on the same file is a no-op the second time."""
    record = {
        "floor_plan_type": "A1",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "asking_rent": 1500,
    }
    v1_id = _v1_id(record)
    state = {"prop_42": {v1_id: dict(record, unit_id=v1_id)}}
    fp = tmp_path / "unit_index.json"
    fp.write_text(json.dumps(state), encoding="utf-8")

    res1 = migrate_unit_index(fp, dry_run=False)
    res2 = migrate_unit_index(fp, dry_run=False)
    assert res1["migrated"] == 1
    assert res2["migrated"] == 0  # second run is a no-op
    after = json.loads(fp.read_text(encoding="utf-8"))
    new_ids = list(after["prop_42"].keys())
    assert len(new_ids) == 1
    assert re.match(r"^inferred_[0-9a-f]{16}$", new_ids[0])


def test_h9b_dry_run_does_not_mutate(tmp_path: Path) -> None:
    """--dry-run must not write any backup or rewrite the file."""
    record = {"floor_plan_type": "A1", "bedrooms": 1, "sqft": 750, "asking_rent": 1500}
    v1_id = _v1_id(record)
    state = {"prop_42": {v1_id: dict(record, unit_id=v1_id)}}
    fp = tmp_path / "unit_index.json"
    fp.write_text(json.dumps(state), encoding="utf-8")
    original = fp.read_text(encoding="utf-8")

    migrate_unit_index(fp, dry_run=True)
    assert fp.read_text(encoding="utf-8") == original
    assert not (tmp_path / "unit_index.v1_backup.json").exists()


def test_unit_index_missing_path_returns_no_op(tmp_path: Path) -> None:
    """Absent state file → no-op result, no error."""
    res = migrate_unit_index(tmp_path / "does_not_exist.json", dry_run=False)
    assert res["exists"] is False
    assert res["migrated"] == 0


# ---- Collision handling ----------------------------------------------------


def test_collision_between_v1_records_logged_not_overwritten(tmp_path: Path) -> None:
    """Two v1 records that differed only in rent collapse to one v2 ID.
    First record wins; second is logged in collisions[]."""
    base = {"floor_plan_type": "A1", "bedrooms": 1, "sqft": 750}
    rec_a = dict(base, asking_rent=1500)
    rec_b = dict(base, asking_rent=1700)
    v1_a = _v1_id(rec_a)
    v1_b = _v1_id(rec_b)
    state = {"prop_42": {v1_a: dict(rec_a, unit_id=v1_a), v1_b: dict(rec_b, unit_id=v1_b)}}
    fp = tmp_path / "unit_index.json"
    fp.write_text(json.dumps(state), encoding="utf-8")

    res = migrate_unit_index(fp, dry_run=False)
    after = json.loads(fp.read_text(encoding="utf-8"))
    # Both v1 records collapse to the same v2 id (rent excluded from v2 hash)
    assert len(after["prop_42"]) == 1
    new_id = list(after["prop_42"].keys())[0]
    assert re.match(r"^inferred_[0-9a-f]{16}$", new_id)
    # The collision was reported (tuple of (property_id, old_id, new_id))
    assert len(res["collisions"]) == 1
    assert res["collisions"][0][0] == "prop_42"


# ---- Postgres dispatch gate ------------------------------------------------


def test_postgres_path_skipped_when_data_provider_is_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When DATA_PROVIDER != postgres, short-circuit with no connection."""
    monkeypatch.setenv("DATA_PROVIDER", "filesystem")
    res = migrate_postgres_units(dry_run=False)
    assert res.get("skipped_reason") == "DATA_PROVIDER != postgres"


# ---- Regex shape -----------------------------------------------------------


def test_v1_id_regex_matches_only_12_hex_chars() -> None:
    """V1 detection regex must not match v2 (16-char) or natural IDs."""
    assert V1_ID_RE.match("inferred_a1b2c3d4e5f6")  # 12 hex
    assert not V1_ID_RE.match("inferred_a1b2c3d4e5f6a1b2")  # 16 hex (v2)
    assert not V1_ID_RE.match("U101")  # natural
    assert not V1_ID_RE.match("inferred_xyz")  # non-hex


# ---- Store C: per-run snapshots -------------------------------------------


def test_run_properties_migrates_v1_unit_ids_in_recent_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recent properties.json with v1 unit ids gets rewritten in place."""
    from datetime import datetime

    runs_dir = tmp_path / "runs"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    shard_dir = runs_dir / today / "shard_0"
    shard_dir.mkdir(parents=True)

    rec = {"floor_plan_type": "A1", "bedrooms": 1, "sqft": 750, "asking_rent": 1500}
    v1_id = _v1_id(rec)
    snapshot = [
        {
            "Property ID": "prop_77",
            "Unique ID": "prop_77",
            "units": [{**rec, "unit_id": v1_id}],
        }
    ]
    fp = shard_dir / "properties.json"
    fp.write_text(json.dumps(snapshot), encoding="utf-8")

    results = migrate_run_properties(dry_run=False, base=runs_dir)
    assert any(r["migrated"] >= 1 for r in results), f"no migrations recorded: {results}"
    rewritten = json.loads(fp.read_text(encoding="utf-8"))
    new_id = rewritten[0]["units"][0]["unit_id"]
    assert re.match(r"^inferred_[0-9a-f]{16}$", new_id)


def test_run_properties_skips_files_outside_retention(tmp_path: Path) -> None:
    """A snapshot dated >30 days ago is not opened or modified."""
    runs_dir = tmp_path / "runs"
    old_dir = runs_dir / "2024-01-01" / "shard_0"
    old_dir.mkdir(parents=True)
    rec = {"floor_plan_type": "A1", "bedrooms": 1, "sqft": 750, "asking_rent": 1500}
    v1_id = _v1_id(rec)
    snapshot = [{"Property ID": "p", "units": [{**rec, "unit_id": v1_id}]}]
    fp = old_dir / "properties.json"
    fp.write_text(json.dumps(snapshot), encoding="utf-8")

    results = migrate_run_properties(dry_run=False, base=runs_dir)
    # File outside the 30-day window should not appear in results
    assert not any(r["file"].endswith(str(fp)) for r in results)
