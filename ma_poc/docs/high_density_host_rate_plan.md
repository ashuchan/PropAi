# High-density host rate-cascade — implementation plan

**Owner:** TBD
**Status:** Draft / proposal — not yet started
**Drafted:** 2026-05-19
**Source incident:** essexapartmenthomes.com — 27 properties failed `FAILED_UNREACHABLE` on the 2026-05-18 cloud run (full repeat from 2026-05-17 and prior). Every fetch returned `HTTP 429`; per-shard `DOMAIN_QUARANTINED_IN_RUN` quarantine kicked in after 3 attempts. Same pattern observed on equityapartments.com (3 today), gscapts.com (4), krcapartments.com (3) — any management company that hosts many properties on a single origin.

---

## Goal

Eliminate the cross-shard rate-cascade for management companies with many properties on a shared backend, **without** adding runtime coordination overhead (the system runs 100 parallel Cloud Run tasks; any synchronous cross-shard primitive eats into the per-property 600s wallclock).

**Performance budget:** the fix must add ≤ 1 second per affected property in steady state, and zero overhead for the 98% of properties on single-host backends.

---

## TL;DR

Two surgical changes, both shippable independently:

1. **Phase 1 — Temporal stagger inside the manifest shuffle.** Today's shuffle (`CsvPropertyCatalogSource.list_active`, [filesystem.py:549-561](ma_poc/data_provider/filesystem.py#L549-L561)) spreads essex PIDs across shards (2-3 per shard rather than all 27 in one shard). It does NOT spread them across *time*: every shard hits essex at t=0 simultaneously, so the origin sees 100× concurrent load instead of the intended 100× spread load. Add a second pass that, within each shard's row order, **packs high-density hosts at offsets driven by a per-shard hash** so the 100 shards collectively visit essex at uniformly-distributed offsets across the first ~5 minutes of the run.

2. **Phase 2 — Wedge-rescue skip on `DOMAIN_QUARANTINED_IN_RUN`.** A property quarantined by the rate-limiter is currently re-tried by the wedge-rescue pass, which hits the same quarantine and emits a duplicate `FAILED_UNREACHABLE` event. This isn't a failure mode the rescue can fix. Add a `wedge_rescue_decision` branch (matches the existing `SKIP_ENTRY_CAPTCHA` pattern from 2026-05-17) that returns `SKIP_DOMAIN_QUARANTINED` and emits a clean telemetry event.

