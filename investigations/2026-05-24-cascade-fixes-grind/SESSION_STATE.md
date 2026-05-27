# Session state — 2026-05-24 cascade-fixes grind

**Branch**: `claude/portal-hop-may19` (worktree: `~/PropAi-main/.claude/worktrees/angry-murdock-c19e06/`)
**Working SHA at end of session**: `ef75170` (cascade-fixes-3886351 + a303462 + ef75170)
**User goal**: cross 80% strict success rate on canary (deterministic, LLM-off)

---

## 🎯 Where we are right now

**Canary baseline (LLM OFF)**: `gs://jugnu-canary/runs/full-d982dbd/` = **3,402 / 4,982 = 68.3%** strict (rent+sqft)

**Measured lift on focused-3886351 canary** (1,580 failing PIDs only):
- Lift rate: **30.8%** (486 properties moved FAILED → SUCCESS)
- Zero regressions
- **New projected canary total: 3,888 / 4,982 = 78.0% strict**
- **Target: 80%** — currently ~100 properties short

---

## 🚦 Active jobs at session end

### 1. Web Unlocker validation test — IN FLIGHT
- Job: `jugnu-unlocker-test-3886351-fl9gv`
- Cohort: 124 no_body residue (BOT_BLOCKED + TRANSIENT + HARD_FAIL subset)
- CSV: `gs://jugnu-canary/property-list/no_body_unlocker_test_2026-05-24.csv`
- Output: `gs://jugnu-canary/runs/2026-05-24-unlocker-test-3886351/`
- Spec: `/tmp/jm_unlocker_test_3886351.yaml`
- Image: `us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/jugnu:cascade-fixes-3886351`
- **Env additions**: `ENABLE_UNLOCKER_TIER=true` + `WEB_UNLOCKER_KEY` secret from `web-unlocker-key-canary`
- Expected cost: ~$0.19 (124 props × $1.50/1000 successful req)
- Expected wall-clock: ~15-20 min
- **Status at handover**: 0/8 shards, exec=Unknown (just started)

### 2. Monitor armed
- Task `bf7blboqh` watches the unlocker test shard growth, terminates at 8/8 or exec terminal
- Timeout: 50 min

---

## ✅ What's been shipped (committed)

### Commit `3886351` — Cascade fixes 2026-05-23
- `_try_curl_cffi_fallback` on BOT_BLOCKED/TRANSIENT/RATE_LIMITED/HARD_FAIL render outcomes
- G5 URN priority fix (canonical `G5_STORE_ID` over `g5-clw-*-{hash}`)
- 5 new adapters: `_apts247`, `_jetengine_repeater`, `_mark_taylor`, `_no_availability`, `_wix_iframe_walker`
- New verdict `SUCCESS_NO_AVAILABILITY`
- JSON-LD gate: reject when both rent+sqft missing
- `_has_rent_sqft_pair` guard on TIER_1_5_EMBEDDED + TIER_3_DOM

### Commit `a303462` — no_body residue fix
- L1 fetcher `_try_curl_cffi_fallback` now tries DIRECT (`proxies={}`) FIRST, falls back to proxied
- Root cause: PROBE_PROXY_URL (BrightData) was getting 403'd on CF-walled vanity hosts where direct GCP IP worked
- Probe-validated: 7/10 recovered

### Commit `ef75170` — RentCafe SHAPE_REJECTED fix
- `_try_rentcafe_securecafe_probe` per-candidate direct-first
- Same root cause: BrightData proxy 403'd on subset of Yardi SC subdomains
- Probe-validated: 3/5 direct vs 2/5 proxied

**Tests at end of session**: 2,253 passing, ruff clean, mypy strict clean on new files.

---

## 📋 Cluster status (post-focused-3886351 canary)

