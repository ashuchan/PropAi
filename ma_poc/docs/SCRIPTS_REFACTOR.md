# `ma_poc/scripts/` — Refactor Instructions

**Author:** Senior architecture review
**Date:** 2026-05-08
**Scope:** Reorganise the 65 files in [`ma_poc/scripts/`](../scripts/) so the
directory tree itself documents the codebase.
**Companion test:** [`ma_poc/tests/structure/test_scripts_layout.py`](../tests/structure/test_scripts_layout.py)
enforces every rule in this document. CI must run it.

---

## 0. Why this refactor

Two facts make the current layout a liability:

1. **`scripts/` is mixed-use.** Half the files are `__main__` entrypoints; the
   other half are libraries (`state_store.py`, `validation.py`, `schema_v2.py`,
   `concurrency.py`, `identity_fallback.py`, `replay.py`) imported from
   `data_provider/`, `validation/`, `services/`, and ~20 test files. A
   `scripts/` import path is a lie for those.
2. **Naming has already paid penalties.**
   [`tests/conftest.py`](../tests/conftest.py) carries a 15-line workaround that
   pre-binds `scripts/validation.py` into `sys.modules` because that filename
   collides with the [`ma_poc/validation/`](../validation/) package. Two
   modules called `validation` is a navigation tax forever; we delete it here.

Outcome of this refactor:
- Every file in `scripts/` ships a `__main__`.
- Libraries move to a real package (`ma_poc/core/`).
- Each `scripts/` subdirectory has a one-sentence charter that a reviewer
  can use to reject misplaced files.
- The `conftest.py` workaround is deleted.

---

## 1. Naming rules — non-negotiable

A reviewer must reject any PR violating these:

| Rule | Why |
|---|---|
| **No `utils/`, `helpers/`, `common/`, `misc/` packages.** | Reviewers can't predict contents → next dev dumps anything → graveyard. |
| **No `validators/` package.** | Collides with the existing [`ma_poc/validation/`](../validation/). One name, one role. |
| **No `report_generators/`, `data_handlers/`, `*_managers/` directories.** | The directory implies the gerund. Use the noun: `reports/`, `sync/`. |
| **No codename in a role-bucket file name.** | `runners/jugnu.py` is OK (the runner *is* Jugnu); `gates/jugnu.py` is OK (audits Jugnu code); but `reports/jugnu_daily.py` is not — the role is `daily`, the codename is project trivia. |
| **Library code has no `__main__`. Script code has nothing else import it.** | Hard split between `core/` (libraries) and `scripts/` (entrypoints). No exceptions. |
| **Each script subdirectory has a `__init__.py` and a one-line module docstring describing the directory's charter.** | Forces the reviewer to articulate what belongs there. |

If a new file does not obviously belong in any existing subdirectory, the
answer is **not** to create `scripts/misc/`. The answer is to articulate the
new directory's charter and add it to the table in §3 *and* to the structure
test in [`test_scripts_layout.py`](../tests/structure/test_scripts_layout.py).

---

## 2. Target directory tree

