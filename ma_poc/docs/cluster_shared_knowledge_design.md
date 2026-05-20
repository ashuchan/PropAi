# Cluster-Level Shared Knowledge for 100-Shard Scraper

**Status:** Proposal · **Date:** 2026-05-20 · **Owner:** TBD
**Related memory:** `project_self_learning_loop_arch.md`, `project_url_pattern_normalization.md`, `project_persistence_hardening.md`

---

## 1. What the system actually looks like today

Verified against the code, not assumed:

### Sharding & lifetime
- 100 Cloud Run Jobs in parallel, sliced by `canonical_id` ceiling-mod in [`stores.py:617-647`](../data_provider/sql/stores.py#L617-L647). ~50 properties/shard at 5000-property scale.
- Each shard writes to its own `/tmp/data/runs/{date}/` and uploads to `gs://.../shard_{idx}/`.
- **No DB writes during the run** — everything flushes via `sync_run_to_pg.py` in the finally block. Shards talk to each other through nothing.
- Infra available: Postgres (Cloud SQL, ~200 max conn, IAM auth via pg8000) + GCS. No Redis, no pub/sub, no advisory locks.

### Per-property learning (already exists)
- `ScrapeProfile` ([`models/scrape_profile.py:341-368`](../models/scrape_profile.py#L341-L368)): per-property JSON blob persisted in `scrape_profiles.payload`.
- `api_hints.blocked_endpoints` (cap 50), `api_hints.known_endpoints` (cap 20, with `json_paths` field mappings), `dom_hints`, `confidence.consecutive_failures`.
- `normalize_url_pattern()` ([`profile_updater.py:161-200`](../services/profile_updater.py#L161-L200)) strips scheme/query so api_key rotation doesn't break replay.
- PMS detected via host regex + management-company priors in [`pms/detector.py:42-160`](../pms/detector.py#L42-L160).

### Rate limiting today — explicitly per-shard, not shared
- [`fetch/rate_limiter.py:47-81`](../fetch/rate_limiter.py#L47-L81) `HostRateLimiter.on_rate_limited()` halves local rps on 429. The docstring literally says: *"Each shard learns independently — no shared state required. With 12 shards collectively hitting one domain, per-shard 2 rps × 12 ≈ 24 rps trips the CDN's global limit."* — this is the bug we are fixing.
- After 3 consecutive 429s, host is quarantined for the rest of the shard ([`fetch/fetcher.py:69-77`](../fetch/fetcher.py#L69-L77)). That state dies when the shard exits.

### Cluster scaffolding already partially built (do not reinvent)
- `cluster_key: str = ""` on profile ([`scrape_profile.py:361`](../models/scrape_profile.py#L361)).
- [`services/cluster_store.py`](../services/cluster_store.py) — already aggregates `LlmFieldMapping`s from HOT cluster-mates with `success_count ≥ 3` for COLD-property warm-start.
- **What it does NOT do**: aggregate `blocked_endpoints` / dead-URL patterns / 429 signals / known-endpoints across the cluster. That is the gap this proposal closes.

---

## 2. Problem framing — three distinct sub-problems being conflated

Different freshness requirements drive different storage choices:

| Sub-problem | Change frequency | Acceptable staleness | Storage |
|---|---|---|---|
| **A. Static cluster knowledge** — URL patterns that reliably yield units, patterns that return no-data, dead hosts | Hours–days | 24h (next warmup) | Postgres |
| **B. COLD-start warm-up** — first-time property needs cluster priors before its first LLM call | One-shot per property | N/A | Postgres (same table) |
| **C. Runtime 429 backpressure** — CDN throttling RIGHT NOW; all shards need to back off in seconds | Seconds | 30–60s | GCS gossip (cheap appends) |

Conflating A/B with C is what kills designs — we end up either over-engineering Postgres (hot-row contention from 100 shards) or under-serving C (24h is too slow when CDN bans the IP pool, as happened on 2026-05-18).

---

## 3. Proposed design — three layers, additive, feature-flagged

### Layer 1 — `pms_cluster_knowledge` table (Postgres, recomputed nightly)

New table, upsert-only, exempt from 3-day retention sweep:

```
pms_cluster_knowledge
  cluster_key             TEXT PRIMARY KEY        -- "rentcafe::propertyabc.com" or "entrata::mark-taylor"
  pms_platform            TEXT
  host_suffix             TEXT
  mgmt_company            TEXT NULL
  member_count            INT                     -- # properties currently in cluster
  known_endpoints         JSONB                   -- [{url_pattern, json_paths, success_rate, n_props_succeeded}]
  blocked_endpoints       JSONB                   -- patterns that returned no-data on ≥3 props × ≥3 days
  dead_url_patterns       JSONB                   -- patterns returning ≥80% 4xx/5xx across ≥3 props × 7d window
  recommended_default_rps REAL                    -- most-aggressive sustainable rate observed cluster-wide
  recommended_max_concurrency INT
  confidence              REAL                    -- bayes-style: scaled by member_count
  schema_version          TEXT                    -- 'v2'
  last_recomputed_at      TIMESTAMPTZ
```

### Layer 2 — `warmup_clusters.py` script (single-instance, pre-fleet)

Runs **once**, T-30min before fleet dispatch (Cloud Scheduler trigger, separate Cloud Run Job, parallelism=1):

1. Read `scrape_profiles WHERE schema_version='v2'` + last 7d of `scrape_events`.
2. Group by `cluster_key`.
3. For each cluster, compute:
   - **known_endpoints**: URL patterns appearing in ≥3 distinct member profiles with `success_count ≥ 3`, ranked by `total_success_count / total_attempts`.
   - **blocked_endpoints**: patterns marked blocked on ≥3 distinct members AND ≥3 days persistent. (Threshold mirrors existing `min_success_count=3` in [`cluster_store.py:43`](../services/cluster_store.py#L43).)
   - **dead_url_patterns**: same idea but for HTTP-layer failures from `scrape_events`.
   - **recommended_default_rps**: `min` of post-decay observed rps across cluster members, floor 0.2.
4. Upsert one row per cluster. Total expected rows: ~50-200.

Output is read-only at run-time — zero write contention from the 100 shards.

### Layer 3 — Shard read path (additive merge)

In `shard_entry.py` startup, once per shard:
```python
cluster_knowledge: dict[str, ClusterKnowledge] = load_all_clusters_from_pg()
```
~1 SELECT, sub-second, sub-MB. Held in memory.

When scraping each property, before request construction:
```
effective_profile = merge(
    naive_bootstrap,
    cluster_knowledge.get(profile.cluster_key),  # NEW priors
    profile,                                      # per-property always wins
)
```
**Authority order: per-property > cluster > naive.** A property that has learned its own answer overrides the cluster default. A COLD property without per-property data inherits the cluster's known/blocked/rate priors and skips Tier 4 LLM on patterns the cluster has already vetted.

### Layer 4 — Runtime 429 gossip via GCS

New `services/host_throttle_gossip.py`. Inside each shard, an `asyncio.create_task` ticker fires every 60s:

**Publish leg**: append the shard's last-60s 429 observations as a small JSON to:
```
gs://{bucket}/runtime/{date}/throttle/shard_{idx}.json
```
Per-shard prefix → no write race.

**Subscribe leg**: list `runtime/{date}/throttle/*.json` (≤100 small files), aggregate per-host counts. If `cluster_observed_429s(host) ≥ N` in last 60s window, call `HostRateLimiter.set_floor(host, computed_rps)`. New method, additive: it only lowers the rate, never raises it.

The existing per-shard 429 decay in [`rate_limiter.py:47-81`](../fetch/rate_limiter.py#L47-L81) stays untouched as fast first-line defense. Gossip is the second-line that prevents the "12 shards × 2 rps = ban" failure mode.

GCS list of 100 small objects + 100 reads/60s = trivial cost (~$0.07/day). No new infra. Files self-expire via existing GCS lifecycle policy on the `runtime/` prefix (e.g., 2-day TTL).

---

## 4. Self-review — risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Cluster poisoning** — one bad property pollutes shared blocklist | Promotion requires ≥3 distinct properties × ≥3 days. Mirrors `cluster_store.py`'s existing `min_success_count=3` invariant. |
| 2 | **Cluster identity drift** — property migrates RentCafe→Entrata | `cluster_key` recomputed at bootstrap on every profile rewrite; old cluster row decays naturally (member_count drops) until next warmup excludes it. |
| 3 | **Warmup failure** | Cluster knowledge is purely additive. If table is empty / unreadable, shards fall back to current per-property-only behavior. Feature flag `ENABLE_CLUSTER_KNOWLEDGE` gates the merge step. |
| 4 | **Postgres hot rows** | Eliminated by design — Layer 1 is read-only at run-time; only `warmup_clusters.py` writes, single-instance. |
| 5 | **GCS gossip staleness** | 60s window is fine because local `HostRateLimiter.on_rate_limited()` already decays on first observed 429. Gossip is reinforcement, not first-line. |
| 6 | **Schema-version skew** with v2-strict DB | `schema_version` column, `WHERE schema_version='v2'` filter in warmup. Mirrors pattern from `project_db_v2_schema.md`. |
| 7 | **3-day retention sweep would wipe it** | Add `pms_cluster_knowledge` to the exemption list in `_apply_retention()` ([`sync_run_to_pg.py`](../scripts/sync_run_to_pg.py)) — same list that exempts `properties`/`units`/`scrape_profiles`. |
| 8 | **Cluster size = 1 (singleton clusters)** | `member_count ≥ 3` gate prevents singleton clusters from getting their own entry; they fall back to per-property behavior. |
| 9 | **Cost of 100 shards × GCS list every 60s** | ~100 × 60 × 24 = 144K LIST ops/day ≈ $0.07/day. Trivial. |
| 10 | **Test coverage** | Unit tests for warmup aggregation rules; contract test that COLD property in a HOT cluster skips Tier 4; chaos test where shard A floods 429s and shards B-J converge to floor within 60s; retention exemption regression test. |

---

## 5. Phasing — ship incrementally, each phase independently valuable

| Phase | Scope | Acceptance signal |
|---|---|---|
| 1 (~3 days) | `pms_cluster_knowledge` table + alembic migration + `warmup_clusters.py` (read-only, writes nothing to fleet yet). Telemetry only. | Table populated nightly; ~50-200 rows; warmup runs <5 min on full dataset. |
| 2 (~3 days) | Shard read path + additive merge. Feature flag `ENABLE_CLUSTER_KNOWLEDGE`. | A/B shows COLD properties in HOT clusters skip Tier 4 LLM on ≥30% of API probes. |
| 3 (~5 days) | GCS-gossip 429 floor. New `host_throttle_gossip.py`. | Chaos test: shard floods 429s; cluster avg rps decays within 60s. Essex-style mass-throttle case (5/18 incident) resolves without manual intervention. |
| 4 (~1 day) | Retention exemption, dashboards (cluster_hit_rate, COLD-warm-start latency, mean throttle convergence). | All weekly gates green; no regression in test suite. |

---

## 6. Implementation commitments

1. **No infra additions.** Postgres + GCS only. If a design pressure points at Redis halfway through, stop and re-design — do not sneak it in.
2. **Workflow steps from `CLAUDE.md` apply to every module** — requirements-first acceptance comments at top of each file, full implementation with type annotations, tests immediately after the module, `ruff` + `mypy --strict` per file, smoke test before declaring a phase done. Do not batch bug-hunt items.
3. **Cluster knowledge is additive, never authoritative over per-property profile.** If a per-property profile's hard-won answer is ever overridden by a cluster default, that is a bug to fix, not to defend.

---

## 7. Open questions

- Cluster identity formula: `{pms_platform}::{host_suffix}` vs `{pms_platform}::{mgmt_company}` vs both as separate keys? Need data on which one yields higher cross-property transferability of `known_endpoints`. Decide in Phase 1 after first warmup run produces real cluster sizes.
- GCS lifecycle policy for `runtime/throttle/` prefix — current bucket policy needs a 2-day TTL rule added. Confirm with infra owner.
- Cloud Scheduler trigger for `warmup_clusters.py` — net-new Terraform resource; confirm naming/ownership conventions match existing `cloud_run_jobs` module.
