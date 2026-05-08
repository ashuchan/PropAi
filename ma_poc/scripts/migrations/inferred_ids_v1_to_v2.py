"""Migrate inferred unit IDs to the current ``compute_fallback_unit_id`` shape.

Two modes:

  Default (``--mode v1->v2``)
    The original one-time migration: v1 (12-char) → v2 (16-char). Used when
    the fallback hash format itself changed length.

  ``--mode rehash``
    Rehash *every* ``inferred_*`` id under the current normalizer. Use this
    after a change to ``identity_fallback._normalize_token`` (e.g. the
    plural-s + numeric-string + area-sentinel fix in 2026-05) so existing
    rows don't appear as ``disappeared`` on the first post-deploy run.
    Idempotent — when the normalizer is stable, re-runs produce 0 changes.

Three dispatch paths regardless of mode:
  A) data/state/unit_index.json  — legacy daily_runner state (optional)
  B) Postgres `units` table       — when DATA_PROVIDER=postgres (authoritative)
  C) data/runs/{date}/{shard}/properties.json — per-run snapshots <30 days old

Dry-run flag prints planned changes without writing. Backup writes
``<file>.v1_backup.json`` next to JSON paths; Postgres uses a single
transaction per property and relies on a pre-migration ``pg_dump``.
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

from ma_poc.core.identity import compute_fallback_unit_id

# v1 = 12-char digest (pre-2026-04 format). v2 = 16-char digest.
V1_ID_RE = re.compile(r"^inferred_[0-9a-f]{12}$")
V2_ID_RE = re.compile(r"^inferred_[0-9a-f]{16}$")
# Either-length matcher used by ``--mode rehash``.
ANY_INFERRED_RE = re.compile(r"^inferred_[0-9a-f]{12,16}$")
RETENTION_DAYS = 30


# Default (v1→v2) mode keeps the historical predicate; rehash mode broadens
# it. Selected once in ``main`` and threaded through the per-store helpers
# as a single regex argument so the migration logic stays identical.
def _id_matcher(mode: str) -> re.Pattern[str]:
    if mode == "rehash":
        return ANY_INFERRED_RE
    if mode == "v1->v2":
        return V1_ID_RE
    raise ValueError(f"unknown mode: {mode!r} (expected 'v1->v2' or 'rehash')")


# ---- Store A: legacy unit_index.json ---------------------------------------

def migrate_unit_index(
    path: Path,
    dry_run: bool,
    id_re: re.Pattern[str] = V1_ID_RE,
) -> dict[str, Any]:
    """Re-key inferred unit IDs in ``data/state/unit_index.json``.

    Returns a result dict: ``{file, exists?, migrated, skipped, collisions}``.
    Two records that differed in surface form (or rent, in v1 mode) and
    therefore had distinct ids collapse to a single new id. The script
    logs each collision but does NOT overwrite — first record wins.

    ``id_re`` selects which surviving IDs get re-hashed:
      * default ``V1_ID_RE`` — only the historical 12-char form.
      * ``ANY_INFERRED_RE`` — every ``inferred_*`` id, used by the
        ``--mode rehash`` path.
    Records whose ids don't match are passed through untouched. Records
    that match but where ``compute_fallback_unit_id`` produces the same
    id are no-ops (counted as ``skipped``) — making rehash mode safely
    idempotent.
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
            if not id_re.match(old_id):
                # Out of scope for the current mode — keep as-is.
                if old_id in rebuilt:
                    continue
                rebuilt[old_id] = rec
                total_s += 1
                continue
            new_id = compute_fallback_unit_id(rec, property_id)
            if new_id is None:
                # Fallback can't bind without floor_plan + 1 other field;
                # keep the existing id so we don't silently drop the record.
                if old_id not in rebuilt:
                    rebuilt[old_id] = rec
                total_s += 1
                continue
            if new_id == old_id:
                # Hash unchanged — already on the current normalizer.
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