```
ma_poc/
├── core/                              # NEW — libraries currently squatting in scripts/
│   ├── __init__.py
│   ├── identity.py                    # ← scripts/identity_fallback.py
│   ├── state_store.py                 # ← scripts/state_store.py
│   ├── schema_v2.py                   # ← scripts/schema_v2.py
│   ├── concurrency.py                 # ← scripts/concurrency.py
│   ├── replay.py                      # ← scripts/replay.py
│   └── issue_log.py                   # ← scripts/validation.py  (renamed)
│
└── scripts/                           # entrypoints ONLY — every file has __main__
    ├── __init__.py
    ├── _common/
    │   ├── __init__.py
    │   └── trigger.py                 # ← scripts/_trigger_common.py
    │
    ├── runners/                       # drive a scrape end-to-end
    │   ├── __init__.py
    │   ├── jugnu.py                   # ← jugnu_runner.py
    │   ├── jugnu_retry.py             # ← jugnu_retry_runner.py
    │   ├── jugnu_retry_merge.py
    │   ├── shard_entry.py             # ← jugnu_shard_entry.py
    │   ├── retry_entry.py             # ← jugnu_retry_entry.py
    │   ├── dispatcher.py              # ← run_script.py
    │   └── jugnu_scheduled.ps1
    │
    ├── triggers/                      # submit jobs to Cloud Run
    │   ├── __init__.py
    │   ├── run.py
    │   ├── retry.py
    │   ├── smoke.py
    │   └── proxy_smoke.py
    │
    ├── reports/                       # produce json/md/html artefacts
    │   ├── __init__.py
    │   ├── daily.py                   # ← generate_daily_report.py
    │   ├── analysis.py                # ← generate_analysis_report.py
    │   ├── health.py                  # ← health_report.py
    │   ├── per_property.py            # ← scrape_report.py
    │   ├── escalation.py              # ← escalation_report.py
    │   └── floor_plan_comparison.py   # ← report_floor_plan_comparison.py
    │
    ├── email/                         # report distribution (depends on reports/)
    │   ├── __init__.py
    │   ├── _client.py                 # ← email_html.py
    │   ├── daily.py                   # ← email_daily_report.py
    │   ├── daily_failures.py
    │   ├── merge_analysis.py
    │   ├── refactor_plan.py
    │   └── send_daily_failures.bat
    │
    ├── gates/                         # PR / phase pass-fail audits
    │   ├── __init__.py
    │   ├── jugnu.py
    │   ├── pr23.py
    │   ├── pr24.py
    │   ├── pr25.py
    │   ├── refactor.py
    │   ├── xsource.py
    │   ├── escalation.py
    │   ├── antibot_fixes.py
    │   ├── stealth_pr1.py
    │   └── prompts_merge_resilience.py
    │
    ├── checks/                        # runtime / deploy preconditions
    │   ├── __init__.py
    │   ├── deployment.py              # ← validate_deployment.py
    │   ├── outputs.py                 # ← validate_outputs.py
    │   ├── adapters_live.py           # ← validate_adapters_live.py
    │   └── csv_mapping.py             # ← verify_csv_mapping.py
    │
    ├── smoke/
    │   ├── __init__.py
    │   ├── imports.py                 # ← smoke_test.py
    │   └── rentcafe_direct.py         # ← smoke_rentcafe_direct.py
    │
    ├── baselines/                     # pre-refactor metric capture
    │   ├── __init__.py
    │   ├── jugnu.py                   # ← jugnu_baseline.py
    │   ├── refactor.py                # ← refactor_baseline.py
    │   └── escalation.py              # ← escalation_baseline.py
    │
    ├── diagnostics/                   # one-off probes — already exists
    │   ├── __init__.py
    │   ├── tls_vs_ip.py               # already there
    │   ├── proxy.py                   # ← check_proxy.py
    │   ├── fetch_probe.py
    │   └── cluster_retry.py
    │
    ├── migrations/                    # destructive, one-shot data shape changes
    │   ├── __init__.py
    │   ├── alembic.py                 # ← migrate.py
    │   ├── inferred_ids_v1_to_v2.py   # ← migrate_inferred_ids_v1_to_v2.py
    │   ├── profiles_v1_to_v2.py       # ← migrate_profiles_v1_to_v2.py
    │   ├── profiles_add_fetch.py      # ← migrate_profiles_add_fetch.py
    │   └── profiles_xsource.py        # ← migrate_profiles_xsource.py
    │
    ├── backfills/                     # idempotent re-derivation of values
    │   ├── __init__.py
    │   ├── postgres.py                # ← backfill_pg.py
    │   ├── artifacts.py               # ← backfill_artifacts_pg.py
    │   ├── floor_plan_id.py           # ← backfill_floor_plan_id.py
    │   └── units_bed_bath.py          # ← backfill_units_bed_bath.py
    │
    ├── sync/                          # cross-store data movement
    │   ├── __init__.py
    │   ├── run_to_pg.py               # ← sync_run_to_pg.py
    │   ├── cloud_to_local.py          # ← sync_cloud_to_local.py
    │   ├── properties_from_snapshots.py
    │   └── csv_to_gcs.py              # ← deploy_csv_sync.py
    │
    └── floor_plans/                   # CSV ⇄ DB comparison toolchain
        ├── __init__.py
        ├── compare.py                 # ← compare_floor_plans_csv.py
        └── export_disagreements.py    # ← export_floor_plan_disagreements.py
```

