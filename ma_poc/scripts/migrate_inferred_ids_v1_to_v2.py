"""One-time migration from v1 (12-char) to v2 (16-char) inferred unit IDs.

Three dispatch paths:
  A) data/state/unit_index.json  — legacy daily_runner state (optional)
  B) Postgres `units` table       — when DATA_PROVIDER=postgres (authoritative)
  C) data/runs/{date}/{shard}/properties.json — per-run snapshots <30 days old

Idempotent: rerunning on an already-migrated store is a no-op.
Dry-run flag: prints planned changes without writing.
Backup: writes <file>.v1_backup.json next to JSON paths; Postgres uses a
single transaction per property and relies on a pre-migration `pg_dump`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ma_poc.scripts.identity_fallback import compute_fallback_unit_id

V1_ID_RE = re.compile(r"^inferred_[0-9a-f]{12}$")
V2_ID_RE = re.compile(r"^inferred_[0-9a-f]{16}$")
RETENTION_DAYS = 30


# ---- Store A: legacy unit_index.json ---------------------------------------

def migrate_unit_index(path: Path, dry_run: bool) -> dict[str, Any]:
    """Migrate v1 IDs in data/state/unit_index.json to v2.

    Returns a result dict: {file, exists?, migrated, skipped, collisions}.
    Two records that differed only in rent (so they had distinct v1 IDs)
    collapse to a single v2 ID. The script logs each collision but does
    NOT overwrite — first record wins.
    """
    if not path.exists():
        return {"file": str(path), "exists": False, "migrated": 0, "skipped": 0, "collisions": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not dry_run:
        backup = path.with_suffix(".v1_backup.json")
        if not backup.exists():
            backup.write_text(json.dumps(data, indent=2), encoding="utf-8")

    total_m = 0
    total_s = 0
    all_collisions: list[tuple[str, str, str]] = []
    new_data: dict[str, Any] = {}

    for property_id, units in data.items():
        if not isinstance(units, dict):
            new_data[property_id] = units
            continue
        rebuilt: dict[str, Any] = {}
        for old_id, rec in units.items():
            if not V1_ID_RE.match(old_id):
                # Already v2 or natural — keep as-is.
                if old_id in rebuilt:
                    continue
                rebuilt[old_id] = rec
                total_s += 1
                continue
            new_id = compute_fallback_unit_id(rec, property_id)
            if new_id is None:
                # Fallback can't bind without floor_plan + 1 other field;
                # keep the v1 id so we don't silently drop the record.
                if old_id not in rebuilt:
                    rebuilt[old_id] = rec
                total_s += 1
                continue
            if new_id in rebuilt:
                all_collisions.append((property_id, old_id, new_id))
                continue
            rec_copy = dict(rec)
            rec_copy["unit_id"] = new_id
            rebuilt[new_id] = rec_copy
            total_m += 1
        new_data[property_id] = rebuilt

    if not dry_run:
        path.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
    return {
        "file": str(path),
        "exists": True,
        "migrated": total_m,
        "skipped": total_s,
        "collisions": all_collisions,
    }


# ---- Store B: Postgres `units` ---------------------------------------------

def migrate_postgres_units(dry_run: bool) -> dict[str, Any]:
    """Migrate v1 IDs in the Postgres `units` table to v2.

    No-op when DATA_PROVIDER != postgres. Bounded by a regex predicate
    so the sweep only touches v1-shaped rows.
    """
    if os.getenv("DATA_PROVIDER", "filesystem") != "postgres":
        return {"skipped_reason": "DATA_PROVIDER != postgres", "migrated": 0, "collisions": []}
    from sqlalchemy import create_engine, text  # local import — only when needed

    db_url = os.environ["DATABASE_URL"]
    engine = create_engine(db_url)
    migrated = 0
    skipped = 0
    collisions: list[tuple[Any, Any, str]] = []
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT canonical_id, unit_id, payload FROM units "
                "WHERE unit_id ~ '^inferred_[0-9a-f]{12}$'"
            )
        ).fetchall()
        for canonical_id, old_id, payload in rows:
            rec = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            new_id = compute_fallback_unit_id(rec, str(canonical_id))
            if new_id is None:
                skipped += 1
                continue
            existing = conn.execute(
                text("SELECT 1 FROM units WHERE canonical_id = :cid AND unit_id = :nid"),
                {"cid": canonical_id, "nid": new_id},
            ).first()
            if existing:
                collisions.append((canonical_id, old_id, new_id))
                continue
            if not dry_run:
                conn.execute(
                    text(
                        "UPDATE units SET unit_id = :nid "
                        "WHERE canonical_id = :cid AND unit_id = :oid"
                    ),
                    {"nid": new_id, "cid": canonical_id, "oid": old_id},
                )
            migrated += 1
    return {"migrated": migrated, "skipped": skipped, "collisions": collisions}


# ---- Store C: per-run properties.json --------------------------------------

# Resolve to ma_poc/data/runs regardless of CWD. The migration script is
# packaged inside ma_poc/scripts/, so walking up two parents lands on the
# ma_poc/ root no matter where the operator invoked it from.
_DEFAULT_RUNS_DIR = Path(__file__).resolve().parents[1] / "data" / "runs"


def _properties_files_in_window(base: Path | None = None) -> Iterator[Path]:
    """Yield properties.json files within the retention window.

    Path layout: runs/{YYYY-MM-DD}/{shard}/properties.json
    """
    base = base or _DEFAULT_RUNS_DIR
    if not base.exists():
        return
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    for path in base.rglob("properties.json"):
        try:
            run_date_str = path.parts[-3]
            run_dt = datetime.strptime(run_date_str, "%Y-%m-%d")
            if run_dt >= cutoff:
                yield path
        except (ValueError, IndexError):
            continue


def migrate_run_properties(dry_run: bool, base: Path | None = None) -> list[dict[str, Any]]:
    """Walk recent per-run properties.json files and rewrite v1 unit_ids."""
    results: list[dict[str, Any]] = []
    for path in _properties_files_in_window(base):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        backup = path.with_suffix(".v1_backup.json")
        if not dry_run and not backup.exists():
            backup.write_text(json.dumps(data, indent=2), encoding="utf-8")
        total_m = 0
        records = data if isinstance(data, list) else []
        for prop in records:
            property_id = (
                prop.get("Unique ID")
                or prop.get("Property ID")
                or prop.get("apartment_id")
                or ""
            )
            for unit in prop.get("units", []) or []:
                old_id = unit.get("unit_id", "") or ""
                if V1_ID_RE.match(old_id):
                    new_id = compute_fallback_unit_id(unit, str(property_id))
                    if new_id:
                        unit["unit_id"] = new_id
                        total_m += 1
        if not dry_run and total_m > 0:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if total_m > 0:
            results.append({"file": str(path), "migrated": total_m})
    return results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    print("=== Store A: data/state/unit_index.json ===")
    state_path = Path(__file__).resolve().parents[1] / "data" / "state" / "unit_index.json"
    res_a = migrate_unit_index(state_path, args.dry_run)
    print(json.dumps(res_a, indent=2, default=str))

    print("\n=== Store B: Postgres units ===")
    res_b = migrate_postgres_units(args.dry_run)
    print(json.dumps(res_b, indent=2, default=str))

    print("\n=== Store C: per-run properties.json (last 30 days) ===")
    res_c = migrate_run_properties(args.dry_run)
    print(json.dumps(res_c, indent=2, default=str))


if __name__ == "__main__":
    main()
