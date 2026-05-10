# Local Canary Plan — replay yesterday's failures against today's code

**Date:** 2026-05-11
**Scope:** Build a single CLI (`scripts/diagnostics/local_canary.py`) that takes the failing properties from a prior cloud run, sets up a disposable local DB, re-runs them through the current code, and produces a delta report. One command, one run, one verdict per property.

This plan does **not** replace the integration test suite ([`INTEGRATION_TEST_PLAN.md`](INTEGRATION_TEST_PLAN.md)). Integration tests verify code behaves correctly against shaped inputs; the local canary verifies code behaves correctly against **yesterday's actual failures**. Both layers are needed: integration tests catch contract regressions, the canary catches "this fix doesn't recover the properties the brief promised".

---

## 0. Why this is needed

Inventory of the current pre-deploy validation pipeline:

| Layer | What it covers | What it misses |
|---|---|---|
| Unit tests under [`tests/services/`](../tests/services/) | Per-function behavior on shaped inputs | Real production data shapes; cross-property interaction |
| Integration tests (per `INTEGRATION_TEST_PLAN.md`) | Layer seams + per-PMS happy paths against the corpus | Long-tail failures the corpus doesn't include |
| [`scripts/smoke_test.py`](../scripts/smoke_test.py) | 5 hand-picked properties end-to-end | The 1100+ properties failing daily; the failure modes the fixes target |
| Synthetic alert shard ([`canary_baselines/`](canary_baselines/)) | Dashboard alert wiring | Whether the fix actually recovers a real failing property |
| Live cloud run | The whole truth | 24h feedback loop; cost; can't isolate one fix from another |

**The PR 1–9 persistence-loop series shipped 9 fixes against the same regression with no way to verify pre-deploy that any individual fix would actually recover the failing properties it targeted.** The synthetic alert shard proved the dashboard would see the fix work; nothing proved the fix would work.

A single property scrape is reproducible: same URL → same fetch → same parse, modulo CDN flake. Re-running yesterday's failures against today's code locally is feasible; it's just engineering effort that hasn't been done.

---

## 1. Tool overview

One CLI:

```bash
python scripts/diagnostics/local_canary.py --from-run 2026-05-10 [options]
```

Phases the CLI executes in order:

```
SETUP        →  Provision a disposable local DB; bootstrap alembic schema; seed
                yesterday's profiles into it (so the canary run starts from
                realistic ground state, not a cold cache).

SELECT       →  Read failures from the prior run (failures.csv produced by
                analyze_cloud_run.py, OR a live DB query). Apply filters
                (--filter-tier, --filter-pms, --limit). Materialise a CSV of
                the canary's input properties.

REPLAY       →  Invoke the existing jugnu_runner against the selected
                properties, with DATABASE_URL pointed at the canary DB and
                the data dir scoped to the canary's output.

COMPARE      →  Diff each property's outcome:
                  - cloud-run yesterday: outcome, terminal tier, units count,
                    LLM cost, errors
                  - local-canary today:  same fields, plus which fix's flag
                    (ENABLE_DEGRADED_MAPPING_PERSIST, etc.) fired
                Attribution is by event-emission: if MAPPING_SAVE_DROPPED
                count went from N→0, persistence hardening fixed it.

REPORT       →  Markdown delta report at canary output dir. Pass/fail summary
                to stdout. Exit code 0 iff every property either improved or
                stayed the same (no regressions).

TEARDOWN     →  (default) Drop the canary DB. (--keep) leaves it for
                forensics with the connection string printed.
```

**Non-goal**: this is not a replacement for the live cloud canary. The cloud canary is the truth at scale (5000+ properties, real proxies, real time-of-day variance). The local canary is the *fast pre-deploy filter* that catches "this fix is wrong" in 5 minutes instead of 24 hours.

---

## 2. CLI surface