---

## 3. Subdirectory charters

Every subdirectory has a one-sentence charter that defines what belongs and
what does not. The first line of each `__init__.py` must be this charter,
verbatim. The structure test asserts both the listing and the charters.

| Path | Charter (must match `__init__.py` docstring) |
|---|---|
| `ma_poc/core/` | "Library modules used by runners and tests; never executed directly." |
| `scripts/_common/` | "Private helpers shared across script subdirectories; not for external import." |
| `scripts/runners/` | "Entrypoints that drive a full scrape pass over a set of properties." |
| `scripts/triggers/` | "Submit jobs to a remote executor (Cloud Run today)." |
| `scripts/reports/` | "Produce a human-consumable artefact (json, markdown, html) from stored data." |
| `scripts/email/` | "Ship a report artefact to recipients; always depends on `scripts.reports`." |
| `scripts/gates/` | "Pass/fail audits of code and structure invariants used in CI." |
| `scripts/checks/` | "Pass/fail audits of runtime preconditions (deploy state, env, data shape)." |
| `scripts/smoke/` | "End-to-end 'does it wake up' tests run before/after deploy." |
| `scripts/baselines/` | "Capture metrics from current code so a refactor can be measured." |
| `scripts/diagnostics/` | "One-off probes that answer 'why is X broken right now?'." |
| `scripts/migrations/` | "Destructive, one-shot changes to data shape on disk or DB." |
| `scripts/backfills/` | "Idempotent re-derivation of values into existing rows under a fixed schema." |
| `scripts/sync/` | "Cross-store data movement that preserves shape (no schema change)." |
| `scripts/floor_plans/` | "CSV ⇄ DB floor-plan comparison toolchain." |

---

## 4. File moves — exhaustive table

The structure test reads this table (encoded in
[`test_scripts_layout.py::EXPECTED_LAYOUT`](../tests/structure/test_scripts_layout.py))
and verifies (a) the new path exists, (b) the old path is gone, (c) no
file was forgotten.

### 4a. Library moves (scripts → core)

| Old path | New path | Rename rationale |
|---|---|---|
| `scripts/identity_fallback.py` | `core/identity.py` | "fallback" is one strategy of three; module computes deterministic IDs in general. |
| `scripts/state_store.py` | `core/state_store.py` | name kept; only the package changes. |
| `scripts/schema_v2.py` | `core/schema_v2.py` | name kept; only the package changes. |
| `scripts/concurrency.py` | `core/concurrency.py` | name kept; only the package changes. |
| `scripts/replay.py` | `core/replay.py` | name kept; only the package changes. |
| `scripts/validation.py` | `core/issue_log.py` | resolves the `ma_poc/validation/` collision; module is an issue logger, not a validator. |

### 4b. Runner moves

| Old | New |
|---|---|
| `scripts/jugnu_runner.py` | `scripts/runners/jugnu.py` |
| `scripts/jugnu_retry_runner.py` | `scripts/runners/jugnu_retry.py` |
| `scripts/jugnu_retry_merge.py` | `scripts/runners/jugnu_retry_merge.py` |
| `scripts/jugnu_shard_entry.py` | `scripts/runners/shard_entry.py` |
| `scripts/jugnu_retry_entry.py` | `scripts/runners/retry_entry.py` |
| `scripts/run_script.py` | `scripts/runners/dispatcher.py` |
| `scripts/jugnu_scheduled_runner.ps1` | `scripts/runners/jugnu_scheduled.ps1` |
| `scripts/_trigger_common.py` | `scripts/_common/trigger.py` |

### 4c. Trigger moves

