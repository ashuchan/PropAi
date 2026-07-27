# Handover — plan-level → unit-level recovery (2026-07-26)

Written for someone picking this up cold. Everything below is either **measured**
(and says so, with the run it came from) or explicitly flagged as **unproven**.
Two claims made during this work were later retracted; both are recorded so the
same ground is not re-covered.

**Shipped:** [PR #104](https://github.com/ashuchan/PropAi/pull/104), merged to
`main` as `8b6ee48`. 39 commits from
`chip/securecafe-applicant-and-render-on-empty-jul16`; 17 of them are this
session's work (`8776acc..f528b1c`).

---

## 1. The headline result

Canary **`2026-07-26-plancohort`** — 1,127 properties, 40/40 shards, 40
succeeded, 0 failed, 1,127/1,127 returned, ~50 min wall-clock.

Scope was the plan-level cohort, chosen deliberately: **every property in it was
plan-level in the baseline**, so any unit-level result is unambiguous with no
inference and no control group needed.

```
now UNIT-LEVEL (real unit_id + rent)   292   25.9%
still plan-level                       773   68.6%
no units                                62    5.5%
unit rows: 11,662 total / 5,259 real-id-with-rent (45%)
```

### Proven

**The SecureCafe portal fallback works: 117 properties reached
`TIER_1_API_RENTCAFE_SECURECAFE`, and 117 converted — 100%.** That tier had no
code path to fire before this branch. This is the one unambiguous win.

### NOT proven — do not claim these

| claim | status |
|---|---|
| `plan_level_only` retry trigger (`d33cd42`) | **Unmeasured in either direction.** See §4. |
| Entrata fixes | Tier fired for 40 properties, 1 converted (2%). Plan-index discovery landed *after* the run — unmeasured. |
| Vanity `/availableunits` | 10 properties in-pipeline vs a 70% hit rate measured on the lever standalone. Unreconciled. |
| Universal-recovery gate fix (`1eb9a98`) | Landed after the run. The 368 properties it targets are **untested in production**. |

---

## 2. The recurring defect: "has units" ≠ "done"

This was the most productive finding of the session. **A plan-level result HAS
units**, so every "did we get anything?" gate treats it as finished and stops
the recovery that would reach the apartments. Four independent instances:

| layer | the gate | consequence |
|---|---|---|
| `reporting/verdict.py` | `unit_id.startswith("inferred_")` — but `unit_id` is minted *later*, in `_format_v2_unit` | ~518 properties mislabelled `SUCCESS` instead of `SUCCESS_PLAN_LEVEL` |
| `pms/scraper.py` retry | plan rows pass quality/rent/area, so the trigger returned `None` | multi-candidate retry (on by default) never fired for any of the 1,127 |
| `pms/scraper.py` universal recovery | `if not adapter_result.units` | PMS portal hop never ran for plan-level — **368 of the 835 misses** |
| `pms/adapters/entrata.py` | drill gated on `"unit-card" in html` | `.option-row` template skipped *before* parsing |

**`core.identity.unit_has_real_anchor` is now the single definition of
plan-vs-unit**, shared by the identity, verdict, retry and recovery layers. Do
not write a fifth copy.

> **Always pair a gate-open with an acceptance guard.** Letting plan-level input
> through means the success path can now *overwrite* real plan rows. A
> plan-level baseline must only accept a genuinely unit-level replacement; an
> empty baseline keeps "anything beats nothing". Without that guard, opening the
> universal-recovery gate would have **destroyed data on 368 properties** —
> worse than the miss it fixed.

---

## 3. The biggest remaining lever (measured, not yet built on)

**368 of the 835 misses (44%) carry a SecureCafe fingerprint on their own page**
and never reached the portal. Given the 117/117 rate they are proven-recoverable.
They died on:

```
144  TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL
 72  TIER_3_DOM_GENERIC
 53  TIER_1_DOM_GENERIC_PLAN_TEXT
 33  TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL
 19  TIER_1_API_RENTCAFE_NO_RESPONSE · 16 no_body_short_circuit · 14 PLAN_TEXT · 7 JSONLD
```

`1eb9a98` should now route them. **Verifying that is the single highest-value
next action.** Expect a large fraction, not all — those 368 did not reach the
route for a reason, and some may be blocked differently.

Full miss breakdown by vendor surface:

```
368 securecafe · 231 none · 51 entrata_pp · 35 realpage_oll · 31 appfolio
 21 rentcafe_availunits · 21 knock · 19 jonah_jd_fp · 18 apts247 · 16 funnel
```

---

## 4. Known instrumentation blind spot — fix before touching retries

`ma_poc/pms/scraper.py`, retry loop:

```python
_next_candidates = detect_pms_candidates(...)
if not _next_candidates:
    break          # ← emits NOTHING
```

A trigger that fires but finds no second candidate is **invisible**. Zero
`plan_level_only` events is therefore equally consistent with "never fired" and
"fired constantly, always dead-ended" — and **37% of the cohort has no second
candidate**, so the silent path is heavily travelled.

**Fix:** emit a `retry_no_candidate` event carrying `trigger_reason` and
`previous_pms` before that `break`. One line. Do this *before* any further retry
work, or the next run is equally unmeasurable.

---

## 5. Pending items, ranked

1. **Verify the 368.** Re-run the plan cohort at `main` (post-`1eb9a98`) and
   check how many now hit `TIER_1_API_RENTCAFE_SECURECAFE`. Cheapest high-value
   measurement available.
2. **Retry telemetry** (§4) — one line, unblocks measuring `d33cd42`.
3. **Jonah Digital adapter — 19 properties, BLOCKED on discovery.** No adapter
   exists; four independent probes converged on the same request. Known:
   - detect via `<meta name="generator" content="Jonah Systems, LLC …">`, or
     `data-jd-fp-selector="…"`, or class `jd-fp-unit-card`
   - `<script type="application/json" id="jd-fp-data-script-app">` holds
     **config only** — `base_uri="/floorplans/"`,
     `renderable_endpoint="_fp-renderable"`, `property_id=None`
   - the obvious guess `{origin}/floorplans/_fp-renderable` is **not** the API —
     it returns the same 235KB page (SPA catch-all)
   - **Next step:** browser-probe one property with `read_network_requests` and
     capture the real XHR. Candidates: livethewatts.com, cornerstonemgm.com,
     arthurcarteret.com, hermannparklofts.com, theunionapts.com.
   - **Trap:** `unit-card` as a bare substring matches `jd-fp-unit-card`. That
     already caused a false positive in the Entrata gate (fixed in `6d579c7`).
     Match on a class boundary.
4. **Entrata plan-index coverage.** `f528b1c` covers 20 of 53 measured (38%) —
   those that expose a `/{city}/{slug}/conventional/` href. The other 33 do not.
5. **Profile seeding at scale.** `ma_poc/scripts/seed_profiles_from_probes.py` is
   ready and dry-run-by-default. ~107 probe records join to a `canonical_id`
   (`canonical_id == apartment_id`), but only **9 have a local profile** — the
   rest live in `gs://jugnu-canary/profiles/`. Must run where the profiles are.
   **Only 53% of agent-reported URLs re-fetch to a roster**, so the verification
   gate is not optional; blind seeding would poison 37 profiles' top hop slot.
6. **OneSite: 0-for-35.** Every `ONESITE` tier in the full 4,982 run was
   `_NO_RESPONSE`. A resident-portal-misroute hypothesis was **tested and
   rejected** (14/14 sampled carry the legitimate *prospect* marker). Before
   investing, check what the earlier "Debug small buckets … ONESITE_NO_RESPONSE"
   work concluded — this may be re-treading it.
7. **Vendor fingerprints.** Investigated and **mostly not warranted** — see §7.

---

## 6. GCP / run reference

```
project           jugnu-494013            region us-central1
job               jugnu-plancohort-e14af1a
execution         jugnu-plancohort-e14af1a-zlthk
image             us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/jugnu:plancohort-e14af1a
                  (sha256:779894ae…; baseline for A/B was jugnu:fixes-8776acc)
RUN_DATE          2026-07-26-plancohort
output            gs://jugnu-canary/runs/2026-07-26-plancohort/   (40 shards, 98 MiB)
property list     gs://jugnu-canary/property-list/plancohort1127.csv
profiles          gs://jugnu-canary/profiles/plancohort-run/      (isolated COPY of hb250-run)
taskCount/parallelism  40 / 40     timeoutSeconds 14400     BROWSERS_PER_TASK 3
COMPLIANCE_MODE   1                (see warning below)
```

### Preflight — do this every time

```
props_per_shard × mean_seconds ÷ pool  <  task_timeout × 0.7
```

At 1,127 props: taskCount 40 → 28/task → **1.61× headroom**. taskCount 25 would
have been **1.01×** — too tight. Note `BROWSERS_PER_TASK=3`, *not* 10; read it
from the spec rather than assuming. A previous run was lost entirely to skipping
this arithmetic.

### ⚠️ Compliance

`COMPLIANCE_MODE=1` forces `web_unlocker_allowed()` → False regardless of
`ENABLE_UNLOCKER_TIER`, per the RealPage legal ruling (Web Unlocker /
FlareSolverr / CAPTCHA-solving are no-fly). It survived this run **only because
the spec was derived from the prior job by changing deltas** rather than being
rebuilt. Zero unlocker events confirmed it held. **Keep following
`reference_canary_per_sha_run`: dump the prior spec, change only the deltas.**
Note `probe_get()` defaults to `unlocker=True`, so this central gate is what
keeps ad-hoc calls compliant.

---

## 7. Things already investigated — don't redo

- **"6 uncovered vendors"** — did not survive measurement against the 1,126
  archived pages. RentVision (33) and Spherexx (17) are **already detected**; the
  proposed new markers matched the identical set. FortressTech: **0 pages**.
  eTenantCare 1, MRI 0, Buildium 3 — and the Buildium instance was a *resident*
  portal on a property confirmed as a true ceiling, so fingerprinting it would
  **create** misroutes. Net: no detector changes warranted.
- **RentVision routing** — 24 of 33 RentVision properties routed to `generic`,
  yet replaying `detect_pms` on their archived HTML returns `rentvision` at 0.85.
  Detection is fine; **detection *timing*** is the gap (routed before the HTML
  was available). `d33cd42` should now catch these — unverified.
- **OneSite resident-portal hypothesis** — tested and rejected (§5.6).

### Two retracted claims

- **"Synthetic ids are a +10.4 gold-point lever."** Wrong. 403 of those 518
  properties are plan-level extractions where a synthetic id is the *correct*
  answer. It was a verdict-labelling bug, not a recoverable-data lever.
- **"90% coverage is not reachable with code."** Wrong, and it talked us out of a
  viable plan. The arithmetic assigned **0%** to 141 properties that were never
  probed; when probed they came back **63% recoverable**.

Both errors came from projecting past what had actually been measured.

---

## 8. Test-suite caveat

`pytest ma_poc/tests` — **12 failed / 7,093 passed** on this branch vs **12
failed / 6,844 passed** on `main`. Net-neutral.

**This suite has order-dependent tests.** Running `pms` before `scripts`
produces a *different* failure set, and `test_proxy_pool_picks_healthiest` flakes
intermittently. **Compare failure SETS across identical invocations — never
counts alone.** Use:

```bash
pytest ma_poc/tests -q -p no:randomly --tb=no --continue-on-collection-errors \
  | grep '^FAILED' | sort > /tmp/after.txt
```

then `comm -13 before.txt after.txt` for genuinely new failures. Two pre-existing
collection errors (`test_resman.py`, `test_merge_anchor_first.py`) come from
stale imports and are unrelated to this work.

---

## 9. Local artifacts

Under the session scratchpad (not in the repo):

```
run25/                  the prior 4,982-property run, 40 shards
canary/final/           this run's 40 shards, named shard_N.json
plan_html/              1,126 archived landing pages (gzipped)
plan_classified.json    per-property vendor fingerprints
final_summary.json      per-property aid / gold / verdict / tier
seed_verified.json      41 verified probe-discovered URLs
```

Probe workflow runs: `wf_f058ce0e-992` (EXTRACTION_MISS, 40 props),
`wf_b9b2191b-65e` (plan-level ceiling, 42), `wf_0a47dbbe-453` (unknown slice, 27).

> When copying shards from GCS, **preserve shard identity** — every file is named
> `properties.json`, so a flat `gsutil cp` silently overwrites them all into one.
> That produced a bogus "19 shards analysed" reading during this session.