```
python scripts/diagnostics/local_canary.py [args]

Required:
  --from-run YYYY-MM-DD       Date of the cloud run whose failures to replay.
                              Reads from c:/tmp/run-{date}/ by default.

Selection (default: all failures from the source run):
  --limit N                   Cap to N properties. Default: 50.
  --filter-tier TIER_NAME     Only include properties whose terminal tier
                              equals this (e.g. TIER_4_LLM_API).
  --filter-pms PMS_NAME       Only include detected PMS == this.
  --filter-outcome OUTCOME    FAILED_NO_DATA | FAILED_UNREACHABLE |
                              CARRY_FORWARD | SUCCESS  (SUCCESS for
                              regression-checks against known-good props).
  --include-property-id ID    Force-include this property even if filters
                              would exclude. Repeatable.
  --properties-csv PATH       Override entirely with an external CSV.

DB:
  --db-mode {sqlite,postgres} Default sqlite (zero-deps, fast).
                              postgres requires CANARY_DATABASE_URL env.
  --seed-from-prod            Copy yesterday's scrape_profiles from the
                              live DB into the canary DB so the run starts
                              with realistic profile state.
  --keep                      Don't drop the canary DB at the end. Prints
                              the DSN/path so the operator can poke at it.

Fix attribution (which feature flags to canary):
  --flag KEY=VALUE            Set an env var for the canary run only.
                              Repeatable. Examples:
                                --flag ENABLE_DEGRADED_MAPPING_PERSIST=true
                                --flag ENABLE_PROMOTE_ON_HINT=false
                                --flag ENABLE_SOURCE_TIERED_BUDGET=true
  --baseline                  Run twice — once with all flags off (baseline),
                              once with the configured flag set (treatment).
                              Report diffs treatment-vs-baseline.

Output:
  --out-dir PATH              Default: data/canary/local_runs/{timestamp}/
  --json                      Emit machine-readable summary.json alongside
                              the markdown report.
  -v / --verbose              Per-property progress to stdout.

Behavior:
  --max-retries N             Retry transient fetch failures within the canary
                              (default 1). Distinct from per-property scrape
                              retries that jugnu_runner already does.
  --timeout-per-property SEC  Default 180. The canary aborts a property
                              after this; reports it as TIMEOUT_IN_CANARY
                              rather than misattributing to a fix.
```

### CLI examples

**Quickest reality check** — replay 50 random failures from yesterday with the default flag set:

```bash
python scripts/diagnostics/local_canary.py --from-run 2026-05-10 --limit 50
```

**Targeted PR validation** — did the URL-normalization fix recover the properties that were failing because of URL drift?

```bash
python scripts/diagnostics/local_canary.py \
  --from-run 2026-05-10 \
  --filter-tier TIER_4_LLM_API \
  --filter-outcome FAILED_NO_DATA \
  --baseline \
  --limit 100 \
  --seed-from-prod
```

The `--baseline` runs once with everything default-off (the pre-fix world) and once with all the post-fix flags on. The diff column tells you whether the fix you shipped actually moved each property from FAILED to SUCCESS.

**Single-property forensics** — re-run one specific property and inspect the resulting profile:

```bash
python scripts/diagnostics/local_canary.py \
  --from-run 2026-05-10 \
  --include-property-id 37685 \
  --keep \
  --verbose
```

`--keep` leaves the SQLite file at `data/canary/local_runs/{ts}/canary.sqlite` so the operator can open it with `db_query.py` and inspect the post-canary profile.

---

## 3. Local DB strategy

The canary owns its DB end-to-end. No reuse of the local `proppy` dev DB; no contamination of cloud DB.

| Mode | Backing store | Schema bootstrap | Use case |
|---|---|---|---|
| **`sqlite`** (default) | `{out_dir}/canary.sqlite` (file) | Programmatic via [`SqliteDataProvider._ensure_schema()`](../data_provider/sqlite.py) — same DDL definitions, sqlite-translated | Default. Zero deps. ~1s setup. ~5s per property scrape. Idempotent. |
| **`postgres`** | URL from `CANARY_DATABASE_URL` env (must be a fresh DB; canary will TRUNCATE all per-run tables on entry) | `alembic upgrade head` against the canary URL at setup | Use when the bug under test interacts with Postgres-specific behavior (jsonb operators, retention sweeper, IAM auth). Requires the operator to provision and clean up the DB. |