| Old | New |
|---|---|
| `scripts/trigger_run.py` | `scripts/triggers/run.py` |
| `scripts/trigger_retry.py` | `scripts/triggers/retry.py` |
| `scripts/trigger_smoke.py` | `scripts/triggers/smoke.py` |
| `scripts/trigger_proxy_smoke.py` | `scripts/triggers/proxy_smoke.py` |

### 4d. Report moves

| Old | New |
|---|---|
| `scripts/generate_daily_report.py` | `scripts/reports/daily.py` |
| `scripts/generate_analysis_report.py` | `scripts/reports/analysis.py` |
| `scripts/health_report.py` | `scripts/reports/health.py` |
| `scripts/scrape_report.py` | `scripts/reports/per_property.py` |
| `scripts/escalation_report.py` | `scripts/reports/escalation.py` |
| `scripts/report_floor_plan_comparison.py` | `scripts/reports/floor_plan_comparison.py` |

### 4e. Email moves

| Old | New |
|---|---|
| `scripts/email_html.py` | `scripts/email/_client.py` |
| `scripts/email_daily_report.py` | `scripts/email/daily.py` |
| `scripts/email_daily_failures_report.py` | `scripts/email/daily_failures.py` |
| `scripts/email_merge_analysis.py` | `scripts/email/merge_analysis.py` |
| `scripts/email_refactor_plan.py` | `scripts/email/refactor_plan.py` |
| `scripts/send_daily_failures_report.bat` | `scripts/email/send_daily_failures.bat` |

### 4f. Gate moves

| Old | New |
|---|---|
| `scripts/gate_jugnu.py` | `scripts/gates/jugnu.py` |
| `scripts/gate_pr23.py` | `scripts/gates/pr23.py` |
| `scripts/gate_pr24.py` | `scripts/gates/pr24.py` |
| `scripts/gate_pr25.py` | `scripts/gates/pr25.py` |
| `scripts/gate_refactor.py` | `scripts/gates/refactor.py` |
| `scripts/gate_xsource.py` | `scripts/gates/xsource.py` |
| `scripts/gate_escalation.py` | `scripts/gates/escalation.py` |
| `scripts/gate_antibot_fixes.py` | `scripts/gates/antibot_fixes.py` |
| `scripts/gate_stealth_pr1.py` | `scripts/gates/stealth_pr1.py` |
| `scripts/gate_prompts_merge_resilience.py` | `scripts/gates/prompts_merge_resilience.py` |

### 4g. Check moves

| Old | New |
|---|---|
| `scripts/validate_deployment.py` | `scripts/checks/deployment.py` |
| `scripts/validate_outputs.py` | `scripts/checks/outputs.py` |
| `scripts/validate_adapters_live.py` | `scripts/checks/adapters_live.py` |
| `scripts/verify_csv_mapping.py` | `scripts/checks/csv_mapping.py` |

### 4h. Smoke moves

| Old | New |
|---|---|
| `scripts/smoke_test.py` | `scripts/smoke/imports.py` |
| `scripts/smoke_rentcafe_direct.py` | `scripts/smoke/rentcafe_direct.py` |

### 4i. Baseline moves

| Old | New |
|---|---|
| `scripts/jugnu_baseline.py` | `scripts/baselines/jugnu.py` |
| `scripts/refactor_baseline.py` | `scripts/baselines/refactor.py` |
| `scripts/escalation_baseline.py` | `scripts/baselines/escalation.py` |

### 4j. Diagnostic moves

| Old | New |
|---|---|
| `scripts/check_proxy.py` | `scripts/diagnostics/proxy.py` |
| `scripts/fetch_probe.py` | `scripts/diagnostics/fetch_probe.py` |
| `scripts/cluster_retry.py` | `scripts/diagnostics/cluster_retry.py` |
| `scripts/diagnostics/tls_vs_ip_diagnostic.py` | `scripts/diagnostics/tls_vs_ip.py` |

### 4k. Migration moves

