"""
Persistent state store for property + unit history across daily runs.
======================================================================
Two JSON files under data/state/:

  property_index.json  — canonical_id → last-known property snapshot
  unit_index.json      — canonical_id → { unit_id → last-seen unit snapshot }

A "snapshot" is the minimal info needed to:
  - detect whether we saw this property yesterday (carry-forward eligibility)
  - detect new vs disappeared properties
  - detect which units disappeared since yesterday (status change)
  - feed carry-forward copies when today's scrape fails

The store is intentionally plain JSON — no database, no locking. Writes go
through a temp-file + atomic rename so an interrupted run cannot corrupt the
state. Concurrent writers are not supported (one daily run at a time).
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── File I/O helpers ──────────────────────────────────────────────────────────

def _atomic_write(path: Path, payload: Any) -> None:
    """Write JSON atomically so a crash can never leave a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)

def _safe_load(path: Path) -> dict:
    """Load JSON, returning {} on missing/corrupt. Corrupt files are moved aside."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"state file {path} is not a JSON object")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        # Don't lose a corrupt file — rename it so it's recoverable and we start fresh.
        backup = path.with_suffix(path.suffix + f".corrupt.{int(datetime.now().timestamp())}")
        try:
            shutil.copy2(path, backup)
        except Exception:
            pass
        print(f"  ⚠ State file {path.name} unreadable ({e}); backed up to {backup.name}, starting fresh")
        return {}

# ── StateStore ────────────────────────────────────────────────────────────────

class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir         = Path(state_dir)
        self.property_index_path = self.state_dir / "property_index.json"
        self.unit_index_path   = self.state_dir / "unit_index.json"
        self.property_index: dict[str, dict] = {}
        self.unit_index: dict[str, dict[str, dict]] = {}

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> None:
        self.property_index = _safe_load(self.property_index_path)
        ui = _safe_load(self.unit_index_path)
        # Normalise shape: {canonical_id: {unit_id: {...}}}
        self.unit_index = {}
        for cid, units in ui.items():
            if isinstance(units, dict):
                self.unit_index[cid] = {str(k): v for k, v in units.items() if isinstance(v, dict)}
            else:
                self.unit_index[cid] = {}

    def save(self) -> None:
        _atomic_write(self.property_index_path, self.property_index)
        _atomic_write(self.unit_index_path, self.unit_index)

    # ── Property operations ──────────────────────────────────────────────────

    def get_property(self, canonical_id: str) -> dict | None:
        return self.property_index.get(canonical_id)

    def is_known(self, canonical_id: str) -> bool:
        return canonical_id in self.property_index

    def upsert_property(self, canonical_id: str, snapshot: dict, run_date: str) -> bool:
        """
        Insert-or-update a property snapshot. Returns True if this canonical_id
        is new to the index, False if it already existed.
        """
        is_new = canonical_id not in self.property_index
        existing = self.property_index.get(canonical_id, {})
        merged = {**existing, **snapshot}
        merged["last_seen_date"] = run_date
        merged["last_seen_at"]   = datetime.now(UTC).isoformat()
        if is_new:
            merged["first_seen_date"] = run_date
        else:
            merged.setdefault("first_seen_date", existing.get("first_seen_date") or run_date)
        self.property_index[canonical_id] = merged
        return is_new

    def all_canonical_ids(self) -> set[str]:
        return set(self.property_index.keys())

    # ── Unit operations ──────────────────────────────────────────────────────

    def get_units(self, canonical_id: str) -> dict[str, dict]:
        return self.unit_index.get(canonical_id, {})

    def upsert_units(
        self,
        canonical_id: str,
        today_units: list[dict],
        run_date: str,
    ) -> dict:
        """
        Update the unit index with today's unit records and return a diff:
          {
            "new":          [unit_id, ...],   # new unit_ids not seen before
            "updated":      [unit_id, ...],   # seen before, rent/date changed
            "unchanged":    [unit_id, ...],
            "disappeared":  [unit_id, ...],   # in yesterday's index but missing today
          }

        Only unit_ids whose schema includes a non-empty value are tracked.
        """
        prior = dict(self.unit_index.get(canonical_id, {}))
        current_ids: set[str] = set()
        diff = {"new": [], "updated": [], "unchanged": [], "disappeared": []}

        for u in today_units:
            uid = str(u.get("unit_id") or "").strip()
            if not uid:
                continue
            current_ids.add(uid)

            # Persist the full unit snapshot (not just rent/availability) so
            # a carry-forward on the next run produces a complete record
            # instead of a stub with bedrooms/bathrooms/sqft/plan all null.
            snapshot = {
                "unit_id":          uid,
                "market_rent_low":  u.get("market_rent_low"),
                "market_rent_high": u.get("market_rent_high"),
                "available_date":   u.get("available_date"),
                "concessions":      u.get("concessions"),
                # Extended fields — see carry_forward_units() below.
                "bedrooms":         u.get("bedrooms") or u.get("_bedrooms"),
                "bathrooms":        u.get("bathrooms") or u.get("_bathrooms"),
                "sqft":             u.get("sqft") or u.get("_sqft") or u.get("area"),
                "floor_plan_name":  u.get("floor_plan_name") or u.get("_floor_plan"),
                "unit_number":      u.get("unit_number") or u.get("_unit_number"),
                "bed_label":        u.get("bed_label"),
                "floor":            u.get("floor"),
                "building":         u.get("building"),
                "rent_range":       u.get("rent_range"),
                "lease_term":       u.get("lease_term") or u.get("_lease_term"),
                "move_in_date":     u.get("move_in_date") or u.get("_move_in_date"),
                "availability_status": u.get("availability_status"),
                "last_seen_date":   run_date,
                "last_seen_at":     datetime.now(UTC).isoformat(),
                "carryforward_days": 0,
            }

            if uid in prior:
                old = prior[uid]
                changed_fields = []
                for k in ("market_rent_low", "market_rent_high", "available_date", "concessions"):
                    if old.get(k) != snapshot.get(k):
                        changed_fields.append(k)
                if changed_fields:
                    diff["updated"].append(uid)
                    snapshot["changed_fields"] = changed_fields
                else:
                    diff["unchanged"].append(uid)
                snapshot["first_seen_date"] = old.get("first_seen_date") or run_date
            else:
                diff["new"].append(uid)
                snapshot["first_seen_date"] = run_date

            prior[uid] = snapshot

        for uid in list(prior.keys()):
            if uid not in current_ids:
                diff["disappeared"].append(uid)
                # Keep the stale record — flag it so the report can surface it and
                # later runs can re-discover if it comes back.
                rec = prior[uid]
                rec.setdefault("disappeared_since", run_date)
                rec["last_absent_date"] = run_date

        self.unit_index[canonical_id] = prior
        return diff

    def carry_forward_units(self, canonical_id: str, run_date: str) -> list[dict]:
        """
        Produce target-schema unit records copied from yesterday's index. Used
        when today's scrape failed — we still emit something rather than leaving
        the property empty. carryforward_days is incremented on each copy.
        """
        prior = self.unit_index.get(canonical_id, {})
        out: list[dict] = []
        for uid, rec in prior.items():
            # Skip units that already disappeared — we don't want to resurrect them.
            if rec.get("disappeared_since"):
                continue
            cfd = int(rec.get("carryforward_days") or 0) + 1
            rec["carryforward_days"] = cfd
            rec["last_seen_date"]    = run_date
            # Emit the full prior snapshot so the v2 transform can populate
            # beds/baths/area/floor_plan_name instead of silently defaulting.
            # Kept optional (.get) so older state files without extended
            # fields still carry forward at least rent/availability.
            out.append({
                "unit_id":            uid,
                "unit_number":        rec.get("unit_number") or uid,
                "market_rent_low":    rec.get("market_rent_low"),
                "market_rent_high":   rec.get("market_rent_high"),
                "available_date":     rec.get("available_date"),
                "lease_link":         None,
                "concessions":        rec.get("concessions"),
                "amenities":          None,
                # Extended carry-forward fields (Phase 1):
                "bedrooms":           rec.get("bedrooms"),
                "bathrooms":          rec.get("bathrooms"),
                "sqft":               rec.get("sqft"),
                "floor_plan_name":    rec.get("floor_plan_name"),
                "bed_label":          rec.get("bed_label"),
                "floor":              rec.get("floor"),
                "building":           rec.get("building"),
                "rent_range":         rec.get("rent_range"),
                "lease_term":         rec.get("lease_term"),
                "move_in_date":       rec.get("move_in_date"),
                "availability_status": rec.get("availability_status"),
                "carryforward_days":  cfd,
            })
        return out