**Profile seeding** (`--seed-from-prod`):

The canary's value goes way up when profiles aren't cold. Seeding takes the previous run's `scrape_profiles` rows from the live DB and writes them into the canary DB, so the cascade starts from realistic profile state (saved mappings, dom_hints, etc.). Without this, every property runs as COLD and we can't observe the replay path the fixes target.

Implementation: re-uses `cloud_to_local.py`'s connection plumbing. SELECT scrape_profiles from cloud → INSERT into canary, scoped to the canary's filtered property IDs only.

**Atomicity**: the canary DB is created fresh at the start of each invocation. Any prior canary leftovers are deleted (sqlite: unlink; postgres: `TRUNCATE` per-run tables, `DELETE FROM scrape_profiles` if not seeding). No surprises from stale state.

---

## 4. Failure-list selection

Two sources, in priority order:

1. **`failures.csv`** produced by [`analyze_cloud_run.py`](../scripts/diagnostics/analyze_cloud_run.py) at `c:/tmp/run-{date}/_analyzer_out/failures.csv`. This is the canonical pre-aggregated list with terminal tier and outcome already attributed. Default source.

2. **Live DB query** — when `failures.csv` is missing (analyzer hasn't run yet), the canary falls back to a Q-style query against `scrape_events` joined to `properties` for the date. Slower but always available.

3. **External CSV override** (`--properties-csv`) — for ad-hoc lists ("the 6 properties the customer reported").

Filters are AND-composed. `--include-property-id` is OR'd in after filtering so the operator can always force a specific property.

The materialised input CSV is written to `{out_dir}/canary_input.csv` for transparency: anyone reading the run can see exactly which properties were chosen and why each one was selected.

---

## 5. Replay execution

The canary invokes [`scripts/runners/jugnu.py`](../scripts/runners/jugnu.py) as a subprocess (not in-process) so:

- The existing `--limit`, `--csv`, `--data-dir` args do their work without modification.
- A subprocess has its own env, so `--flag KEY=VAL` injections don't leak to the parent.
- A crashed scrape doesn't take down the canary harness.
- Stdout/stderr capture is straightforward; per-property logs go to `{out_dir}/jugnu.log`.

Subprocess invocation:

```python
env = {
    **os.environ,
    "DATABASE_URL": canary_dsn,
    "DATA_DIR": str(out_dir),
    **flag_overrides,                  # from --flag args
}
subprocess.run([
    sys.executable, "scripts/runners/jugnu.py",
    "--csv", canary_input_csv,
    "--data-dir", str(out_dir),
    "--run-date", today,
    # The canary always scrapes — no change-detection skip — because we want
    # to see what the fixes actually do.
    "--force-scrape",                  # NEW flag on jugnu.py; defaults False
], env=env, timeout=timeout_per_property * limit, check=False)
```

If `jugnu.py` doesn't have `--force-scrape` yet, this plan adds it (one-line gate that bypasses the change-detection check). It's useful outside the canary too: operators debugging a single property frequently want to bypass the skip.

The cloud-run baseline (yesterday's outcome per property) is read from `failures.csv` + `c:/tmp/run-{date}/shard_*/report.json`. No re-scraping needed for the baseline side.

---

## 6. Comparison + report

For each property in the canary input, the report has one row:

| `property_id` | `cloud_outcome` | `cloud_tier` | `cloud_units` | `canary_outcome` | `canary_tier` | `canary_units` | `verdict` | `attributed_fix` |
|---|---|---|---|---|---|---|---|---|

`verdict` ∈ `{IMPROVED, REGRESSED, UNCHANGED_OK, UNCHANGED_FAIL}`.

`attributed_fix` is a best-effort string built from the events emitted during the canary scrape:

- `MAPPING_SAVE_DROPPED` count went from non-zero to 0 → `persistence_hardening` (writer fix took effect)
- A `PROFILE_REPLAY_HIT` event fired against a saved mapping → `url_pattern_normalization`
- `DOM_HINTS_DEGRADED_SAVED` fired AND the property succeeded via DOM tier → `degraded_dom_hint_persistence`
- `FIELD_PATCH_HIT` fired → `field_patch_persistence`
- A previously-LLM-Tier-4 property succeeded at a lower tier → `source_tiered_budget` (saved an LLM call) or `dom_hint_quality_tiered_eviction` (got out of a stale-hint cycle)

Attribution is heuristic, not authoritative — the report flags it with a `(heuristic)` suffix when more than one fix could explain the outcome, so the reader knows to dig.

**Summary block at the top of the report**:

```
Local canary: 50 properties from cloud-run 2026-05-10
  IMPROVED:        21  (was failing, now succeed)
  UNCHANGED_OK:    12  (was succeeding, still succeed — sanity baseline)
  UNCHANGED_FAIL:  16  (was failing, still failing — fix doesn't cover them)
  REGRESSED:        1  (was succeeding, now fail — STOP, do not deploy)

Pre-deploy gate: REGRESSED == 0 → PASS
```

**Exit code semantics**:

- `0` — no `REGRESSED`. Safe to proceed with deploy.
- `1` — at least one `REGRESSED`. Halt. Report names the offending properties.
- `2` — canary infrastructure failure (DB setup, subprocess crash, etc.). Investigate canary tool itself before drawing conclusions about the code under test.

---

## 7. Worked example — canary the persistence-loop PR series

The 9 PRs shipped 5 feature flags. To validate them as a unit before push:

```bash
# Step 1 — baseline run (everything default-off, pre-fix world)
python scripts/diagnostics/local_canary.py \
  --from-run 2026-05-10 \
  --filter-outcome FAILED_NO_DATA \
  --limit 100 \
  --seed-from-prod \
  --flag ENABLE_DEGRADED_MAPPING_PERSIST=false \
  --flag ENABLE_DEGRADED_DOM_PERSIST=false \
  --flag ENABLE_PROMOTE_ON_HINT=false \
  --flag ENABLE_PERSISTENCE_PROBE=false \
  --out-dir data/canary/local_runs/baseline/

# Step 2 — treatment run (all the flags ON, post-fix world)
python scripts/diagnostics/local_canary.py \
  --from-run 2026-05-10 \
  --filter-outcome FAILED_NO_DATA \
  --limit 100 \
  --seed-from-prod \
  --flag ENABLE_DEGRADED_MAPPING_PERSIST=true \
  --flag ENABLE_DEGRADED_DOM_PERSIST=true \
  --flag ENABLE_PROMOTE_ON_HINT=true \
  --flag ENABLE_PERSISTENCE_PROBE=true \
  --out-dir data/canary/local_runs/treatment/

# Step 3 — diff the two reports
python scripts/diagnostics/local_canary.py compare \
  --baseline data/canary/local_runs/baseline/ \
  --treatment data/canary/local_runs/treatment/
```

The compare subcommand produces one delta row per property: did the fix bundle move it from FAILED to SUCCESS, or not. Aggregate:

```
+21  IMPROVED   (treatment recovered them; cite events for attribution)
 -1  REGRESSED  (treatment broke a previously-OK property)
+13  UNCHANGED  (failure mode is outside the persistence-loop's scope)
```

The 1 regression name appears in the report. That's the property that needs investigation before push — the fix bundle isn't safe yet.

For a single PR (e.g. just the URL-normalization fix), drop all the other `--flag` args and only flip `ENABLE_PROMOTE_ON_HINT=false` (since promote depends on URL-normalization producing replay hits in the first place).

---

## 8. Test plan for the canary tool itself

The canary CLI is itself code that will fail — and when it does, operators will trust its output and ship broken code. So it gets tested.

| Test file | Asserts |
|---|---|
| `tests/scripts/test_local_canary_input_selection.py` | `failures.csv` parser handles the actual analyzer output schema (multiple shards, mixed outcomes); filters AND-compose correctly; `--include-property-id` is OR'd after filtering; missing `failures.csv` falls back to live-DB query; `--limit 0` is rejected with a clear error. |
| `tests/scripts/test_local_canary_db_setup.py` | sqlite mode creates the file; alembic-equivalent schema is present (assert all tables in `SqliteDataProvider._SCHEMA`); postgres mode raises clearly when `CANARY_DATABASE_URL` is unset; `--seed-from-prod` copies only the filtered profile IDs, not the entire `scrape_profiles` table; canary teardown actually removes state (sqlite file deleted; postgres tables truncated). |
| `tests/scripts/test_local_canary_replay_subprocess.py` | Subprocess is invoked with the right env (DATABASE_URL set, flag overrides applied); per-property timeout enforced; subprocess crash → reported as `CANARY_INFRA_FAILURE`, not as a property-level FAIL; stdout captured to `{out_dir}/jugnu.log`. |
| `tests/scripts/test_local_canary_comparison.py` | Verdict assignment: synthesize cloud + canary outcome pairs, assert the right verdict (IMPROVED/REGRESSED/UNCHANGED_*); attribution heuristics — synthesize event sequences and assert the right `attributed_fix` string. Multi-fix-could-explain → `(heuristic)` suffix appended. |
| `tests/scripts/test_local_canary_report_render.py` | Markdown report contains every property row; summary stats match the row counts; exit code 0 when no REGRESSED, 1 when ≥1 REGRESSED, 2 when subprocess infrastructure failed. |
| `tests/scripts/test_local_canary_smoke.py` | End-to-end: 3-property in-tree corpus → tiny synthetic `failures.csv` → real subprocess invocation against a fake `jugnu_runner` (`--dry-run-replay` flag exposed for testing only) → assert report file exists with 3 rows. Validates the CLI wire end-to-end without needing a real cloud run. |

All canary-tool tests live under `tests/scripts/` (matching where `test_persistence_health_analyzer.py` and `test_backfill_persistence.py` already live). They use the integration-test conventions from `INTEGRATION_TEST_PLAN.md` (real in-process logic, fakes only at the subprocess and DB-driver boundaries).

---

## 9. Phased rollout

Each phase is one PR. No phase ships the next phase's behavior.

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **L1 — Skeleton + sqlite mode** | `local_canary.py` with: argparse surface (all flags from §2 declared, even if some are no-ops); SQLite DB setup/teardown; `--from-run` reading `failures.csv`; subprocess invocation; minimum-viable comparison (verdict only, no attribution); markdown report stub. Tests for input-selection + db-setup + report-render. | Operator can run `local_canary.py --from-run YYYY-MM-DD --limit 5` against any prior cloud run and get a report with verdicts. No attribution yet. |
| **L2 — Profile seeding** | `--seed-from-prod` reads from cloud DB and writes into the canary DB. Re-uses `cloud_to_local.py` connection plumbing. Test for seed-only-the-filtered-IDs behavior. | Canary runs against a seeded canary DB show the cascade hitting saved mappings — provable by `PROFILE_REPLAY_HIT` events appearing in `events.jsonl`. |
| **L3 — Postgres mode** | `--db-mode postgres`. `CANARY_DATABASE_URL` env. Alembic-driven schema bootstrap. Test marked `@pytest.mark.db`, opt-in. | An operator with a Postgres instance can run the canary against it; same report shape as sqlite mode. |
| **L4 — Fix attribution** | Heuristic event-trace → `attributed_fix` string. Per-fix detection rules in a single registry module so adding a new fix class is one entry. Tests for each detection rule. | Reports for the persistence-loop PR series correctly attribute each IMPROVED row to the right fix. |
| **L5 — Baseline/treatment + compare subcommand** | `--baseline` flag + the `compare` subcommand. The compare command takes two prior canary out-dirs and emits a delta report. | Operator can run the worked example in §7 and get the IMPROVED/REGRESSED/UNCHANGED counts as a single delta block. |
| **L6 — `--force-scrape` plumbing in jugnu.py** | One-line gate that bypasses change-detection. Useful outside the canary too. Test that the existing change-detection path is unchanged when the flag is absent. | Canary runs always re-scrape, even when the property would normally be SKIPPED. |
| **L7 — Smoke + integration tests** | The 6 tests listed in §8. Wire `test_local_canary_smoke.py` into the default CI run (~30s budget). | `pytest tests/scripts/test_local_canary_*.py` exits 0; the smoke test catches a deliberately-broken-canary commit in a regression check. |

### Risks and tradeoffs

- **Subprocess invocation has overhead** (~2s per property for Python startup + Playwright init). For a 50-property canary that's ~100s, plus the actual scrapes — call it 5-10 minutes total. That's acceptable for a pre-deploy gate; it's intolerable for unit-test inner loops, which is why the canary is its own tool and not a pytest fixture.
- **SQLite ≠ Postgres at the edges.** SQLite mode gives speed; Postgres mode gives fidelity. The default is SQLite because most fixes don't depend on Postgres-specific behavior. Fixes that DO (retention sweeper, jsonb path operators, the writer's exception handling under serialization-failure errors) MUST be canaried in Postgres mode before deploy. The runbook for each PR should state which mode is required.
- **Profile seeding is a copy, not a snapshot.** If yesterday's profile state changes between the seed and the canary run (e.g., the live runner updates a profile mid-canary), the seed becomes inconsistent. Mitigate by snapshotting profiles into a parquet/CSV file at canary start, not by re-querying mid-run.
- **Attribution is heuristic.** When two fixes could explain an IMPROVED row, the canary marks it `(heuristic)` and the operator reads the events.jsonl. Better to be honest about ambiguity than to guess wrong.
- **The canary doesn't catch CDN flake.** A property that succeeds locally because its server happened to be up may fail in the cloud where it was down. The canary IS a pre-deploy filter, not the only deploy gate; the cloud-side canary (`persistence_loop_canary_verification.md`) remains authoritative for production behavior.

---

## 10. Recommended first PR

L1 only. Single PR contents:

- [`scripts/diagnostics/local_canary.py`](../scripts/diagnostics/local_canary.py) (new) — argparse + SQLite mode + subprocess invocation + verdict-only report
- [`tests/scripts/test_local_canary_input_selection.py`](../tests/scripts/test_local_canary_input_selection.py) (new)
- [`tests/scripts/test_local_canary_db_setup.py`](../tests/scripts/test_local_canary_db_setup.py) (new)
- [`tests/scripts/test_local_canary_report_render.py`](../tests/scripts/test_local_canary_report_render.py) (new)
- A 3-property fixture under [`tests/fixtures/canary/`](../tests/fixtures/canary/) (new) — `failures.csv` + 3 minimal HTML snapshots so the smoke path has data to chew on
- Updates to [`docs/persistence_loop_canary_verification.md`](persistence_loop_canary_verification.md) to reference the local canary as Step 0 (run before commit + push)

That keeps the review tractable: the SQLite-mode skeleton is independently useful (any operator can already run "did my fix recover the failing properties?" against a recent run), and L2-L7 are additive without breaking the L1 contract.

---

## 11. Cross-references

- Cloud-side canary runbook: [`persistence_loop_canary_verification.md`](persistence_loop_canary_verification.md)
- Pre-deploy verification results that proved the dashboard wiring: [`persistence_loop_canary_results.md`](persistence_loop_canary_results.md)
- Integration test plan (sibling but distinct): [`INTEGRATION_TEST_PLAN.md`](INTEGRATION_TEST_PLAN.md)
- DB diagnostic queries the canary's compare phase reuses: [`scripts/diagnostics/profile_persistence_health.sql`](../scripts/diagnostics/profile_persistence_health.sql)
- Cloud→local DB sync the seeding phase reuses: [`scripts/sync/cloud_to_local.py`](../scripts/sync/cloud_to_local.py)
- The persistence-loop PRs that motivated this plan: see memory hooks `project_persistence_hardening.md` through `project_quality_promotion_and_f2_preconditions.md`