| Old | New |
|---|---|
| `scripts/migrate.py` | `scripts/migrations/alembic.py` |
| `scripts/migrate_inferred_ids_v1_to_v2.py` | `scripts/migrations/inferred_ids_v1_to_v2.py` |
| `scripts/migrate_profiles_v1_to_v2.py` | `scripts/migrations/profiles_v1_to_v2.py` |
| `scripts/migrate_profiles_add_fetch.py` | `scripts/migrations/profiles_add_fetch.py` |
| `scripts/migrate_profiles_xsource.py` | `scripts/migrations/profiles_xsource.py` |

### 4l. Backfill moves

| Old | New |
|---|---|
| `scripts/backfill_pg.py` | `scripts/backfills/postgres.py` |
| `scripts/backfill_artifacts_pg.py` | `scripts/backfills/artifacts.py` |
| `scripts/backfill_floor_plan_id.py` | `scripts/backfills/floor_plan_id.py` |
| `scripts/backfill_units_bed_bath.py` | `scripts/backfills/units_bed_bath.py` |

### 4m. Sync moves

| Old | New |
|---|---|
| `scripts/sync_run_to_pg.py` | `scripts/sync/run_to_pg.py` |
| `scripts/sync_cloud_to_local.py` | `scripts/sync/cloud_to_local.py` |
| `scripts/sync_properties_from_snapshots.py` | `scripts/sync/properties_from_snapshots.py` |
| `scripts/deploy_csv_sync.py` | `scripts/sync/csv_to_gcs.py` |

### 4n. Floor-plan moves

| Old | New |
|---|---|
| `scripts/compare_floor_plans_csv.py` | `scripts/floor_plans/compare.py` |
| `scripts/export_floor_plan_disagreements.py` | `scripts/floor_plans/export_disagreements.py` |

### 4o. Files that stay where they are (docs)

| Path | Reason |
|---|---|
| `scripts/CLAUDE.md` | Operator-facing doc; rewrite §"Architecture overview" to point at new paths in the same PR. |
| `scripts/JUGNU_ALGORITHM.md` | Doc; no move. |
| `scripts/llm_fallbacks.md` | Doc; no move. |

---

## 5. Import-update requirements

**Every** `from scripts.*` and `from ma_poc.scripts.*` import in the
codebase must be rewritten. The structure test asserts there are zero
remaining references to the old module names.

### 5a. Production import sites (must update in lock-step)

| File | Old import | New import |
|---|---|---|
| [`data_provider/filesystem.py`](../data_provider/filesystem.py) | `from scripts.state_store import StateStore` | `from ma_poc.core.state_store import StateStore` |
| [`data_provider/sql/stores.py`](../data_provider/sql/stores.py) | `from ma_poc.scripts.identity_fallback import …` | `from ma_poc.core.identity import …` |
| [`validation/schema_gate.py`](../validation/schema_gate.py) | `from ma_poc.scripts.identity_fallback import compute_fallback_unit_id` | `from ma_poc.core.identity import compute_fallback_unit_id` |
| [`validation/identity_fallback.py`](../validation/identity_fallback.py) | re-exports from `ma_poc.scripts.identity_fallback` | re-export from `ma_poc.core.identity`; **then delete this re-export shim in a follow-up PR** once external callers move. |
| [`services/source_merger.py`](../services/source_merger.py) | check `grep` output | adjust accordingly |
| [`scripts/runners/jugnu.py`](../scripts/runners/jugnu.py) (was `jugnu_runner.py`) | `import scripts.X` (concurrency, state_store, schema_v2, validation, etc.) | `from ma_poc.core import …` |
| Every other file in `scripts/` that imports a sibling | rewrite as `from ma_poc.core.X import …` or `from scripts.<subdir>.X import …` | — |

### 5b. Test import sites (≥20 files)

Run the audit:
```bash
grep -rn "from scripts\.\|import scripts\.\|from ma_poc\.scripts\|import ma_poc\.scripts" ma_poc --include="*.py"
```
Each match either rewrites to the new path or, for a script-under-test, the
test moves into a structurally parallel directory under `ma_poc/tests/`
(e.g. `tests/scripts/test_state_store_nodrop.py` → `tests/core/test_state_store.py`).