_PG_ID_PREDICATES: dict[str, str] = {
    # SQL counterpart of the regexes above. Postgres ``~`` is POSIX regex.
    "v1->v2": r"^inferred_[0-9a-f]{12}$",
    "rehash": r"^inferred_[0-9a-f]{12,16}$",
}


def migrate_postgres_units(dry_run: bool, mode: str = "v1->v2") -> dict[str, Any]:
    """Re-key inferred IDs in the Postgres ``units`` table.

    No-op when ``DATA_PROVIDER != postgres``. Bounded by a regex
    predicate so the sweep only loads rows in scope for the chosen mode.

    The ``units`` SQL row doesn't carry the original input dict, so we
    rehash from the *current state* fields (floor_plan_name, beds, baths,
    area). That's the same data the writer used to compute the id at
    insert time, so on a stable normalizer the rehash produces an
    identical id and the row is left alone.
    """
    if os.getenv("DATA_PROVIDER", "filesystem") != "postgres":
        return {"skipped_reason": "DATA_PROVIDER != postgres", "migrated": 0, "collisions": []}
    if mode not in _PG_ID_PREDICATES:
        raise ValueError(f"unknown mode: {mode!r}")
    from sqlalchemy import create_engine, text  # local import — only when needed

    db_url = os.environ["DATABASE_URL"]
    engine = create_engine(db_url)
    migrated = 0
    skipped = 0
    collisions: list[tuple[Any, Any, str]] = []
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT canonical_id, unit_id, "
                "       floor_plan_name, beds, baths, area "
                "FROM units WHERE unit_id ~ :predicate"
            ),
            {"predicate": _PG_ID_PREDICATES[mode]},
        ).fetchall()
        for canonical_id, old_id, fp, beds, baths, area in rows:
            rec = {
                "floor_plan_name": fp,
                "beds": beds,
                "baths": baths,
                "area": area,
            }
            new_id = compute_fallback_unit_id(rec, str(canonical_id))
            if new_id is None or new_id == old_id:
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
# packaged inside ma_poc/scripts/migrations/, so walking up two parents
# (parents[2]) lands on the ma_poc/ root no matter where the operator invoked
# it from.
_DEFAULT_RUNS_DIR = Path(__file__).resolve().parents[2] / "data" / "runs"


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


def migrate_run_properties(
    dry_run: bool,
    base: Path | None = None,
    id_re: re.Pattern[str] = V1_ID_RE,
) -> list[dict[str, Any]]:
    """Walk recent per-run properties.json files and rewrite inferred unit_ids.

    ``id_re`` selects which ids are in scope (see :func:`migrate_unit_index`).
    """
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
                if not id_re.match(old_id):
                    continue
                new_id = compute_fallback_unit_id(unit, str(property_id))
                if new_id and new_id != old_id:
                    unit["unit_id"] = new_id
                    total_m += 1
        if not dry_run and total_m > 0:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if total_m > 0:
            results.append({"file": str(path), "migrated": total_m})
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--mode",
        choices=("v1->v2", "rehash"),
        default="v1->v2",
        help=(
            "v1->v2 (default): migrate the original 12-char IDs to the "
            "16-char shape. rehash: recompute every inferred_* id under "
            "the current normalizer (use after a normalizer-rule change)."
        ),
    )
    args = p.parse_args()
    id_re = _id_matcher(args.mode)
    print(f"Mode: {args.mode}  (matching {id_re.pattern})")

    print("\n=== Store A: data/state/unit_index.json ===")
    state_path = Path(__file__).resolve().parents[2] / "data" / "state" / "unit_index.json"
    res_a = migrate_unit_index(state_path, args.dry_run, id_re)
    print(json.dumps(res_a, indent=2, default=str))

    print("\n=== Store B: Postgres units ===")
    res_b = migrate_postgres_units(args.dry_run, args.mode)
    print(json.dumps(res_b, indent=2, default=str))

    print("\n=== Store C: per-run properties.json (last 30 days) ===")
    res_c = migrate_run_properties(args.dry_run, id_re=id_re)
    print(json.dumps(res_c, indent=2, default=str))


if __name__ == "__main__":
    main()