### Still failing (top clusters in measured canary)
| Cluster | Count | Status | Next action |
|---|---:|---|---|
| `no_body_short_circuit` | 136→~37 | ✅ FIXED `ef75170` + Unlocker (in-flight) | Validate Unlocker run, then ship cascade |
| `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | 105→~65 | ✅ FIXED `ef75170` (direct-first) | Validate in next canary |
| `TIER_1_API_ENTRATA_EMPTY` | 103 | ❌ NOT STARTED | Probe ≥3 — likely same direct-vs-proxy issue OR auth cookie missing |
| `TIER_1_DOM_GENERIC_PLAN_TEXT` | 67 | ⚠️ Partial | Plan-text extracted but no rent+sqft pair |
| `TIER_1_API` (generic) | 66 | ❌ NOT STARTED | Per-vendor probe needed |
| `TIER_3_DOM` | 62 | ⚠️ Partial | DOM extracted but no pair (gate fix shipped, didn't fully recover) |
| `TIER_1_KNOCK_API` | 47 | ❌ NOT STARTED | Knock returned 0 units — operator empty inventory? |
| `TIER_1_API_ONESITE_NO_RESPONSE` | 45 | ❌ NOT STARTED | OneSite API empty |
| `TIER_1_API_ENTRATA_SHAPE_REJECTED` | 41 | ❌ NOT STARTED | Same pattern as RentCafe? |

---

## 🧠 Key learnings this session (UPDATE MEMORY)

### 1. PROBE_PROXY_URL routing is a recurring trap
**Pattern**: `probe_get()` defaults to using `PROBE_PROXY_URL` (BrightData) when set. Direct curl_cffi from GCP worker IPs often beats BrightData on CF-walled vanity hosts because:
- BrightData residential pool has burned reputation
- Cloudflare scopes blocks per-subdomain (operator-vanity host vs Yardi SC subdomain have DIFFERENT IP blocklists)
- GCP worker IPs are widely blocked but NOT on every subdomain

**Rule going forward**: any new bot-wall bypass attempt should use the direct-first chain:
```python
# 1. probe_get(url, proxies={}, verify=True) DIRECT
# 2. If non-200 AND PROBE_PROXY_URL is set, fall back to proxied
# 3. If both fail, optionally escalate to Web Unlocker
```

### 2. Web Unlocker is the right paid escalation
**Cost model**: per-request (~$1.50/1000), not per-byte. `format=raw` returns HTML only. Cost-gating prevents runaway.
- For 300-bot-walled-props/run: ~$0.45/run = ~$164/year
- FlareSolverr deferred — would save $127/year but adds infra complexity

### 3. Canary verification is non-negotiable
**Pattern**: probe-validated lift estimates from local dev are systematically off by 30-50%. Real factors:
- IP reputation (dev Mac vs GCP)
- Proxy chain differences
- Container startup quirks (Playwright timing, browser pool exhaustion)

**Rule**: every cluster fix needs a focused canary on the failing cohort (~$5-15 each) before claiming the lift number.

### 4. Always run focused canary, not full
Full 4,982-prop canary = 30-60 min wall-clock + ~$15. Focused failing-cohort canary = 15-20 min + ~$3-5 AND gives cleaner signal (no noise from already-passing properties).

---

## 🗂 Important paths + handles

### GCP
- Project: `jugnu-494013`
- Region: `us-central1`
- Image registry: `us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/`
- Service account: `jugnu-worker-production@jugnu-494013.iam.gserviceaccount.com`
- GCS bucket: `gs://jugnu-canary/`

### Secrets in Secret Manager
- `brightdata-customer-id`
- `brightdata-probe-proxy` ← PROBE_PROXY_URL
- `brightdata-resi-password`
- `brightdata-resi-zone`
- **`web-unlocker-key-canary`** ← WEB_UNLOCKER_KEY (the one we just enabled)

### Local artifacts (will be wiped on session end)
- `/tmp/focused_results/` — focused-3886351 canary properties.json shards
- `/tmp/focused_events_all/` — focused canary events.jsonl (for tracing curl_cffi escalations)
- `/tmp/canary_full/` — baseline canary `full-d982dbd` shards (use for lift comparison)
- `/tmp/no_body_residue.json` — 141 BOT_BLOCKED+TRANSIENT residue PIDs
- `/tmp/rentcafe_shape_cohort.json` — 105 SHAPE_REJECTED PIDs
- `/tmp/properties_failing_1538.csv` — full 1,580 failing CSV
- `/tmp/no_body_unlocker_test.csv` — 124 unlocker test cohort
- `/tmp/jm_focused_3886351.yaml`, `/tmp/jm_unlocker_test_3886351.yaml` — job specs
- `/tmp/canary_post_analysis.sh` — analysis script template

### GCS run outputs
- Baseline canary: `gs://jugnu-canary/runs/full-d982dbd/shard_*/`
- Focused (cascade-fixes): `gs://jugnu-canary/runs/2026-05-23-focused-3886351/shard_*/`
- **Unlocker test (in-flight)**: `gs://jugnu-canary/runs/2026-05-24-unlocker-test-3886351/shard_*/`

### Files in Downloads (client/handoff)
- `~/Downloads/scraped_units_combined_2026-05-23.xlsx` — combined prod+canary best-of, client-ready (no source col)
- `~/Downloads/prod_vs_canary_gap_2026-05-23.xlsx` — gap analysis report

---

## 🔁 How to resume next session

### Step 0: Get oriented
```bash
cd /Users/ankur/PropAi-main/.claude/worktrees/angry-murdock-c19e06
git log --oneline -5  # confirm at ef75170 or later
git status            # should be clean
```