### 5c. `tests/conftest.py` cleanup (mandatory)

After the move, delete the `scripts/validation.py` workaround block (lines
~21–~50 in current `conftest.py`). The collision is gone, so the
`importlib.util.spec_from_file_location("validation", …)` hack must go.
The structure test asserts the workaround is absent.

### 5d. External caller updates

| Caller | What references the old path | Required update |
|---|---|---|
| Cloud Run job spec (`deploy/cloud_run/*.yaml` or equivalent) | `command: ["python", "scripts/jugnu_shard_entry.py"]` | `command: ["python", "-m", "scripts.runners.shard_entry"]` |
| `Dockerfile` | `CMD` and `COPY` paths | match new paths; verify with a local build |
| `scripts/runners/jugnu_scheduled.ps1` | `python scripts/jugnu_runner.py …` | `python -m scripts.runners.jugnu …` |
| `scripts/email/send_daily_failures.bat` | `python scripts/email_daily_failures_report.py` | `python -m scripts.email.daily_failures` |
| `pyproject.toml` | any `[project.scripts]` entries (currently 2 references — verify) | rewrite to new module paths |
| GitHub Actions workflows | `python scripts/gate_*.py` | new `gates/` paths |
| `ma_poc/CLAUDE.md` and `scripts/CLAUDE.md` | every `python scripts/X.py` example | rewritten |

---

## 6. Phased execution plan

Execute in this order. Do **not** parallelise phases — each one ends with a
green test suite that the next phase depends on.

### Phase 1 — Library evacuation (highest risk)

**Goal:** Move the 6 `core/` files. Update every importer. Tests green.

Steps:
1. Create `ma_poc/core/__init__.py` with the charter docstring.
2. Move all 6 files using `git mv` (preserves history).
3. Rename `validation.py → issue_log.py` and `identity_fallback.py → identity.py`.
4. Run the audit `grep -rn "from scripts\.\(state_store\|validation\|schema_v2\|concurrency\|replay\|identity_fallback\)\b"` and rewrite every hit.
5. Delete the `tests/conftest.py` `scripts/validation.py` pre-bind workaround.
6. Run `pytest ma_poc/ -v` until green.
7. Run `python -m scripts.gates.jugnu all` (after step also moves it; if not yet, run the old path).

Do not start Phase 2 until step 6 is green.

### Phase 2 — External-facing entrypoints

**Goal:** Move `runners/`, `triggers/`, `email/` and update Cloud Run /
Dockerfile / `.ps1` / `.bat` / GitHub Actions in the *same* PR.

Steps:
1. `git mv` files into the new subdirectories. Add `__init__.py` charters.
2. Rewrite Cloud Run YAML, Dockerfile, `.ps1`, `.bat`, GHA workflows.
3. Run `python -m scripts.smoke.imports` (after Phase 3 also runs, otherwise
   the existing path).
4. Deploy a smoke job (`python -m scripts.triggers.smoke`) before merging.

### Phase 3 — Internal-only entrypoints

**Goal:** Move `gates/`, `checks/`, `smoke/`, `baselines/`, `diagnostics/`,
`migrations/`, `backfills/`, `sync/`, `floor_plans/`. Update internal docs.

Steps:
1. `git mv` and create charter `__init__.py`s.
2. Rewrite `scripts/CLAUDE.md` and `ma_poc/CLAUDE.md` examples.
3. `pytest ma_poc/ -v` green.

### Phase 4 — Delete shims

**Goal:** Remove any temporary re-export shims (`validation/identity_fallback.py`
likely needs one for one PR cycle) and confirm no `from scripts.<oldname>`
imports remain anywhere.

Steps:
1. Delete shim files.
2. Run the structure test (§7); it must pass.
3. Run the full pytest suite.

### Shim policy

Re-export shims are permitted **only** between phases of *this* refactor and
**must** be deleted by Phase 4. No long-lived backwards-compat shim survives
this work. Anyone who wants the old import name back has to argue for it in
review.

