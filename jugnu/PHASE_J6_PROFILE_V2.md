# Phase J6 — Profile v2 Schema & Migration

> Upgrade the `ScrapeProfile` model to v2: add detection state, cost
> stats, unreachable tracking, LRU caps, and the cluster key. Migrate
> every existing v1 profile file to v2 in place, keeping v1 copies in
> an audit folder. Populate `api_provider` for the ~60% of profiles
> that currently have it as `null`.
>
> Reference: Jugnu architecture §6 (Profile evolution under Jugnu),
> §7.1 J6 gate. Bridges to `claude_refactor.md` Phase 6 with the
> additions listed in this file.

---

## Inbound handoff (from J5)

From J5 you must have:

- `ma_poc.observability.events.emit()` working — the migration
  script emits events per profile processed.
- `data/runs/<date>/events.jsonl` format fixed — migration events go
  into a dedicated `events.jsonl` under
  `data/migrations/<timestamp>/`.
- `CostLedger` available — J7's report consumes the new profile
  `stats` section alongside the cost ledger.

From J3 you must have:

- `ma_poc.pms.detector.detect_pms()` stable — the migration calls it
  on every v1 profile to fill `api_provider`.

If detector isn't green, stop. Migration depends on it.

---

## What J6 delivers

1. **`ma_poc/models/scrape_profile.py`** — v2 Pydantic model with the
   fields listed in architecture doc §6.1.
2. **`scripts/migrate_profiles_v1_to_v2.py`** — one-shot migration.
3. **Audit archive** — every v1 profile preserved at
   `config/profiles/_audit/<cid>_v1.json`.
4. **Migration report** — `data/migrations/<ts>/report.md` with
   before/after counts.

---

## The v1 → v2 schema deltas

From Jugnu architecture §6.1, all applied verbatim:

| Field path | Change | Why |
|---|---|---|
| `api_hints.api_provider` | Required (was `Optional[str]`) | Enforce detection always ran |
| `api_hints.client_account_id` | **NEW** `Optional[str]` | Cluster key for cross-property learning (captured, not yet used) |
| `dom_hints.platform_detected` | **REMOVED** | Duplicate of `api_provider` |
| `navigation.explored_links` | Cap at 50, LRU | Unbounded today |
| `api_hints.blocked_endpoints` | Cap at 50, LRU | Unbounded today |
| `api_hints.llm_field_mappings` | Cap at 20, LRU | Unbounded today |
| `cluster_id` | **REMOVED** | Dead field, never implemented |
| `confidence.last_success_detection` | **NEW** `DetectedPMS \| None` | HOT-path routing |
| `confidence.consecutive_unreachable` | **NEW** `int` | Distinct from parse failures; drives DLQ |
| `stats` | **NEW** section | p50 / p95 / LLM cost running totals |

### The new `stats` section

```python
class ProfileStats(BaseModel):
    total_scrapes: int = 0
    successes: int = 0
    failures: int = 0
    llm_cost_cumulative_usd: float = 0.0
    vision_cost_cumulative_usd: float = 0.0
    scrape_duration_p50_ms: int | None = None
    scrape_duration_p95_ms: int | None = None
    last_updated: datetime | None = None
```

`p50` and `p95` are updated using a rolling window of the last 50
scrape durations, not a cumulative quantile — cheaper and responsive
to recent behaviour.

---

## File creation order

1. `ma_poc/models/scrape_profile.py` — v2 model (modify in place)
2. `ma_poc/models/profile_stats.py` — the new `ProfileStats` class
3. `ma_poc/models/_v1_legacy.py` — frozen copy of v1 schema for the
   migration reader
4. `scripts/migrate_profiles_v1_to_v2.py`
5. `tests/profile/test_scrape_profile_v2.py`
6. `tests/profile/test_profile_stats.py`
7. `tests/profile/test_migration.py`

---

## Module specifications

### 1. `scrape_profile.py` v2

Keep backward-compatibility with v1 JSON during load — Pydantic's
`model_validate` handles unknown extra fields with `extra="ignore"`,
and removed fields (`cluster_id`, `dom_hints.platform_detected`) are
absent so defaulting works.