**Optional Phase 3** (only if Phases 1+2 don't recover ≥ 80% of essex on the first cloud run after deploy): deferred-retry queue backed by a Postgres `deferred_retry` table consumed by a follow-up Cloud Run job scheduled 30 minutes after the main run.

---

## Why the existing fixes don't help

| Layer | What it does | Why essex still fails |
|---|---|---|
| Per-host `rate_limiter.on_rate_limited` ([fetch/rate_limiter.py](ma_poc/fetch/rate_limiter.py)) | Halves the host's rps on every 429, floor 0.1 | Per-shard state. 100 shards each independently halve their already-low rate; the origin still sees 100× the intended concurrent load. |
| `_DOMAIN_QUARANTINE_THRESHOLD` in-job quarantine ([fetch/fetcher.py](ma_poc/fetch/fetcher.py)) | After N consecutive 429s, all remaining requests to that host return `RATE_LIMITED / DOMAIN_QUARANTINED_IN_RUN` | Per-shard. Doesn't help — by the time a shard quarantines, it has already failed its essex PIDs. |
| Manifest shuffle ([data_provider/filesystem.py:549](ma_poc/data_provider/filesystem.py#L549)) | Spreads essex's 27 contiguous CSV rows over 100 shards via deterministic shuffle | Spreads them across **shards**, not across **time**. All 100 shards start at t=0 and hit essex in their first ~30 seconds. |
| Wedge-rescue retry ([scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py)) | Re-tries `PARTIAL` / `FAILED_UNREACHABLE` with `RenderMode.HEAD` | Quarantine ignores RenderMode; the rescue re-fails immediately and double-emits `FAILED_UNREACHABLE`. |

**Quantified blast radius today:** 27 essex + 7 (gsc+krc+equity) = 34 deterministic failures every cloud run from this one bug class. Manual extrapolation across CSV (`grep -c "essexapartmenthomes.com\|equityapartments.com\|gscapts.com\|krcapartments.com\|fmgnj.com\|sentral.com\|venterraliving.com\|abbeyresidential.com" config/properties.csv`) gives ~85 properties on known high-density backends.

---

## Phase 1 — Temporal stagger inside the manifest shuffle

### Mechanism

Today's `list_active` shuffles globally and shards by modulo:

```python
pairs = list(pairs)
rng.shuffle(pairs)
pairs = self._apply_shard(pairs, filters)   # keeps rows where idx % shard_count == shard_index
```

After this, each shard sees its essex PIDs at *whatever modulo positions they happened to land* — typically tightly clustered in the first few rows because the global shuffle doesn't know to avoid high-density-host clumps within the same shard.

The change: after `_apply_shard`, add a **per-shard host-aware reordering pass** that pushes high-density-host PIDs to staggered offsets, where the stagger is driven by a hash of `(host, shard_index)` so each shard hits essex at a different wall-clock offset.

```python
# data_provider/filesystem.py — new helper, called immediately after _apply_shard
def _temporally_stagger_high_density_hosts(
    pairs: list[tuple[str, dict]],
    shard_index: int,
    host_density_threshold: int = 5,
) -> list[tuple[str, dict]]:
    """Re-order this shard's rows so high-density-host PIDs land at
    staggered offsets driven by hash(host, shard_index).

    "High-density" = the host appears >= ``host_density_threshold`` times
    in the GLOBAL catalog. Looked up from a precomputed
    ``_global_host_density`` map; cached on the CsvPropertyCatalogSource
    instance after first read. The lookup is O(1) per row.

    For each high-density host present in this shard, compute an offset
    ``offset = hash(host, shard_index) % (len(pairs) // 2)`` and move
    that host's PIDs to start at ``offset``. Stagger increment between
    PIDs of the same host: hash(host) % 50 rows, so they don't cluster
    inside the shard either.
    """
```

### Why this works

- Essex has 27 PIDs globally. With shuffle, ~each shard gets 0-2 of them. Pre-fix, those 0-2 land in the shard's first few rows (because the shuffle is uniform but tasks process rows sequentially → first rows execute earliest).
- Post-fix, shard 0 puts its essex PID at offset 17 (hash-derived), shard 1 at offset 89, shard 33 at offset 2, etc. The 100 shards' essex hits are now uniformly spread across rows 0-2500 — at ~3-5 seconds per property avg, that's a 100x flatter rate curve at the origin.

### Performance

- **Catalog read:** unchanged (still one CSV scan).
- **Global host density count:** 1 additional pass at first read, O(N) where N = 5000. ~30 ms.
- **Per-row stagger lookup:** O(1) hash. ~1 µs/row.
- **No runtime coordination:** the stagger is pure-function of `(host, shard_index, global_density_map)`. Each shard computes its own stagger from data it already has.

### Code change

| File | Change | LOC |
|---|---|---|
| `ma_poc/data_provider/filesystem.py` | Add `_global_host_density` cached property + `_temporally_stagger_high_density_hosts` helper + call after `_apply_shard` | ~40 |
| `ma_poc/data_provider/dtos.py` | Optional: add `host_density_threshold: int = 5` to `CatalogFilters` so the threshold is tunable per-run | ~3 |
| Tests | `test_temporal_stagger_distributes_high_density_hosts` — fixture: 100 PIDs from one high-density host, 100 shards; assert the per-shard offset histogram is roughly uniform | ~80 |

### What it does NOT fix

- A single high-density host with >100 PIDs (essex has 27, so ≤ 1 per shard; if a host had 200 PIDs, several would still land in the same shard's first few rows). For >100-density hosts add a secondary intra-shard delay — see Phase 1.5 below.
- A backend that genuinely cannot serve at 1 req/min (some PMS portals throttle aggressively regardless of distribution). Those are real `DEAD_URL`-class issues, not rate-cascade. The Phase 2 wedge-rescue skip surfaces them cleanly.

### Phase 1.5 (deferred, only if a >100-density host appears)

When a host has ≥ 100 PIDs globally, fall back to in-shard temporal sleep: before each fetch to that host, sleep for `(hash(pid) % 60)` seconds. Adds up to 60s latency per high-density PID but at most 1-2 PIDs per shard, so wallclock impact is bounded.

---

## Phase 2 — Wedge-rescue skip on `DOMAIN_QUARANTINED_IN_RUN`

### Today

```python
# scripts/runners/jugnu.py — pre-fix wedge_rescue_decision (paraphrased)
def wedge_rescue_decision(meta, *, has_units):
    verdict = (meta.get("verdict") or "").upper()
    if verdict not in {"PARTIAL", "FAILED_UNREACHABLE"} or has_units:
        return "NO_RETRY"
    if meta.get("entry_captcha_detected") or meta.get("entry_bot_blocked"):
        return "SKIP_ENTRY_CAPTCHA"
    return "RETRY"
```

PID 12586 (essex) trace from 2026-05-18:
```
16:11:02  output.property_emitted verdict=FAILED_UNREACHABLE   (attempt loop exhausted)
16:24:42  fetch.completed outcome=RATE_LIMITED error_signature=DOMAIN_QUARANTINED_IN_RUN elapsed_ms=0
16:24:42  output.property_emitted verdict=FAILED_UNREACHABLE   (wedge-rescue duplicate)
```

The wedge-rescue retry fired despite the domain being already quarantined for the entire run. Result: **2× FAILED_UNREACHABLE emit per quarantined PID**, inflating the failure metric and burning a per-property task slot for nothing.

### Change

Add a branch to `wedge_rescue_decision` that mirrors the existing `SKIP_ENTRY_CAPTCHA` pattern:

```python
if meta.get("entry_rate_limited") or meta.get("domain_quarantined_in_run"):
    return "SKIP_DOMAIN_QUARANTINED"
```

Plumb the flag from `_fetch_diagnostic` into `_meta` in `_process_property` (same one-liner as the captcha plumbing landed 2026-05-17).

### Code change

| File | Change | LOC |
|---|---|---|
| `ma_poc/scripts/runners/jugnu.py` | Add `SKIP_DOMAIN_QUARANTINED` branch + `_meta.domain_quarantined_in_run` plumbing | ~12 |
| `ma_poc/observability/events.py` | Update `WEDGE_RESCUE_RETRY_RESOLVED` docstring with the new resolution | ~3 |
| Tests | Extend `tests/scripts/test_wedge_rescue_decision.py` with 4 cases: quarantined → SKIP, quarantined + has_units → NO_RETRY, captcha precedence, plain RATE_LIMITED w/o quarantine → RETRY | ~50 |

### Performance

- **Saved work per quarantined PID:** 1 wedge-rescue HEAD fetch (~1-2s) + 1 redundant `output.property_emitted` write
- **Saved telemetry volume:** halves the FAILED_UNREACHABLE event count for the affected cluster (today: ~34 PIDs → 17 dropped events)
- **No new dependencies; no runtime coordination.**

---

## Phase 3 (optional) — Deferred retry queue

**Only build if Phase 1+2 don't recover ≥ 80% of high-density-host PIDs on the first post-deploy cloud run.**

### Mechanism

- New Postgres table `deferred_retries (canonical_id text, url text, host text, deferred_at timestamptz, reason text, attempts int)`
- When a PID hits `DOMAIN_QUARANTINED_IN_RUN` + Phase 2 SKIP, insert a row instead of emitting `FAILED_UNREACHABLE`
- New Cloud Run job `jugnu-deferred-retry-{env}` runs 30 minutes after the main daily job, consumes the table, retries with `--concurrency 1 --per-host-delay 5s`
- Successful retries upsert into `properties` / `units` like a normal run; failures get a final `FAILED_UNREACHABLE` emit with `reason: "deferred_retry_exhausted"`

### Performance

- **Main job:** zero overhead (just one INSERT per quarantined PID instead of one duplicate event emit).
- **Deferred job wallclock:** ~5 min for 100 PIDs at concurrency 1 + 5s spacing. Costs ~$0.10/day.
- **Latency:** quarantined-PID data lands ~30 min late in the daily output. Acceptable for the ~5% of affected properties.

### Code change

| File | Change | LOC |
|---|---|---|
| `ma_poc/data_provider/dtos.py` + alembic migration | New table | ~30 |
| `ma_poc/scripts/runners/jugnu.py` | Insert deferred row in `SKIP_DOMAIN_QUARANTINED` branch | ~15 |
| `ma_poc/scripts/runners/deferred_retry.py` | New thin runner | ~120 |
| `gcp/terraform/cloud_run_jugnu_deferred_retry.tf` | New scheduled job | ~40 |
| Tests | End-to-end retry test + Phase 2 INSERT-not-emit test | ~80 |

---

## Rollout sequencing

| Step | What | Gate | Owner |
|---|---|---|---|
| 1 | Land Phase 1 (temporal stagger) + tests | Pytest green; `_temporally_stagger_high_density_hosts` unit test asserts uniform offset distribution | |
| 2 | Local canary: 32 PIDs from yesterday's failures + 4 essex sentinels + 4 single-host sentinels (regression guard). Compare HEAD vs Phase 1. | Phase 1: ≥ 50% essex recovery, 0 single-host regressions | |
| 3 | Deploy Phase 1 to production. Watch one daily run. | Production essex success rate ≥ 60% | |
| 4 | Land Phase 2 (wedge-rescue skip) + tests | Pytest green; duplicate `FAILED_UNREACHABLE` count drops to 0 | |
| 5 | Deploy Phase 2. Confirm. | No new failure modes; telemetry cleaner | |
| 6 | Decide on Phase 3 | If essex success < 80% after Phase 1+2, ship Phase 3 | |

Phase 1 alone should recover ≥ 60% of essex (the ones whose 429 was triggered by concurrent load, not by genuine origin capacity exhaustion). Phase 2 cleans up the duplicate emits but doesn't recover any additional units. Phase 3 catches the long tail.

---

## Non-goals

- **Global cross-shard rate-limit primitive (Redis / DB token bucket).** Adds infra dependency + ~10ms per fetch round-trip × 500K fetches = ~83 min CPU-time inflation. Not justified for the ~5% of PIDs this affects.
- **Negotiating with management companies for higher rate limits.** Possible long-term but out of scope for code.
- **Switching to residential proxies for all high-density-host fetches.** Residential proxies don't help with origin-side rate limits; the origin sees the same IP space at the proxy edge.

---

## Open questions

- Should the `host_density_threshold` be tunable per-PMC or set globally? Initial recommendation: global at 5, revisit after one week of data.
- Phase 3's deferred job — schedule via Cloud Scheduler trigger or as a follow-up step inside the main job's Terraform `depends_on` chain? Cleaner separation with Cloud Scheduler; less moving parts as a follow-up step.
- Should we expose the per-shard temporal stagger map as a debug artifact in `data/runs/{date}/`? Helpful for forensic when the stagger doesn't behave as expected. Recommend: yes, dump as `manifest_stagger_shard_{N}.json`.

---

## Appendix — sanity-check the global host density

```sql
-- Run against config/properties.csv via the local proppy DB or via a quick Python
-- pandas one-liner. Pre-Phase-1 numbers to set the threshold sensibly.
SELECT
  regexp_replace(lower(url), '^https?://(www\.)?([^/]+).*$', '\2') AS host,
  count(*) AS n
FROM properties
WHERE coalesce(extra->>'active', 'true') = 'true'
GROUP BY 1
HAVING count(*) >= 5
ORDER BY n DESC
LIMIT 25;
```

Expected top: essexapartmenthomes.com (27), equityapartments.com, irvinecompanyapartments.com, princetonmanagement.com, gscapts.com, krcapartments.com, venterraliving.com.