---

## 7. Acceptance criteria — what "done" looks like

A reviewer signs this off only when **all** of the following are true:

1. `pytest ma_poc/tests/structure/test_scripts_layout.py -v` passes (the
   structure test enforces every rule in §1–§4).
2. `pytest ma_poc/ -v` passes with no `xfail` related to this refactor.
3. `grep -rn "from scripts\.\|import scripts\.\|from ma_poc\.scripts\|import ma_poc\.scripts" ma_poc --include="*.py"` returns zero matches whose right-hand side is one of the *old* names.
4. `grep -n "scripts/validation\.py\|importlib.*validation" ma_poc/tests/conftest.py` returns zero matches.
5. The Cloud Run job spec, Dockerfile, `.ps1`, `.bat`, and GitHub Actions
   workflows all reference the new paths.
6. `scripts/CLAUDE.md` and `ma_poc/CLAUDE.md` have been rewritten so every
   `python scripts/X.py` example points at a file that exists.
7. A test smoke run (`python -m scripts.triggers.smoke`) succeeds in
   staging.
8. No file under `ma_poc/scripts/` lacks an `if __name__ == "__main__":`
   block (enforced by the structure test, see §8).
9. No file under `ma_poc/core/` declares `if __name__ == "__main__":`
   (also enforced).

---

## 8. The structure test

Authoritative file:
[`ma_poc/tests/structure/test_scripts_layout.py`](../tests/structure/test_scripts_layout.py).

It enforces:

| Test | What it checks |
|---|---|
| `test_target_files_exist` | Every new path in §4 exists. |
| `test_old_paths_are_gone` | Every old path in §4 has been deleted. |
| `test_no_unexpected_files_in_scripts_root` | `ma_poc/scripts/` root contains **only** `__init__.py`, `*.md`, and the listed subdirectories. No stragglers. |
| `test_subdir_init_charters` | Each subdir's `__init__.py` first-line docstring matches the charter table in §3. |
| `test_no_banned_directory_names` | `utils`, `helpers`, `common`, `misc`, `validators`, `report_generators`, `data_handlers` do not exist anywhere under `ma_poc/`. |
| `test_scripts_files_have_main_guard` | Every `.py` under `ma_poc/scripts/` (except `__init__.py` and files starting with `_`) contains an `if __name__ == "__main__":` line. |
| `test_core_files_have_no_main_guard` | No `.py` under `ma_poc/core/` contains an `if __name__ == "__main__":` line. |
| `test_no_legacy_imports` | `grep` against the repo for `from scripts.<oldname>` and `from ma_poc.scripts.<oldname>` patterns finds zero hits, where `<oldname>` is any of the 60+ moved files. |
| `test_conftest_workaround_removed` | `tests/conftest.py` no longer contains the `scripts/validation.py` pre-bind block. |

Adding a new script subdirectory is a three-step change:

1. Add the directory and its `__init__.py` charter.
2. Add the charter to the table in §3 of this doc.
3. Add the directory to `EXPECTED_LAYOUT` in `test_scripts_layout.py`.

If any of those three is missed, CI fails — which is the point.

---

## 9. Out of scope for this refactor

This is a *layout* refactor. The following are explicitly **not** addressed
here and belong to [`SOLID_REFACTOR_PLAN_v2.md`](SOLID_REFACTOR_PLAN_v2.md):

- Splitting god modules ([`scripts/jugnu_runner.py`](../scripts/jugnu_runner.py),
  [`scripts/sync_run_to_pg.py`](../scripts/sync_run_to_pg.py),
  [`scripts/health_report.py`](../scripts/health_report.py),
  [`scripts/generate_daily_report.py`](../scripts/generate_daily_report.py)).
- Deleting the legacy `daily_runner.py` + `entrata.py` pipeline.
- De-duplicating the rent-extraction / SightMap / RealPage parsers across
  `entrata.py`, `scrape_properties.py`, and `pms/adapters/generic.py`.

Those are larger surgeries. This refactor unblocks them by giving every file
a defensible home.