```python
class ScrapeProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")  # accept v1 extras silently

    canonical_id: str
    version: int = 2                        # bumped from 1
    schema_version: Literal["v2"] = "v2"    # explicit marker
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by: str = "BOOTSTRAP"

    navigation: NavigationConfig = NavigationConfig()
    api_hints: ApiHints = ApiHints()
    dom_hints: DomHints = DomHints()
    confidence: ExtractionConfidence = ExtractionConfidence()
    llm_artifacts: LlmArtifacts = LlmArtifacts()
    stats: ProfileStats = ProfileStats()

    # cluster_id removed; client_account_id lives under api_hints now
```

### LRU-capped lists

Don't roll your own. Use a small helper that exposes the cap both at
model-validation time (trim on load) and on append:

```python
# ma_poc/models/_lru_list.py
def trim_lru(items: list[T], cap: int) -> list[T]:
    """Keep the last `cap` items (most recently added)."""
    return items[-cap:] if len(items) > cap else items
```

Then in Pydantic:

```python
@field_validator("explored_links", mode="after")
@classmethod
def _cap_explored_links(cls, v: list[str]) -> list[str]:
    return trim_lru(v, cap=50)
```

This means a badly-shaped v1 profile with 200 explored_links gets
down to 50 on first load — no separate migration step needed for
caps.

### `DetectedPMS` reference

`confidence.last_success_detection` stores a full `DetectedPMS`
object (Pydantic serialises it to a nested dict). This gives J7's
per-property report the adapter, confidence, and evidence at a
glance without re-running the detector.

---

## Named tests (from `claude_refactor.md` Phase 6, extended)

Under `tests/profile/test_migration.py` and
`tests/profile/test_scrape_profile_v2.py`:

| Test | Check |
|---|---|
| `test_v2_defaults` | A fresh `ScrapeProfile(canonical_id="x")` has `schema_version=="v2"`, `stats.total_scrapes==0`, `confidence.consecutive_unreachable==0` |
| `test_v2_loads_untouched_v1_json` | Direct `model_validate` of a v1 JSON (has `cluster_id`, missing `stats`) succeeds; extras silently dropped |
| `test_v2_caps_explored_links` | Construct with 200 explored_links → result has 50 |
| `test_v2_caps_blocked_endpoints` | Same with 100 blocked → 50 |
| `test_v2_caps_llm_field_mappings` | Same with 100 mappings → 20 |
| `test_v2_removes_dom_platform_detected` | v1 JSON with `dom_hints.platform_detected="entrata"` → v2 model has no such attribute (model field removed) |
| `test_migration_populates_api_provider_from_url` | v1 profile with OneSite URL and null provider → v2 has `api_provider="onesite"` |
| `test_migration_preserves_llm_field_mappings` | v1 with 3 mappings → v2 has same 3 |
| `test_migration_drops_cluster_id` | v1 with `cluster_id="X"` → v2 has no cluster_id |
| `test_migration_audit_copy_written` | After migration, `_audit/<cid>_v1.json` exists byte-identical to original |
| `test_migration_is_idempotent` | Running twice produces same final state |
| `test_migration_stats_zero_initialised` | No historical data reconstructed; `total_scrapes==0` is correct |
| `test_migration_consecutive_unreachable_initial_zero` | Fresh v2 field starts at 0 regardless of `consecutive_failures` |
| `test_migration_hot_with_unknown_provider_emits_warning` | A profile with `maturity="HOT"` and `api_provider=="unknown"` logs a warning (indicates a detection gap) |
| `test_migration_report_counts_correct` | After run, `report.md` shows `migrated=N, was_unknown=M, now_detected=K` |
| `test_migration_detector_errors_do_not_halt` | Detector raises on one profile → migration skips it with a logged warning and processes the rest |

---

## The migration script

```python
# scripts/migrate_profiles_v1_to_v2.py
"""
Jugnu J6 — migrate ScrapeProfile v1 → v2.

Invocation:
  python scripts/migrate_profiles_v1_to_v2.py \
      [--profiles-dir config/profiles] \
      [--dry-run]

Idempotent — running twice is safe.
"""
from __future__ import annotations
import argparse, json, logging, shutil
from datetime import datetime, UTC
from pathlib import Path
from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms.detector import detect_pms

log = logging.getLogger("migrate_profiles")

def migrate_one(profile_path: Path, audit_dir: Path, dry_run: bool) -> dict:
    ...

def main() -> int:
    ...
```

