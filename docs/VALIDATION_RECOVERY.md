# CLAUDE_VALIDATION_RECOVERY_PR1.md

**Mission:** Migrate `schema_gate.check()` from the legacy `compute_fallback_id` (v1) to `compute_fallback_unit_id` (v2). Add v2 canonical field-name reads to the rent and sqft validators (`rent_low`/`rent_high`, `area`). Convert string-date-format rejections into placeholder pass-through. Ship a one-time state-migration script for the v1→v2 inferred-id format change. Single mergeable PR. No phased rollout, no feature flag.

**Predecessor:** None within this series. Motivated by [`2026-05-05_validation_failure_RCA.md`](./2026-05-05_validation_failure_RCA.md), which identified the v1 fallback as the structural cause of ~85% of the 30,117 record-level rejections in the 2026-05-05 production run.

**Successor:** [`CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md`](./CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md) — implements the architectural fix (multi-tier confidence, cross-run rescue, multi-stream output). Phase 1 must not make choices that conflict with Phase 2's design; see §1 *Forward-compat constraints*.

**Audience:** Claude Code, executing autonomously against `ashuchan/PropAi`.

**Scope estimate:** ~145 LoC production + ~360 LoC tests + ~80 LoC migration script. Across 4 production files and 3 new test files. ~1 day.

**Out of scope:**
- Multi-stream output (Phase 2).
- Cross-run identity rescue (Phase 2).
- Confidence-tier classification (Phase 2).
- Floor-plan synthesis for the 174-row missing-fp signature (Phase 3 long-tail).
- Empty-record drop at the extractor (Phase 3 long-tail).
- The unit_id junk-token filter fix (Phase 3 long-tail).
- Concession/amenity writer wiring (separate Tier-4 prompt PR).

---

## 1. What this PR does and does NOT do

**Does:**

