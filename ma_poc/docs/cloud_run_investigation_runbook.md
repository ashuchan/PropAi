# Cloud-run investigation runbook

**Audience:** Claude (or any engineer) running a post-cloud-run failure investigation. Use this when a daily run finishes and you need to (a) verify what actually happened, (b) cluster failures by pattern, (c) trace patterns back to specific lines of code, (d) decide what to fix.

**Source authority for this runbook:** the 2026-05-11 investigation captured in [`run_2026_05_11_manual_analysis.md`](run_2026_05_11_manual_analysis.md) and [`2026_05_11_regressions_fix_design.md`](2026_05_11_regressions_fix_design.md). Every technique below is shown with the actual command used and the actual bug it surfaced.

---

## Before you start — orientation

### Read these first (5 min)

1. `ma_poc/CLAUDE.md` — the project's source of truth. Tells you which scripts are canonical (`scripts/daily_runner.py` + `scripts/entrata.py` for production; `scripts/jugnu_runner.py` for the Jugnu pipeline used by the cloud run).
2. `ma_poc/scripts/CLAUDE.md` — scripts-directory implementation guide. Documents the 7-tier extraction cascade and the self-learning loop.
3. Your auto-memory `MEMORY.md` (loaded automatically by the harness) — has project state, e.g. "ma_poc is canonical for data and config dirs", "SCHEMA_VERSION env var is the single source of truth", DB retention policies. Don't re-derive things that are already known.
4. The two most-recent manual-analysis docs: `ma_poc/docs/run_YYYY_MM_DD_manual_analysis.md`. They tell you what the run *looked like* yesterday and which bugs were recently fixed (might already be healed in the run you're looking at).

### Constants you'll need

| Constant | Value | Where it lives |
|---|---|---|
| GCS bucket | `jugnu-raw-production` | `scripts/diagnostics/analyze_cloud_run.py:GCS_BUCKET` |
| Per-run GCS prefix | `gs://jugnu-raw-production/runs/YYYY-MM-DD/` | same |
| Local mirror root | `c:/tmp/run-YYYY-MM-DD/` (Windows) — change to `~/tmp/...` on Linux/Mac | analyzer's `DEFAULT_LOCAL_MIRROR` |
| Analyzer output | `ma_poc/data/reports/cloud_run_YYYY-MM-DD/` | `DEFAULT_OUT_ROOT` |
| Working directory for commands | `ma_poc/` (the application root), not `PropAi/` (the repo root) | Every `python -m pytest …` and `python scripts/…` invocation assumes cwd is `ma_poc/`. |
| Today's date | from the harness's `currentDate` in your system reminders | Yesterday = today − 1 day. |
| Expected shard count | **drifts** — was 20 on 2026-05-09/10, 50 on 2026-05-11 | Always check actual count first (Phase 1.1) before passing `--expected-shards`. |

### Vocabulary glossary

The codebase uses two distinct uses of "Pn"; this runbook uses both. Don't confuse them.

| Symbol | Means |
|---|---|
| **Principle P1, P2, P3, P4** | The four cross-cutting design principles documented in `2026_05_11_regressions_fix_design.md`. Used in Phase 8 ("code patterns to look for"). |
| **Failure pattern P2, P3, P4, P6, P7, P8** | The analyzer's `pattern_id` column in `failures.csv`. P2=CF-blocked, P3=generic-TIER_1_API, P4=Entrata-no-CF, P6=platform-zero, P7=unreachable, P8=LLM-gate-no-body. Used in Phase 4. P1 and P5 are intentionally unused (legacy slots). |
| **Verdict** (`_meta.verdict`) | Per-property outcome: `SUCCESS`, `FAILED_NO_DATA`, `FAILED_UNREACHABLE`, `CARRY_FORWARD`, `PARTIAL` |
| **Tier** (`_extract_result.tier_used`) | Which extraction strategy won. Convention: `TIER_<N>_<NAME>[_<SUBNAME>][_LLM_RESCUE]`. Examples: `TIER_1_API_ENTRATA`, `TIER_3_DOM`, `TIER_1_PROFILE_MAPPING` (zero-LLM-cost replay tier), `TIER_MERGED_CROSS_PAGE` (link-hop succeeded). `LLM_RESCUE` suffix means the LLM rescue path won after the platform adapter returned empty. |
| **Property identity** | Same property has multiple keys in different layers: `Property ID` / `Unique ID` (CSV input), `apartmentid` (v2 schema), `canonical_id` (Jugnu runner sets this in `_meta.canonical_id`), `property_id` (the event-ledger key). They're all the same string. The runner ensures consistency. |
| **Bug 1** (mentioned in retrospectives) | A prior bug fixed 2026-05-11 morning: `ApiEndpoint.json_paths` Pydantic validation crashed `update_profile_after_extraction` for properties whose LLM mappings had null values. ~245/day until commit `639ccc3`. Documented in [`run_2026_05_10_manual_analysis.md`](run_2026_05_10_manual_analysis.md). Important context: its multi-day collateral seeded Bug B's symptom. |

### When you have no prior-day mirror (first-time investigation)

`--compare-date` only works if both days are mirrored. If you're investigating the first run you've ever looked at:

1. Skip `--compare-date` in Phase 2; the analyzer still produces `summary.md` + `failures.csv` + `successes.csv`.
2. Skip Phase 5 (trace-divergence requires both days). Jump straight from Phase 4 (clustering) to Phase 6 (suspect-narrowing) — use error-string grep + git-log from the deploy window instead of a yesterday-vs-today diff.
3. Pull the previous day's mirror in the background while doing Phase 4 so Phase 5 becomes available later: `python c:/tmp/pull_<date>.py YYYY-MM-DD` for the previous date.

### Quick health check — kill-switch before deep analysis

A fresh session should NOT do the full investigation if the run is fine. After Phase 2 (analyzer ran), check `summary.json`:

```python
import json
s = json.load(open('ma_poc/data/reports/cloud_run_2026-05-11/summary.json'))
total = s['totals']['properties']
# Use the events-derived numbers, not totals.succeeded — see Phase 3.
import collections
from pathlib import Path
ev_succ = ev_fail = 0
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get('kind') == 'output.property_emitted':
            v = e.get('verdict','')
            if v == 'SUCCESS': ev_succ += 1
            elif v.startswith('FAILED'): ev_fail += 1
true_rate = ev_succ / max(1, total)
print(f'true success rate: {true_rate:.2%}  ({ev_succ}/{total})')
```

| True rate | Action |
|---|---|
| ≥ 95 % AND no day-over-day regression cluster > 50 properties | Run is healthy. Write a 5-line "all clear" note. Stop. |
| 90–95 % | Soft regression — still worth Phase 4 to identify which pattern moved, but don't expect a new bug. |
| < 90 % | Real problem. Continue with full investigation. |

---

**Time budget per phase:**

| Phase | Wall clock | What you produce |
|---|---|---|
| 1. Pull the data | 5–10 min | local mirror at `c:/tmp/run-<date>/` |
| 2. Run the analyzer | 1 min | `data/reports/cloud_run_<date>/summary.md` |
| 3. **Verify the headline metric** | 5 min | reconciliation table — report.json vs events.jsonl |
| 4. Cluster failures by pattern | 15–30 min | trace-shape histogram + per-pattern domain clusters |
| 5. Trace divergence analysis | 30 min | side-by-side event-kind sequence today vs yesterday for one representative property |
| 6. Suspect-narrowing | 30 min | the specific file + line + git commit that introduced the divergence |
| 7. Quantify | 10 min | "this hypothesis explains N of M failures" |
| 8. Reach the fix design | flows into [`2026_05_11_regressions_fix_design.md`](2026_05_11_regressions_fix_design.md)-shaped doc | |

The hardest mistakes to recover from happen in Phase 3 (trusting the wrong number) and Phase 5 (generalising from one property without bucketing). Get those right and the rest is mechanical.

---

## Phase 1 — Pull the data

### 1.1 Auth

Two paths, try in order:

**Option A: gcloud auth (when fresh)**

```bash
gcloud auth list
gcloud storage ls gs://jugnu-raw-production/runs/2026-05-11/ | head
```

If `gcloud storage` works, you can use `gcloud storage rsync` directly (the analyzer's `--pull` flag uses this).

**Option B: ADC token + REST (when gcloud auth has expired interactively)**

This is what worked on 2026-05-11 when gcloud login was stale and the harness couldn't do an interactive `gcloud auth login`:

```bash
gcloud auth application-default print-access-token
# → returns a token even when interactive auth is broken
```

Then call the GCS JSON API directly:

```bash
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://storage.googleapis.com/storage/v1/b/jugnu-raw-production/o?prefix=runs/2026-05-11/&delimiter=/&fields=prefixes" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('prefixes',[])), 'shard prefixes')"
```

### 1.2 What to mirror — and what to skip

**Mirror these (the analyzer needs them):**

| File | Per-shard size | Why it matters |
|---|---|---|
| `events.jsonl` | 1–3 MB | **Authoritative event ledger.** Every `extract.*` / `fetch.*` / `output.property_emitted` event lives here. |
| `report.json` | ~2 KB | Per-shard summary. **Verify against events.jsonl — do not trust blindly** (see Phase 3). |
| `issues.jsonl` | ~10 KB | Validation issues with ERROR/WARNING/INFO codes per property. |
| `llm_report.json` | ~15 KB | LLM cost rollup per property + per tier. |
| `bot_blocked_properties.json` | ~1 KB | Per-shard fetch-outcome detail. |
| `amenities_report.json` / `concessions_report.json` | <1 KB | Run-level aggregates. |

**Skip these (large, rarely needed for aggregate analysis):**

| File | Why skip |
|---|---|
| `*.html` | Raw HTML dumps, 50–500 KB each × thousands per run. Only pull on-demand for one property when investigating its specific trace. |
| `NNN.json` (per-property) | Per-property raw extraction body. Same: pull on-demand. |
| `NNN.md` (per-property report) | Pre-rendered per-property report. Pull on-demand. |
| `cost_ledger.db` | SQLite, ~30 KB × shards. Not used by the analyzer; the `llm_report.json` summary is enough. |

### 1.3 The REST mirror script

This is the script that worked on 2026-05-11 when `gcloud storage rsync` was auth-blocked. Save it as `c:/tmp/pull_<date>.py`:

**Windows gotcha** — the script's `gcloud_token()` shells out to `gcloud auth application-default print-access-token`. On Windows, `gcloud` is a `.cmd` file; Python's `subprocess.run(["gcloud", …])` won't find it without `shell=True`. The script handles this AND accepts a pre-computed token via the `GCLOUD_TOKEN` env var. Always export it explicitly:

```bash
export GCLOUD_TOKEN=$(gcloud auth application-default print-access-token)
python c:/tmp/pull_<date>.py YYYY-MM-DD
```


```python
"""REST-based mirror of gs://jugnu-raw-production/runs/<date>/.
Pulls only files the analyzer needs (events, report, issues, llm_report,
bot_blocked, amenities, concessions). Skips *.html, NNN.json, *.db.
"""
import json, os, shutil, subprocess, sys, time, urllib.parse, urllib.request
import concurrent.futures as cf
from pathlib import Path

BUCKET = "jugnu-raw-production"
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-05-11"
PREFIX = f"runs/{DATE}/"
DEST = Path(f"c:/tmp/run-{DATE}")
WANT = {
    "events.jsonl", "issues.jsonl", "report.json", "llm_report.json",
    "bot_blocked_properties.json", "amenities_report.json",
    "concessions_report.json", "summary.json",
}

def token():
    e = os.environ.get("GCLOUD_TOKEN")
    if e: return e.strip()
    return subprocess.run(
        "gcloud auth application-default print-access-token",
        capture_output=True, text=True, check=True, shell=True,
    ).stdout.strip()

def list_objects(tok):
    page_token = None
    while True:
        params = {"prefix": PREFIX, "maxResults": "1000",
                  "fields": "items(name,size),nextPageToken"}
        if page_token: params["pageToken"] = page_token
        url = (f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        for item in data.get("items", []): yield item
        page_token = data.get("nextPageToken")
        if not page_token: break

def download(tok, name, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    enc = urllib.parse.quote(name, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{enc}?alt=media"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        dest_path.write_bytes(resp.read())

def main():
    if DEST.exists(): shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    tok = token()
    wanted = []
    for item in list_objects(tok):
        name = item["name"]
        if name.rsplit("/", 1)[-1] in WANT:
            wanted.append((name, DEST / name[len(PREFIX):]))
    print(f"[list] {len(wanted)} files to download")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(lambda t: download(tok, t[0], t[1]), wanted))
    print(f"[done] {time.time()-t0:.1f}s")

if __name__ == "__main__":
    sys.exit(main() or 0)
```

Invoke:

```bash
export GCLOUD_TOKEN=$(gcloud auth application-default print-access-token)
python c:/tmp/pull_2026-05-11.py 2026-05-11
# → ~340 files, ~80 MB, ~55 seconds for 50 shards
```

### 1.4 Verify the mirror is complete

```bash
ls c:/tmp/run-2026-05-11/ | wc -l
# → should equal the shard count from the GCS prefix list
ls c:/tmp/run-2026-05-11/shard_0/
# → events.jsonl, report.json, issues.jsonl, llm_report.json, etc.
```

---

## Phase 2 — Run the analyzer

```bash
cd ma_poc
python scripts/diagnostics/analyze_cloud_run.py \
  --date 2026-05-11 \
  --compare-date 2026-05-10 \
  --expected-shards 50 \
  --local-mirror c:/tmp
```

**Always pass `--expected-shards` explicitly.** Default is 20 but shard count drifts — 2026-05-11 ran with 50, 2026-05-10 ran with 20. The analyzer flags missing shards based on this value.

**Outputs land in `ma_poc/data/reports/cloud_run_<date>/`:**

| File | Use it for |
|---|---|
| `summary.md` | First read. Top-line metrics, tier distribution, fetch signatures, persistence-health SLOs, per-pattern domain clusters. **Headline `Success rate` may be wrong — see Phase 3.** |
| `summary.json` | Same data, machine-readable. Use this when feeding to other scripts. |
| `failures.csv` | Flat per-property failure table. Columns include `pattern_id` (P2/P3/P4/P6/P7/P8), `terminal_tier`, `fetch_error_signature`, `domain`, `issue_codes`. Sort by `pattern_id` then `domain` to find clusters. |
| `successes.csv` | Same shape for successful properties — useful for "what changed between today's successes and today's failures?" |
| `comparison_with_<prev>.md` | Day-over-day diff: regressions (passed→failed), recoveries (failed→passed), repeat failures, top regressed domains. **The fastest signal of a regression deploy.** |
| `INDEX.md` | One-time index. Add narrative links here when you write the manual analysis doc. |

### 2.1 Glossary for `comparison_with_<prev>.md`

The day-over-day comparison file uses terms that aren't standard. They mean:

| Term | Definition |
|---|---|
| **Regression** | Property passed yesterday and failed today. Strongest signal of a code regression. The sample table lists 50; the full set lives in `failures.csv` after a cross-reference. |
| **Recovery** | Failed yesterday, passed today. Useful for verifying that a deployed fix did what it claimed. |
| **Repeat failure** | Failed both days. The long-tail "hard cases" — usually CF-blocked, syndication-only, or genuinely-down sites. |
| **New failure** | In today's run but not in yesterday's, failed today. Usually means the input CSV was extended. |
| **Dropped from run** | In yesterday's run, missing from today's. Either CSV shrunk or shards were unequal. |
| **New in run** | Mirror of "dropped". |
| **Repeat-failure top domains** | Management-company clustering of the "repeat failure" set. Use this to identify "is the long tail concentrated on one PMC's template?" |

### 2.2 Distinguishing pre-existing failures from new ones

A common question during investigation: "is this test failure / pattern caused by what changed today, or did it exist before?" Use git stash:

```bash
# At cwd = ma_poc/
git -C /c/Users/ashus/OneDrive/Documents/Code/PropAi stash
python -m pytest tests/path/to/specific_test.py::TestName -q --tb=line 2>&1 | tail -5
git -C /c/Users/ashus/OneDrive/Documents/Code/PropAi stash pop > /dev/null 2>&1
```

If the test fails AT HEAD too, it's pre-existing. **This is the only honest way to attribute a test failure to your changes (or absolve them).** Used repeatedly during the May-11 self-review to separate "my fix broke X" from "X was already broken when I started."

---

## Phase 3 — Verify the headline metric (do NOT skip)

This is the single most important phase. The May-11 analysis got pulled here for an entire afternoon: the analyzer's summary.md said **99.92 % success**, and the truth was **58.95 %**. The 41-pp gap was Bug A.

### 3.1 The reconciliation check

`report.json` reports `totals.succeeded` per shard; `events.jsonl` carries the authoritative `output.property_emitted` verdicts per property. They should agree. When they don't, something between the runner and the on-disk JSON dropped the verdict.

```python
import json, collections
from pathlib import Path

RUN = Path('c:/tmp/run-2026-05-11')
report_totals = collections.Counter()
event_verdicts = collections.Counter()

for shard in sorted(RUN.iterdir()):
    if not shard.is_dir() or not shard.name.startswith('shard_'):
        continue
    rep = shard / 'report.json'
    ev = shard / 'events.jsonl'
    if rep.exists():
        r = json.loads(rep.read_text(encoding='utf-8'))
        t = r.get('totals', {})
        report_totals['properties'] += int(t.get('properties') or 0)
        report_totals['succeeded'] += int(t.get('succeeded') or 0)
        report_totals['failed'] += int(t.get('failed') or 0)
    if ev.exists():
        for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
            try: e = json.loads(line)
            except json.JSONDecodeError: continue
            if e.get('kind') == 'output.property_emitted':
                event_verdicts[e.get('verdict')] += 1

print('=== report.json totals (sum across shards) ===')
for k, v in report_totals.items(): print(f'  {k}: {v}')
print('=== events.jsonl emit verdicts ===')
for k, v in event_verdicts.items(): print(f'  {k}: {v}')
```

### 3.2 What "they disagree" means

| report.json `succeeded` vs events `SUCCESS` | Meaning |
|---|---|
| Equal (or within 5–10) | Reporting layer is healthy. Trust summary.md. |
| `report.json.succeeded` >> `events SUCCESS` | **Bug A class.** The runner emitted FAILED events but `_meta.verdict` got dropped before `report.json` was written. The headline metric is wrong. |
| `events SUCCESS` >> `report.json.succeeded` | Runner emitted SUCCESS but the shard report.json hadn't been written for those properties (run was interrupted mid-shard). Check `shards_seen` vs `shards_expected`. |
| `events SUCCESS` ≈ 0 | The runner crashed before the verdict-writer ran for many properties. Look at `extract.tier_failed` events for the late-crash root cause. |

### 3.3 If they disagree — per-shard breakdown

```python
import json
from pathlib import Path
RUN = Path('c:/tmp/run-2026-05-11')
for shard in sorted(RUN.iterdir(), key=lambda p: int(p.name.split('_')[1]) if p.name.startswith('shard_') and p.name.split('_')[1].isdigit() else -1):
    if not shard.is_dir() or not shard.name.startswith('shard_'): continue
    rep = shard / 'report.json'
    ev = shard / 'events.jsonl'
    if not (rep.exists() and ev.exists()): continue
    r = json.loads(rep.read_text(encoding='utf-8'))
    t = r.get('totals', {})
    rj_s = int(t.get('succeeded') or 0)
    rj_f = int(t.get('failed') or 0)
    ev_s = ev_f = 0
    for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get('kind') == 'output.property_emitted':
            v = e.get('verdict') or ''
            if v == 'SUCCESS': ev_s += 1
            elif v.startswith('FAILED'): ev_f += 1
    ok = 'OK' if (rj_s == ev_s and rj_f == ev_f) else 'MISMATCH'
    print(f'{shard.name:10s}  rj_succ={rj_s:3d} rj_fail={rj_f:3d}  ev_succ={ev_s:3d} ev_fail={ev_f:3d}  {ok}')
```

A uniform MISMATCH across every shard is the signature of a single-point reporting bug (Bug A's 2026-05-11 shape). A few-shards-mismatched is more likely a shard-crash or per-shard timing issue.

### 3.4 If they disagree — sample properties.json directly

When the verdict count is broken, look at what `properties.json` actually contains:

```bash
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://storage.googleapis.com/storage/v1/b/jugnu-raw-production/o/runs%2F2026-05-11%2Fshard_0%2Fproperties.json?alt=media" \
  > /tmp/shard_0_properties.json

python -c "
import json, collections
d = json.load(open('/tmp/shard_0_properties.json', encoding='utf-8'))
verdicts = collections.Counter()
for p in d:
    meta = p.get('_meta', {}) or {}
    verdicts[meta.get('verdict')] += 1
print(verdicts)
"
```

If every property has `_meta.verdict == None`, the formatter dropped the verdict between writer and on-disk JSON. Suspect file: `ma_poc/scripts/runners/jugnu.py:_format_v1` / `_format_v2`.

---

## Phase 4 — Cluster failures by pattern

### 4.1 Start with the analyzer's pattern distribution

`summary.md` has a "Failure pattern distribution" table:

| Pattern | What it means |
|---|---|
| **P2** | Cloudflare / captcha on Entrata-style sites — `fetch_error_signature == CF_CHALLENGE` or captcha detected, terminal tier was the platform adapter or fetch never produced body |
| **P3** | Generic `TIER_1_API` failure with no platform-specific adapter — the largest bucket on most runs. Drill down by domain. |
| **P4** | Entrata adapter failure with no CF — real adapter bug, not a fetch problem |
| **P6** | Platform-specific adapter zero — AppFolio / OneSite / AMLI / Squarespace / Wix terminal tier reached and produced no units |
| **P7** | Pure unreachable — fetch never produced usable HTML, not in P2 |
| **P8** | `LLM_GATE_NO_BODY` — LLM gate refused to send body (body empty or too small) |

Pick the **biggest** bucket. Open `failures.csv`, filter to that pattern_id, look at the top 25 domains. **Domain concentration is the strongest signal.** If 11 of 12 failures are on one management-company template, that's a single bug. If failures are spread across 1500 unique domains, it's a cross-cutting issue.

### 4.2 Cluster by event-trace shape (this is what found Bug B)

Pattern-id tells you the terminal tier; trace-shape tells you **how the property got there**. Two properties can both end in `TIER_1_API` for very different reasons.

```python
import json, collections
from pathlib import Path

RUN = Path('c:/tmp/run-2026-05-11')
shape_counts = collections.Counter()

for shard in RUN.glob('shard_*'):
    ev = shard / 'events.jsonl'
    if not ev.exists(): continue
    traces = collections.defaultdict(list)
    for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        pid = str(e.get('property_id') or '')
        if pid: traces[pid].append(e.get('kind'))

    for pid, kinds in traces.items():
        # Choose the discriminating events for the question you're asking.
        # For Bug B (link-hop suppression), these were the right knobs:
        shape = (
            'extract.llm_rescue_attempted' in kinds,
            'extract.llm_rescue_failed' in kinds,
            'extract.link_hop_started' in kinds,
            next((e for e in kinds if e == 'output.property_emitted'), '') == 'output.property_emitted',
        )
        shape_counts[shape] += 1

print(f'{"rescue":>8} {"resc_fail":>10} {"link_hop":>10} {"emit":>6}  count')
for shape, n in shape_counts.most_common(15):
    print(f'{shape[0]!s:>8} {shape[1]!s:>10} {shape[2]!s:>10} {shape[3]!s:>6}  {n}')
```

For the 2026-05-11 data this printed:

```
   True       True      False    True  1633   ← Bug B's footprint
   True       True       True    True  1430   ← Bug B's "saved by link-hop" cohort
   ...
```

The top bucket size is your headline. 1633 properties sharing one trace shape is one bug, not 1633 individual problems.

### 4.3 Cluster `extract.llm_rescue_failed` by error string

```python
import json, collections
from pathlib import Path
err = collections.Counter()
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get('kind') == 'extract.llm_rescue_failed':
            err[' | '.join((e.get('errors') or [])[:3])] += 1
for s, n in err.most_common(10): print(f'  ({n:4d}) {s!r}')
```

For 2026-05-11:

```
  (1927) ''
  (1644) 'no candidates after filtering'   ← Bug B
  ( 408) 'unsupported adapter: onesite'    ← Bug D
  (  19) 'unsupported adapter: amli'       ← Bug D
```

Big duplicates in error strings = a single code path firing many times = the suspect lives where that string is emitted (Phase 6).

### 4.4 Cross-tab detector signals against adapter selection (this is what found Bug C)

When a fingerprint matches but PMS detection returns "unknown", you've found a downgrade that's dropping signal.

```python
import json, collections
from pathlib import Path
mismatch = collections.Counter()
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    per_pid = collections.defaultdict(dict)
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        pid = str(e.get('property_id') or '')
        if not pid: continue
        if e.get('kind') == 'extract.detector_signals':
            per_pid[pid]['fp'] = e.get('fingerprints_matched') or []
        elif e.get('kind') == 'extract.pms_detected':
            per_pid[pid]['pms'] = e.get('pms')
        elif e.get('kind') == 'output.property_emitted':
            per_pid[pid]['verdict'] = e.get('verdict')
    for pid, d in per_pid.items():
        if d.get('fp') and d.get('pms') == 'unknown' and d.get('verdict','').startswith('FAILED'):
            mismatch[tuple(d['fp'])] += 1
for fps, n in mismatch.most_common(15):
    print(f'  ({n:4d}) {list(fps)}')
```

For 2026-05-11 this printed 1055 FAILED properties with a fingerprint match but PMS=unknown — 558 of them RentCafe-only-matched. That's how Bug C surfaced.

---

## Phase 5 — Trace divergence analysis

You now have one big failure bucket. Pick a representative property from it (the analyzer's `comparison_with_<prev>.md` lists 50 sample regressions — pick the first one). Pull its full event trace for **today and yesterday** and diff the kinds-sequences.

```python
import json
from pathlib import Path

target = '10141'  # one of the regressions sampled from comparison_with_<prev>.md
for day in ('2026-05-10', '2026-05-11'):
    print(f'\n=== {target} on {day} ===')
    found = False
    for shard in Path(f'c:/tmp/run-{day}').glob('shard_*'):
        ev = shard / 'events.jsonl'
        if not ev.exists(): continue
        events = []
        for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
            try: e = json.loads(line)
            except: continue
            if str(e.get('property_id') or '') == target:
                events.append(e)
        if events:
            found = True
            kinds_in_order = []
            for e in events:
                k = e.get('kind')
                if k not in kinds_in_order: kinds_in_order.append(k)
            verdict = next((e.get('verdict') for e in events if e.get('kind') == 'output.property_emitted'), None)
            print(f'  shard={shard.name}, {len(events)} events, verdict={verdict}')
            for k in kinds_in_order: print(f'    {k}')
            break
    if not found: print('  NOT FOUND')
```

**The divergence point in the kind-sequence names the suspect code path.** On 2026-05-11 for property 10141:

```
May 10                                May 11
fetch.started                         fetch.started
fetch.completed                       fetch.completed
extract.detector_signals              extract.detector_signals
extract.html_characterized            extract.html_characterized
extract.tier_attempted                extract.tier_attempted
extract.llm_rescue_attempted          extract.llm_rescue_attempted
extract.llm_rescue_failed             extract.llm_rescue_failed
extract.pms_detected                  extract.pms_detected
extract.adapter_selected              extract.adapter_selected
extract.link_hop_started      ←━━━━━━ MISSING TODAY
extract.link_hop_fetched              extract.amenities_observed
planner.link_hop_budget_refresh       extract.tier_failed
extract.link_hop_recovered            output.property_emitted (FAILED_NO_DATA)
extract.tier_won
output.property_emitted (SUCCESS)
```

Yesterday had `extract.link_hop_started`. Today did not. That's the locus: somewhere between `extract.adapter_selected` and `extract.amenities_observed`, the link-hop path was suppressed.

### 5.1 When trace shapes look identical but outcomes differ

Sometimes today and yesterday have the **same** kinds-sequence but different outcomes. Then drill into event payloads, not just kinds:

```python
import json
from pathlib import Path
target = '10141'
for day in ('2026-05-10', '2026-05-11'):
    for shard in Path(f'c:/tmp/run-{day}').glob('shard_*'):
        ev = shard / 'events.jsonl'
        if not ev.exists(): continue
        for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
            try: e = json.loads(line)
            except: continue
            if str(e.get('property_id') or '') == target and e.get('kind') == 'extract.link_hop_started':
                print(f'{day} candidates:')
                for c in (e.get('candidates') or [])[:5]:
                    print(f'  {c}')
                break
```

For 2026-05-10 this printed:

```
{'url': 'https://www.wymberlycrossing.com/floorplans', 'score': 10001, 'anchor': 'profile:winning_page_url'}
{'url': 'https://www.wymberlycrossing.com/floorplans', 'score': 10000, 'anchor': 'profile:availability_link'}
{'url': 'https://www.wymberlycrossing.com/scheduletour', 'score': 60,    'anchor': 'schedule a tour'}
```

…and the absence of those `profile:*` candidates today told us the profile was empty (data starvation downstream of Bug 1's prior crashes) — see Phase 8 below for the Bug B retrospective.

---

## Phase 6 — Suspect-narrowing

You have:
- a divergence point in the kind-sequence (which event went missing or changed)
- ideally a unique string from event payloads or error messages

Now find the code.

### 6.1 Grep for the unique string

```bash
grep -rn "no candidates after filtering" ma_poc/
# → ma_poc/services/llm_api_rescue.py:670
```

Read the surrounding 50 lines. Identify the function that emits the string and what gates lead to it.

### 6.2 Git-log the suspect file (and its callers)

```bash
# Did the file itself change in the last 48h?
git log --since=2026-05-09 --until=2026-05-11 --oneline -- ma_poc/services/llm_api_rescue.py

# If NOT — the BEHAVIOR changed but the file didn't. Check callers.
grep -rn "from ma_poc.services.llm_api_rescue" ma_poc/

# Did any caller change?
git log --since=2026-05-09 --until=2026-05-11 --oneline -- ma_poc/pms/scraper.py
git log --since=2026-05-09 --until=2026-05-11 --oneline -- ma_poc/scripts/runners/jugnu.py
```

### 6.3 List all of yesterday's commits

When you're not sure which file changed, run a broad commit log scoped to yesterday:

```bash
git log --since=2026-05-09 --until=2026-05-11 --pretty=format:"%h %ai %an %s" --name-only
```

Read each commit message + file list. The "Fixing the ever alluding llm feedback loop" commit on May 10 was a 5184-line monster touching 40 files — that's the kind of diff that hides ordering bugs like Bug A.

### 6.4 When the file IS unchanged but behavior changed

This is the trickiest case. Two situations to consider:

1. **The caller passes different input.** Run the analyzer's day-over-day comparison and look at the event payloads going INTO the unchanged function. Bug B's `_try_link_hop` was unchanged, but its `profile.navigation.winning_page_url` input was empty today because Bug 1 had been silently zeroing it for days.
2. **A dependency changed.** Look at imports inside the unchanged file; git-log those.
3. **An environment variable or feature flag changed.** Check `config/feature_flags.py` and the Cloud Run service config.

### 6.5 Read the SHIP-state, not just the diff

A commit message can lie or omit. After identifying a suspect commit, read the file as it stands NOW. Note every function that was touched and trace the data flow through them in your head. Bug A's hoist looked benign in the diff (just moved 20 lines) — the bug was in the implicit ordering relationship with code 70 lines later.

### 6.6 When the suspect is in the self-learning loop (persistence channels)

If the symptom is about profile-learned state — `winning_page_url` empty, replay-cache zero hits, `MAPPING_SAVE_DROPPED` increasing — the issue is in one of the five learning channels documented in the auto-memory `project_self_learning_loop_arch.md` entry. The diagnostic SQL lives at:

```bash
ma_poc/scripts/diagnostics/profile_persistence_health.sql
```

Run it via:

```bash
"C:/Users/ashus/bin/cloud-sql-proxy.exe" --port 5433 --auto-iam-authn \
  jugnu-494013:us-central1:jugnu-db-production &

DATABASE_URL='postgresql+pg8000://ashu%40surgexdigital.com@127.0.0.1:5433/jugnu' \
  python ma_poc/scripts/diagnostics/db_query.py \
    ma_poc/scripts/diagnostics/profile_persistence_health.sql \
    --query Q4_channel_row_counts
```

`Q4_channel_row_counts` shows the row counts for each persistence channel (mappings / patches / blocked-endpoints / dom-hints / known-endpoints). When two channels with the same writer differ by >100× in DB row counts, the writer is broken for one channel — see the diagnostic playbook in auto-memory `feedback_diagnostic_playbook.md`. The analyzer also runs this query if invoked with `--check-db`.

---

## Phase 7 — Quantify the hypothesis

Before declaring "bug X is the cause", count how many production properties match the hypothesised failure mode. If the count is much smaller than the failure population, your hypothesis is incomplete.

```python
# Hypothesis: "rescue filter returned 'no candidates after filtering' and link-hop didn't fire"
# How many of today's failures match?
import json, collections
from pathlib import Path
match = 0
total_failed = 0
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    per_pid = collections.defaultdict(set)
    pid_verdict = {}
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        pid = str(e.get('property_id') or '')
        if not pid: continue
        per_pid[pid].add(e.get('kind'))
        if e.get('kind') == 'output.property_emitted':
            pid_verdict[pid] = e.get('verdict')
    for pid, kinds in per_pid.items():
        v = pid_verdict.get(pid, '')
        if v == 'FAILED_NO_DATA':
            total_failed += 1
            if ('extract.llm_rescue_failed' in kinds
                and 'extract.link_hop_started' not in kinds):
                match += 1
print(f'hypothesis matches {match} of {total_failed} FAILED_NO_DATA properties '
      f'({100*match/total_failed:.1f} %)')
```

On 2026-05-11 this printed `1633 of 1877 (87.0 %)`. Hypothesis confirmed: this is the dominant failure mode.

If your hypothesis only explains 15 % of failures, **the hypothesis is wrong or partial**. Go back to Phase 4 and find the bigger bucket.

---

## Phase 8 — Cross-cutting code patterns to look for

These are the four bug archetypes that came out of the 2026-05-11 analysis. When reading suspect code, use them as a checklist.

### P1 — dict captured by `.get(key, {})` then mutated separately (Bug A)

**Look for:**

```python
meta = result.get("_meta", {})   # captures a fresh dict if absent
# ... later in same function or another function ...
result["_meta"]["foo"] = bar     # writes to a different dict object
```

**Fix:** `result.setdefault(key, {})` returns and stores the same dict object.

### P2 — duplicated literal across two files that must agree (Bug D)

**Look for:**

```python
# file A
if x in {"a", "b", "c"}: ...

# file B
KNOWN_SET = frozenset({"a", "b", "c"})
```

Same literal in two places, neither imports from the other. Drift is inevitable.

**Fix:** one file owns the constant; the other imports it. Add an AST-walk invariant test under `tests/integration/contracts/`.

### P3 — binary classification on absence-of-positive-evidence (Bug C)

**Look for:**

```python
for x in candidates:
    if matches_current_classification(x):
        return keep()
# none matched → demote
return reclassify_as_unknown()
```

Absence of confirmation is being treated as disconfirmation. Wrong rule.

**Fix:** require positive evidence of an alternative classification before demoting. Iterate other classifiers; preserve if no positive cross-match.

### P4 — candidate gathering returns None when one source is empty (Bug B)

**Look for:**

```python
ranked = primary_signal()
if not ranked:
    return None     # gives up after one source
```

**Fix:** layered fallback — secondary signal, tertiary signal, template prior. Each rung is additive, scored, and tried in order. Never give up on one source.

---

## Bug-by-bug retrospective — what helped find each one

### Bug A — `_meta.verdict` lost

| Step | What did it |
|---|---|
| Symptom in events | `report.json` totals.succeeded didn't match events.jsonl SUCCESS verdicts across **every** shard |
| Discovery technique | Phase 3 reconciliation: per-shard `rj_succ vs ev_succ` table |
| Killer evidence | Pulled `shard_0/properties.json` directly via REST; found `_meta = {}` for all 100 properties |
| Suspect file | `ma_poc/reporting/run_report.py:117` — reads `meta.get("verdict") or ""` |
| Root cause file | `ma_poc/scripts/runners/jugnu.py:_format_v1` / `_format_v2` — hoisted before `_meta` initialised |
| Why it survived review | Commit 3013362 was 5184 lines across 40 files; the dict-ordering bug was implicit |
| Test gap | E2E test at `tests/integration/e2e/test_e2e_5_property_smoke_filesystem.py` constructed `_meta` by hand instead of going through `_process_property` |

**Tip:** Always cross-verify the headline metric. Trust events.jsonl over report.json.

### Bug B — link-hop suppressed by data starvation

| Step | What did it |
|---|---|
| Symptom in events | 884 day-over-day regressions, sample showed yesterday `TIER_3_DOM` → today `TIER_1_API` |
| Discovery technique | Phase 5 trace divergence for one regression: today's trace missing `extract.link_hop_started` |
| Bucketing confirmed | Phase 4 trace-shape histogram: 1633 properties shared `(rescue=fail, link-hop=missing)` |
| Suspect file | `ma_poc/pms/scraper.py:_try_link_hop` — returns None at line 1336 when `ranked` empty |
| Twist | The suspect file was **unchanged** yesterday. Bug was data starvation — Bug 1's prior multi-day crashes had emptied `profile.navigation.winning_page_url` |
| Killer evidence | Comparing `extract.link_hop_started.candidates` payloads: yesterday had `profile:winning_page_url` entries; today had none |

**Tip:** A regression in behavior doesn't always mean a regression in code. Profile/state starvation is a common cause. Always check whether the inputs to an unchanged function changed.

### Bug C — confirm_detection demoted on noise

| Step | What did it |
|---|---|
| Symptom in events | `extract.detector_signals.fingerprints_matched: ['rentcafe']` but `extract.pms_detected.pms: 'unknown'` |
| Discovery technique | Phase 4 cross-tab — count properties with FP-match-but-unknown-detection |
| Bucketing confirmed | 1055/1877 FAILED_NO_DATA had this exact mismatch; 558 were RentCafe-only-matched |
| Suspect file | `ma_poc/pms/detector.py:confirm_detection` — the only function that can downgrade after a positive fingerprint match |
| Root cause | Binary "no positive match → demote" rule treating noise (analytics, captcha) as disconfirmation |
| Why it survived review | Existing tests covered the eb18889 use case (Windsor case where bodies ARE Funnel-shaped); no test covered the noise-only case |

**Tip:** When the event ledger contradicts itself (FP matched but PMS unknown), there's a downgrade decision dropping signal. Grep for "demote" / "fallback" / "unknown" in the suspect module.

### Bug D — rescue allow-list drift

| Step | What did it |
|---|---|
| Symptom in events | 427 `extract.llm_rescue_failed` events with `errors=['unsupported adapter: onesite']` or `['unsupported adapter: amli']` |
| Discovery technique | Phase 4 error-string histogram surfaced the exact text |
| Suspect file | `grep "unsupported adapter"` → `ma_poc/services/llm_api_rescue.py:653-654` (frozenset gate) |
| Cross-file pair | `grep "onesite" ma_poc/pms/scraper.py` → inline allow-list at line 713 (widened in May 9 commit) but rescue's frozenset wasn't updated |
| Killer evidence | AST-walk: scraper.py inline set had 5 names, rescue's frozenset had 3 |
| Why it survived review | Existing test `test_f1_3_rescue_fires_for_onesite_adapter` mocked `rescue_from_api_responses`, so the real `SUPPORTED_ADAPTERS` check never executed |

**Tip:** "X rejected this" errors in events are almost always two-files-out-of-sync. Always check both ends of the contract. Suspect tests that mock the very function whose contract you're verifying.

---

## Anti-patterns and pitfalls

| Pitfall | Cost on 2026-05-11 | How to avoid |
|---|---|---|
| Trusting summary.md headline without cross-verifying against events.jsonl | Would have masked Bug A for another day | Phase 3 reconciliation is mandatory, not optional |
| Reading one property's trace and generalising | Risks wrong hypothesis | Phase 4 bucket first; pick representatives from biggest bucket second |
| Assuming yesterday's commits caused today's regression | Bug B was downstream of multi-day data starvation, not a code change | Check whether the suspect file was actually modified. If not, check whether its INPUTS changed |
| Trusting tests that mock the unit-under-test | Bug D's existing test mocked `rescue_from_api_responses` itself | Suspect any `patch("X.f")` where `f` is the asserted target |
| Pulling all HTML dumps from GCS | 80%+ of bandwidth wasted | Skip `*.html`, `NNN.json`, `cost_ledger.db` in the initial mirror; pull on-demand |
| Wrong `--expected-shards` | Missing-shard warnings get hidden | Always check the actual shard count from GCS first: `curl … &delimiter=/&fields=prefixes` |
| Skipping the AST-walk on contracts | Bug D could recur silently | For any cross-file invariant, add an `ast.walk()` test under `tests/integration/contracts/` |

---

## Reading the manual analysis doc — examples

Two complete past analyses live in `ma_poc/docs/`:

| Doc | What it shows |
|---|---|
| [`run_2026_05_10_manual_analysis.md`](run_2026_05_10_manual_analysis.md) | Single-bug analysis (Bug 1 — `ApiEndpoint.json_paths` Pydantic crash). Compact, single-action conclusion. Use this as the template when one issue dominates. |
| [`run_2026_05_11_manual_analysis.md`](run_2026_05_11_manual_analysis.md) | Multi-bug analysis (Bugs A/B/C/D). Adds: per-shard reconciliation table, trace-divergence example, four cross-cutting principles, an "addendum" section with deep-dives, integration-test-anti-pattern catalogue. Use this as the template when multiple issues compose. |

The pattern in both: **TL;DR first** (no scrolling needed for the headline), **mechanism explained per bug** (events.jsonl quotes are the strongest evidence), **per-bug impact quantified** (X properties / Y % of run), **fix-landing state** with commit references, **explicit follow-ups** for what didn't ship.

---

## Library of one-liners (copy-paste)

### Find all unique `kind` values in events.jsonl

```python
import json, collections
from pathlib import Path
kinds = collections.Counter()
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: kinds[json.loads(line).get('kind')] += 1
        except: pass
for k, n in kinds.most_common(): print(f'  {k:50s} {n:>8d}')
```

### Find properties with a specific event kind

```python
import json
from pathlib import Path
target_kind = 'extract.link_hop_recovered'
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get('kind') == target_kind:
            print(f'{shard.name} pid={e.get("property_id")} url={e.get("url")}')
```

### Top failures by management-company domain

```python
import json, collections
from pathlib import Path
from urllib.parse import urlparse

def dom(u):
    try: h = (urlparse(u).hostname or '').lower()
    except: return ''
    for p in ('www.', 'lp.'):
        if h.startswith(p): h = h[len(p):]
    return h

fail_by_domain = collections.Counter()
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    per_pid = collections.defaultdict(dict)
    for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        pid = str(e.get('property_id') or '')
        if not pid: continue
        if e.get('kind') == 'fetch.started' and 'url' not in per_pid[pid]:
            per_pid[pid]['url'] = e.get('url')
        elif e.get('kind') == 'output.property_emitted':
            per_pid[pid]['verdict'] = e.get('verdict')
    for pid, d in per_pid.items():
        if (d.get('verdict') or '').startswith('FAILED'):
            fail_by_domain[dom(d.get('url'))] += 1
for d, n in fail_by_domain.most_common(25):
    print(f'  {d:45s} {n}')
```

### Day-over-day delta of failures by domain

```python
import json, collections
from pathlib import Path
from urllib.parse import urlparse

def dom(u):
    try: h = (urlparse(u).hostname or '').lower()
    except: return ''
    for p in ('www.', 'lp.'):
        if h.startswith(p): h = h[len(p):]
    return h

def fails_by_domain(run_dir):
    out = collections.Counter()
    for shard in Path(run_dir).glob('shard_*'):
        per_pid = collections.defaultdict(dict)
        for line in (shard/'events.jsonl').read_text(encoding='utf-8', errors='ignore').splitlines():
            try: e = json.loads(line)
            except: continue
            pid = str(e.get('property_id') or '')
            if not pid: continue
            if e.get('kind') == 'fetch.started' and 'url' not in per_pid[pid]:
                per_pid[pid]['url'] = e.get('url')
            elif e.get('kind') == 'output.property_emitted':
                per_pid[pid]['verdict'] = e.get('verdict')
        for pid, d in per_pid.items():
            if (d.get('verdict') or '').startswith('FAILED'):
                out[dom(d.get('url'))] += 1
    return out

today = fails_by_domain('c:/tmp/run-2026-05-11')
yest = fails_by_domain('c:/tmp/run-2026-05-10')
deltas = [(d, today[d], yest[d], today[d] - yest[d])
          for d in set(today) | set(yest)]
deltas.sort(key=lambda r: -r[3])  # biggest INCREASE first
for d, t, y, dl in deltas[:25]:
    if dl > 0:
        print(f'  {d:45s}  today={t:3d}  yest={y:3d}  +{dl}')
```

### Sample one property's full event timeline

```python
import json
from pathlib import Path
target = '10141'
for shard in Path('c:/tmp/run-2026-05-11').glob('shard_*'):
    ev = shard / 'events.jsonl'
    if not ev.exists(): continue
    rows = []
    for line in ev.read_text(encoding='utf-8', errors='ignore').splitlines():
        try: e = json.loads(line)
        except: continue
        if str(e.get('property_id') or '') == target: rows.append(e)
    if rows:
        for e in rows:
            extras = {k: v for k, v in e.items()
                      if k not in ('event_id','property_id','run_id','task_id','ts','kind')
                      and v is not None}
            print(f'{e.get("ts","")[:26]:26s}  {e.get("kind","?"):40s}  {extras}')
        break
```

### Pull one property's HTML on-demand (REST)

```bash
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://storage.googleapis.com/storage/v1/b/jugnu-raw-production/o/runs%2F2026-05-11%2Fshard_0%2F10141.html?alt=media" \
  > /tmp/10141.html
```

### List which commits touched any file in a list, scoped to a date window

```bash
git log --since=2026-05-09 --until=2026-05-11 --pretty=format:"%h %ai %s" -- \
  ma_poc/pms/scraper.py \
  ma_poc/services/llm_api_rescue.py \
  ma_poc/scripts/runners/jugnu.py
```

### AST-walk for invariant check between two files

```python
import ast, inspect

def find_inline_set_against(name, module):
    """Find all 'name in {...}' set literals in the module's source.
    Returns the literal contents as sets of strings."""
    src = inspect.getsource(module)
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name) and node.left.id == name
            and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
            and isinstance(node.comparators[0], ast.Set)):
            vals = {elt.value for elt in node.comparators[0].elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
            if vals: found.append(vals)
    return found

# Example: compare scraper's inline allow-list to rescue's frozenset
from ma_poc.pms import scraper
from ma_poc.services.llm_api_rescue import SUPPORTED_ADAPTERS
inline = find_inline_set_against("adapter_name", scraper)
print(f'inline: {inline}')
print(f'constant: {sorted(SUPPORTED_ADAPTERS)}')
```

---

## Writing the manual analysis doc

When the investigation is done, write the result as `ma_poc/docs/run_<date>_manual_analysis.md`. Structure:

1. **TL;DR table** — one row per signal, columns "Value | What it tells us". Reader should not need to scroll.
2. **Per-bug section**, in priority order:
   - Mechanism (events.jsonl trace quote)
   - Per-shard / per-domain distribution (the killer count: "1633 of 1877 = 87 %")
   - Suspect file + line + commit
   - Fix-landing state — has it been committed? Will the next run heal?
   - Expected impact next run (table of "metric now vs after fix")
3. **Day-over-day comparison** (numbers from `comparison_with_<prev>.md`, augmented with anything the analyzer missed)
4. **Side-finds** — bugs in the analyzer itself, doc inconsistencies, env issues. Don't ship them silently.
5. **What to verify after the next cloud run executes** — the exact commands to re-confirm the fix worked. Operators run these.
6. **Cross-references** — links to `data/reports/cloud_run_<date>/summary.md`, the design doc, the manual analysis docs from adjacent days.

When multiple bugs interact (the May-11 case), also write the design doc as `ma_poc/docs/<date>_regressions_fix_design.md` and grow it incrementally per bug. The 2026-05-11 doc has the template — start with **Status / Final tally / Principles** at the top, then per-bug **Symptom → Root cause → Options → Selected design → Tests → Close-out**.

---

## When to stop investigating

### If the run was healthy

If the pre-flight check (`Before you start → Quick health check`) showed ≥ 95 % true success rate AND no day-over-day regression cluster > 50 properties, stop **immediately** and write a 5-line note: today's date, true success rate (events-derived), tier distribution headline, any persistence-health SLO alerts from `summary.md`, name of the previous-day comparison file. Don't generate a manual analysis doc — there's nothing to document.

### If you found one or more bugs

You are done when you can answer all of these:

1. What is the **true** success rate (events-derived, not report.json)?
2. What's the biggest failure bucket (by trace shape), and what % of failures does it explain?
3. For that bucket: which file + line is responsible, and which commit introduced (or exposed) the issue?
4. Is the cause **a code change** (which commit), **input/state drift** (which earlier run started it), or **a pre-existing latent issue** (why now)?
5. Quantified expected improvement: "next run, metric X drops from Y to Z because…"
6. The exact post-run verification commands the operator runs next day to confirm.

If you can answer 1–6, write the manual analysis doc and the design doc. If you can answer 1–4 but not 5–6, your hypothesis is partial — go back to Phase 7 and quantify harder, or find the second bug interacting with the first (the May-11 case: Bugs A, B, C, D were four distinct issues found by repeating Phases 4–7 four times).

### How to pick the representative property for Phase 5

The runbook's examples use `target = '10141'`. That's not magic — it was the FIRST entry in `comparison_with_2026-05-10.md`'s "Regressions" sample table. When investigating a different day:

1. Open `data/reports/cloud_run_<date>/comparison_with_<prev>.md`.
2. Find the "## Regressions (sample, up to 50 of N)" table.
3. The first row whose **yesterday-terminal-tier** differs from **today-terminal-tier** is a good representative — the tier-change signals the divergence you'll be tracing.
4. If your bucket isn't "regression" (e.g., you're investigating a new failure type with no day-over-day signal), pick a property from the dominant trace-shape bucket (Phase 4.2) by grepping `failures.csv` for the matching `pattern_id` and taking any row.

The property ID is just an entry point — you'll generalise from one trace to the bucket in Phase 4. Don't get attached to a specific ID. If the first one you pick has weird signals that don't match the bucket's pattern, pick the second one.