Per-profile flow:

1. Read JSON → `v1_raw` dict.
2. If already has `schema_version == "v2"`, skip (idempotency).
3. Copy original to `_audit/<cid>_v1.json` if missing.
4. Compute deltas:
   - If `api_hints.api_provider` is null: call `detect_pms(entry_url,
     csv_row=None, page_html=None)`; set `api_provider =
     detected.pms`; add new field `api_provider_source =
     "migration_detect"`.
   - Drop `cluster_id`, `dom_hints.platform_detected`.
   - Initialise `stats`.
   - Initialise `confidence.consecutive_unreachable = 0`.
   - Apply LRU caps (model validator does this automatically on
     load).
5. Bump `version` to 2, `updated_at` to now, `updated_by` to
   `"MIGRATION_V1_TO_V2"`.
6. Write v2 JSON to original path.
7. Emit event `migration.profile_upgraded` with cid + delta summary.

### Migration report

`data/migrations/<ts>/report.md` with columns:

```markdown
# Profile Migration Report — <ISO>

## Summary
| Metric | Value |
|---|---|
| Total profiles | {N} |
| Migrated | {M} |
| Already v2 (skipped) | {K} |
| Failed | {F} |
| `api_provider` was null → now populated | {J} |
| `api_provider` still unknown (detection failed) | {U} |

## Detection breakdown (newly populated)
| PMS | Count |
|---|---|
| entrata | {e} |
| rentcafe | {r} |
...

## Failed profiles
{list of cids with exception type + message}
```

---

## Refactoring / code-quality checklist

- [ ] `ScrapeProfile` v2 fits in one file under 400 lines.
- [ ] v1-specific code lives only in `_v1_legacy.py` and
      `migrate_profiles_v1_to_v2.py`. No v1 compat in the main model
      beyond `extra="ignore"`.
- [ ] Migration script has a `--dry-run` flag that exercises every
      code path without writing.
- [ ] `mypy --strict ma_poc/models/` clean.
- [ ] `ruff check` clean.
- [ ] Coverage ≥ 90% on `ma_poc/models/` (pure Pydantic — easy).
- [ ] Migration script never mutates a profile in place before the
      audit copy is written (write audit → transform → write new).

---

## Gate — `scripts/gate_jugnu.py phase 6`

Passes iff:

- All ~16 tests pass.
- Static analysis clean, coverage ≥ 90% on `ma_poc/models/`.
- **Observable check:** running migration on the real
  `config/profiles/` produces:
    - One `_audit/<cid>_v1.json` per migrated profile.
    - Post-migration `api_provider == "unknown"` count is < 10% of
      total (architecture doc §6.1 target).
    - Every v2 profile validates via
      `ScrapeProfile.model_validate_json()`.
    - Migration report exists and summary row totals match.
- **Idempotency check:** run migration twice. Second run's report
  shows `Migrated: 0, Already v2 (skipped): N`.
- **Compatibility check:** existing `daily_runner` path can still
  load profiles (integration test loads 5 random profiles, runs them
  through `profile_store.load()`).

---

## Outbound handoff (to J7 Report v2)

- **Model** `ma_poc.models.scrape_profile.ScrapeProfile` v2 stable.
- **Field** `profile.stats` populated post-migration with zeros;
  daily_runner will update on each scrape (J8).
- **Field** `profile.confidence.last_success_detection` — J7's
  per-property report reads this to display detection at the top.
- **Field** `profile.api_hints.client_account_id` — captured but not
  yet consumed. Flag this to reviewers in the PR.
- **Script** `migrate_profiles_v1_to_v2.py` archived; J9 gate runs
  it once more to confirm idempotency.
- **Baseline delta:** J9 report will compare J0's
  `api_provider_null_pct` to post-migration value.

Commit: `Jugnu J6: ScrapeProfile v2 + migration`.

---

*Next: `PHASE_J7_REPORT_V2.md`.*