1. **F1 — Migrate `schema_gate.check()` from v1 to v2 fallback.** Single import change at [`schema_gate.py:16`](../ma_poc/validation/schema_gate.py#L16), call-site change at [`schema_gate.py:167`](../ma_poc/validation/schema_gate.py#L167), signature change to `check(record, property_id)`, orchestrator update at [`orchestrator.py:88`](../ma_poc/validation/orchestrator.py#L88) to thread `property_id` through.
2. **F2 — Add v2 rent canonical-name reads.** [`schema_gate.py:123`](../ma_poc/validation/schema_gate.py#L123) currently reads `asking_rent | market_rent_low | rent` and misses v2 canonical `rent_low`/`rent_high`. v2-strict DB rows (per `project_db_v2_schema.md`) carry only those names; today they slip past `INVALID_RENT_NEGATIVE` and `INVALID_RENT_ABSURD` because the lookup short-circuits on `None`. Extend the chain to `rent_low | rent_high` after the existing v1 names so v1-shaped records continue to win first.
3. **F3 — Add v2 sqft canonical-name read.** [`schema_gate.py:137-139`](../ma_poc/validation/schema_gate.py#L137) reads only `sqft | square_feet` and misses v2 canonical `area`. Same blind spot as F2: pathological `area` values bypass `INVALID_SQFT_ABSURD`. The `_PHYSICAL_SIGNAL_FIELDS` set 50 lines above already accepts `area`, so the asymmetry is internal to the file. Extend the lookup to `sqft | square_feet | area`.
4. **F4 — Convert `INVALID_DATE_FORMAT` string-path rejections into placeholder pass-through.** Records with unparseable date strings (`"Spring 2026"`, `"Coming Soon"`, `"Q3"`) currently reject outright. Change the contract: null `available_date`, stash the original string in `_date_placeholder`, accept the rest of the record. Emit `validate.date_placeholder_observed` for telemetry.
5. **F8 — Ship a one-time state-migration script.** `scripts/migrate_unit_index_v1_to_v2_ids.py` walks every store that can hold inferred IDs (legacy `data/state/unit_index.json`, per-run `data/runs/{date}/{shard}/properties.json`, and the Postgres `units` table when `DATA_PROVIDER=postgres`), recomputes inferred IDs from v1 12-char to v2 16-char format, and rewrites in place. Idempotent, with backup files for the JSON paths and a transactional update for Postgres. See §4.4 for the dispatch matrix.
6. **F9 — Emit `validate.identity_gap` events on rejections.** When a record rejects with `IDENTITY_FALLBACK_INSUFFICIENT`, additionally emit a structured event carrying the field-presence map AND a tentative signature key computed from whatever identifying fields ARE present. F9 has no consumer in Phase 0; it builds a one-week dataset that informs Phase 1's signature-key design before Phase 1 begins. Per [`CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md`](./CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md) §11.

**Does not:**

- Change the v1 function. v1 remains importable from `identity_fallback.py` for the migration script's use during state thaw. A subsequent PR (after one full deprecation cycle) removes it.
- Modify cross-run sanity, drift detection, or quality-gate logic. Those continue to operate on whatever `unit_id` is on the record after `schema_gate` runs.
- Change cascade ordering, sub-tier routing, or any extractor behavior.
- Persist rejected records to disk, route records to multiple output streams, or wire any cross-run identity rescue. Those are Phase 2.
- Remove `INVALID_DATE_FORMAT` from the rejection-reason enum. The reason still fires for non-string date values (corrupted types, integers); only the string-parse-failure path reroutes.

### Forward-compat constraints (do not violate; Phase 2 depends on these)

| Constraint | Why |
|---|---|
| The `inferred_id: bool` flag on `SchemaGateResult` must remain a bool, not a tri-state. Phase 2 introduces a separate `binding_tier` field; `inferred_id` continues to mean exactly what it means today (v2 fallback bound the id). | Phase 2's cross-run rescue produces a third state (`history_bound`) that needs its own field; mixing it into `inferred_id` would force unwinding. |
| The `RejectedRecord` dataclass shape must not change. Phase 2 will repurpose its fields when persisting candidate records. | Schema stability across the v1→v2 transition. |
| Do not add new output files under `data/runs/{date}/`. Phase 2 introduces `floor_plan_inventory.jsonl` and `candidates.jsonl`; pre-empting the namespace creates conflicts. | Output-stream namespace ownership belongs to Phase 2. |
| Do not change the orchestrator's signature beyond threading `property_id` into `schema_check`. Phase 2 will add a `cross_run_rescue` stage. | Keep the orchestrator's contract minimal until Phase 2 lands. |

---

## 2. Hard invariants

| ID | Invariant | Validating test |
|---|---|---|
| H1 | `schema_gate.py` does not import `compute_fallback_id` (v1). It imports `compute_fallback_unit_id` (v2). | `test_h1_schema_gate_imports_v2_only` (static scan) |
| H2 | `schema_gate.check()` accepts a record carrying `floor_plan_name + sqft` (no other identifying fields, no rent, no available_date) with `inferred_id=True`. | `test_h2_floor_plan_name_alias_recovery` |
| H3 | `schema_gate.check()` accepts a record carrying the legacy `floor_plan_type + bedrooms + sqft` shape with `inferred_id=True` (backward compat — v2 reads both alias families). | `test_h3_legacy_floor_plan_type_still_works` |
| H4 | **Phase 0 only — superseded in Phase 1.** `schema_gate.check()` rejects a record carrying only `floor_plan_name` (no other identifying fields) with `IDENTITY_FALLBACK_INSUFFICIENT`. v2's "fp + ≥1 other" precondition is preserved. Phase 1 (`CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md` §11) replaces this with H4-bis: such records route to `units_weak` rather than rejecting. | `test_h4_floor_plan_only_still_rejects` |
| H5 | The inferred unit_id assigned by `schema_gate.check()` is identical between two records that differ only in rent and/or available_date. (v2 hash stability.) | `test_h5_rent_change_does_not_alter_unit_id` |
| H6 | A record with an unparseable date string (`"Spring 2026"`) is **accepted** with `available_date=None`, `_date_placeholder="Spring 2026"`, and emits `validate.date_placeholder_observed`. The reject reason `INVALID_DATE_FORMAT` does **not** appear. | `test_h6_date_placeholder_pass_through` |
| H7 | A record with a non-string date value (e.g., int `42`) continues to reject with `INVALID_DATE_FORMAT`. | `test_h7_non_string_date_still_rejects` |
| H8 | The orchestrator's `_field_presence` map continues to use the wide alias set (no change). All reject events continue to carry the map. | `test_h8_field_presence_unchanged` |
| H9 | The migration script idempotently rewrites a v1 `inferred_<sha12>` ID to a v2 `inferred_<sha16>` ID using the same input fields. Re-running on an already-migrated index is a no-op. | `test_h9_migration_script_idempotent` |
| H10 | No production code path imports `compute_fallback_id` (v1) except the migration script. | `test_h10_v1_only_called_from_migration_script` (static scan) |
| H11 | `schema_gate.check()` rejects a v2-only record with `rent_low=60000` (absurd) with `INVALID_RENT_ABSURD`. (Pre-fix this slips through because the v1 lookup chain returns None.) | `test_h11_v2_rent_low_absurd_rejected` |
| H12 | `schema_gate.check()` rejects a v2-only record with `rent_low=-100` with `INVALID_RENT_NEGATIVE`. | `test_h12_v2_rent_low_negative_rejected` |
| H13 | `schema_gate.check()` rejects a v2-only record with `area=99999` with `INVALID_SQFT_ABSURD`. (Pre-fix slips through.) | `test_h13_v2_area_absurd_rejected` |
| H14 | `schema_gate.check()` accepts a v2-only record with `area=-1` (sentinel) without firing `INVALID_SQFT_NEGATIVE`. (Sentinel semantics carry over to v2 name.) | `test_h14_v2_area_minus_one_sentinel` |
| H15 | When both v1 and v2 rent fields are present (`asking_rent=1500`, `rent_low=1600`), v1 wins (preserves existing precedence) and validation runs on `1500`. | `test_h15_rent_lookup_precedence_v1_first` |

---

## 3. File map

| File | Change | Fixes / tests |
|---|---|---|
| [`ma_poc/validation/schema_gate.py`](../ma_poc/validation/schema_gate.py) | Import (line 16) + fallback call-site (line 167) + signature change + rent lookup (line 123) + sqft lookup (line 137-139) + F4 date branch (line 153-161) | F1, F2, F3, F4 / H1, H2, H3, H4, H5, H6, H7, H11, H12, H13, H14, H15 |
| [`ma_poc/validation/identity_fallback.py`](../ma_poc/validation/identity_fallback.py) | No change (deprecated shim retained for migration script) | — |
| [`ma_poc/validation/orchestrator.py`](../ma_poc/validation/orchestrator.py) | Thread `property_id` into `schema_check` call (line 88) | F1 / H1, H8 |
| [`ma_poc/observability/events.py`](../ma_poc/observability/events.py) | Add `DATE_PLACEHOLDER_OBSERVED` to `EventKind` enum | F4 / H6 |
| [`ma_poc/tests/validation/test_schema_gate_unit_id_alone.py`](../ma_poc/tests/validation/test_schema_gate_unit_id_alone.py) | Mechanical: pass `property_id="P1"` to all 10 `check()` calls; rewrite the misleading docstring at line 45-51 to describe v2 alias acceptance instead of v1 fallback | F1 / — |
| [`ma_poc/tests/validation/test_schema_gate.py`](../ma_poc/tests/validation/test_schema_gate.py) | Mechanical: pass `property_id="P1"` to all 9 `check()` calls; replace the v1-shaped `_valid_record()` fixture with a v2-canonical baseline + a v1-legacy variant to exercise both alias families | F1, F2, F3 / — |
| `ma_poc/scripts/migrate_inferred_ids_v1_to_v2.py` (new) | One-time state migration: dispatches across JSON state, per-run snapshots, and Postgres `units` (when `DATA_PROVIDER=postgres`) | F8 / H9 |
| `ma_poc/tests/validation/test_schema_gate_v2_migration.py` (new) | F1 + F2 + F3 + F4 invariants — production-shaped fixtures | H1–H7, H11–H15 |
| `ma_poc/tests/validation/test_orchestrator_property_id_threading.py` (new) | Orchestrator signature change + property_id propagation | H1, H8 |
| `ma_poc/tests/scripts/test_migrate_inferred_ids.py` (new) | Migration script idempotency + correctness across all three stores | H9 |
| `ma_poc/scripts/gate_validation_recovery.py` (new) | Gate runner | All |

---

## 4. Detailed fixes

### 4.1 F1 — schema_gate v1→v2 migration

**Symptom.** Per the RCA: 25,634 of 30,117 record rejections (85%) caused by v1's incomplete alias chain failing on records carrying the canonical `floor_plan_name` key.

**Fix.** Replace the import and update the call site:

```python
# schema_gate.py:16  (current import line)
from .identity_fallback import compute_fallback_unit_id

# schema_gate.py — change check() signature
def check(record: dict[str, Any], property_id: str) -> SchemaGateResult:
    """Validate a single unit record against the schema.

    Args:
        record: Raw unit record dict from L3 extraction.
        property_id: The property the record belongs to. Required for v2
            fallback's hash input.

    Returns:
        SchemaGateResult with accepted record or rejection reasons.
    """
    # ... existing rent/sqft validation — see F2/F3 below ...
    # ... date validation: see F4 ...

    # Unit ID: if missing, try v2 fallback (line 164-180 in current file)
    unit_id = record.get("unit_id") or record.get("unit_number")
    inferred = False
    if not unit_id:
        fallback_id = compute_fallback_unit_id(record, property_id)
        if fallback_id:
            record = dict(record)
            record["unit_id"] = fallback_id
            inferred = True
        else:
            reasons.append("IDENTITY_FALLBACK_INSUFFICIENT")
    else:
        if not _has_physical_signal(record):
            reasons.append("IDENTITY_REQUIRES_PHYSICAL_SIGNAL")

    if reasons:
        return SchemaGateResult(accepted=None, rejection_reasons=reasons)
    return SchemaGateResult(accepted=record, inferred_id=inferred)
```

**Orchestrator update:**

```python
# orchestrator.py:88 — current
gate_result = schema_check(record)

# orchestrator.py:88 — fix
gate_result = schema_check(record, property_id)
```

`property_id` is already in scope at [`orchestrator.py:78`](../ma_poc/validation/orchestrator.py#L78) (`extract_result.property_id`). One-line change at the call site.

**Why a signature change rather than reading `property_id` from the record dict.** The v2 function's signature already separates them. Reading from the record would require either an alias-stuffing convention that pollutes the record shape, or a leakage from validator into data model. Tests for `check()` become more self-documenting when `property_id` is explicit. And Phase 2 will need this signature anyway for cross-run rescue.

**Backward compat.** v1 `inferred_<sha12>` IDs in existing state files do not match v2 `inferred_<sha16>` IDs. F8 mitigates. See §4.4.

### 4.2 F2 — v2 rent canonical-name reads

**Symptom.** [`schema_gate.py:123`](../ma_poc/validation/schema_gate.py#L123) reads:

```python
rent = record.get("asking_rent") or record.get("market_rent_low") or record.get("rent")
```

A v2-strict DB row carries `rent_low` / `rent_high` only — the chain returns `None`, the `if rent is not None:` guard skips, and pathological values (negative, >$50K) silently pass. The DB schema is v2-strict per memory `project_db_v2_schema.md`; rows fetched via the data-provider layer in `DATA_PROVIDER=postgres` mode arrive in this shape.

**Fix.**

```python
# schema_gate.py:123 — extend chain (v1 names retain priority for back-compat with H15)
rent = (
    record.get("asking_rent")
    or record.get("market_rent_low")
    or record.get("rent")
    or record.get("rent_low")
    or record.get("rent_high")
)
```

**Why v1 names keep priority.** Existing tests (`test_schema_gate.py`) and the in-flight scrape pipeline emit v1 names; flipping precedence would re-route healthy records through a different field and could shift validation behavior on edge values. The DB-read path (where v2 names appear standalone) is the only path that needs the new fallthrough. H15 locks the precedence in.

### 4.3 F3 — v2 sqft canonical-name read

**Symptom.** [`schema_gate.py:137-139`](../ma_poc/validation/schema_gate.py#L137):

```python
sqft = record.get("sqft")
if sqft in (None, "", -1, "-1"):
    sqft = record.get("square_feet")
```

Misses v2 canonical `area`. `_PHYSICAL_SIGNAL_FIELDS` 50 lines above already accepts `area` ([`schema_gate.py:79-99`](../ma_poc/validation/schema_gate.py#L79)), so the omission is internal-only.

**Fix.**

```python
# schema_gate.py:137-139 — add area as third fallback
sqft = record.get("sqft")
if sqft in (None, "", -1, "-1"):
    sqft = record.get("square_feet")
if sqft in (None, "", -1, "-1"):
    sqft = record.get("area")
```

**Sentinel preservation.** The `-1` "unknown sqft" sentinel must continue to bypass `INVALID_SQFT_NEGATIVE`; the existing branch at line 143 (`if sqft_val == -1: pass`) handles this for whichever field name supplied the `-1`. H14 asserts the sentinel works under the v2 `area` name.

### 4.4 F4 — Date placeholder pass-through

**Symptom.** 1,117 records reject with `INVALID_DATE_FORMAT`. 90.8% are missing sqft, 80.8% missing unit_id — these are "Coming Soon" placeholder rows where the source advertises a unit but provides a marketing string instead of a parseable date. The records carry useful rent + plan + bed/bath signal that is currently discarded along with the bad date.

**Fix.** Replace the reason-append with re-route. **Telemetry exception scope is narrow** — only catch the two failure modes that can legitimately fire here (event module not yet upgraded with the new `EventKind`, or import path changes during refactor). A bare `except Exception` would mask real bugs in the emit path; the date placeholder behavior is too important for the SLO dataset to silently lose events.

```python
# schema_gate.py — date validation path
avail_date = record.get("availability_date") or record.get("available_date")
if avail_date is not None and isinstance(avail_date, str):
    parsed = False
    try:
        datetime.fromisoformat(avail_date.replace("Z", "+00:00"))
        parsed = True
    except ValueError:
        try:
            date.fromisoformat(avail_date)
            parsed = True
        except ValueError:
            parsed = False
    if not parsed:
        # F4: treat unparseable string dates as placeholders.
        record = dict(record)
        record["_date_placeholder"] = avail_date
        record["available_date"] = None
        record["availability_date"] = None
        try:
            from ma_poc.observability.events import EventKind, emit
            emit(
                EventKind.DATE_PLACEHOLDER_OBSERVED,
                property_id=property_id,
                placeholder_value=avail_date[:64],
            )
        except (ImportError, AttributeError):
            # ImportError: event module not yet present in older deploys.
            # AttributeError: DATE_PLACEHOLDER_OBSERVED enum value not yet added.
            # Anything else (TypeError, OSError, ledger backend errors) must surface.
            pass
elif avail_date is not None and not isinstance(avail_date, str):
    # H7: non-string dates (corrupted types) still reject.
    reasons.append("INVALID_DATE_FORMAT")
```

**Why null both aliases.** The orchestrator's `_field_presence` reads both `available_date` and `availability_date`. Leaving the original string in place under either alias would distort post-hoc analysis on the next run.

### 4.5 F8 — State migration script

#### 4.5.1 Why this needs three dispatch paths (Postgres, JSON state, per-run snapshots)

The plan as originally drafted walked only `data/unit_index.json` + a non-existent `data/runs/*/state.json`. Verification against the live tree shows the actual state surface is more nuanced. The migration must dispatch across three stores; here's why each one matters and what the cost of skipping it would be.

**Store A — `data/state/unit_index.json` (legacy, optional).**
- **What it is.** A 934 KB JSON file, 78 properties × 2,454 units, written by `scripts/state_store.py` from the legacy `daily_runner.py` pipeline. Per [`sync_run_to_pg.py:18-21`](../ma_poc/scripts/sync_run_to_pg.py#L18), Jugnu **never writes this file**.
- **Current ID composition (verified 2026-05-05).** Zero `inferred_*` IDs. All 2,454 unit IDs are natural (extracted from source). The migration is a no-op on the current snapshot of this file — but skipping it means future legacy runs that emit v1 IDs into the file would fail to ever be re-keyed.
- **Decision.** Walk it for completeness and to keep the legacy pipeline unbroken if anyone re-runs it. Cost: ~1 second for a 1 MB file.

**Store B — Postgres `units` table (authoritative for Jugnu, when `DATA_PROVIDER=postgres`).**
- **What it is.** v2-strict schema (alembic 0002). Per memory `project_postgres_retention_policy.md`: properties/units are upsert-only, never trimmed. This is the cross-run truth source for the v2 pipeline.
- **Why JSON-only migration is insufficient.** When `DATA_PROVIDER=postgres`, the data-provider read layer fetches units from this table. If F1 starts emitting v2 IDs but the table still holds v1 IDs from any prior run, every subsequent comparison in cross-run sanity (when re-enabled in Phase 2) and every diff in `state_store` mistakes the same physical unit for a brand-new one. State_store gets a flood of `disappeared + new` deltas instead of `updated` — which exactly mirrors the Phase 0 problem we're trying to fix.
- **Why this isn't urgent today.** Verified above: Jugnu's `validate(extract_result)` call at [`jugnu_runner.py:512`](../ma_poc/scripts/jugnu_runner.py#L512) passes no `history` argument, so `cross_run_sanity` returns no flags regardless of ID continuity. The downstream consumer is dormant. **But.** Once Phase 2 wires history back in, stale v1 IDs in Postgres become a silent correctness bug — at which point we've shipped F1 already and can't migrate cleanly without taking the system offline. Migrate now, while the consumer is dormant and the read window is benign.
- **Decision.** Walk the `units` table when `DATA_PROVIDER=postgres` is detected. Use a transactional `UPDATE` keyed on the primary key, with a `WHERE unit_id ~ '^inferred_[0-9a-f]{12}$'` filter so the sweep is bounded to v1-shaped rows. Run in a single transaction per property to keep the diff atomic.

**Store C — `data/runs/{date}/{shard}/properties.json` (per-run snapshots).**
- **What it is.** Per-shard JSON written by Jugnu and the legacy runner. Currently retained on disk for ~30 days (raw_html cleanup) and synced to Postgres `property_snapshots` (3-day retention per memory).
- **Why include it.** These are the inputs to backfill / re-sync runs. If a backfill replays a 2026-05-04 properties.json into Postgres after the migration, the v1 IDs would be re-introduced into the v2 `units` table via the upsert path.
- **Decision.** Walk every `data/runs/*/*/properties.json` for files newer than the retention window (30 days). Backfills outside that window are served from GCS-archived per-run dirs (per memory) and don't need migration — those archives are read-only history.

**What the plan no longer needs to walk.**
- `data/runs/*/state.json` — this path **does not exist**. The previous spec invented it. Removed from the dispatch.
- `property_snapshots` Postgres table — covered by F8's Store-C pass since snapshots are derived from the same properties.json.

**Run order with the corrected dispatch.** Merge PR → deploy → run migration with `--dry-run` to confirm counts → run migration without flag → trigger next scheduled run. Expected counts on the current production state: `migrated_jsonl=0`, `migrated_postgres={depends on inferred_ counts in units table}`, `migrated_runs=N` for any shards that hit the v1 fallback in the last 30 days. If `migrated_postgres > 0` and we have not yet shipped F1, the migration must NOT run — it would briefly leave the gate emitting v1 IDs while the table holds v2 IDs.

#### 4.5.2 Algorithm

```python
# ma_poc/scripts/migrate_inferred_ids_v1_to_v2.py
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

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from ma_poc.scripts.identity_fallback import compute_fallback_unit_id

V1_ID_RE = re.compile(r"^inferred_[0-9a-f]{12}$")
RETENTION_DAYS = 30


def _migrate_record_dict(units: dict, property_id: str) -> tuple[int, int, list[tuple[str, str]]]:
    """Returns (migrated, skipped, collisions). collisions = [(old_id, existing_new_id)]."""
    migrated = 0
    skipped = 0
    collisions: list[tuple[str, str]] = []
    new_units: dict = {}
    for old_id, rec in units.items():
        if not V1_ID_RE.match(old_id):
            new_units[old_id] = rec
            skipped += 1
            continue
        new_id = compute_fallback_unit_id(rec, property_id)
        if new_id is None:
            new_units[old_id] = rec
            skipped += 1
            continue
        if new_id in new_units:
            collisions.append((old_id, new_id))
            continue  # do not overwrite; first record wins
        rec = dict(rec)
        rec["unit_id"] = new_id
        new_units[new_id] = rec
        migrated += 1
    return migrated, skipped, collisions


# ---- Store A: legacy unit_index.json ---------------------------------------

def migrate_unit_index(path: Path, dry_run: bool) -> dict:
    if not path.exists():
        return {"file": str(path), "exists": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not dry_run:
        backup = path.with_suffix(".v1_backup.json")
        if not backup.exists():
            backup.write_text(json.dumps(data, indent=2), encoding="utf-8")
    total_m = total_s = 0
    all_collisions: list = []
    new_data = {}
    for property_id, units in data.items():
        if not isinstance(units, dict):
            new_data[property_id] = units
            continue
        m, s, coll = _migrate_record_dict(units, property_id)
        total_m += m
        total_s += s
        if coll:
            all_collisions.extend((property_id, *c) for c in coll)
        # Reuse the migrated map
        rebuilt: dict = {}
        for old_id, rec in units.items():
            if V1_ID_RE.match(old_id):
                new_id = compute_fallback_unit_id(rec, property_id)
                if new_id and new_id not in rebuilt:
                    rec = dict(rec)
                    rec["unit_id"] = new_id
                    rebuilt[new_id] = rec
                    continue
            rebuilt[old_id] = rec
        new_data[property_id] = rebuilt
    if not dry_run:
        path.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
    return {"file": str(path), "migrated": total_m, "skipped": total_s, "collisions": all_collisions}


# ---- Store B: Postgres `units` ---------------------------------------------

def migrate_postgres_units(dry_run: bool) -> dict:
    if os.getenv("DATA_PROVIDER", "filesystem") != "postgres":
        return {"skipped_reason": "DATA_PROVIDER != postgres"}
    from sqlalchemy import create_engine, text  # local import — only when needed
    db_url = os.environ["DATABASE_URL"]
    engine = create_engine(db_url)
    migrated = 0
    skipped = 0
    collisions: list = []
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT canonical_id, unit_id, payload FROM units "
            "WHERE unit_id ~ '^inferred_[0-9a-f]{12}$'"
        )).fetchall()
        for canonical_id, old_id, payload in rows:
            rec = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            new_id = compute_fallback_unit_id(rec, str(canonical_id))
            if new_id is None:
                skipped += 1
                continue
            existing = conn.execute(text(
                "SELECT 1 FROM units WHERE canonical_id = :cid AND unit_id = :nid"
            ), {"cid": canonical_id, "nid": new_id}).first()
            if existing:
                collisions.append((canonical_id, old_id, new_id))
                continue
            if not dry_run:
                conn.execute(text(
                    "UPDATE units SET unit_id = :nid WHERE canonical_id = :cid AND unit_id = :oid"
                ), {"nid": new_id, "cid": canonical_id, "oid": old_id})
            migrated += 1
    return {"migrated": migrated, "skipped": skipped, "collisions": collisions}


# ---- Store C: per-run properties.json --------------------------------------

def _properties_files_in_window() -> Iterator[Path]:
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    for path in Path("ma_poc/data/runs").rglob("properties.json"):
        try:
            run_date_str = path.parts[-3]  # runs/{date}/{shard}/properties.json
            run_dt = datetime.strptime(run_date_str, "%Y-%m-%d")
            if run_dt >= cutoff:
                yield path
        except (ValueError, IndexError):
            continue


def migrate_run_properties(dry_run: bool) -> list[dict]:
    results: list[dict] = []
    for path in _properties_files_in_window():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not dry_run:
            backup = path.with_suffix(".v1_backup.json")
            if not backup.exists():
                backup.write_text(json.dumps(data, indent=2), encoding="utf-8")
        total_m = 0
        for prop in data if isinstance(data, list) else []:
            property_id = prop.get("Unique ID") or prop.get("Property ID") or ""
            for unit in prop.get("units", []) or []:
                old_id = unit.get("unit_id", "")
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
    res_a = migrate_unit_index(Path("ma_poc/data/state/unit_index.json"), args.dry_run)
    print(json.dumps(res_a, indent=2, default=str))

    print("\n=== Store B: Postgres units ===")
    res_b = migrate_postgres_units(args.dry_run)
    print(json.dumps(res_b, indent=2, default=str))

    print("\n=== Store C: per-run properties.json (last 30 days) ===")
    res_c = migrate_run_properties(args.dry_run)
    print(json.dumps(res_c, indent=2, default=str))


if __name__ == "__main__":
    main()
```

**Collision behavior.** Two v1 records that differed only in rent (so they had distinct v1 IDs) collapse to a single v2 ID. The script logs each collision but does NOT overwrite — first record wins, second is dropped from the migrated index. This is correct: the two records describe the same physical unit, and one of them was an artifact of the rent-volatility bug. The Phase 2 cross-run rescue will reconcile any genuinely lost data using yesterday's history. Collision count is reported per store and must appear verbatim in the PR description.

**Run order at deploy time.** Merge PR → deploy F1 → run `python -m ma_poc.scripts.migrate_inferred_ids_v1_to_v2 --dry-run` (review counts) → run without flag → trigger next scheduled run. The script must run **after** F1 is deployed (so v2 is the active emit format) and **before** the next production run (so the index is consistent on first lookup).

---

## 5. Tests

The brief is unambiguous: tests must validate **production behavior**, not implementation. That means each test fixture must shape itself like a real record from the production pipeline (Jugnu adapter output, DB read via data-provider, or a backfill replay), not a hand-rolled minimal dict tuned to the function under test.

### 5.1 Production-shaped fixture taxonomy

Three fixture archetypes are pulled into a single conftest at `ma_poc/tests/validation/conftest.py` and re-used across all test files. Each one mirrors a real production source with verified field shapes.

```python
# ma_poc/tests/validation/conftest.py
"""Production-shaped record fixtures for validation tests.

Each fixture mirrors a real production source:
  - jugnu_v2_record: shape emitted by GenericAdapter._format_v2_unit
                     (canonical v2 names: floor_plan_name, beds, baths, area,
                     rent_low, rent_high)
  - legacy_v1_record: shape emitted by scripts/scrape_properties.py
                     (legacy names: floor_plan_type, bedrooms, bathrooms,
                     sqft, asking_rent, market_rent_low)
  - mixed_record: shape emitted when a Jugnu adapter falls back through
                     scrape_properties._add (carries both alias families)
"""
from __future__ import annotations

import pytest


@pytest.fixture
def jugnu_v2_record() -> dict:
    """Canonical Jugnu v2 record. Mirrors _format_v2_unit output.

    Confirmed against scripts/CLAUDE.md "V2 unit-dict conventions" table:
    rent_low/rent_high (int), floor_plan_name, beds, baths, area.
    """
    return {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1.0,
        "area": 750,
        "rent_low": 1450,
        "rent_high": 1450,
        "available_date": "2026-05-12",
    }


@pytest.fixture
def legacy_v1_record() -> dict:
    """Pre-Jugnu legacy record. Mirrors scrape_properties._add output."""
    return {
        "unit_id": "1004",
        "floor_plan_type": "A1",
        "bedrooms": 1,
        "bathrooms": 1,
        "sqft": 750,
        "asking_rent": 1450,
        "market_rent_low": 1450,
        "availability_date": "2026-05-12",
    }


@pytest.fixture
def mixed_record() -> dict:
    """Record carrying both v1 and v2 names (transitional shape).

    Emitted when a Jugnu adapter's V2 transform copies through legacy
    fields without stripping them. v1 names should win for back-compat
    per F2's lookup precedence.
    """
    return {
        "unit_id": "1004",
        "floor_plan_name": "A1",
        "floor_plan_type": "A1",
        "beds": 1,
        "bedrooms": 1,
        "area": 750,
        "sqft": 750,
        "rent_low": 1500,
        "asking_rent": 1450,  # v1 wins per H15 — validates against 1450
        "available_date": "2026-05-12",
    }


@pytest.fixture
def jugnu_v2_no_unit_id_record() -> dict:
    """Production-real shape: extractor emitted plan + signal but no unit_id.
    This is the case that drove 25,634 of 30,117 rejections in the RCA.
    """
    return {
        "floor_plan_name": "Aspen 1BR",
        "beds": 1,
        "baths": 1.0,
        "area": 740,
        "rent_low": 2150,
        "rent_high": 2150,
    }


@pytest.fixture
def coming_soon_record() -> dict:
    """Production-real shape: 'Coming Soon' marketing string in date field.
    F4 reroutes this to placeholder pass-through.
    """
    return {
        "floor_plan_name": "B2",
        "beds": 2,
        "area": 1100,
        "rent_low": 2400,
        "available_date": "Spring 2026",
    }
```

### 5.2 `tests/validation/test_schema_gate_v2_migration.py` — full bodies

```python
"""F1 + F2 + F3 + F4 invariants. All fixtures are production-shaped."""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from ma_poc.validation.schema_gate import check


# ---- H1: static scan of the import surface ---------------------------------

def test_h1_schema_gate_imports_v2_only() -> None:
    """schema_gate.py must not import v1 fallback by name."""
    from ma_poc.validation import schema_gate
    src = open(schema_gate.__file__, encoding="utf-8").read()
    assert "compute_fallback_unit_id" in src
    assert "from .identity_fallback import compute_fallback_id" not in src
    # Permitted: importing both via comma in the deprecated shim re-export
    # (validates the shim itself, not the gate)
    assert re.search(r"\bcompute_fallback_id\b\s*\(", src) is None, \
        "schema_gate.py must not CALL compute_fallback_id"


# ---- H2: the central recovery test (production-shape) ----------------------

def test_h2_floor_plan_name_alias_recovery(jugnu_v2_no_unit_id_record: dict) -> None:
    """The 25,634-rejections fix. v2 record with no unit_id must be
    accepted via inferred fallback."""
    result = check(jugnu_v2_no_unit_id_record, property_id="prop_123")
    assert result.accepted is not None, \
        f"Expected accept, got reasons: {result.rejection_reasons}"
    assert result.inferred_id is True
    assert result.accepted["unit_id"].startswith("inferred_")
    # v2 hash digest is 16 hex chars
    assert len(result.accepted["unit_id"]) == len("inferred_") + 16


# ---- H3: legacy v1 shape still works ---------------------------------------

def test_h3_legacy_floor_plan_type_still_works(legacy_v1_record: dict) -> None:
    """Records emitted by scrape_properties._add must still pass."""
    record = dict(legacy_v1_record)
    del record["unit_id"]  # Force fallback path
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert result.inferred_id is True


# ---- H4: floor_plan-only still rejects (Phase 0 contract) ------------------

def test_h4_floor_plan_only_still_rejects() -> None:
    """v2's 'fp + ≥1 other identifying field' rule preserved in Phase 0.
    Phase 1 will replace this with units_weak routing — see H4-bis spec."""
    record = {"floor_plan_name": "A1"}
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "IDENTITY_FALLBACK_INSUFFICIENT" in result.rejection_reasons


# ---- H5: rent-stability of inferred IDs ------------------------------------

def test_h5_rent_change_does_not_alter_unit_id(jugnu_v2_no_unit_id_record: dict) -> None:
    """v2 inferred IDs must be stable across rent changes (vs v1 which
    rolled rent into the hash). This is the bug that caused diff churn."""
    base = dict(jugnu_v2_no_unit_id_record)
    r1 = check({**base, "rent_low": 2000, "rent_high": 2000}, property_id="prop_123")
    r2 = check({**base, "rent_low": 2200, "rent_high": 2200}, property_id="prop_123")
    r3 = check({**base, "rent_low": 1700, "rent_high": 1900}, property_id="prop_123")
    assert r1.accepted is not None and r2.accepted is not None and r3.accepted is not None
    assert r1.accepted["unit_id"] == r2.accepted["unit_id"] == r3.accepted["unit_id"]


def test_h5b_available_date_change_does_not_alter_unit_id(jugnu_v2_no_unit_id_record: dict) -> None:
    """Companion to H5: available_date must also not affect identity."""
    base = dict(jugnu_v2_no_unit_id_record)
    r1 = check({**base, "available_date": "2026-05-01"}, property_id="prop_123")
    r2 = check({**base, "available_date": "2026-08-15"}, property_id="prop_123")
    assert r1.accepted["unit_id"] == r2.accepted["unit_id"]


# ---- H6/H7: F4 date placeholder routing ------------------------------------

def test_h6_date_placeholder_pass_through(coming_soon_record: dict) -> None:
    """Unparseable string date is accepted with placeholder stashing."""
    with patch("ma_poc.observability.events.emit") as mock_emit:
        result = check(coming_soon_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["available_date"] is None
    assert result.accepted["availability_date"] is None
    assert result.accepted["_date_placeholder"] == "Spring 2026"
    assert "INVALID_DATE_FORMAT" not in (result.rejection_reasons or [])
    # Telemetry fired exactly once with the placeholder value (truncated to 64)
    mock_emit.assert_called_once()
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs.get("placeholder_value") == "Spring 2026"


def test_h7_non_string_date_still_rejects() -> None:
    """Non-string corrupted date types must still reject — F4 only reroutes
    the string-parse-failure path, not corrupted types."""
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "available_date": 42,  # int — corrupted type
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_DATE_FORMAT" in result.rejection_reasons


# ---- H11–H14: F2 + F3 v2 canonical-name reads ------------------------------

def test_h11_v2_rent_low_absurd_rejected() -> None:
    """v2-only record with absurd rent_low must reject. Pre-fix this
    slipped through because the lookup chain returned None."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 60000,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons


def test_h12_v2_rent_low_negative_rejected() -> None:
    """v2-only record with negative rent_low must reject."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": -100,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons


def test_h13_v2_area_absurd_rejected() -> None:
    """v2-only record with absurd area must reject. Pre-fix this slipped
    through because sqft lookup never checked area."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 99999,
        "rent_low": 1500,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_SQFT_ABSURD" in result.rejection_reasons


def test_h14_v2_area_minus_one_sentinel_accepted() -> None:
    """area=-1 sentinel ('unknown') must NOT fire INVALID_SQFT_NEGATIVE.
    Verifies sentinel semantics carry over from sqft to area."""
    record = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": -1,
        "rent_low": 1500,
    }
    result = check(record, property_id="prop_123")
    assert result.accepted is not None
    assert "INVALID_SQFT_NEGATIVE" not in (result.rejection_reasons or [])
    assert "INVALID_SQFT_ABSURD" not in (result.rejection_reasons or [])


# ---- H15: rent lookup precedence -------------------------------------------

def test_h15_rent_lookup_precedence_v1_first(mixed_record: dict) -> None:
    """When both v1 (asking_rent=1450) and v2 (rent_low=1500) names are
    present, v1 wins per F2's lookup chain. The record validates against
    1450, which is in-range. If v2 had won, this test would still pass
    by accident (1500 is also in range), so we add an absurd-v1 guard:"""
    record = dict(mixed_record)
    record["asking_rent"] = 60000      # v1 absurd
    record["rent_low"] = 1500          # v2 healthy
    result = check(record, property_id="prop_123")
    assert result.accepted is None
    assert "INVALID_RENT_ABSURD" in result.rejection_reasons, \
        "If v2 had won, rent_low=1500 would pass; v1 priority lock failed"


# ---- Migration trace: end-to-end Jugnu-shape happy path --------------------

def test_jugnu_v2_record_passes_unchanged(jugnu_v2_record: dict) -> None:
    """A complete v2 record from a healthy Jugnu adapter passes cleanly,
    with the natural unit_id preserved. inferred_id must be False."""
    result = check(jugnu_v2_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["unit_id"] == "1004"
    assert result.inferred_id is False
    assert result.rejection_reasons == []


def test_legacy_v1_record_passes_unchanged(legacy_v1_record: dict) -> None:
    """Symmetric companion: full v1 record passes cleanly too."""
    result = check(legacy_v1_record, property_id="prop_123")
    assert result.accepted is not None
    assert result.accepted["unit_id"] == "1004"
    assert result.inferred_id is False
```

### 5.3 `tests/validation/test_orchestrator_property_id_threading.py`

```python
"""H1 (signature) + H8 (field_presence preservation) for the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

from ma_poc.validation.orchestrator import validate, _field_presence


@dataclass
class _StubExtractResult:
    property_id: str
    records: list = field(default_factory=list)


def test_h1_orchestrator_threads_property_id_into_schema_check() -> None:
    """orchestrator.validate must pass the extract_result.property_id into
    the schema_check call, not call it without args."""
    er = _StubExtractResult(
        property_id="prop_42",
        records=[{"floor_plan_name": "A1", "beds": 1, "area": 750, "rent_low": 1500}],
    )
    with patch("ma_poc.validation.orchestrator.schema_check") as mock_check:
        mock_check.return_value.accepted = None
        mock_check.return_value.rejection_reasons = ["IDENTITY_FALLBACK_INSUFFICIENT"]
        mock_check.return_value.inferred_id = False
        validate(er)
    mock_check.assert_called_once()
    args, kwargs = mock_check.call_args
    # Either positional or keyword form is acceptable
    assert "prop_42" in args or kwargs.get("property_id") == "prop_42"


def test_h8_field_presence_unchanged_for_v2_record() -> None:
    """The wide-alias field_presence map must continue to mark v2 fields
    as present even after the schema_check signature change."""
    record = {
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
    }
    presence = _field_presence(record)
    assert presence["floor_plan_name"] is True
    assert presence["beds"] is True
    assert presence["sqft"] is True   # canonical key in the alias map (area aliases to sqft)
    assert presence["rent"] is True   # canonical key (rent_low aliases — verify in F9)


def test_h8b_field_presence_unchanged_for_v1_record() -> None:
    """Symmetric: v1-shaped record must still mark all fields present."""
    record = {
        "floor_plan_type": "A1",
        "bedrooms": 1,
        "sqft": 750,
        "asking_rent": 1500,
    }
    presence = _field_presence(record)
    assert presence["floor_plan_name"] is True
    assert presence["beds"] is True
    assert presence["sqft"] is True
    assert presence["rent"] is True
```

> **Note for the implementer.** `_IDENTITY_FIELD_ALIASES["rent"]` in [`orchestrator.py:26`](../ma_poc/validation/orchestrator.py#L26) currently does **not** include `rent_low` / `rent_high`. F9 (the identity-gap event) needs to see these as "rent present". Add `rent_low` and `rent_high` to that tuple in the same edit. This also brings the orchestrator's presence map in line with F2.

### 5.4 `tests/scripts/test_migrate_inferred_ids.py`

```python
"""Migration script: idempotency, dry-run safety, collision handling, and
correct dispatch across the three stores."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ma_poc.scripts.migrate_inferred_ids_v1_to_v2 import (
    migrate_unit_index,
    migrate_postgres_units,
    V1_ID_RE,
)
from ma_poc.scripts.identity_fallback import compute_fallback_unit_id


def _v1_id(record: dict) -> str:
    """Build a synthetic v1-shaped 12-char ID for fixtures."""
    import hashlib
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


# ---- Collision handling ----------------------------------------------------

def test_collision_between_v1_records_logged_not_overwritten(tmp_path: Path) -> None:
    """Two v1 records that differed only in rent collapse to one v2 ID.
    First record wins; second is logged in collisions[]."""
    base = {"floor_plan_type": "A1", "bedrooms": 1, "sqft": 750}
    rec_a = dict(base, asking_rent=1500)
    rec_b = dict(base, asking_rent=1700)  # different rent → different v1 id
    v1_a = _v1_id(rec_a)
    v1_b = _v1_id(rec_b)
    state = {"prop_42": {v1_a: dict(rec_a, unit_id=v1_a), v1_b: dict(rec_b, unit_id=v1_b)}}
    fp = tmp_path / "unit_index.json"
    fp.write_text(json.dumps(state), encoding="utf-8")

    res = migrate_unit_index(fp, dry_run=False)
    after = json.loads(fp.read_text(encoding="utf-8"))
    # Both v1 records collapse to the same v2 id (rent excluded from hash)
    assert len(after["prop_42"]) == 1
    new_id = list(after["prop_42"].keys())[0]
    assert re.match(r"^inferred_[0-9a-f]{16}$", new_id)
    # The collision was reported, not silently dropped
    assert any(
        c[1] == v1_b or c[2] == new_id
        for c in res.get("collisions", [])
    ), f"Expected collision report; got {res}"


# ---- Postgres dispatch gate ------------------------------------------------

def test_postgres_path_skipped_when_data_provider_is_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """When DATA_PROVIDER != postgres, the Postgres path must short-circuit
    without attempting a connection."""
    monkeypatch.setenv("DATA_PROVIDER", "filesystem")
    res = migrate_postgres_units(dry_run=False)
    assert res.get("skipped_reason") == "DATA_PROVIDER != postgres"


# ---- Regex shape -----------------------------------------------------------

def test_v1_id_regex_matches_only_12_hex_chars() -> None:
    """The v1 detection regex must not match v2 (16-char) or natural IDs."""
    assert V1_ID_RE.match("inferred_a1b2c3d4e5f6")  # 12 hex
    assert not V1_ID_RE.match("inferred_a1b2c3d4e5f6a1b2")  # 16 hex (v2)
    assert not V1_ID_RE.match("U101")  # natural
    assert not V1_ID_RE.match("inferred_xyz")  # non-hex
```

### 5.5 Mechanical updates to existing test files

Two files need a one-time signature update to pass `property_id`. These changes are mechanical but the [`test_schema_gate_unit_id_alone.py`](../ma_poc/tests/validation/test_schema_gate_unit_id_alone.py) update also corrects the misleading docstring at lines 45-51 that explicitly references the v1 fallback's behavior — under v2 that docstring becomes wrong.

**[`test_schema_gate_unit_id_alone.py`](../ma_poc/tests/validation/test_schema_gate_unit_id_alone.py) — full edit set:**

```python
# Line-by-line:
#   line 14:  res = check({"unit_id": "101"})
#         →  res = check({"unit_id": "101"}, property_id="P1")
#   line 20:  res = check({"unit_id": "101", "floor_plan_name": "Aspen"})
#         →  res = check({"unit_id": "101", "floor_plan_name": "Aspen"}, property_id="P1")
#   line 26, 31, 36, 41:  same pattern — append `, property_id="P1"`
#   line 45-62:  rewrite the function name + docstring + body to use v2 names

def test_unit_id_with_inferred_fallback_still_works() -> None:
    """When unit_id is missing but identity_fallback succeeds, no rejection.

    Post-F1: ``compute_fallback_unit_id`` (v2) accepts both ``floor_plan_name``
    (canonical) and ``floor_plan_type`` (legacy alias). This test exercises
    the legacy alias path to confirm backward compat (mirrors H3).
    """
    res = check(
        {
            "floor_plan_type": "Aspen",
            "bedrooms": 1,
            "bathrooms": 1,
            "sqft": 750,
            "asking_rent": 1500,
        },
        property_id="P1",
    )
    assert res.accepted is not None
    assert res.inferred_id is True
```

**[`test_schema_gate.py`](../ma_poc/tests/validation/test_schema_gate.py) — full edit set:**

The existing 9 `check(...)` calls each get `, property_id="P1"` appended. Additionally, augment the v1-only `_valid_record()` fixture with a v2 sibling so the same suite exercises both shapes:

```python
def _valid_v2_record(**overrides: object) -> dict:
    r = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "rent_high": 1500,
    }
    r.update(overrides)
    return r


# Append a v2 mirror for each of the 9 existing tests, e.g.:
def test_schema_accepts_full_valid_v2_record() -> None:
    result = check(_valid_v2_record(), property_id="P1")
    assert result.accepted is not None
    assert result.rejection_reasons == []


def test_schema_rejects_negative_rent_v2() -> None:
    result = check(_valid_v2_record(rent_low=-100), property_id="P1")
    assert result.accepted is None
    assert "INVALID_RENT_NEGATIVE" in result.rejection_reasons
```

Net: 9 mechanical signature appends + 9 v2-mirror tests + 1 fixture function. Coverage doubles cleanly without touching the original test bodies.

### 5.6 Coverage map

| Test | File | Validates |
|---|---|---|
| `test_h1_schema_gate_imports_v2_only` | `test_schema_gate_v2_migration.py` | F1 / H1 |
| `test_h2_floor_plan_name_alias_recovery` | `test_schema_gate_v2_migration.py` | F1 / H2 (RCA-validated central fix) |
| `test_h3_legacy_floor_plan_type_still_works` | `test_schema_gate_v2_migration.py` | F1 / H3 |
| `test_h4_floor_plan_only_still_rejects` | `test_schema_gate_v2_migration.py` | F1 / H4 |
| `test_h5_rent_change_does_not_alter_unit_id` | `test_schema_gate_v2_migration.py` | F1 / H5 (rent-stability) |
| `test_h5b_available_date_change_does_not_alter_unit_id` | `test_schema_gate_v2_migration.py` | F1 / H5 |
| `test_h6_date_placeholder_pass_through` | `test_schema_gate_v2_migration.py` | F4 / H6 (incl. emit telemetry) |
| `test_h7_non_string_date_still_rejects` | `test_schema_gate_v2_migration.py` | F4 / H7 |
| `test_h11_v2_rent_low_absurd_rejected` | `test_schema_gate_v2_migration.py` | F2 / H11 |
| `test_h12_v2_rent_low_negative_rejected` | `test_schema_gate_v2_migration.py` | F2 / H12 |
| `test_h13_v2_area_absurd_rejected` | `test_schema_gate_v2_migration.py` | F3 / H13 |
| `test_h14_v2_area_minus_one_sentinel_accepted` | `test_schema_gate_v2_migration.py` | F3 / H14 |
| `test_h15_rent_lookup_precedence_v1_first` | `test_schema_gate_v2_migration.py` | F2 / H15 |
| `test_jugnu_v2_record_passes_unchanged` | `test_schema_gate_v2_migration.py` | E2E happy-path (Jugnu-shape) |
| `test_legacy_v1_record_passes_unchanged` | `test_schema_gate_v2_migration.py` | E2E happy-path (legacy shape) |
| `test_h1_orchestrator_threads_property_id_into_schema_check` | `test_orchestrator_property_id_threading.py` | F1 / H1 (orchestrator side) |
| `test_h8_field_presence_unchanged_for_v2_record` | `test_orchestrator_property_id_threading.py` | F1 / H8 |
| `test_h8b_field_presence_unchanged_for_v1_record` | `test_orchestrator_property_id_threading.py` | F1 / H8 |
| `test_h9_unit_index_migration_idempotent` | `test_migrate_inferred_ids.py` | F8 / H9 |
| `test_h9b_dry_run_does_not_mutate` | `test_migrate_inferred_ids.py` | F8 / H9 |
| `test_collision_between_v1_records_logged_not_overwritten` | `test_migrate_inferred_ids.py` | F8 (collision contract) |
| `test_postgres_path_skipped_when_data_provider_is_filesystem` | `test_migrate_inferred_ids.py` | F8 (dispatch gate) |
| `test_v1_id_regex_matches_only_12_hex_chars` | `test_migrate_inferred_ids.py` | F8 (regex shape) |

---

## 6. Gate runner

```python
# ma_poc/scripts/gate_validation_recovery.py

import argparse
import subprocess
import sys

FIX_TEST_MAP = {
    "F1": ["ma_poc/tests/validation/test_schema_gate_v2_migration.py",
           "ma_poc/tests/validation/test_orchestrator_property_id_threading.py"],
    "F2": ["ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h11_v2_rent_low_absurd_rejected",
           "ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h12_v2_rent_low_negative_rejected",
           "ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h15_rent_lookup_precedence_v1_first"],
    "F3": ["ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h13_v2_area_absurd_rejected",
           "ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h14_v2_area_minus_one_sentinel_accepted"],
    "F4": ["ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h6_date_placeholder_pass_through",
           "ma_poc/tests/validation/test_schema_gate_v2_migration.py::test_h7_non_string_date_still_rejects"],
    "F8": ["ma_poc/tests/scripts/test_migrate_inferred_ids.py"],
}

def run_fix(fix_id: str) -> bool:
    targets = FIX_TEST_MAP.get(fix_id, [])
    cmd = ["pytest", "-v"] + targets
    return subprocess.run(cmd).returncode == 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["all", "phase"])
    parser.add_argument("phase", nargs="?", default=None)
    args = parser.parse_args()
    targets = list(FIX_TEST_MAP.keys()) if args.command == "all" else [args.phase]
    results = {f: run_fix(f) for f in targets}
    print(f"\n{'='*60}\nSummary:")
    for f, ok in results.items():
        print(f"  Fix {f}: {'PASS' if ok else 'FAIL'}")
    if not all(results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()
```

---

## 7. Definition of Done

### 7.1 New tests pass

```bash
cd ma_poc && pytest \
  tests/validation/test_schema_gate_v2_migration.py \
  tests/validation/test_orchestrator_property_id_threading.py \
  tests/scripts/test_migrate_inferred_ids.py \
  -v
```

### 7.2 Prior gates still green

```bash
pytest . --ignore=data --ignore=config
```

Mechanical updates to existing tests (per §5.5):
- All 9 `check(...)` calls in `test_schema_gate.py` get `, property_id="P1"` appended; v2 mirror tests added (no changes to existing logical assertions).
- All 10 `check(...)` calls in `test_schema_gate_unit_id_alone.py` get `, property_id="P1"` appended.
- The misleading docstring at `test_schema_gate_unit_id_alone.py:45-51` ("the v1 ``compute_fallback_id`` reads ``floor_plan_type``...") is rewritten to describe v2's dual-alias acceptance — see §5.5 for the replacement text.

### 7.3 New gate

```bash
python ma_poc/scripts/gate_validation_recovery.py all
```

All three fixes report PASS.

### 7.4 Pre-merge empirical confirmation

Three checks must complete before merge:

**Check 1 — production sample (F1 recovery).**

```bash
jq '.[] | select(._extract_result.records[]?.unit_id | not)
        | ._extract_result.records[]
        | {fpn: .floor_plan_name, fpt: .floor_plan_type, flpn: .floorplan_name}' \
   ma_poc/data/runs/2026-05-05/shard_0/properties.json \
   | jq -s 'group_by(.fpn != null, .fpt != null, .flpn != null)
        | map({key: (map(.) | length), count: length})'
```

Expected: ≥80% of records-without-unit_id carry `floor_plan_name` rather than `floor_plan_type` or `floorplan_name`. If <50%, escalate.

**Check 2 — single-property dry run (F1 recovery).** Pick property `218359`. Re-run validation locally with F1 applied. Confirm `IDENTITY_FALLBACK_INSUFFICIENT` count drops by ≥80%. If <50%, halt merge.

**Check 3 — F2/F3 production exposure scan.** Quantify how many records currently slip past rent/sqft validation due to v2-only naming. Run against the most recent production shard:

```bash
jq -r '.[] | ._extract_result.records[]?
        | [.unit_id // "(none)",
           (.asking_rent // .market_rent_low // .rent // "null"),
           (.rent_low // "null"),
           (.sqft // .square_feet // "null"),
           (.area // "null")]
        | @csv' \
   ma_poc/data/runs/2026-05-05/shard_0/properties.json \
   | awk -F',' '$2=="\"null\"" && $3!="\"null\"" {v2_rent_only++}
                $4=="\"null\"" && $5!="\"null\"" {v2_area_only++}
                END {print "v2_rent_only:", v2_rent_only, "v2_area_only:", v2_area_only}'
```

Expected: a non-zero count for either metric demonstrates F2/F3 are recovering real production records. Zero counts mean either the bug doesn't currently manifest in this shard (acceptable; the fix is still correct for v2-strict DB reads) or the data-provider boundary is rewriting field names before validation (in which case verify the rewrite is intentional).

Capture all three check outputs in the PR description.

### 7.5 Code quality

- `mypy --strict` clean on the four modified production files.
- `ruff check` clean.

### 7.6 PR description must be honest

- Lists each fix (F1, F2, F3, F4, F8, F9) with its hard-invariant numbers (H1–H15).
- Pastes the §7.4 Check 1, Check 2, Check 3 outputs verbatim.
- Pastes the §8 staging migration `--dry-run` and live-pass outputs (Store A, B, C counts + collisions).
- States the v1 function remains in `ma_poc/scripts/identity_fallback.py` for the migration script's use, and references Phase 2 as the next step (cross-run rescue, multi-stream output).
- Notes that drift-detection alarms may be elevated for one cycle as v1→v2 IDs roll forward through the index — currently dormant in Jugnu but will activate when Phase 2 wires history.
- Does NOT claim a properties-recovered uplift number until measured on the next production run.

---

## 8. Rollout

Single PR. After merge:

1. Deploy to staging shard (1 shard, 499 properties).
2. Snapshot Postgres before migration: `gcloud sql backups create --instance=jugnu-db-staging`.
3. Run F8 migration script against staging — first `--dry-run`, then the live pass:
   ```bash
   DATA_PROVIDER=postgres python -m ma_poc.scripts.migrate_inferred_ids_v1_to_v2 --dry-run
   DATA_PROVIDER=postgres python -m ma_poc.scripts.migrate_inferred_ids_v1_to_v2
   ```
   Verify all three stores reported counts (Store A, B, C). Capture stdout in the PR description.
4. Trigger a manual run on staging.
5. Compare metrics against the 2026-05-05 baseline:
   - `validate.record_rejected` total count: expected to drop from ~3,012/shard to ≤1,000/shard.
   - `IDENTITY_FALLBACK_INSUFFICIENT` reason count: expected to drop by ≥80% on the affected signatures.
   - `validate.identity_fallback` count (successful inferences): expected to rise by ~2,500/shard.
   - `validate.date_placeholder_observed` (new event): expected to fire for ~1,100 records/shard (F4 recovery).
   - `INVALID_RENT_ABSURD` / `INVALID_RENT_NEGATIVE` / `INVALID_SQFT_ABSURD` reason counts: should be **non-decreasing** (F2/F3 makes the gate stricter, never laxer).
   - `success_rate`: expected to rise from ~84% to ≥94%.
   - LLM cost: expected to remain flat.
6. If metrics meet expectations, snapshot production Postgres, then deploy to remaining 9 shards.
7. Run F8 migration script against production (same dry-run-then-live cadence).
8. Trigger normal scheduled run.
9. Monitor `cross_run_sanity` flags for 48 hours. Note: per §4.5.1 Store B, cross-run sanity is currently dormant in Jugnu — flags will be empty; the watch is for when Phase 2 enables it.

If any staging metric is materially below the expected delta (e.g., success_rate stays below 90%), halt the production deploy. Roll back via the Postgres backup snapshot from step 2 + revert the PR.

---

## 9. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Records use `floor_plan_type` more than expected; F1's recovery is smaller | Low | §7.4 Check 1 catches before merge |
| v2 hash collision within a property collapses to one record (multiple identical units) | Medium | Existing merge cascade R1d/R1e handles; **fully addressed by Phase 2's cross-run rescue** which fans out using yesterday's history |
| F8 Postgres `units` migration races against a concurrent production run | Low (script must run during scheduled-job downtime per §4.5.1) | Script runs in single transaction per property; `WHERE unit_id ~ '^inferred_[0-9a-f]{12}$'` filter is bounded; pre-migration `pg_dump` snapshot is mandatory per §8 |
| F8 Postgres path silently no-ops because `DATA_PROVIDER` env is unset on the migration host | Medium | Script prints `skipped_reason` explicitly; PR description must paste the migration output. Add `assert os.getenv("DATA_PROVIDER")` to the runbook step. |
| Cross-run sanity raises spurious flags during migration cycle | Low (currently dormant — see §4.5.1 Store B). Becomes High when Phase 2 wires history back in | F8 script eliminates the v1/v2 split before Phase 2 ships |
| F4's date-null change exposes a downstream consumer that assumed `available_date is not None` | Medium | Spot-check `ma_poc/reporting/`, `ma_poc/scripts/state_store.py`, and `ma_poc/scripts/sync_run_to_pg.py` before merge |
| F2's lookup-precedence change (v1 first) breaks a v2-only test that didn't anticipate v1-shaped overrides | Low | H15 explicitly tests the precedence; if it fails, fix lookup chain rather than reverting precedence |
| F3's `area` addition collides with code that uses `area` for a different semantic (e.g., a string description) | Very low | `_PHYSICAL_SIGNAL_FIELDS` already treats `area` as numeric sqft alias; consistent with existing assumption |
| Tightened F4 `except (ImportError, AttributeError)` lets a real bug in `emit()` crash validation | Low (validation has its own try/except in `orchestrator.validate`) | Outer orchestrator catch at [`orchestrator.py:129`](../ma_poc/validation/orchestrator.py#L129) treats this as `VALIDATION_EXCEPTION` reject, never propagates |

---

## 10. Anti-scope creep

If during implementation any of the following come up, write `# PR-FUTURE-WORK:` and move on:

- **Removing the v1 `compute_fallback_id` function entirely.** Migration script needs it (transitively).
- **Persisting rejected records to disk.** That's Phase 2.
- **Cross-run identity rescue.** Phase 2.
- **Multi-stream output (floor-plan inventory, candidates).** Phase 2.
- **Synthesizing floor_plan_name from beds+baths.** Phase 3.
- **Filtering empty records at extractor.** Phase 3.
- **Adding rent or available_date to v2's hash inputs.** Don't. Per the userMemories invariant.
- **Re-enabling cross-run sanity by wiring history into `validate(extract_result)`.** Currently dormant per [`jugnu_runner.py:512`](../ma_poc/scripts/jugnu_runner.py#L512). Phase 2 owns this — F8 only ensures the data is consistent so Phase 2 can flip it on.
- **Migrating callers off `ma_poc/validation/identity_fallback.py` (the deprecated shim).** Several test files still import from the shim; migration to `ma_poc/scripts/identity_fallback.py` is mechanical and orthogonal. Track separately.
- **Promoting v2 names to lookup-priority over v1.** Out of scope for Phase 0; H15 locks in v1-first precedence to avoid behavior shifts in the in-flight pipeline.

---

End of Phase 1 spec. Phase 2 spec follows in [`CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md`](./CLAUDE_PROGRESSIVE_VALIDATION_ANALYSIS.md).