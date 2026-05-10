# Persistence-loop backfill (one-time replay through fixed writers)

Once the persistence layer correctly accepts mappings and patches the LLM produces, prior days' extractions (which were dropped silently by the now-fixed bugs) are still NOT in the DB. Without a backfill the cache builds organically over 7+ days; with one, the cache is hot tomorrow.

`scripts/diagnostics/backfill_persistence.py` is a one-time script (re-runnable, idempotent) that walks today's cloud-run artifacts and replays everything through the fixed `save_llm_field_mapping` / `save_field_patch` writers.

## What's reachable from cloud-run artifacts

Per-shard layout in `gs://jugnu-raw-production/runs/{date}/shard_{N}/`:

| Artifact | Has what we need |
|---|---|
| `events.jsonl` | Property IDs that succeeded; tier_won; URL of fetch outcome (no api_url_pattern) |
| `llm_report/{pid}.json` | Per-property LLM interactions. For `tier == "API_ANALYSIS"` interactions: `raw_response` carries `json_paths` + `response_envelope`. `user_prompt` carries `API URL: <url>` (recoverable via line scan). |
| `llm_diagnostics/{pid}_field_recovery.json` | Per-property field-recovery results: `recovered_fields[].field_name`, `confidence`, `source_path`, `parser_fix`. **No URL recoverable** — see "Channel 4 backfill limitation" below. |
| `properties.json` | Per-property `_extract_result.tier_used` + `llm_cost_usd`. **No `_raw_api_responses`, no `_llm_analysis_results`.** Cannot use directly for mapping reconstruction. |

Conclusion: Channel 1 backfill data is in `llm_report/`. Channel 4 backfill from artifacts is **not currently feasible** — see below.

## Channel 4 backfill limitation (discovered 2026-05-10 smoke test)

The null_field_recovery prompt template (`config/prompts/null_field_recovery.txt`) gives the LLM SOURCE_FRAGMENT, PARSER_LOGIC_SUMMARY, PROPERTY_CONTEXT — but **not the API URL**. At runtime, the producer (`scripts/runners/jugnu.py::_run_null_field_recovery`) computes `source_url` via `_resolve_source_url(raw_apis, unit)` and attaches it to the in-memory patch_dict for that turn — but neither the prompt nor the resulting `_llm_interaction` cost-accounting block records the URL on disk.

Smoke test against `c:/tmp/run-2026-05-09/` confirmed: 0 of 9 field_recovery files had a recoverable URL. Backfill therefore emits 0 patches.

To make Channel 4 backfillable in future cloud runs, the producer would need to record the resolved api_url (or a cross-reference to the API_ANALYSIS interaction that produced the unit) inside the field_recovery JSON. That is out of scope for PR 4 — PR 4 focuses on Channel 1 where the URL IS recoverable. Channel 4 cache will rebuild organically once PR 2 ships (real-time persistence works).

## Backfill data flow

```
For each per-property file in shard's llm_report/ and llm_diagnostics/:
  - Read the file, extract the (pid, mapping/patch dicts) pairs
  - Look up profile for pid (skip if missing — orphaned diagnostic without a profile)
  - For each mapping_dict: call save_llm_field_mapping(profile, mapping_dict)
  - For each patch_dict:   call save_field_patch(profile, patch_dict)
  - profile_store.save(profile) at end of each property's batch
Aggregate counters: profiles_touched, mappings_persisted, patches_persisted, dropped_*
```

## Idempotency

Both writers use upsert semantics:
- `save_llm_field_mapping` matches by `api_url_pattern`; same URL re-saved overwrites
- `save_field_patch` matches by `(api_url_pattern, field_name)`; same pair re-saved overwrites

Re-running the backfill twice produces the same DB state (with a slightly newer `discovered_at` on entries that re-saved). Safe to re-run; safe to interrupt and resume.

## Dry-run mode

The default is dry-run: walk the artifacts, count what WOULD be persisted, write a report (`backfill_dryrun_{date}.md`) under `data/reports/`. The report shows per-channel counts, per-shard breakdowns, and any dropped reasons. Operator inspects, then re-runs with `--apply` to actually write.

## CLI

```bash
# Dry-run against a local mirror of today's run:
python scripts/diagnostics/backfill_persistence.py --run-date 2026-05-10

# Auto-pull from GCS first:
python scripts/diagnostics/backfill_persistence.py --run-date 2026-05-10 --pull

# Apply (writes to DB):
python scripts/diagnostics/backfill_persistence.py --run-date 2026-05-10 --apply

# Limit to one shard for testing:
python scripts/diagnostics/backfill_persistence.py --run-date 2026-05-10 --shard 0 --apply

# Verbose: emit per-property progress:
python scripts/diagnostics/backfill_persistence.py --run-date 2026-05-10 -v
```

Default mirror path matches the analyzer's: `c:/tmp/run-{date}/`.

## Output

- `data/reports/backfill_{date}.md` — markdown report with per-channel counts, dropped reasons, top properties by entries persisted
- Both dry-run and apply modes write the report; the dry-run version has a `[DRY RUN]` banner at the top

## Failure modes the backfill must handle

1. **Profile missing for a pid in artifacts** — log warning, skip; some pids may have been deleted between the run and now
2. **Malformed JSON in artifact files** — try/except per file, log + skip; never crash the whole backfill
3. **DB connection drops mid-run** — sqlalchemy retries on transient errors; if a write fails, log + count + continue (don't roll back the batch)
4. **Mapping/patch validates fine but Pydantic raises on append** — caught by save_*_field_mapping's existing try/except; counted as dropped
5. **PR 1's `ENABLE_DEGRADED_MAPPING_PERSIST=false`** — degraded mappings get dropped per the writer's contract; counted with reason `disabled_by_flag`. Operator should run with the flag ON for backfill.

## Tests

1. End-to-end with a synthetic shard directory mirroring real cloud-run layout (3 properties, mix of mapping wins, field recovery wins, and noise classifications). Assert:
   - Dry-run counts mappings/patches that WOULD save
   - Apply mode actually writes to a `tmp_path`-backed FS profile store
   - Idempotency: running apply twice produces the same DB state, second run shows 0 new (all upserts)
2. URL extraction from prompt text — both `API URL: ` form and any drift the prompt template might introduce
3. Skip mode for orphaned pids (no profile in store)
4. Malformed-JSON tolerance (one bad file doesn't crash the run)

## What this PR does NOT do

- Backfill from `raw_api/` directories (those aren't preserved in cloud-run artifacts and Cloud SQL retention only goes 3 days back per `project_postgres_retention_policy.md`)
- Cross-day backfill (only walks one date's artifacts; operator runs once per missing day)
- Replace the live runner's persistence path (the runner already writes via PR 1 + PR 2 fixes)
