# Handoff to Codex — main moved, 2026-07-27

**Read this before merging anything.** `main` advanced from `8b6ee48` to
`68ca9dd` while your work sat uncommitted. Two of the five merged PRs touch
files you have open, and one of them **supersedes a change you wrote**.

Written by the Claude session that landed those PRs. Everything below was
verified against the repo, not inferred.

---

## 1. What landed on `main` (`68ca9dd`)

| PR | change | touches your files? |
|---|---|---|
| [#105](https://github.com/ashuchan/PropAi/pull/105) | ResMan tests rewritten against the surviving adapter | no |
| [#106](https://github.com/ashuchan/PropAi/pull/106) | fixture paths anchored on `__file__` + CWD guard | no |
| [#107](https://github.com/ashuchan/PropAi/pull/107) | **`asyncio.to_thread` on the crawl-GET gate** | `pms/scraper.py` |
| [#108](https://github.com/ashuchan/PropAi/pull/108) | **network-egress guard** + 28 test files | `tests/pms/adapters/test_rentcafe.py`, `test_entrata.py` |
| [#109](https://github.com/ashuchan/PropAi/pull/109) | **`RETRY_EPISODE` closed retry funnel** | `pms/scraper.py`, `observability/events.py`, `tests/pms/test_path_b_retry_telemetry.py` |

Full suite on `68ca9dd`: **7 failed, 7192 passed** (was 13 failed). The 7 are
pre-existing: `test_scripts_layout` ×3, `test_output_provenance`,
`test_phase_a_correctness`, `test_h5_visited_urls_dedupe`,
`test_shard_safeguards`.

---

## 2. Your retry telemetry is superseded — please drop it, don't merge it

You added `RETRY_NO_CANDIDATE = "extract.retry_no_candidate"` and an emit at the
`if not _next_candidates:` break. We independently built the same fix, wider,
and it merged as #109.

**#109 covers all six exits, not one.** A single `RETRY_EPISODE` event is
emitted from a `finally`, carrying a 12-value `outcome`:

```
not_triggered · no_budget · no_candidate · telemetry_only · won
lost_candidates_exhausted · lost_adapter_error · lost_dead_end
lost_max_retries · aborted_error · aborted_cancelled · setup_error
```

Your `retry_no_candidate` maps exactly onto `outcome="no_candidate"` (first
iteration) and `outcome="lost_candidates_exhausted"` (later iterations) — a
distinction your version could not make, because `attempt` alone doesn't
separate "pool was empty from the start" from "pool emptied after dispatches".

**One fact that likely changes your analysis: an episode is not a property.**
`scrape()` recurses per link-hop sub-page reusing the same `property_id`
(`pms/scraper.py:3866`). One property hit the retry block **13 times** in the
2026-07-16 ledger, and **43% reach it more than once**. Any per-property
aggregation of retry events over-counts by ~3.7×. #109 adds an `episode_id`
join key to the three pre-existing retry events for exactly this reason.

### Do this

```bash
git checkout main -- ma_poc/observability/events.py ma_poc/pms/scraper.py ma_poc/tests/pms/test_path_b_retry_telemetry.py
```

Then re-apply **only** your `scraper.py` changes that are not retry telemetry,
if any. Resolving this file by hand is the main risk in this handoff: your +19
lines land precisely on the break that #109 restructured, and #107 changed the
same file at line ~3343. A careless merge yields either two overlapping
telemetry systems or a silent loss of #109's coverage.

To read the funnel: `python -m ma_poc.scripts.reports.retry_funnel <run-dir>`,
or the at-a-glance split now in `scripts/diagnostics/analyze_cloud_run.py`.

---

## 3. Your floorplanId find is the most valuable thing in this branch

```diff
-            or item_lc.get("floorplanid")
+        floorplan_id = str(item_lc.get("floorplanid") or "").strip()
+        source_ids = {"rentcafe_floorplan_id": floorplan_id} if floorplan_id else {}
```

Confirmed on `main`: `pms/adapters/rentcafe.py` still says in its own module
docstring, *"Unit ID field: floorplanId (floorplan-level, not unit-level)"*. It
documented the defect and nobody read it as one.

**Why this is bigger than one adapter.** `core.identity.unit_has_real_anchor` —
the predicate the identity, verdict, retry and universal-recovery layers all use
to decide plan-vs-unit — returns **True** for a floorplanId. It is non-empty and
does not start with `inferred_`, which is all that predicate checks. So every
property with this shape was:

* **counted as unit-level gold** when it is plan-level, and
* **skipped by recovery**, because it looked like it already had real units

Both directions are wrong at once: gold is over-stated and the recoverable
population is under-stated. This is the plan-level defect one layer deeper — not
"plan rows pass the gate" but "plan rows wearing a unit's identity".

**Please land this fix on its own, ahead of the rest.** It is small, it is
correct, and every cohort count computed before it is suspect. When it lands,
the 1,127 and 602 plan-level cohorts need re-counting.

**Then sweep the other adapters for the same shape** — any identity-bearing
field fed from a plan-level key. If RentCafe did it, others plausibly do, and
none of them are visible to `unit_has_real_anchor`.

---

## 4. Your strict definition is better than the shipped one — consider promoting it

`scripts/diagnostics/browser_endpoint_discovery.py:strict_listing_rows` requires
a real unit id **and a numeric rent in the same row**. `unit_has_real_anchor`
checks identity only, so it admits a real-but-unpriced unit emitted beside a
priced plan aggregate.

Yours is the correct definition of *gold*. `unit_has_real_anchor` remains the
right gate for *"should recovery keep going"* — those are different questions
and it is fine for them to have different predicates, but the gold one should be
yours. Worth lifting out of `scripts/diagnostics/` into `core/identity.py` so the
verdict layer can use it.

---

## 5. Test-suite traps that will waste your time

This suite reports wrong numbers in **three independent ways**. Two are fixed as
of `68ca9dd`; one is not.

| trap | status | what you must do |
|---|---|---|
| a collect error aborts the whole run and reports **zero** results, exiting clean | fixed (#105) | always pass `--continue-on-collection-errors` anyway |
| CWD-relative fixture paths: **82** failures from inside `ma_poc/` vs **5** from repo root, same tree | fixed (#106) | run from the **repo root**: `python -m pytest ma_poc/tests/...` |
| order-dependent tests | **not fixed** | compare failure **SETS**, never counts |

### And the one that will bite your discovery work specifically

**A stub that no longer stubs fails OPEN.** When production moves to a new seam,
an old stub silently stops intercepting — the call still *succeeds*, against the
real internet, so the test passes until the world changes and then reads as
flake. A full suite was making **~356 live requests to 20 real hostnames**.

#108 now blocks this and raises `UnstubbedNetworkCall` naming the test and URL.
Production fetches through `ma_poc.pms.adapters._probe.probe_get` — a sync
curl_cffi call, so stubbing `httpx` / `get_adapter` / `resolve_target` /
`detect_pms` does **not** stop it. Stub that seam, or mark the test:

* `@pytest.mark.probe_seam` — you drive `probe_get` deliberately and mock
  curl_cffi underneath
* `@pytest.mark.live_network` — you genuinely want the internet

Two live examples: `test_sylvan_tributary_sightmap` spent months asserting real
`sightmap.com` inventory (its "85 units, expected 125" was true availability on
2026-07-26); and the #109 telemetry tests were reaching `probe_get` via
`_enrich_probe` and the hop gate — runtime dropped **20.88s → 0.63s** once
stubbed. **If a test takes seconds where it should take milliseconds, it is
doing I/O you did not intend.**

Relevant to you directly: any shell with `PROBE_PROXY_URL` set was burning
BrightData residential bandwidth on every test call.

---

## 6. Merge protocol — separately-green branches can still break main

#108 and #109 were each green in isolation and **broke when merged together**:
the network guard failed 19 of #109's tests because those tests were doing real
network I/O. `git merge-tree` reported **zero conflicts** — the collision was
semantic, not textual.

Before landing your branch:

```bash
git worktree add --detach /tmp/combined main
cd /tmp/combined && git merge --no-edit <your-branch>
python -m pytest ma_poc/tests/ -p no:randomly -q --continue-on-collection-errors
```

Compare the failure **set** against `68ca9dd`'s seven. Zero-conflict is not
sufficient evidence.

---

## 7. Open questions

1. **Cost model for the 602-property browser sweep.** Browser context plus
   residential session per property is the expensive path. Worth an estimate
   before the run, not after — there is prior history of proxy spend being
   discovered retrospectively.
2. **#107's effect is unmeasured.** It off-loads a 10s blocking `probe_get` off
   the event loop in `_try_link_hop`; `920d050` fixed the same defect in
   `fetcher.py`/`rentcafe.py` and missed this call site. Whether it recovers
   timeouts needs a canary. It is also a second candidate explanation for the
   72.6% postfix canary previously attributed to the throttle — worth keeping in
   mind if your run's timing looks different from expectations.
3. **Does `strict_listing_rows` belong in `core/identity.py`?** See §4.

---

## 8. Uncommitted, not ours to touch

Still loose in `.claude/worktrees/angry-murdock-c19e06`: the ProspectPortal /
endpoint-discovery modules (`_prospectportal_warm_replay.py`,
`unit_endpoint_discovery.py`, `cohort_endpoint_route_plan.py`,
`browser_endpoint_discovery.py`, `services/endpoint_discovery_profiles.py`, plus
tests) and two handover docs. ~2,700 lines with no commit behind them, in a tree
several sessions have been writing to. Please commit them somewhere recoverable.