### Step 1: Check the in-flight Unlocker test
```bash
RUN_DATE="2026-05-24-unlocker-test-3886351"
EXEC="jugnu-unlocker-test-3886351-fl9gv"
gsutil ls "gs://jugnu-canary/runs/$RUN_DATE/shard_*/properties.json" 2>/dev/null | wc -l
gcloud run jobs executions describe $EXEC --region us-central1 --project jugnu-494013 \
  --format='value(status.conditions[0].status,status.completionTime)'
```

### Step 2: When complete, analyze lift
```bash
# Pull shards
mkdir -p /tmp/unlocker_results
gsutil ls "gs://jugnu-canary/runs/$RUN_DATE/shard_*/properties.json" 2>/dev/null | \
  xargs -I {} -P 8 sh -c '
    uri="{}"; shard=$(echo "$uri" | grep -oE "shard_[0-9]+")
    gsutil -q cp "$uri" "/tmp/unlocker_results/${shard}.json"
  '

# Compute lift on the 124 cohort (all were FAILED in baseline)
/opt/anaconda3/bin/python3 -c "
import json, glob
strict = 0; total = 0
for p in glob.glob('/tmp/unlocker_results/*.json'):
    for x in json.load(open(p)):
        total += 1
        u = x.get('units') or []
        if any((y.get('rent_low') or 0)>0 and (y.get('area') or 0)>0 for y in u):
            strict += 1
print(f'Unlocker lift on 124 cohort: {strict}/{total} = {strict*100/total:.1f}%')
"

# Also count CURL_CFFI escalations + Unlocker escalations in events.jsonl
gsutil cat gs://jugnu-canary/runs/$RUN_DATE/shard_*/events.jsonl 2>/dev/null | \
  /opt/anaconda3/bin/python3 -c "
import sys, json
from collections import Counter
tiers = Counter()
for line in sys.stdin:
    try: e = json.loads(line)
    except: continue
    if e.get('kind') == 'fetch.tier_escalated':
        tiers[e.get('tier')] += 1
print(dict(tiers))
"
```

### Step 3: Build the next focused canary (re-run failing cohort with NEW fixes from ef75170 + Unlocker)
1. Commit any new code first
2. Build new image (5 min cold): `gcloud builds submit --tag us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/jugnu:<tag> --project jugnu-494013 --timeout=2400 .` from a fresh worktree
3. Mirror spec from `/tmp/jm_focused_3886351.yaml`, update RUN_DATE + image tag
4. `gcloud run jobs replace` + `execute --async`
5. Wait ~20 min, pull shards, compute new lift vs 78.0% baseline

### Step 4: Move to next cluster
After validating the no_body residue + RENTCAFE_SHAPE fixes in canary, work the next-biggest cluster: **TIER_1_API_ENTRATA_EMPTY (103)**.

Use the same probe rule:
1. Pull cohort PIDs from `/tmp/focused_results/`
2. Probe ≥3 properties with direct vs proxied curl_cffi
3. Identify root cause
4. Ship + test (≥3 unit tests, ≥3 probe validations)
5. Re-canary to measure

---

## ⚠️ Pitfalls to avoid

1. **DON'T run a full 4,982-prop canary** when you want to measure lift on a fix — always use a focused CSV (cost 4x more for noisier signal)
2. **DON'T trust dev-machine probes as canary projections** — IP reputation differs by 30-50%
3. **DON'T set `proxies={}` in `probe_get` calls for SecureCafe drill itself when the URL is the operator's main marketing site** — the marketing host blocks GCP, but the SC subdomain often doesn't. Different rules per subdomain.
4. **DON'T use `gcloud builds submit` without a `.gcloudignore`** — strips tracked CSVs via `.gitignore` and the build fails silently. The worktree at `/tmp/cb_<sha>/.gcloudignore` is correct; copy it forward.
5. **DON'T reuse scheduled-job names** (`jugnu-measure` etc.) for per-sha jobs.
6. **Per CLAUDE.md** — every module must have ≥1 live-fixture test or it's a regression timer. The G5 URN fix tests included 4 new tests including the canonical dataLayer extraction.

---

## 📊 Math toward 80% (target)

Current measured (focused-3886351): **78.0%** (3,888 / 4,982)
- ef75170 direct-first (no_body): +~99 (probe-projected)
- ef75170 direct-first (RentCafe SHAPE): +~30-40
- Unlocker (if test passes): +~100 (estimated 80% of 124)
- **Combined expected: ~78.0% + 50-150 props more = ~80-81% in next canary**

If next canary measures >80%, mission accomplished on the target. Remaining clusters (Entrata empty, Knock, OneSite) are bonus.
