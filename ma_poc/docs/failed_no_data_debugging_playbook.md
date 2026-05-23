# FAILED_NO_DATA Debugging Playbook

**Working directory for every command:** `ma_poc/`
**Audience:** Claude Code (or any engineer) debugging extraction failures after a cloud run.
**Updated:** 2026-05-22. Last campaign reviewed: 2026-05-21 cloud run (4,982 properties, post-merge audit + per-adapter telemetry shipment).
**This update (2026-05-22, later):** new §12b *Cloud canary workflow on `canary-introspect`* — end-to-end recipe for validating a code change against the production-equivalent GCP egress (different ASN, BrightData proxy wired, no residential-IP CF clearance). Covers the 6-step build/spec/execute/pull lifecycle, the `.gcloudignore` + `_SimpleProfileStore.mkdir` Dockerfile gotcha I hit on the 2026-05-22 Plan-Summary fixes canary, the analyze script shape, when to use cloud-vs-local, and the cohort-selection guidance (cloud canary is most valuable when the fix targets CF / cross-origin / proxy-dependent paths — residential IP can't validate those).
**Prior update (2026-05-22):** new §20 *Cross-origin proxy gap + platform-wide adapter telemetry*. Twelve sub-sections covering: §20.1 the discovery (0 SecureCafe wins on 1,885 RentCafe-detected props vs 259 wins on the proxy-enabled canary), §20.2 the proof (probe-experiment from a real Cloud Run egress: 20/20 SC URLs `blocked_403` from AS396982 vs 200 + AvailUnitRow rows from any residential IP), §20.3 the cross-origin clearance asymmetry that explains why Cortland/Irvine/AvalonBay succeed but SecureCafe fails over the same egress, §20.4 the `_adapter_telemetry.py` shared module + the platform-wide `adapter_exit` event at `scraper.py:933+`, §20.5 the per-adapter stages reference (`xhr_capture`, `urn_pick`, `sc_probe`, `prospectportal_probe`, `cascade_exit`, `_diag`), §20.6 SecureCafe new-template regex relaxation (PIDs 72944/24561/6550/40584/67750 lost units silently for weeks), §20.7 G5 deterministic URN picker (Cloudinary CDN anchor — replaces the broken `max(matches, key=len)`), §20.8 OneSite negative gate (`static2.apts247.info` + `doorway.knck.io` competing-CDN demotion), §20.9 Entrata ProspectPortal probe restored from git history (commit 8b1bfa4 was reverted by 4c9dbf8), §20.10 the RentCafe→SightMap misroute hypothesis we DISPROVED by live verification (the co-occurrence rule was a no-op), §20.11 new anti-patterns #18-20 (trust-the-agent / cross-host-clearance / order-of-detector-branches), §20.12 the diagnostic-from-events.jsonl workflow that yesterday's bugs would have taken 5 minutes to find.
**Prior update (2026-05-20):** new §19 RentCafe / AppFolio data-quality gaps with 11 sub-sections — §19.1 avail-date-only-gap sub-cause split (5 sub-causes A-E with fix paths F7a-d), §19.2 RentCafe `fp-container` data-attr extractor (F2 + F3), §19.3 RentCafe Interactive Property Map (F5, best-effort), §19.4 AppFolio SSR `_ADDRESS_RE` regex break (F1), §19.5 SecureCafe portal demote (F8a), §19.6 OneSite DOM `data-availability` augmentation (F7d), §19.7 MAA embedded-JSON price aliases (F6, best-effort), §19.8 telemetry shipped (T1/T2/T3/T4/T5/F8b), §19.9 State Diff fix (F4), §19.10 new anti-pattern #17 (urllib lies on JS-hydrated PMS), §19.11 next-week priorities (10 items, M/should/nice-to-have).
**Prior update (2026-05-21):** new §18 Concession data debugging (full debugging instructions, symptom decoder, Q14-Q17 checklist, 7 fixes implemented, telemetry SQL queries, known-good tradeoffs, file reference, decision tree); §15 file reference appended with 2026-05-21 concession additions block; §16 closing checklist gains item #10 concession-pipeline audit; glossary gains Preserve-and-flag invariant + `_concession_quality` + `concessions_structured` + `stealth_probe` + `HOP_CAPTCHA_DETECTED` + `CONCESSION_PROBE_RESULT`.
**Prior update (2026-05-17):** new §0 anti-patterns 14-16 (verdict-vs-unit-count, cascade-overwrite misdiagnosis, internal-vs-v2-shape); new Q13 in §3 (unit-fidelity check); §5 verdict decoder gains SUCCESS_PARTIAL row + analyzer-label-leak note; new §8.18-§8.22 extraction gaps (plan_summaries dropped at v2 formatter, hop plan_summaries not propagated, AVAILABLE+rent classification, cross-host per-plan URL discovery, wedge-rescue captcha guard); §15 file reference appended with 2026-05-17 additions; glossary gains SUCCESS_PARTIAL + plan_summaries.

---

## TL;DR — the 5-minute first pass

```
1. Pull artifacts        scripts/diagnostics/analyze_cloud_run.py --date YYYY-MM-DD --compare-date <yesterday>
2. Open                  data/reports/cloud_run_<date>/{summary.md, comparison_with_<yesterday>.md, failures.csv}
3. Read the top-N        terminal_tier histogram from summary.md
4. For each top bucket   sample 3 PIDs; run the §3 9-question checklist on ONE before forming a hypothesis
5. Adapter-level signal  Filter events.jsonl by tier_key starting with "<pms>:"
                         The platform-wide ``adapter_exit`` event (scraper.py:933+) fires for
                         every adapter dispatch — diagnose without per-adapter telemetry.
                         See §20.5 for the per-adapter stage reference.
6. Decide               (a) code fix, (b) data fix (CSV stale URL), (c) infra fix (CF/proxy)
```

**If a fix is "supposed to be deployed but the number didn't move":** do not assume it didn't deploy. Investigate whether the fix actually fires by tracing events.jsonl for ONE specific PID. The fix may be a stub against a missing upstream field — see §4 Verification protocols and §10 Architecture invariants.

**If a PMS adapter shows 0 wins on a non-zero detection count** (e.g. 0/1,885 SecureCafe, 0/155 G5): suspect either (a) a silent failure mode with no telemetry — check §20.4 for the adapter-telemetry shape, or (b) `PROBE_PROXY_URL` is not set in the production env but the adapter probes a cross-origin host that's CF-fronted (§20.3 cross-origin clearance asymmetry). The `via_proxy` boolean on every adapter event tells you the env state in one query.

---

## Phase 0 — Anti-patterns I keep falling into

Be explicit with yourself when you catch yourself doing any of these. Most "obvious" first-pass answers below were wrong in the last 3 days of investigation.

| Anti-pattern | What I did wrong | What to do instead |
|---|---|---|
| **Trusting the tier label** | Claimed `TIER_1_API_RENTCAFE_SHAPE_REJECTED` meant an API was captured but malformed. Reality: 0 RentCafe JSON XHRs were ever captured; the label fires whenever ANY response (incl. HTML page itself) is buffered. | Always verify the tier label against the actual `network_log` content. Decode each label per §5 *Tier-label decoder*. |
| **Assuming undeployed when numbers don't move** | "Entrata 306 → 306 means today's fixes didn't deploy." Reality: fixes deployed but a deeper bug (missing `ctx.hop_depth` field) silently nullified them. | Pick ONE PID in the unchanged cluster; trace its events.jsonl for the new tier_attempted reasons the fix should have produced. If those reasons are absent, the fix didn't fire — figure out *why*. |
| **Trusting agent code claims** | Believed an agent's "`_probe_known_endpoints()` uses `ctx.base_url`" without grepping. Reality: code used `page.url`. The proposed fix would have changed nothing. | After any agent code claim, `grep -n "<function>" <file>` and read the actual implementation before forming a fix. |
| **Trusting agent URL claims** | Believed agent's "bowmanstation has `dnn506yrbagrg.cloudfront.net` CDN". Live fetch found `g5-c-` classes and `g5-assets-cld-res.cloudinary.com` — completely different. | When an analysis names URLs/classes/domains, live-fetch and verify before changing code. `python -c "import urllib.request..."` takes 5 seconds. |
| **Skipping body/text ratio** | Said "8181medcenter homepage has no unit data, only contact form dropdowns." Reality: 678 units in a 1.02 MB embedded JSON state blob. | If `body_bytes / text_bytes > 20`, the page is a heavy SSR state. WALK every `<script type="application/json">` block before declaring no data. See §8 *Embedded-JSON walker*. |
| **Generic clustering without sample PIDs** | "AppFolio cluster needs a fix" with no specific evidence. | Always sample 2 IMPROVED + 2 UNCHANGED_FAIL in the same cluster and diff their candidate lists + portal URLs. The difference IS the gap. See §9. |
| **Stopping at the first plausible cause** | For 29washington, blamed `rcLoadContent.ashx` AJAX gap exclusively. Real cause stacked: `rc3_defer` ran on hop pages because `ctx.hop_depth` was always 0. | Don't stop after one plausible explanation. If the page is genuinely reachable and has signals, also check tier-cascade behaviour for hop_depth, gate misfires, label leaks. |
| **Delegating live fetches to agents** | Agent got blocked on Bash perms; I waited and re-launched 3 times. | Run urllib live fetches in your own Bash. 30 seconds vs 3 minutes. |
| **Surface diff reading** | "17 LLM_DOM regressions" with no per-PID divergence point. | Diff events.jsonl for ONE PID between yesterday and today; the divergence appears immediately. |
| **Treating FAILED_NO_DATA as homogeneous** | "676 failures" without splitting by terminal_tier. | Always cluster by `terminal_tier` first, then by `fetch_outcome` to separate ENV_MISMATCH (CF-blocked locally) from real extraction misses. |
| **Declaring STUB without enumerating frames** | Called 3 Wix sites (46179 / 118965 / 292955) "STUB sites" after a Playwright probe that only checked top-level `document.body.innerText`. Reality: each had real unit data in cross-origin iframes (`wix-visual-data.appspot.com/index?pageId=...&compId=...` with the table; `yourcrossstreet.com/property/...` with availability). The static HTML and even top-level rendered text had nothing — but `page.frames` enumeration would have surfaced the data. | Before declaring STUB, enumerate `page.frames` (Playwright) AND grep the rendered HTML for iframe `src=` patterns. See §11 *STUB classifier* and §14 *Frame enumeration snippet*. |
| **Trusting a CSV URL when the redirect host owns the inventory** | For 53592 (1701arch.com) and 119144 (windsorburnet.com), `_rank_internal_links` filtered out the actual unit-page anchors because they pointed at `livethearch.com` / `windsorcommunities.com` (the redirect target), not the CSV host. Same-host filter dropped the only correct links. | When `fetch_result.final_url` host ≠ `base_url` host, ALSO accept anchors on the landed host as "same-site". PMS-priors should be synthesised against the landed host too. See §8.9 *Redirect-aware landed_url*. |
| **Trusting fp_signal count without listing-structure check** | PID 119144 had fp_signals=3 from `priceRange: "$1490 - $3824"` (LocalBusiness JSON-LD aggregate) + marketing copy ("Choose from our 1, 2, 3-bedroom apartments") + amenityFeature description. LLM_DOM ran and correctly returned 0 units — but $0.005 wasted and the page was misclassified as "extractable but extraction failed" rather than STUB_AGGREGATE_COPY. | When fp_signals ≥ 2 AND `has_listing_structure(html) == False` → STUB_AGGREGATE_COPY. The structural check looks for ≥2 `<tr>` rows with rent / ≥2 unit-card class markers / ≥2 Offer-array entries / ≥2 PropertyValue dimension entries. See §8.10 *fp_signal listing-structure gate*. |
| **Trusting verdict alone, not unit-fidelity** (2026-05-17) | Reported "all 4 sentinels UNCHANGED_OK, deploy is safe" based on verdict==SUCCESS unchanged. Reality: 3 of 5 sentinels lost units (65399 8→1, 1375 12→7, 285558 18→0). The asset-hop-filter cases reported as "IMPROVED" lost a third of their units (37156 31→1, 59540 58→1). The verdict-only metric was wrong. | Always count units shipped per PID, not just verdict. Diff cloud vs canary `units` by PID; total delta < 0 means data quality regressed even when verdict went green. New Q13 in §3 codifies this — run BEFORE celebrating any "IMPROVED" cluster. |
| **Diagnosing unit-loss as cascade overwrite when post_process classification is the real culprit** (2026-05-17) | For PID 53592 livethearch, traced `dom_scan ran_units=26` + `llm_dom_targeted ran_units=1` → emit 1 unit. First diagnosed as "cascade overwrites earlier larger result". Wrong — `_merge_into_result_units` at `generic.py:2717` ALREADY merges (26 + 1 → 27 in test harness). The real drop was `post_process.classify()` partitioning 27 rows into `units=0` + `plan_summaries=27`, then the v2 formatter dropping plan_summaries silently (§8.18). | When a unit-loss bug looks like "cascade overwrites", reproduce the post_process pipeline in isolation against the raw extractor output before blaming the cascade. `extraction/post_process.post_process(units, property_id=...)` is pure — give it the LLM/dom_scan output and inspect `r.units` vs `r.plan_summaries`. The split tells you the real drop site. |
| **Reading the internal unit dict expecting v2-formatted fields** (2026-05-17) | Wrote a cross-host per-plan discovery helper that read `u.get("floor_plan_name")` to extract plan names. Test passed in isolation; live canary fired the function but `plan_names_for_match` was always empty. The internal unit dict at link-hop time has `floor_plan_name=""` for RentCafe / SecureCafe extractions — the human-readable name is only materialised LATER by `floorplan_snap` on the v2 output path. | Don't assume the internal in-flight unit dict matches v2 output. Either (a) match by URL SHAPE rather than name (the eventual fix), or (b) use the canonical alias resolver `get_str(u, FP_NAME_KEYS)` AND check the value against `""` (empty string is the common no-name placeholder). Best — log `sorted(unit.keys())[:25]` once when a downstream lookup returns empty so the shape surfaces. |
| **`urllib` lies about JS-hydrated PMS sites** (2026-05-20) | Concluded "no rent on this page" from a `urllib.request.urlopen` of `1105townbrookhaven-apts.com/floorplans` and reported the property as SecureCafe-CF-gated. User pushed back; Playwright render showed 19 `.fp-container` cards with `data-floorplan-price="1660-2199"` plain attrs. The page IS public — our extractor missed the data attrs. | Any "page has no X" claim that drives a fix MUST use Playwright (`networkidle` + scroll), not urllib. RentCafe / G5 / modern PMS sites are JS-hydrated; urllib gets the shell only. AND grep for `data-*` attributes alongside visible text — modern PMS templates push canonical values into data attributes for analytics tracking. See §19.10 + §19.2. |
| **Trusting an agent's proposed fix without live verification** (2026-05-22) | Earlier agent proposed a "RentCafe + SightMap co-occurrence demote" detector rule to fix the 385 RentCafe→SightMap misroute. Would have shipped a no-op. Live-fetched 6 sample misroute PIDs: 6/6 have rentcafe portal marker in entry HTML, **0/6** have any SightMap signal in entry HTML — SightMap is discovered only at link-hop depth ≥2. The proposed rule would have fired on zero of the 385 properties. | After any agent proposes a detector rule, fetch ≥5 sample PIDs with `curl_cffi` chrome120 and grep for the proposed signals **before** writing the code. If 5/5 are missing the signal, the rule won't fire — investigate why before shipping. See §20.10. |
| **Cross-host clearance asymmetry** (2026-05-22) | Assumed Cortland/Irvine/AvalonBay adapters succeeding from GCP meant `probe_get` works for any host from GCP. Reality: those adapters reuse the patchright CF clearance for the property's *own* origin via `_with_clearance` at `_probe.py:73-86`. Cross-origin probes (SecureCafe, ProspectPortal) get no clearance and CF-403 every time from GCP. Production had 0 SecureCafe wins on 1,885 detected props because of this asymmetry. | Any adapter probing a host different from the property's marketing origin needs `PROBE_PROXY_URL` set in production. Mark proxy-dependent adapters in the file docstring. The platform-wide `adapter_exit` event carries a `via_proxy` boolean so this is queryable from events.jsonl in one filter. See §20.3. |
| **Detector branch order — fall-through reaches the wrong adapter** (2026-05-22) | Wrote an OneSite negative gate that SKIPPED the OneSite return when a Knock-doorway-loader was present, expecting the page to fall through to the Knock branch later in `_detect_html_markers`. But the RealPage OLL branch lives BETWEEN OneSite and Knock in source order — the page fell into RealPage OLL and shipped 0 units. Canary caught PID 19245 regressing from `TIER_4_LLM` 4 units → `TIER_1_API_REALPAGE_OLL` 0 units. | When introducing a negative gate that demotes one PMS in favor of another, EXPLICITLY `return` the intended PMS literal from inside the gate. Don't rely on source-order fall-through — the order is fragile and any future detector branch insertion can silently rewire your gate. See §20.8 + §20.11. |

---

## Phase 1 — Pull artifacts

```bash
# Local-only when data is already mirrored at c:/tmp/run-<date>/
python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-14 --compare-date 2026-05-13

# Force-pull from GCS first (requires gcloud auth + cloud-sql-proxy not needed)
python scripts/diagnostics/analyze_cloud_run.py --date 2026-05-14 --pull --expected-shards 100
```

If `gcloud storage rsync` fails with "Reauthentication failed" but ADC is healthy, mirror via the storage REST API instead — sample script at [C:/tmp/mirror_2026_05_14.py](C:/tmp/mirror_2026_05_14.py) (uses `google.cloud.storage` with default credentials, downloads only `report.json + events.jsonl + properties.json + llm_report.json + issues.jsonl` per shard).

Output: `data/reports/cloud_run_<date>/{summary.md, comparison_with_<prev>.md, failures.csv, successes.csv, summary.json}`.

---

## Phase 2 — Cluster failures (read summary.md and failures.csv)

Top of [summary.md](ma_poc/data/reports/) lists terminal-tier counts. The biggest bucket is usually 200-300 failures. Don't try to fix the whole bucket — pick 3-5 representative PIDs.

```python
import csv
from collections import defaultdict
rows = list(csv.DictReader(open('data/reports/cloud_run_2026-05-14/failures.csv', encoding='utf-8')))
buckets = defaultdict(list)
for r in rows:
    if r['verdict'] == 'FAILED_NO_DATA':
        buckets[r['terminal_tier']].append(r)
for t, lst in sorted(buckets.items(), key=lambda x: -len(x[1])):
    print(f'  {len(lst):>4}  {t}')
```

**Always also split by fetch_outcome.** A `TIER_1_API_RENTCAFE_SHAPE_REJECTED` failure with `fetch_outcome=OK` is an extraction bug; with `fetch_outcome=BOT_BLOCKED` it's a label leak — the entry page never loaded.

---

## Phase 3 — Per-PID 9-question diagnostic checklist

For EVERY FAILED_NO_DATA PID you investigate, answer all 9 in order before forming a hypothesis. Each takes <2 minutes from `events.jsonl`.

| # | Question | Where to look | What different answers tell you |
|---|---|---|---|
| **Q1** | Entry-page `fetch.completed` outcome | first `fetch.completed` for this PID | OK → L3 problem; BOT_BLOCKED / CF_CHALLENGE → infra; DEAD_URL → CSV data quality |
| **Q2** | `body_bytes / text_bytes` ratio | `extract.html_characterized` event | ratio > 20 → heavy SSR state blob, walk embedded JSON (§8); ratio 2-5 → normal HTML; text_bytes < 1000 → React shell, anchor-stability gate may not have fired |
| **Q3** | `floor_plan_signal_count` | `extract.html_characterized` event | 0 → genuinely no unit signals on this page (probably STUB_URL — see §11); 1 → at threshold, marginal; ≥2 → unit data is present, extraction missed it |
| **Q4** | `jsonld_types` | `extract.html_characterized` event | Only `ApartmentComplex`/`PostalAddress`/`ImageObject` → property-level metadata, no unit Offers; presence of `Apartment`/`FloorPlan`/`Offer` → extractable JSON-LD |
| **Q5** | Candidate list size + composition | `extract.link_hop_started.candidates` | 1 candidate at score 10001 (profile:winning_page_url) → §6 self-fetch suppression eligibility; 0 → discovery failed entirely; ALL 5 at score ≥5000 with same host → no real anchors found, only PMS priors |
| **Q6** | Tier sequence | series of `extract.tier_attempted` | `generic:llm` with `reason=rc3_defer_monolithic_to_hop` on HOP page (`hop_index ≥ 1`) → §10 hop_depth bug; `generic:embedded_json ran_empty "1 SSR blob(s) had no unit signals"` AND body/text ratio > 20 → §8 walker not catching vendor path |
| **Q7** | Terminal label vs reality | last `extract.tier_won` or terminal in CSV | SHAPE_REJECTED tier without ANY captured rentcafe-host JSON → §5 label leak |
| **Q8** | Profile state | prod DB query (§6) | `wpu` matches infra-URL pattern → profile-poisoning; `wpu == entry_url` → §6 self-fetch eligible; `explored_links > 10` and includes a PMS prior → §6 explored_skip eligible |
| **Q9** | Portal URLs anywhere in HTML | live fetch + grep | `sightmap.com/embed/`, `*.appfolio.com/listings`, `*.onlineleasing.realpage.com` present but NOT in candidate list → §8 portal scan miss |
| **Q10** | Cross-origin iframes rendered into the DOM | Playwright `page.frames` + Playwright `page.content()` after scroll/wait | Any frame URL not on a known infra host (Google analytics, maps, parastorage CDN, social) with unit-shaped innerText (`\d+ bed`, `$NNN`, `sqft`) → §8.11/§8.15 vendor-iframe widget. Common offenders: `wix-visual-data.appspot.com`, `yourcrossstreet.com/property/`, `embed.fortresstech.io`, `my.hy.ly`. Static-HTML grep can MISS these — Wix injects the iframe post-hydration. |
| **Q11** | Entry-page redirect host vs CSV host | `fetch_result.final_url` vs `base_url` | When hosts differ, anchors on the page point at the LANDED host (cross-domain). The CSV-host filter in `_rank_internal_links` drops them. → §8.9 redirect-aware landed_url. Diagnostic command: live-fetch the CSV URL with `urllib.request.urlopen(...)`; if `resp.geturl()` returns a different host, that host owns the inventory. |
| **Q12** | Listing structure vs fp_signals | `has_listing_structure(html)` against entry/hop HTML | fp_signals ≥ 2 BUT `count_listing_structural_signals == 0` → marketing aggregate copy (priceRange in LocalBusiness JSON-LD, "Choose from 1, 2, 3-bedroom"), not real listings. Classify as STUB_AGGREGATE_COPY rather than chasing extraction. → §8.10. |
| **Q13** | Unit-fidelity: extractor `units_found` vs emitted `units` | sum of `extract.tier_attempted.units_found` (only `ran_units`) vs `output.property_emitted.units` | Extractor reported N, emit shows M < N → the gap is the unit-loss bug class (§8.18-8.20). Categorize: M=0 with N≥1 → §8.18 plan_summaries dropped at v2 OR §8.19 hop plan_summaries not propagated. M < N/2 with `verdict=SUCCESS` → §8.20 AVAILABLE+rent classification (rows demoted to plan_summaries). M < N with `verdict=PARTIAL` → validation-majority-rejected, real units gate-failed (data-quality issue, not a code bug). Run THIS before declaring an "IMPROVED" delta a real fix — the verdict can go SUCCESS while losing two-thirds of the units. |
| **Q14** | Per-adapter stage history (added 2026-05-22) | `extract.tier_attempted` events where `tier_key` starts with `<adapter>:` (e.g. `rentcafe:sc_probe`, `g5:urn_pick`, `entrata:prospectportal_probe`) | Tells you **which stage of the adapter cascade actually fired and what outcome each produced**. Pre-2026-05-22 PMS adapters returned silently — this telemetry filled the gap. Look for: (1) the `outcome` field — `ok`/`status_403`/`cf_challenge_shell`/`parse_returned_empty`/`exception:X`; (2) the `via_proxy` boolean — when `False` on cross-origin probes (sc_probe, prospectportal_probe, wp_probe), the env is missing `PROBE_PROXY_URL`; (3) the platform-wide `<adapter>:adapter_exit` event — fires once per dispatch with the final `tier_used` + `units` + error summary. See §20.4 / §20.5 for the per-adapter stage reference. |
| **Q15** | Silent-empty parser diagnostic (added 2026-05-22) | `extract.tier_attempted` events with `tier_key=<adapter>:<stage>_diag` and `outcome=parser_silent_empty` | When a stage parsed `0` rows despite the body containing visible inventory markup, the `_diag` companion event carries `signal_caption_samples`, `signal_heading_samples`, `signal_data_label_inventory`, `signal_first_row_ctx`, `signal_vendor_markers`, `signal_cf_marker_counts`. Cluster across PIDs to detect new template variants without re-fetching pages. The 2026-05-22 SecureCafe regex bug would have surfaced in one jq query against `signal_caption_samples`. See §20.4 + §20.12. |

When you finish Q1–Q15, the root cause is almost always one of: § ENV_MISMATCH (CF-blocked locally only), §6 profile poisoning, §8 extraction gap, §10 architectural invariant violation, §11 STUB_URL, **§20.3 cross-origin clearance asymmetry (missing PROBE_PROXY_URL in prod env)**.

---

## Phase 4 — Verification protocols (never trust without verifying)

Apply before every code change:

| Claim type | Verification command | Example |
|---|---|---|
| "Function `_X()` does Y" | `grep -n "def _X\b" pms/**/*.py` then read the actual code | Agent claimed `_probe_known_endpoints()` reads `ctx.base_url`; code reads `page.url`. |
| "URL `https://X` returns Y" | `python -c "import urllib.request; ..."` 5-second live fetch | Agent claimed mark-taylor has SightMap CDN `dnn506yrbagrg.cloudfront.net`; live fetch found `g5-c-` Vue components. |
| "Tier W fires under condition Z" | grep events.jsonl for the specific PID; match against tier sequence | The 2026-05-13 `hop_depth == 0` decider guard "fired" but events showed the same `rc3_defer` on hop pages — gate was reading a missing ctx field. |
| "Fix deployed on date D" | `git log --since=D --until=D+1 --pretty=oneline` in `ma_poc/` | Always confirm the commit landed; "should have deployed" ≠ "deployed". |
| "Profile contains X" | Run §6 SQL summary query, NOT the export query | Don't load the full 305 KB profile_json blob just to check a maturity tag. |
| "Cluster of N failures has cause Y" | `python C:/tmp/trace_clusters.py` against 2 fail + 2 success PIDs in the same cluster | "AppFolio fails because adapter X" became "the AppFolio iframe URL pattern wasn't in `_PORTAL_URL_PATTERNS`" only after the diff. |

---

## Phase 5 — Tier-label decoder

The terminal_tier in `failures.csv` is NOT a description of what happened. It's a label assigned by the last adapter that ran. Common label-vs-reality leaks:

| Label | Misleading interpretation | What it actually means | How to verify |
|---|---|---|---|
| `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | "RentCafe API captured but malformed" | At least one response was buffered in `_api_responses` AND none passed `_is_rentcafe_response`. The response might be the HTML page itself or a third-party tracker JSON. | grep events.jsonl for `rentcafeapi.com\|widgets.rentcafe.com\|securecafe.com\|yardi.com` — if no hits, NO RentCafe JSON was captured. The 2026-05-15 fix tightened this to require json content-type AND a rentcafe-family host. |
| `TIER_1_API_ENTRATA` | "Entrata adapter captured an API" | Adapter ran (PMS detected as entrata); 0 units emerged from any tier. Most often 0 XHRs captured because the widget XHR is sub-page-only and the hop probe didn't fire (`page=None` bug — see §10). | grep for `/Apartments/module/widgets/`; if absent, the probe never fired. |
| `TIER_1_API_SIGHTMAP_SHAPE_REJECTED` | "SightMap API returned wrong shape" | Sometimes correct. Often: the page has a SightMap iframe (`sightmap.com/embed/...`) but the L1 fetcher never navigated into it. The adapter sees zero sightmap-shape responses → SHAPE_REJECTED. | Live-fetch the entry HTML; grep for `sightmap.com/embed/`. Presence + 0 captured sightmap responses → §8 portal scan missed it. |
| `FAILED_NO_DATA` (verdict, not tier) | "Page exists but extraction failed" | Entry-page `fetch.completed` was OK; aggregate extraction produced 0 units. Doesn't say WHERE in the cascade. | Q1-Q13 checklist. |
| `FAILED_UNREACHABLE` (verdict) | "Site down" | Entry-page fetch_outcome ≠ OK. Locally vs cloud distinction matters: CF-blocked locally → ENV_MISMATCH; CF-blocked in cloud too → real infra problem. | Compare local canary `fetch.completed` to cloud's. |
| `SUCCESS_PARTIAL` (verdict, 2026-05-17+) | "Partial failure" | Timeout-rescue success. Per-property wallclock fired before cascade completed BUT link-hop accumulator buffered ≥1 valid unit. Counts as success in `reporting.verdict._SUCCESS_VERDICTS`. Tracked separately under `properties_success_partial` in the analyzer so the rescue population is visible. | Look for `Property X timed out after 600s — attempting partial recovery` log line + units > 0 in the emit event. |
| `PARTIAL` (verdict, 2026-05-17+ semantics) | "Same as SUCCESS_PARTIAL" | **Different bucket.** Validation-majority-rejected: the schema gate dropped >50% of extracted rows on validity grounds (dim-less rows, junk floor-plan names). The surviving rows ship but are suspect. **Excluded from `_SUCCESS_VERDICTS`.** Tracked under `properties_partial_validation_rejected`. | grep events.jsonl for `validate.record_rejected` count vs `validate.record_accepted` count — if rejected > accepted, you're looking at PARTIAL not SUCCESS_PARTIAL. |
| `FAILED (no property_emitted — likely killed by per-property timeout)` (analyzer label) | "Property crashed / wallclock-killed before output" | **Misleading label that survives in older analyzer output.** Pre-2026-05-17 analyzer dumped any property whose verdict wasn't `SUCCESS`/`FAILED_NO_DATA`/`FAILED_UNREACHABLE` into `properties_failed_other` and labelled them "no property_emitted". In reality those properties DID emit — with verdict `PARTIAL` (now `SUCCESS_PARTIAL`) and units > 0. The 2026-05-17 analyzer surfaces them under `SUCCESS_PARTIAL` / `PARTIAL (validation-rejected)` distinctly. Old reports still carry the misleading label. | Count `output.property_emitted` events per shard and reconcile against `properties_failed_other`. If they're equal, the label is stale. |

---

## Phase 6 — Profile-state inspection (DO THIS BEFORE READING ANY CODE)

A poisoned profile can cause MORE failures than any code bug. Run this query against prod first.

### 6.1 Summary query (no payload — fast)

```sql
SELECT canonical_id,
       version,
       updated_by,
       updated_at::date AS last_updated,
       payload->'confidence'->>'maturity' AS maturity,
       payload->'navigation'->>'winning_page_url' AS winning_page_url,
       jsonb_array_length(coalesce(payload->'navigation'->'explored_links', '[]'::jsonb)) AS explored_count,
       jsonb_array_length(coalesce(payload->'navigation'->'availability_links', '[]'::jsonb)) AS avail_count
FROM scrape_profiles
WHERE canonical_id IN (<pids>)
ORDER BY maturity DESC NULLS LAST, canonical_id;
```

### 6.2 What to flag immediately

| Pattern in `winning_page_url` | What it means | Source bug |
|---|---|---|
| `*.execute-api.*.amazonaws.com` | Lambda backend got persisted as the unit-data URL | `_is_infra_api_url` filter missing `execute-api` pattern |
| `*.theconversioncloud.com`, `*.omappapi.com`, `*.matterport.com`, `*.nestiolistings.com`, `*.supabase.co`, `*.hereapi.com`, `*.firebaseio.com` | Third-party tracker/CMS API; widely observed | `_is_infra_api_url` blocks these — but old profiles still have them. Cold-profile retry recovers (§10). |
| `.../?id=X/floorplans` (corrupted query+path) | The 2026-05-14 URL composition bug captured this as wpu | Cold-profile retry (§10) is the only recovery |
| `winning_page_url == entry_url` (after `_normalize_url`) | Self-fetch — homepage was incorrectly tagged as the unit page | Fix G (self-fetch suppression, 2026-05-15) — wpu no longer injected as candidate |

`explored_count > 10` typically means accumulated empty-extraction URLs poisoning the skip-list. The 2026-05-15 read-side carve-out lets high-score URLs (PMS priors, anchor-discovered links) bypass `explored_skip`. The writer-side fix only persists URLs when fetch_outcome != OK.

### 6.3 Export profile_json for canary seeding

```sql
\copy (
  SELECT canonical_id,
         json_build_object(
           'version',        version,
           'schema_version', schema_version,
           'created_at',     to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
           'updated_at',     to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
           'updated_by',     updated_by,
           'payload',        payload
         ) AS profile_json
  FROM scrape_profiles
  WHERE canonical_id IN (<pids>)
) TO 'C:/tmp/canary_profiles_<date>.csv' WITH CSV HEADER;
```

Seed with [C:/tmp/seed_canary_profiles.py](C:/tmp/seed_canary_profiles.py) which upserts into local proppy without nuking existing stubs. The local canary then auto-detects `DATA_PROVIDER=postgres` and reads them.

---

## Phase 7 — Cluster-diff investigation (when N PIDs share a failure)

When a bucket has 30+ failures and you can't fix all of them at once: pick 2 IMPROVED + 2 UNCHANGED_FAIL from the same `terminal_tier`. Diff their attributes:

```python
# Quick cluster trace — pick PIDs from successes.csv (IMPROVED) + failures.csv (UNCHANGED_FAIL)
# in the same terminal_tier bucket, then diff:
import json
from pathlib import Path
ev = Path(r'C:\tmp\run-<date>\events.jsonl')  # or canary events
for pid in IMPROVED_PIDS + FAILED_PIDS:
    print(f'=== {pid} ===')
    for line in ev.read_text(encoding='utf-8').splitlines():
        e = json.loads(line)
        if str(e.get('property_id') or '') != pid: continue
        if e.get('kind') == 'extract.link_hop_started':
            for c in e.get('candidates', []):
                print(f'  score={c.get("score"):>6} {c.get("anchor","")[:30]:<30} {c.get("url","")[:80]}')
        if e.get('kind') == 'extract.html_characterized':
            print(f'  signals: fp={e.get("floor_plan_signal_count")} jsonld={e.get("jsonld_types")[:3]} script_count={e.get("script_count")}')
```

The **first different attribute between IMPROVED and UNCHANGED_FAIL** is almost always the gap.

Example (2026-05-15 AppFolio investigation):
- 77734 (SUCCESS): candidate list contained `appfolio.com/connect?a=cw` AND `/vacancies` at score 5100 → walker found the AppFolio iframe path.
- 219388 (FAILED): candidate list had only `/our-communities/*` siblings and 404-ing PMS priors. The `franklin.appfolio.com/listings` iframe in the HTML was never queued.
- Diff revealed: `_PORTAL_URL_PATTERNS` only matched `apartments.appfolio.com` and `widgets.appfolio.com`, missing `{slug}.appfolio.com/listings`. One-line fix.

---

## Phase 8 — Extraction-gap reference

The 8 extraction gaps observed in the last 3 days, with verified fixes (each links to the actual landed code).

### 8.1 Embedded-JSON walker — for SSR inventory blobs (Razz, Wix Studio, custom CMS)

**Signal:** `body_bytes >> text_bytes` (ratio > 20). Page is 99% inline state.
**Cause:** CMS serialises inventory into a `<script type="application/json">` block at a vendor-specific key path. `extract_embedded_blobs_from_html` finds the block but `_find_list` is a 2-level walker — vendor paths are 4+ deep.
**Fix:** `find_unit_arrays(blob, min_signals=2)` in [pms/adapters/_api_parser.py](ma_poc/pms/adapters/_api_parser.py) (shipped 2026-05-15). Recursive DFS; finds any list whose items have ≥2 canonical unit-signal keys (after `normalize_field_key` vendor-variant collapse). Picks the longest list (avoids 5-row plan-summary picks beating a 678-unit inventory).
**Tested vendor key paths:**

| Vendor | Path to unit array |
|---|---|
| Razz / MyRazz | `requestedScreen → screenStoreState → initialStoreState → $inventory → units` |
| Next.js | `__NEXT_DATA__.props.pageProps.*` (any list) |
| Nuxt | `__NUXT__.data[0].*` |
| Wix Studio | `viewModel.* → items[]` |

**Size cap:** raised 1 MB → 4 MB in [_html_extract.py:719-728](ma_poc/pms/adapters/_html_extract.py#L719-L728) on 2026-05-15 because the Razz blob is 1.02 MB.

### 8.2 Portal URL 3-pass scan — iframe + anchor + quoted-URL

**Signal:** Live HTML grep shows `sightmap.com/embed/`, `*.appfolio.com/listings`, `*.onlineleasing.realpage.com` but the URL is NOT in `extract.link_hop_started.candidates`.
**Cause:** Pre-2026-05-15 `_extract_portal_iframe_hints` only scanned `<iframe src>`. Real-world portal URLs appear in 3 places:
1. `<iframe src="...">` (AppFolio embed)
2. `<a href="...">` (OneSite "Apply Now" CTA)
3. `"yardi_apply_now_link":"https://9026050.onlineleasing.realpage.com/..."` inside inline JS (mark-taylor.com)

**Fix:** 3-pass scan in [pms/scraper.py:_extract_portal_iframe_hints](ma_poc/pms/scraper.py) — iframe → anchor → quoted-URL-anywhere. Add new portal hosts to `_PORTAL_URL_PATTERNS` at [pms/adapters/_html_extract.py:891-913](ma_poc/pms/adapters/_html_extract.py#L891-L913).

**Currently recognised portal patterns:**
```
sightmap.com/embed/, embed.engrain.com         → sightmap
onlineleasing.realpage.com, myleasingoffice.com → realpage_oll
rentcafe.com/apartments/, rentcafe.com/onlineleasing → rentcafe
funnelleasing.com/embed                         → funnel
apartments.appfolio.com, widgets.appfolio.com,
.appfolio.com/listings, .appfolio.com/connect  → appfolio
myresman.com/portal/*, resman.com/portal/      → resman
.yardi.com                                     → yardi
```

### 8.3 JSON-LD vendor-key fuzzy match

**Signal:** `extract.html_characterized.jsonld_types` includes `ApartmentComplex` but `generic:jsonld -> ran_empty: "no Apartment/Offer schema in HTML"`.
**Cause:** `_jsonld_item_has_unit_signal` checked Schema.org canonical keys (`offers.price`, `numberOfRooms`, `floorSize`) but vendor data uses camelCase variants (`monthlyRent`, `numberOfBedrooms`, `bedroomCount`, `squareFootage`). Added a second-pass check that normalises each key via `FIELD_ALIASES`.
**Fix:** [pms/adapters/_api_parser.py:_jsonld_item_has_unit_signal](ma_poc/pms/adapters/_api_parser.py) (2026-05-15).
**Added alias:** `floorsize → sqft` for Schema.org `floorSize` after lowercase normalisation.

### 8.4 RentCafe content-type gate

**Signal:** `terminal_tier=TIER_1_API_RENTCAFE_SHAPE_REJECTED` with `fetch_outcome=OK` and entry-page body is the property's own marketing site (not a securecafe portal).
**Cause:** `ctx._api_responses` includes EVERY response with content-type containing `json|xml|html|text` — so the HTML page itself made `api_responses` non-empty, and the SHAPE_REJECTED classifier fired even when zero rentcafe-host JSON XHRs were captured.
**Fix:** [pms/adapters/rentcafe.py:_classify_rentcafe_failure](ma_poc/pms/adapters/rentcafe.py) (2026-05-15). A response only counts as a RentCafe candidate when **content-type ∈ json AND host ∈ {rentcafe.com, securecafe.com, yardi.com}**. Third-party trackers (callrail, gtm, osano) no longer trigger SHAPE_REJECTED.

### 8.5 Subpath URL composition

**Signal:** Many hops to URLs of shape `?id=XXX/floorplans`, `?id=XXX/pricing`, `?id=XXX/floor-plans` — the SPA returns 200 with the same homepage shell every time.
**Cause:** `_psp_url = sub_url.rstrip("/") + _psp` — naive concatenation when `sub_url` contains a query string produces a path-segment after the query.
**Fix:** [pms/scraper.py:1958-1972](ma_poc/pms/scraper.py#L1958-L1972) — `urlparse` + `urlunparse` composition that preserves the query string. Stops the spiral.

### 8.6 Stripped-text dedup hash

**Signal:** SPA tab-switcher pages (`#floor-plans`, `#pricing`, `#amenities`) all return distinct raw bytes (timestamps, CSRF, chunk URLs differ) but the same rendered text.
**Cause:** Body-hash dedup was SHA256 of raw bytes. SPA pages had 26 KB of inline-state variance defeating the hash.
**Fix:** [pms/scraper.py:1094-1117](ma_poc/pms/scraper.py#L1094-L1117) — `_strip_html_for_dedup` removes script/style/tags, collapses whitespace, hash the result.

### 8.7 SightMap-vs-Entrata detector tiebreaker

**Signal:** PMS detected as `entrata` but unit data lives in a SightMap iframe on the same page (chaseknollsapts.com — 2026-05-15).
**Cause:** Detector Pass 1 STRONG marker (`commoncf.entrata.com`) beat SightMap (Pass 3 WEAK). Entrata adapter ran, found nothing.
**Fix:** [pms/detector.py:365-394](ma_poc/pms/detector.py#L365-L394) — when BOTH `sightmap.com/embed/` AND any Entrata STRONG marker are present, route to `sightmap` at 0.90.

### 8.8 FP-signal gate on LLM_DOM

**Signal:** `generic:llm_dom_targeted ran_empty` on pages with `floor_plan_signal_count == 0` (SPA shells, marketing pages with no inventory).
**Cause:** `_extract_rent_dom_section` falls back to `body[:cap]` when no structural container exists, so the LLM gets a non-empty input. Burns ~$0.01 per property × thousands of properties.
**Fix:** [pms/adapters/generic.py:2356-2375](ma_poc/pms/adapters/generic.py#L2356-L2375) — `if html and _has_fp_signals(html, SIGNAL_THRESHOLD_ANY)` gate before invoking the DOM-LLM. Emits visible `skipped, reason="no floor-plan signals in body — skipping LLM DOM"` event.

### 8.9 Redirect-aware `landed_url` for cross-domain anchor admit (2026-05-15)

**Signal:** Entry URL is `https://X.com/`; after fetch `final_url` lands on `https://Y.com/properties/X/`. Page has anchors pointing at `Y.com/properties/X/floorplans/` ("View Availability" link) and `Y.com/properties/X/find-apartments/`. None of those make it into the `extract.link_hop_started.candidates` list — only Y-host PMS-priors at score 5095 appear. PIDs 53592 (1701arch.com → livethearch.com) and 119144 (windsorburnet.com → windsorcommunities.com) are canonical.

**Cause:** `_rank_internal_links` at [pms/scraper.py:1487-1495](ma_poc/pms/scraper.py#L1487-L1495) had a `is_same_site = link_host == base_host …` check. With `base_host = "windsorburnet.com"` and `link_host = "windsorcommunities.com"`, the check fails and the anchor is dropped. Even relative anchors like `href="floorplans/"` resolve against `base_url` (CSV's pre-redirect host), so the resolved URL lands on the wrong host. PMS-prior synthesis has the same bug.

**Fix:** [pms/scraper.py:1401-1556](ma_poc/pms/scraper.py#L1401-L1556) — `_rank_internal_links` now takes an optional `landed_url` parameter. Accepts BOTH `base_host` and `landed_host` as same-site. Uses `landed_url` as the `urljoin` base when entry redirected to a different host. [pms/scraper.py:2790+](ma_poc/pms/scraper.py#L2790) — caller extracts `fetch_result.final_url` and passes as `landed_url` to `_try_link_hop`, which propagates into the ranker AND uses it as the PMS-prior synthesis base when present.

### 8.10 fp_signal listing-structure gate (STUB_AGGREGATE_COPY) (2026-05-15)

**Signal:** `floor_plan_signal_count >= 2` triggers extraction tiers but every tier returns 0 units. Live-grep of the page shows the patterns came from marketing copy ("Choose from our 1, 2, or 3-bedroom apartments") + a `LocalBusiness` JSON-LD `priceRange: "$1490 - $3824"` aggregate + amenity descriptions ("793 square feet fitness center"). NO actual per-unit table or Offer array exists. PID 119144 (windsorburnet.com) is canonical.

**Cause:** `count_floor_plan_signals` matches raw text patterns. It can't distinguish a unit listing from marketing aggregate copy that happens to mention bedrooms/baths/sqft.

**Fix:** new `count_listing_structural_signals(html)` + `has_listing_structure(html)` in [pms/signal_engine/floor_plan_signals.py:391-490](ma_poc/pms/signal_engine/floor_plan_signals.py#L391-L490) that checks for:
- ≥2 `<tr>` rows containing a rent shape ($NNN) — covers Entrata/RentCafe/RealPage pricing tables
- ≥2 DOM nodes with unit-card / floorplan-card / listing-card class markers
- ≥2 JSON-LD `@type: Apartment|FloorPlan|Offer|Product` declarations
- ≥2 JSON-LD `PropertyValue` entries naming a dimension (covers Squarespace `Product + additionalProperty`)

Gate is plumbed into the LLM_DOM tier at [pms/adapters/generic.py:2367-2389](ma_poc/pms/adapters/generic.py#L2367-L2389) — when fp_signals fire but `has_listing_structure == False`, the LLM_DOM call is skipped with reason `stub_aggregate_copy — fp_signals present but no listing structure`. Saves ~$0.005/property × N marketing-aggregate sites.

### 8.11 JSON-LD `Product + additionalProperty: [PropertyValue]` (Squarespace e-commerce) (2026-05-15)

**Signal:** `extract.tier_attempted generic:jsonld ran_empty "no Apartment/Offer schema in HTML"` despite the page having structured product data. JSON-LD `@graph` contains multiple sibling `Product` nodes each with `category: "Apartment Floor Plan"`, a SINGLE `offers` dict (not a list), and `additionalProperty: [{name: "Bedrooms", value: 1}, {name: "Bathrooms", value: 1}, {name: "Square Footage", value: 830}]`. The original parser only knew `numberOfBedrooms` / `numberOfBathroomsTotal` / `floorSize` as direct keys. PID 61950 (250high.com) is canonical — 5 floor plans, $1,723-$2,447.

**Cause:** Pass 2 `_extract_offers_as_units` requires `offers: [...]` with len ≥ 2; the per-Product single-offer dict fails the check. Pass 3 `_extract_standalone_offers` walks bare Offers nested inside AggregateOffer dicts but loses the parent Product context, so `additionalProperty` is never read.

**Fix:** new `_read_additional_property(item)` helper + new `_extract_product_floorplans_as_units` Pass 4 at [pms/adapters/_html_extract.py:445-525](ma_poc/pms/adapters/_html_extract.py#L445-L525). Pass 4 collects Products with `apartment` / `floor plan` / `rental` category hints, wraps each Product's single `offers` dict + sets `itemOffered = product`, then calls `_build_unit_from_offer`. The unit builder at [_html_extract.py:232-310](ma_poc/pms/adapters/_html_extract.py#L232-L310) reads `additionalProperty` and fills missing `bedrooms` / `bathrooms` / `sqft` from PropertyValue entries. **Pass 4 must run BEFORE Pass 3** otherwise the bare-Offer walker eats the nested per-Product offers with no parent context.

### 8.12 Generic inline-JS PMS init parser (JS-injected iframes) (2026-05-15)

**Signal:** Live HTML grep finds `SightMap.init({sightmap_id: "..."})` or `<div data-sightmap-id="...">` or fortresstech UUID embedded in inline JS, but NO `<iframe src="sightmap.com/embed/...">` in static HTML. The 3-pass iframe / anchor / quoted-URL scan returns empty for these portals because the iframe is JS-injected at runtime.

**Cause:** SightMap (and several other portals) ship a script loader (`sightmap.com/embed/api.js`) that hydrates the iframe at runtime. The static HTML the L1 fetcher saved BEFORE hydration only contains the loader tag. PID 20959 dovevalleyapts.com is canonical.

**Fix:** new `_scan_inline_js_pms_init(html)` 4th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1218-1330](ma_poc/pms/scraper.py#L1218-L1330). Pattern library (`_INLINE_JS_INIT_PATTERNS`) covers SightMap, AppFolio `data-tenant`, RealPage OLL `clientId`, FortressTech UUID, Hyly `propertyId`, FunnelLeasing GUID. Each pattern captures the property-specific key; `url_synth_fn(key)` returns the canonical data URL.

### 8.13 AppFolio slug-to-listings synthesis (2026-05-15)

**Signal:** Marketing site embeds `{slug}.appfolio.com/connect/users/sign_in` iframes (Pay-Rent / Tenant-Portal CTAs) but NO `/listings` iframe. After RC #3b removed `.appfolio.com/connect` from `_PORTAL_URL_PATTERNS`, no AppFolio URL gets queued at all → all hops are host-root 404s. PIDs 259733 (ekoliving.life), 11399 (ironridge-capital.com), 50178 (leescrossingapartments.net) canonical.

**Cause:** The fix removed the wrong-target match but didn't add affirmative listings-URL discovery.

**Fix:** 5th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1232-1262](ma_poc/pms/scraper.py#L1232-L1262). Uses [`_APPFOLIO_SUBDOMAIN_RE`](ma_poc/pms/detector.py#L173) to detect the CMS slug from any `{slug}.appfolio.com` occurrence in HTML; when no `/listings` URL is already queued, prepends `https://{slug}.appfolio.com/listings` at portal score 10000. AppFolio adapter's existing 4 extraction modes (XHR capture, SSR DOM parse, /detail page, offboarded-tenant detection) handle the rest. PID 259733 ekoliving.life recovered 122 units this way.

### 8.14 Vendor-iframe widgets — Wix Visual Data + Cross Street + Fortress (2026-05-15)

**Signal:** PIDs 46179 hayloft / 118965 16bennett / 292955 3140clybourn / 1713 brooklaneapts. Static HTML / urllib fetch shows no inventory text. Live Playwright fetch enumerates `page.frames` and finds a cross-origin frame holding the actual data — host varies per vendor:
- `wix-visual-data.appspot.com/index?pageId={page}&compId={comp}&siteRevision={n}` — Wix Visual Data widget; AngularJS shell renders a Wix Collection as a table
- `yourcrossstreet.com/property/{slug}/?floorplan={id}` — Cross Street leasing widget; React SPA  
- `embed.fortresstech.io/unit-availability/{guid}` + `portal.fortresstech.io/{guid}/` — Funnel/FortressTech React SPA

**Fix:** added to `_PORTAL_URL_PATTERNS` at [pms/adapters/_html_extract.py:891-1135](ma_poc/pms/adapters/_html_extract.py#L891-L1135) + late-render whitelist at [fetch/fetcher.py:866-895](ma_poc/fetch/fetcher.py#L866-L895). 292955 (yourcrossstreet) recovered. 46179 / 118965 still fail because the Wix Visual Data iframe URL is JS-injected with query params that come from the Wix component config — static HTML scan only sees the BASE URL without `pageId` / `compId`. URL synthesis from `wix-viewer-model` blob is deferred to a follow-up.

### 8.15 Open-by-default unknown-portal discovery + telemetry (2026-05-15)

**Signal:** A new vendor's iframe widget hosts inventory; every property on that vendor fails until someone hand-edits `_PORTAL_URL_PATTERNS`. No runtime learning.

**Fix:** added 6th pass in `_extract_portal_iframe_hints` at [pms/scraper.py:1264-1303](ma_poc/pms/scraper.py#L1264-L1303). Walks every `<iframe src>` in entry HTML; skips known portals (already queued), same-origin URLs, and any host on `_PORTAL_INFRA_BLACKLIST` ([pms/adapters/_html_extract.py:1135-1240](ma_poc/pms/adapters/_html_extract.py#L1135-L1240) — analytics, maps, chat, social, parastorage CDN, Wix internal services). Remaining hosts are queued at `_UNKNOWN_PORTAL_SCORE = 9_000` with anchor `unknown:{host}`, capped at 3 per property.

Each unknown emits `embedded_portal.unknown_host_seen` event ([observability/events.py](ma_poc/observability/events.py)) so cross-run aggregation can identify trending vendors. Full design doc at [docs/generic_portal_discovery.md](ma_poc/docs/generic_portal_discovery.md). Phase 3 (profile-persisted learned hosts) + Phase 4 (cross-run promotion to `_PORTAL_URL_PATTERNS`) are planned but not yet shipped.

### 8.16 Decider rule conflation — Rule 2 vs Rule 4 both return HOP_TO_URL (2026-05-15)

**Signal:** `extract.tier_attempted reason=rc3_defer_monolithic_to_hop` fires on a hop page (`hop_index ≥ 1`) even AFTER the 2026-05-15 `hop_depth` wiring fix landed in production. 53.7% of FAILED_NO_DATA had this — meaning the monolithic LLM was suppressed on every hop, never running anywhere.

**Cause:** the `ActionDecider` ([pms/signal_engine/decider.py](ma_poc/pms/signal_engine/decider.py)) has TWO rules that return `ActionType.HOP_TO_URL`:
- Rule 2 (RC3 deferral) — rationale `dom_analysis_defer_monolithic_to_hop`, gated on `hop_depth == 0`
- Rule 4 (top-signal dispatch via `_map_to_action`) — rationale `top_signal:{...}`, NOT gated; returns HOP_TO_URL for LLM_HINT / PROFILE_WINNING / PMS_PRIOR signals

The caller in [pms/adapters/generic.py:2655](ma_poc/pms/adapters/generic.py#L2655) only checked `action_type == HOP_TO_URL` and set `_rc3_deferred = True`. On hop pages Rule 2 correctly skipped (hop_depth=1), but Rule 4 still returned HOP_TO_URL because the LLM_HINT signal was in the ranker → caller mislabeled it as rc3_defer and suppressed the LLM.

**Fix:** [pms/adapters/generic.py:2655-2670](ma_poc/pms/adapters/generic.py#L2655-L2670) — check rationale, not just action_type: `if _decision.action_type == HOP_TO_URL and _decision.rationale == "dom_analysis_defer_monolithic_to_hop"`. **Architectural lesson** (now in §10): when reading an `ActionDecider` decision, the action_type alone is ambiguous — read the rationale for which rule fired.

### 8.17 Wix vendor-key fuzzy normalization (2026-05-15)

**Signal:** SSR JSON blobs contain unit-shaped items (e.g. `{numBedrooms: 1, numBathrooms: 1, floorSize: 750, priceText: "$1,450"}`), but `find_unit_arrays` walker reports `0 SSR blob(s) had no unit signals`. `_item_has_unit_signals` runs each key through `normalize_field_key`, but Wix camelCase variants weren't in the alias table.

**Cause:** `FIELD_ALIASES` had `bedroomcount` / `bedroom_count` / `numberofbedrooms` but NOT `numbedrooms` / `bedroomsnumber` / `pricetext` (Wix wix-data collection vendor spellings).

**Fix:** [pms/signal_engine/floor_plan_signals.py:151-280](ma_poc/pms/signal_engine/floor_plan_signals.py#L151-L280) — added exact aliases for the observed Wix keys, AND added a `_fuzzy_normalize` helper with prefix patterns (`num<X>` / `number<X>` / `total<X>`) and suffix patterns (`<X>Count` / `<X>Number` / `<X>Total`) that maps to canonical when the stem is in `_FUZZY_STEM_CANONICAL`. Critically: `_PROTECTED_CANONICAL_KEYS` guards `min_rent` / `max_rent` etc. so the `min<X>` / `max<X>` semantic prefixes survive normalisation.

### 8.18 Plan-summaries silently dropped at v2 output boundary (2026-05-17)

**Signal:** Property emits SUCCESS with N units but the extractor's `extract.tier_attempted ran_units` reported a higher count (e.g. PID 20959 dovevalleyapts: LLM emitted 12 units, output shipped 6). Trace shows `B4 dedup: dropped K plan-level rows that duplicated existing unit-level rows`, then post_process partitions the rest, then the v2 formatter ships only `result.units` and ignores `result.plan_summaries`.

**Cause:** `_format_v1` / `_format_v2` at [scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py) read only `result["units"]` from the in-process result dict. `result["plan_summaries"]` (the second partition produced by `extraction.post_process.post_process`) was never surfaced into the v2 record. Plan-level rows admitted by `post_process` (real rent + dims, just no per-apartment identity) shipped nowhere.

**Fix:** v1 emits `Floor Plans: [...]` (capitalised); v2 emits `floor_plans: [...]` (snake). Both pipe through `_format_v2_unit` so the same field-name normalisation runs on both partitions. Contract test in [tests/integration/contracts/test_floor_plans_emit.py](ma_poc/tests/integration/contracts/test_floor_plans_emit.py).

**Companion:** §8.20 (AVAILABLE+rent promotion) reduces how often a row lands in plan_summaries; this fix ensures the rows that genuinely belong there still reach output.

### 8.19 Hop-level plan_summaries not propagated to entry result (2026-05-17)

**Signal:** Hop emits `generic:llm_dom_targeted ran_units=6` but final emit shows units=0 and no floor_plans (PID 300327 flatson10th — 3 hop ran_units events totalling 13 units, final 0). The hop ran extraction; post_process partitioned all rows into plan_summaries (no per-unit signal in the SecureCafe portal extraction); the hop_result returned with `units=[]` and `plan_summaries=[6 rows]`. Entry-side `_try_link_hop` only checked `sub_result.get("units")` for `had_data` and discarded the hop entirely.

**Cause:** in `_try_link_hop` ([pms/scraper.py](ma_poc/pms/scraper.py)), the `had_data = bool(sub_result.get("units"))` check gated all downstream merging. Hops with only plan_summaries were treated as empty and skipped.

**Fix:** `had_data = bool(sub_result.get("units")) or bool(sub_result.get("plan_summaries"))`. Added `"plan_summaries"` to the key-copy list at the entry-side `if _hop_has_data:` branch in `scrape_jugnu` so the partition reaches the entry result alongside `units`. Renamed the local `_hop_has_data` predicate for clarity.

### 8.20 AVAILABLE+rent rows misclassified as plan-level (2026-05-17)

**Signal:** Extractor returns N units, post_process emits 0 in `units` and N in `plan_summaries`. Example (PID 20959 dovevalleyapts, LLM output): 12 rows with `floor_plan_name`, beds/baths/sqft, `market_rent_low`, and `availability_status="AVAILABLE"` — but `available_date=None` on half of them. `classify()` demoted every dateless row to plan-level because `_has_per_unit_signal` required `available_date` / `floor` / `building`.

**Cause:** `extraction/classify.py:_has_per_unit_signal` checked `_UNIT_LEVEL_SIGNAL_KEYS = ("available_date", "availability_date", "floor", "building", …)` only. A row with explicit AVAILABLE status + rent ≠ a plan summary — it represents an available apartment at that price even when the move-in date is unknown.

**Fix:** new `_is_available_with_rent` predicate in [extraction/classify.py](ma_poc/extraction/classify.py). `_has_per_unit_signal` now also returns True when status is in `{AVAILABLE, AVAIL, OPEN, TRUE, 1, YES}` (read via canonical `AVAIL_STATUS_KEYS`) AND rent_low / rent_high is present. UNAVAILABLE rows + UNKNOWN-status rows correctly stay plan-level. Test suite: [tests/extraction/test_classify_available_rent_promotion.py](ma_poc/tests/extraction/test_classify_available_rent_promotion.py) (11 cases — covers alias keys, distinct rents on the same plan, AVAIL synonym, regression guard for plan-aggregates without rent).

**New canonical alias table:** `AVAIL_STATUS_KEYS` in [extraction/canonical.py](ma_poc/extraction/canonical.py). Companion to `RENT_LO_KEYS` / `AVAIL_DATE_KEYS` — covers vendor variants of the per-row status string.

### 8.21 Cross-host per-plan detail discovery (2026-05-17)

**Signal:** Hop produces 10 plan-shape rows from a portal host (e.g. `*.securecafe.com`) and emits SUCCESS, but the per-plan detail URLs (one per plan, each carrying actual per-apartment inventory) live on the marketing host — e.g. `alexandriacarmel.com/floorplans/the-diplomat-1-br-1-ba`. The same-host HTML fallback in `_try_link_hop` searches only the SECURECAFE page's anchors and finds nothing matching. The entry candidate queue DOES contain the per-plan URL at score 5980 with anchor "the diplomat - 1 br 1 ba", but the link-hop loop returned on the first hop with units (LEAF return) and never reached it.

**Cause:** the floor-plan-accumulation `fp_hints` discovery was scoped to same-host sub-page anchors (`_rank_internal_links(sub_html, sub_url)` with a same-prefix filter). Cross-host per-plan URLs already in the entry queue weren't bridged into accumulation mode.

**Fix:** `_discover_cross_host_per_plan_urls` in [pms/scraper.py](ma_poc/pms/scraper.py). Pure helper — given the candidate queue, visited set, the just-hopped URL, and current fp_hints, returns URLs whose path matches `_PER_FLOORPLAN_DETAIL_PATH_RE` (`/floor[-]?plans?/{slug}` or `/plans/{slug}` or `/units/{slug}`) AND whose slug contains a bed/bath/studio token AND whose queue score ≥ 4000. Anchored against URL SHAPE, not plan-name, because the in-flight internal unit dict's `floor_plan_name` is empty at this stage (see §0 anti-pattern 16). Test suite: [tests/pms/test_cross_host_per_plan_discovery.py](ma_poc/tests/pms/test_cross_host_per_plan_discovery.py) (15 cases — URL-shape matchers, queue filters, edge cases).

**Companion ranking changes (2026-05-17):**
- `slugged_plan_detail` URL-shape score boosted 5_000 → 6_500 in [pms/scraper.py](ma_poc/pms/scraper.py) `_URL_SHAPE_PATTERNS`. Per-plan URLs now outrank generic anchor-discovered links (~5_100) so the entry queue surfaces them earlier.
- New anchor keywords in [pms/signal_engine/defaults.py](ma_poc/pms/signal_engine/defaults.py): `view details` (70), `apply now` (60), `only ` (75 — substring prefix for "only N left" / "only available").
- `_PORTAL_INFRA_BLACKLIST` filter lifted from the unknown-portal scan into the known-portal pattern dispatch in [pms/scraper.py](ma_poc/pms/scraper.py). Pre-fix, `resources.yardi.com/legal/cookie-notice/` matched the bare `.yardi.com` known pattern at score 10_110 and burned a hop slot ahead of the Diplomat URL.

**Status:** ships one cross-host per-plan URL per hop today. The remaining sibling per-plan URLs (Ambassador, Justice, etc.) aren't in the entry candidates and require sibling-URL synthesis from a template — deferred (see §17 Bug #5).

### 8.22 Wedge-rescue captcha guard — SGCAPTCHA properties (2026-05-17)

**Signal:** Property scored `FAILED_UNREACHABLE` on entry due to a captcha challenge (entry HTML was an 11 KB SGCAPTCHA wall, body_bytes=11961, captcha_detected=True). Wedge-rescue retry pass then refetched with HTTP-only GET, received a 215-byte captcha stub, the `LLM_GATE_NO_BODY` gate rejected it (<1024 bytes required), and the runner re-emitted `output.property_emitted verdict=FAILED_NO_DATA`. The later emit shadowed the correct FAILED_UNREACHABLE verdict. 37 properties on 2026-05-16: 298969 thewattapts, 300327 flatson10th, 3188 thepointeatlapts, 55317 abodes, … — all SiteGround-hosted.

**Cause:** wedge-rescue's retry-candidate filter at [scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py) admitted any record with verdict in `{PARTIAL, FAILED_UNREACHABLE}` and `len(units)==0`. Didn't check whether the failure was a captcha-block — those properties have nothing useful that GET would surface (the captcha page is the same on RENDER and GET).

**Fix:** new pure helper `wedge_rescue_decision(meta, *, has_units)` in [scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py) returning `"RETRY"` / `"SKIP_ENTRY_CAPTCHA"` / `"NO_RETRY"`. SKIP_ENTRY_CAPTCHA fires when `_meta.entry_captcha_detected` or `entry_bot_blocked` is truthy. The captcha info is propagated from `result["_fetch_diagnostic"]["captcha_detected"]` into `_meta` by `_process_property` after each scrape. SKIP_ENTRY_CAPTCHA emits a `WEDGE_RESCUE_RETRY_RESOLVED resolution=SKIPPED_ENTRY_CAPTCHA` event for telemetry. Test suite: [tests/scripts/test_wedge_rescue_decision.py](ma_poc/tests/scripts/test_wedge_rescue_decision.py) (20 cases — RETRY / SKIP_ENTRY_CAPTCHA / NO_RETRY branches, case-insensitive verdict, falsy-flag handling, units-outranks-captcha priority).

---

## Phase 9 — Profile-state and recovery mechanisms

Two distinct mechanisms to recover from poisoned WARM/HOT profiles:

### 9.1 Self-fetch suppression (Fix G, 2026-05-15)

**Trigger:** `profile.navigation.winning_page_url == entry_url` (path-normalised match — handles trailing-slash + http/https variants).
**Effect:** `winning_page_url` is NOT injected as a hop candidate. Real anchor-discovered links (`/Marketing/FloorPlans` etc.) take the top slot.
**Why safe:** Entry-page extraction runs BEFORE `_try_link_hop`. Adding the entry URL as a hop just re-fetches the same body. LLM budgets are per-property; entry already consumed them.
**Code:** [pms/scraper.py:1520-1561](ma_poc/pms/scraper.py#L1520-L1561).

### 9.2 Cold-profile retry (Fix I, 2026-05-15)

**Trigger:** First `scrape_jugnu` returned 0 units AND profile maturity is WARM/HOT.
**Effect:** Second `scrape_jugnu` call with `force_cold=True`. The function clones the profile in-memory and clears `winning_page_url`, `availability_links`, `explored_links`, `dead_links`, `dom_hints.field_selectors`. Persisted profile is NOT mutated. One retry per property max.
**Why useful:** PMS providers change without notice; a property can migrate from RentCafe to Entrata between runs. The stale wpu / cached selectors actively misdirect the scraper.
**Code:** [scripts/runners/jugnu.py:687-727](ma_poc/scripts/runners/jugnu.py#L687-L727), [pms/scraper.py:2402-2459](ma_poc/pms/scraper.py#L2402-L2459).

### 9.3 LLM_DOM retry-on-empty when prior winner

**Trigger:** `generic:llm_dom_targeted` returned 0 AND `profile.confidence.last_success_tier == 4`.
**Effect:** Second LLM-DOM call with a `_retry_hint=prior_llm_dom_win_empty_today` in property_context. Mitigates non-determinism on OpenRouter when same HTML yields different completions across runs.
**Code:** [pms/adapters/generic.py:2407-2452](ma_poc/pms/adapters/generic.py#L2407-L2452).

---

## Phase 10 — Architecture invariants you must verify when changing gates

A gate that depends on a missing context field is silently always-True (or always-False). Every gate change must verify the data flow from upstream.

### 10.1 `AdapterContext` field inventory

The fields every gate may read — and the upstream sites that populate them:

| Field | Set at | Read by | What goes wrong if missing |
|---|---|---|---|
| `hop_depth` | `pms/scraper.py` `scrape()` from kwarg; recursive `scrape()` call from `_try_link_hop` passes `hop_depth=1` | `decider.py:137` (RC3 monolithic deferral), `generic.py:2644` | RC3 defers LLM on every hop — entire LLM budget wasted. (The 2026-05-13 fix added the gate but missed the upstream wiring. Fixed 2026-05-15.) |
| `floor_plan_signal_count` | `pms/scraper.py:599` from `extract.html_characterized` | `decider.py:649-652` (RC3 suppression on content-rich entry) | RC3 always defers even when entry has real unit data |
| `adapter_unit_count` | `pms/scraper.py` after PMS-specific adapter run | `generic.py` LLM skip-gate | LLM stays skipped even after Entrata/RentCafe returned 0 |
| `_api_responses` | `pms/scraper.py` from network_log | Every adapter probe + LLM_API_RESCUE | Adapter sees zero responses; SHAPE_REJECTED label leaks |
| `profile` | `pms/scraper.py:587` | DOM-hints replay, LLM_DOM retry, cold-retry trigger | Replay / retry mechanisms inert |

### 10.2 Verification rule

When adding a new gate that reads `ctx.X`:
1. `grep -n "X\s*=\|X:" pms/adapters/base.py` — confirm the field exists.
2. `grep -n "AdapterContext(" pms/scraper.py` — confirm every construction site sets it.
3. Write a unit test that exercises the gate via a real `AdapterContext` instance, not a Mock — Mocks silently grant any `getattr()`.

### 10.3 Profile-dependent gates

Same rule: every gate that reads `profile.X` must have `X` populated by `profile_updater.py`. Common slots:

| `profile.X` | Set by | Read by |
|---|---|---|
| `navigation.winning_page_url` | `profile_updater.py:_update_winning_url` after SUCCESS | `pms/scraper.py:_try_link_hop` profile_top builder |
| `navigation.explored_links` | `profile_updater.py:record_explored_link(had_data=False)` | `pms/scraper.py:_try_link_hop` explored_skip builder |
| `confidence.last_success_tier` | `profile_updater.py:_update_confidence` on SUCCESS | `generic.py:2407` LLM_DOM retry gate |
| `dom_hints.field_selectors` + `.field_selectors_quality` | `profile_updater.py` after LLM_DOM success | `generic.py:1818-1856` DOM-cascade replay |
| `confidence.maturity` (COLD/WARM/HOT) | `profile_updater.py:_promote_or_demote` | `services/source_planner.py:compute_budget`, runner cold-retry trigger |

---

## Phase 11 — STUB_URL classifier (genuine no-data with high confidence)

Some properties have a perfectly-loaded entry page with ZERO unit data — they're marketing wrappers. Distinguishing these from "extraction missed data that exists" lets us route data-quality issues to the CSV maintainer instead of into the extraction-bug triage queue.

**Verdict `STUB_URL` (high-confidence genuine empty) fires when, aggregated across entry + every hopped sub-page, ALL of these are zero/empty:**

| Signal | Source |
|---|---|
| Inline rent markers (`$NNN`, "price", "rent") in visible text | live fetch + regex |
| Floor-plan structural signals (bed/bath/sqft/studio) | `floor_plan_signal_count` |
| JSON-LD `Apartment`/`FloorPlan`/`Offer` nodes | `jsonld_types` |
| Unit-shaped arrays in `<script type="application/json">` | walker in §8.1 returned empty |
| Portal URLs anywhere in HTML (iframe/anchor/quoted) | 3-pass scan in §8.2 |
| Captured API JSON responses with ≥2 unit-signal keys | `_api_responses` count |
| Internal anchors matching floor-plan keywords that loaded OK | `extract.link_hop_fetched` outcomes |
| **Cross-origin iframe frames** (Playwright `page.frames`) carrying unit-shaped innerText | Playwright frame enumeration |
| **Listing structure** (`<tr>` + rent, unit-card classes, Offer/Product arrays, PropertyValue dims) | `count_listing_structural_signals(html)` ≥ 1 |

**Critical precondition before declaring STUB:** ENUMERATE FRAMES. The 2026-05-15 false-STUB call on 46179 / 118965 / 292955 happened because I only checked top-level `document.body.innerText`. Each had real inventory inside a cross-origin iframe (`wix-visual-data.appspot.com`, `yourcrossstreet.com/property/`). Static HTML grep would have missed it too — Wix injects the iframe post-hydration. Per §14 *Frame enumeration snippet*: open with Playwright, scroll, then iterate `page.frames` and read each frame's `document.body.innerText`. ONLY declare STUB when EVERY frame returns marketing-only text.

**Two distinct STUB classes:**
- **STUB_URL** — no rent / bed / sqft anywhere in HTML or any frame. Truly no data.
- **STUB_AGGREGATE_COPY** — `fp_signals ≥ 2` BUT `has_listing_structure(html) == 0`. The signals come from marketing copy ("Choose from 1, 2, 3-bedroom") + a LocalBusiness JSON-LD `priceRange` aggregate. PID 119144 windsorburnet.com canonical. Use `has_listing_structure` from [pms/signal_engine/floor_plan_signals.py](ma_poc/pms/signal_engine/floor_plan_signals.py) to discriminate.

**Examples confirmed STUB (2026-05-15):**
- PID 267183 thesiennaapartments.com — 3 application/json blocks, all are Squarespace form configs (`formFields`, `submissionTextAlignment`); 0 `$NNN`, 0 bed, 0 sqft. `/floorplans` and `/floor-plans` both 404.

**False-STUB war stories (don't repeat these):**
- 2026-05-15: 46179 hayloftapartmenthomes.com declared STUB after `body.innerText = 1442 chars` marketing-only check. Reality: `wix-visual-data.appspot.com/index?pageId=q0o07&compId=ja77je2z` iframe held 5 floor plans + rent + sqft. User correction caught this.
- 2026-05-15: 118965 16bennett.com same pattern — wix-visual-data iframe had 30+ per-unit table rows.
- 2026-05-15: 292955 3140clybourn.com same pattern — `yourcrossstreet.com/property/the-clybourn-2/?floorplan=4` iframe had 6 floor plans.

**Status:** STUB_URL / STUB_AGGREGATE_COPY classifier not yet emitted as a distinct verdict. The `has_listing_structure` gate is in place at the LLM_DOM tier (cost savings) but properties still emit `FAILED_NO_DATA` externally. Verdict-level classification (separate from FAILED_NO_DATA in metrics) is P3 work — requires changes to `reporting/verdict.py`, `slo_watcher.py`, `analyze_cloud_run.py`, frontend schema, DB.

---

## Phase 12 — Local canary workflow (proven 3-day cadence)

### 12.1 Build manifest from yesterday's failures

```python
# Sample 3-4 PIDs per terminal_tier bucket + force-include any PIDs you specifically diagnosed
# + 4 sentinels with varied PMS types (8-80 units, varied domains)
```

Reference implementation patterns shipped in [data/canary/local_runs/](ma_poc/data/canary/local_runs/) folder names. Key flags for the canary tool:
- `--regression-basket-size 0` — don't auto-select sentinels (provided in manifest)
- `--keep` — preserve sqlite DB for forensics
- `--timeout-per-property 240` — accommodates LLM-DOM retries
- `--from-run YYYY-MM-DD` — yesterday's run for the diff baseline

### 12.2 Cold vs profile-seeded — always run both

| Run type | What it exercises | What it MISSES |
|---|---|---|
| Cold (default) | Adapter extraction, embedded JSON walker, portal scan, hop_depth, FP-signal gates | Profile-dependent fixes: Fix G self-fetch, B3 carve-out, LLM_DOM retry, cold-retry trigger |
| With prod profiles seeded (§6.3 + seed_canary_profiles.py) | Everything above PLUS profile-dependent fixes | Real Cloudflare bypass (still local-direct fetch) |

### 12.3 Cloud SQL proxy gotchas

```bash
# Port 5433 often in use after a crashed session — use 5434 fallback
"C:\Users\ashus\bin\cloud-sql-proxy.exe" --port 5434 jugnu-494013:us-central1:jugnu-db-production

# If "409 invalidState" — Cloud SQL instance is under maintenance.
# Either wait or have a teammate run the export query through gcloud sql connect / Cloud SQL Studio.
```

### 12.4 Output paths under SCHEMA_VERSION=v2

```
data/canary/local_runs/<out-dir>/
├── canary_input.csv                   ← manifest
├── canary.sqlite                      ← canary DB (--keep)
├── report.md                          ← summary + per-property delta table
├── jugnu.log                          ← full runner log
└── v2/runs/<run-date>/                ← actual jugnu output (note v2/ prefix!)
    ├── events.jsonl
    ├── properties.json
    └── property_reports/{pid}.md
```

The canary tool reads `events.jsonl` from `v2/runs/<date>/` when `SCHEMA_VERSION=v2`. If it reports "TIMEOUT × N" for every property, it's looking at the wrong path — verify both `runs/` and `v2/runs/`.

### 12.5 Verdict interpretation

| Outcome | What it means | Deploy gate |
|---|---|---|
| IMPROVED (failure → success) | Fix worked | Counts positive |
| UNCHANGED_OK (sentinel still passes) | No regression | Required |
| UNCHANGED_FAIL (failure didn't recover) | Fix doesn't cover this PID; classify residue per §3 Q1-Q9 | Acceptable |
| REGRESSED (sentinel now fails) | **STOP — deploy blocked** | Must be 0 |
| ENV_MISMATCH (CF/bot-block locally; works in cloud) | Not a code regression | Acceptable, document |

A pass rate of 40-50% IMPROVED on a representative bucket sample is good. 100% is unrealistic — some properties are genuinely STUB_URL or behind anti-bot infra.

---

## Phase 12b — Cloud canary workflow (`canary-introspect`)

The local canary (§12) exercises the extraction code path but runs from a residential IP — CF and most PMS hosts behave very differently for GCP egress (AS396982). For fixes that touch any of the following, the local result alone is **not** sufficient evidence the change works in production:

- Cloudflare / per-path challenge clearance behaviour
- Cross-origin proxy paths (`PROBE_PROXY_URL`, `_with_clearance`)
- Any adapter whose probe target is not the property's own origin (SecureCafe, ProspectPortal, Knock Doorway, AppFolio iframe, RealPage OLL portal)
- Anything gated by `via_proxy` in the platform-wide `adapter_exit` telemetry (§20.4)

For those changes, run a cloud canary on the **`canary-introspect`** Cloud Run job before declaring the fix done. The job is intentionally re-spec'd each run (the prior `gcloud run jobs replace`-from-stdin pattern in `.github/workflows/probe-experiment.yml`) so you can swap in any entry-point + image without affecting production jobs.

### 12b.1 When cloud canary is mandatory

| Change class | Local canary sufficient? | Cloud canary required? |
|---|---|---|
| Pure parser logic (regex / JSON walker / DOM selector) | ✅ Yes | Optional |
| Plan-summary emission, post_process semantics | ✅ Yes | Optional (no infra dependency) |
| CF-shell detection, `probe_get` fallback, cross-origin probe | ❌ No — local IP clears CF | **YES** |
| `PROBE_PROXY_URL`-dependent paths (SecureCafe / ProspectPortal / Knock by-domain) | ❌ No | **YES** |
| New Dockerfile / entrypoint / runtime env wiring | ❌ No | **YES** |
| Detector-branch reordering with infra side effects | ❌ No — local routing may differ | **YES** |
| Telemetry-only changes (new `tier_attempted` shape) | ✅ Yes (unit tests + local canary verify emit shape) | Optional, but recommended if the new events guide a production diagnosis |

### 12b.2 Pre-requisites (one-time)

```bash
gcloud auth list                           # ASCII '*' on the right account
gcloud config get-value project            # → jugnu-494013
gcloud run jobs describe canary-introspect --region=us-central1   # job exists
gcloud storage ls gs://jugnu-canary/        # write access
```

Optional: confirm Cloud Build quota (the build is the slow step — typically 3–5 min cold, ~1 min cached).

### 12b.3 The 6-step lifecycle

```
1. Build canary CSV    (5 properties is typical; cohort guidance below)
2. Upload CSV to GCS   gs://jugnu-canary/canary/<run-tag>.csv
3. Build image         gcloud builds submit --tag jugnu:<canary-tag>
4. Re-spec the job     gcloud run jobs replace canary-introspect-spec.yaml
5. Execute + wait      gcloud run jobs execute canary-introspect --wait
6. Pull artifacts      gcloud storage rsync gs://jugnu-canary/runs/<run-date>/shard_0/
```

#### Step 1 — Build the CSV

Same shape as production: `apartmentid,name,address,city,state,zip,website`. Sourcing from a recent cloud run's `properties.json` keeps the metadata production-equivalent:

```python
# c:/tmp/canary_<topic>/build_csv.py
import json, csv, os
TARGETS = ["232316", "77913", "264329", "271966", "5295"]   # your cohort
out = {}
for i in range(100):
    fp = f"c:/tmp/run-<YYYY-MM-DD>/shard_{i}/properties.json"
    if not os.path.exists(fp): break
    for p in json.load(open(fp, encoding="utf-8")):
        pid = str(p.get("apartment_id") or "")
        if pid in TARGETS and pid not in out:
            out[pid] = {k: p.get(k, "") for k in
                ("apartment_id","proj_name","address","city","state","zip_code","website")}
# Write with the renames jugnu expects.
with open("c:/tmp/canary_<topic>/canary.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["apartmentid","name","address","city","state","zip","website"])
    for pid in TARGETS:
        r = out[pid]
        w.writerow([r["apartment_id"], r["proj_name"], r["address"],
                    r["city"], r["state"], r["zip_code"], r["website"]])
```

#### Step 2 — Upload to the canary bucket

```bash
gcloud storage cp c:/tmp/canary_<topic>/canary.csv \
  gs://jugnu-canary/canary/<YYYY-MM-DD>-<topic>.csv
```

#### Step 3 — Build the image via Cloud Build

```bash
SHORT_SHA=$(git rev-parse --short=12 HEAD)
CANARY_TAG="canary-<topic>-$(date +%Y%m%d-%H%M)-${SHORT_SHA}"
IMAGE_URI="us-central1-docker.pkg.dev/jugnu-494013/jugnu-images/jugnu:${CANARY_TAG}"
gcloud builds submit --tag "${IMAGE_URI}" \
  --timeout=1800s --machine-type=e2-highcpu-8 --project=jugnu-494013
```

You need a `.gcloudignore` at the repo root that excludes `node_modules/`, `*.venv/`, frontend build artifacts, etc. — otherwise the upload is ~300 MB (mostly `frontend/node_modules`). **Critical:** do NOT exclude `ma_poc/services/` (it's a Python package the image imports at build time via `ma_poc.scripts.checks.deployment`). The earlier mistake of writing `ma_poc/services/` instead of `ma_poc/services/coverage/` failed the build with:

```
DEPLOYMENT VALIDATION FAILED
  - FloorplanCatalog import failed: No module named 'ma_poc.services'
```

**Also critical (§12b.5):** if `.gcloudignore` excludes `ma_poc/config/profiles/`, the runtime `ProfileStore.__init__` will fail with `PermissionError: '/app/ma_poc/config/profiles'` because the dir doesn't exist and parent ownership is ambiguous post-build. Either include the dir in the upload context, OR add a Dockerfile step that pre-creates it with the right ownership.

#### Step 4 — Re-spec `canary-introspect`

Save the following as `canary-introspect-spec.yaml` (substitute `__IMAGE_URI_PLACEHOLDER__`). The env block mirrors `jugnu-scrape-canary` so behaviour matches the production canary as closely as possible:

```yaml
apiVersion: run.googleapis.com/v1
kind: Job
metadata:
  name: canary-introspect
  labels:
    cloud.googleapis.com/location: us-central1
spec:
  template:
    spec:
      parallelism: 1
      taskCount: 1
      template:
        spec:
          containers:
          - command: [python]
            args: [ma_poc/scripts/runners/shard_entry.py]
            env:
            - {name: BROWSERS_PER_TASK,  value: '5'}
            - {name: CSV_GCS_URI,        value: gs://jugnu-canary/canary/<YYYY-MM-DD>-<topic>.csv}
            - {name: BUCKET_NAME,        value: jugnu-canary}
            - {name: DATA_PROVIDER,      value: filesystem}     # no PG writes
            - {name: SCHEMA_VERSION,     value: v2}
            - {name: LLM_PROVIDER,       value: gemini}
            - {name: SHARD_SOURCE,       value: csv}
            - {name: RUN_DATE,           value: <YYYY-MM-DD>-<topic>}
            - {name: CURL_CFFI_FOR_DIRECT, value: '1'}
            - name: OPENROUTER_API_KEY
              valueFrom: {secretKeyRef: {name: openrouter-api-key-production, key: latest}}
            - name: ANTHROPIC_API_KEY
              valueFrom: {secretKeyRef: {name: anthropic-api-key-production, key: latest}}
            - name: GEMINI_API_KEY
              valueFrom: {secretKeyRef: {name: gemini-api-key-canary, key: latest}}
            - name: PROXY_POOL_URLS
              valueFrom: {secretKeyRef: {name: proxy-credentials-production, key: latest}}
            - name: PROBE_PROXY_URL
              valueFrom: {secretKeyRef: {name: brightdata-probe-proxy, key: latest}}
            - name: WEB_UNLOCKER_KEY
              valueFrom: {secretKeyRef: {name: web-unlocker-key-canary, key: latest}}
            image: __IMAGE_URI_PLACEHOLDER__
            resources: {limits: {cpu: '2', memory: 4Gi}}
          maxRetries: 0
          serviceAccountName: jugnu-worker-production@jugnu-494013.iam.gserviceaccount.com
          timeoutSeconds: '1800'
```

```bash
sed "s|__IMAGE_URI_PLACEHOLDER__|${IMAGE_URI}|g" canary-introspect-spec.yaml \
    > canary-introspect-spec.resolved.yaml
gcloud run jobs replace canary-introspect-spec.resolved.yaml \
    --region=us-central1 --project=jugnu-494013
```

**Do NOT set `DATABASE_URL`** in the canary spec — its presence triggers the production PG sync in `shard_entry._sync_to_postgres` and would pollute the `properties` / `units` tables with canary data.

#### Step 5 — Execute + wait

```bash
gcloud run jobs execute canary-introspect \
    --region=us-central1 --project=jugnu-494013 --wait
```

Typical wallclock: 5–10 min for 5 properties (3–5 min provisioning + 2–5 min scrape).

If the job fails, pull the full container log to find the root cause — exit reasons are often hidden behind the `gcloud` "Executing job failed" line:

```bash
gcloud logging read \
  'resource.type="cloud_run_job"
   AND resource.labels.job_name="canary-introspect"
   AND labels."run.googleapis.com/execution_name"="<execution-name>"' \
  --limit=50 --order=asc --format='value(textPayload)' \
  --project=jugnu-494013 | tail -60
```

The execution name appears in the `gcloud run jobs execute` failure output: `gcloud run jobs executions describe canary-introspect-<5-char-hash>`.

#### Step 6 — Pull artifacts + analyze

```bash
gcloud storage rsync -r \
  gs://jugnu-canary/runs/<YYYY-MM-DD>-<topic>/shard_0/ \
  c:/tmp/canary_<topic>/cloud_results/ \
  --project=jugnu-494013
```

Per-property verification script — adapt to the fix under test. Skeleton:

```python
# c:/tmp/canary_<topic>/analyze.py
import json
from pathlib import Path
RUN = next(Path("c:/tmp/canary_<topic>/cloud_results").glob("**/properties.json")).parent
TARGETS = {"232316": ("Panton Mill", 5),  # PID → (name, expected_units)
           "77913":  ("Sierra Vista", 1),
           ...}
props = json.load(open(RUN / "properties.json", encoding="utf-8"))
print(f"{'PID':>7} | {'name':25} | tier | units | plans | verdict")
for p in props:
    pid = str(p.get("apartment_id") or "")
    if pid not in TARGETS: continue
    units, plans = p.get("units") or [], p.get("floor_plans") or []
    er, meta = p.get("_extract_result") or {}, p.get("_meta") or {}
    name, expected = TARGETS[pid]
    print(f"{pid:>7} | {name:25} | {er.get('tier_used','?')[:25]:25} "
          f"| {len(units):>5} | {len(plans):>5} | {meta.get('verdict','')}")
# Fix-specific assertions live below — e.g. for telemetry checks:
events = (RUN / "events.jsonl").read_text(encoding="utf-8").splitlines()
for line in events:
    e = json.loads(line)
    if e.get("tier_key", "").startswith("rentcafe:nestin_") \
            and str(e.get("property_id")) in TARGETS:
        print(e["tier_key"], e["outcome"], e.get("reason", "")[:80])
```

### 12b.4 Cohort-selection guidance

5 properties is the right size for a fix-specific canary — large enough to cover the targeted code path on diverse PMS shapes, small enough to fit in <15 min wallclock.

| Fix type | Cohort composition |
|---|---|
| Adapter parser change (Knock, APTS247, SightMap, …) | 2 PIDs known to exercise the new code path + 1 sentinel from the same PMS that already worked (no-regression guard) |
| CF / cross-origin clearance change | 2 PIDs with known cross-origin probe targets + 2 PIDs where the same adapter wins via same-origin (regression guard) + 1 wildcard from a different PMS |
| Telemetry / diagnostic event change | Pick PIDs where the new events SHOULD fire based on yesterday's `events.jsonl` filter |
| post_process / partition semantic | Pick at least one PID per partition class: pure unit-level (e.g. AvalonBay), pure plan-level (RentVision), mixed (Knock layouts + units) |

Sourcing PIDs from yesterday's `failures.csv` / `successes.csv` (per §1, §2) is preferred over hand-picking — production traffic shape is more representative than memory.

### 12b.5 Gotchas that bit me (and how to avoid)

| Symptom | Root cause | Fix |
|---|---|---|
| `DEPLOYMENT VALIDATION FAILED: No module named 'ma_poc.services'` during Cloud Build | `.gcloudignore` excluded `ma_poc/services/` (intended to skip a Node.js artifact dir but caught the Python package too) | Use precise paths in `.gcloudignore`: `ma_poc/services/coverage/`, `ma_poc/services/node_modules/`, NOT `ma_poc/services/`. |
| `PermissionError: '/app/ma_poc/config/profiles'` at runtime in canary, while production runs fine | `.dockerignore` excludes `ma_poc/config/profiles/`. Production GitHub-Actions build still leaves `/app/ma_poc/config` pwuser-writable; Cloud Build layer interaction does not. | Two options: (a) include the dir in build context, or (b) add a defensive Dockerfile step: `RUN mkdir -p /app/ma_poc/config/profiles && chown -R pwuser:pwuser /app/ma_poc/config` before `USER pwuser`. Option (b) is the safer defensive hardening. |
| Cloud canary scrapes succeed but the fix you wanted to test never fires | The fix's code path requires a precondition that your cohort doesn't actually trigger (e.g. RentCafe Nestin recovery only fires after a SecureCafe probe fails; if your cohort's SecureCafe probe succeeds the Nestin recovery never runs) | Pre-verify by reading the entry-page detector signals for each PID — the cohort's `pms_detected` + first-tier outcomes must match the fix's preconditions. |
| `gcloud run jobs execute … --wait` exits 0 but `properties.json` is missing in the bucket | `shard_entry.py` upload is in a `try/finally`, but `_resolve_run_dir` reads the wrong path when `SCHEMA_VERSION=v2` and the runner crashed before writing | Check both `runs/<date>/` and `v2/runs/<date>/` in the bucket. Pull whichever exists; both contain `events.jsonl` for diagnosis. |
| Canary scrapes all properties but `DATA_PROVIDER` was unset | Defaults to `postgres` in some code paths; canary writes to prod DB | Always set `DATA_PROVIDER=filesystem` explicitly in the canary spec. |
| Canary scrapes everything but `floor_plans[]` is always 0 even though the fix should have surfaced them | The image's `config/profiles/{pid}.json` is missing, so the property runs cold-profile every time. Some plan-summary emission paths depend on `winning_page_url` from the profile to know which hop to scrape. | Pre-seed profiles into the canary CSV bucket OR confirm the fix doesn't depend on profile state by reading the parser directly. |
| Same image works in `gcloud run jobs execute` interactive but fails when launched from `jugnu-scrape-canary` | The two jobs have different env blocks. The canary-introspect spec we re-spec'd may have dropped a secret. | Always diff `gcloud run jobs describe canary-introspect --format=yaml` against `gcloud run jobs describe jugnu-scrape-canary --format=yaml` before executing. |

### 12b.6 Cleanup

The `canary-introspect` job stays re-spec'd to your last command until someone else re-runs `probe-experiment.yml` or a follow-up canary. That's fine for diagnosis but means another engineer's "where is canary-introspect pointing today?" answer is unpredictable. Document the current spec in your fix's PR description so reviewers can reproduce the exact state.

Image tags accumulate in Artifact Registry — there's no per-engineer cleanup automation today. Tag with a unique discriminator (e.g. `canary-<topic>-<sha>`) so other engineers can identify yours, and don't reuse tags between unrelated investigations.

### 12b.7 Anti-patterns specific to cloud canary

- **Treating cloud canary as a substitute for unit tests.** The cloud cycle is 10+ min per iteration. If you're still debugging the parser, iterate locally with pytest first. Use cloud only when local can't prove the change works (CF, proxy, infra paths).
- **Running the cloud canary on a stale image.** Always check the build output's image digest and confirm the spec's `image:` field matches; the temptation to re-execute the previous job with a different env to "save the build" routinely produces wrong-image confusion.
- **Assuming "local 5/5 succeeded" means "cloud 5/5 will succeed".** My 2026-05-22 Plan-Summary canary: local got 5 units on PID 232316; cloud got 0 (entry-page CF-blocked before the fix could even run). Local validates the parser; cloud validates the environment around it. Both are needed.
- **Not checking `via_proxy` on the adapter_exit event.** Every PMS adapter now emits `via_proxy: bool` on the platform-wide `adapter_exit` event (§20.4). When the cloud canary shows your fix didn't fire as expected, `via_proxy=false` in the event tells you `PROBE_PROXY_URL` wasn't in the env — the most common cause of "fix didn't run".

---

## Phase 13 — Agentic investigation patterns

When to dispatch parallel agents vs do it yourself.

### Dispatch agents when…

- You need to read 6+ files of code for one investigation (offload context)
- You're running 5+ live fetches against 5+ different sites
- Multiple independent hypotheses need triangulation

### Do it yourself when…

- Single PID deep-dive on events.jsonl (faster in your own bash)
- Code change that needs 3-5 file reads (faster in your own session)
- Anything requiring Bash permissions that may be denied (agents get blocked on `python -c "..."` calls; you don't)

### Briefing template that works

Every agent prompt that produced useful results in the past 3 days included:
1. **The specific question** (verbatim from user when possible)
2. **Concrete inputs** — exact file paths, exact PIDs, exact event types
3. **What I've already ruled out** — prevents the agent re-walking dead ends
4. **Required output format** — "Quote events verbatim; cite file:line; no speculation."
5. **A length cap** — "under 600 words"

Bad briefing example (produces shallow output):
> "Investigate why AppFolio properties are failing"

Good briefing example (produces deep output):
> "PID 219388 liveatfranklin.com terminal=TIER_1_API_APPFOLIO. Events at C:\tmp\run-2026-05-14\shard_70\events.jsonl. Find: (1) what URLs were in the candidate list, (2) what URLs appeared in <iframe src> on the entry HTML, (3) whether _PORTAL_URL_PATTERNS matches any of them. Output: per-PID table with quoted event excerpts. Under 400 words."

### Anti-pattern: trusting the agent's synthesis

Agents are excellent at reading and triangulating but can confabulate. ALWAYS:
- Verify a quoted file:line by reading that line yourself
- Verify a quoted URL by live-fetching it yourself
- Verify a quoted event by grepping events.jsonl yourself
- One inconsistency → re-verify everything in that section

---

## Phase 14 — Tool reference (commands cheat-sheet)

### Quick event trace for a PID

```python
import json
from pathlib import Path

base = Path(r'C:\tmp\run-2026-05-14')
pid = '290347'
# Find the right shard from failures.csv first, OR grep across all shards:
for shard_dir in sorted(base.iterdir()):
    ev = shard_dir / 'events.jsonl'
    if not ev.exists(): continue
    text = ev.read_text(encoding='utf-8', errors='ignore')
    if f'"property_id":"{pid}"' not in text and f'"property_id": "{pid}"' not in text:
        continue
    print(f'=== {pid} in {shard_dir.name} ===')
    for line in text.splitlines():
        try: e = json.loads(line)
        except: continue
        if str(e.get('property_id') or '') != pid: continue
        k = e.get('kind','')
        if k in ('fetch.completed','extract.tier_attempted','extract.link_hop_started','extract.link_hop_fetched','extract.html_characterized','output.property_emitted'):
            keep = {kk: str(vv)[:120] for kk, vv in e.items()
                    if kk in ('url','tier_key','outcome','reason','body_bytes','units','verdict','tier_used','hop_index','final_url','candidates','floor_plan_signal_count','jsonld_types','script_count','text_bytes')}
            print(f'  {k[:34]:<34}', keep)
    break
```

### Live page inspection

```python
import urllib.request, re, json
url = 'https://www.example.com/floorplans'
req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'})
try:
    html = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')
except Exception as e:
    print('FAIL:', e); raise SystemExit
print(f'len={len(html)}')

# Application/json blocks
for i, m in enumerate(re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>', html, re.IGNORECASE)):
    body = m.group(1).strip()
    try:
        d = json.loads(body)
        keys = list(d.keys())[:8] if isinstance(d, dict) else type(d).__name__
    except Exception:
        keys = '<parse-fail>'
    print(f'  [json block {i}] len={len(body)} top_keys={keys}')

# Portal URLs anywhere (iframe / anchor / quoted)
for pat, label in [
    (r'<iframe[^>]+src=["\']([^"\']+)["\']',  'iframe'),
    (r'<a[^>]+href=["\']([^"\']+)["\']',       'anchor'),
    (r'["\'](https?://[^"\'\\s<>]+)["\']',     'quoted'),
]:
    for m in re.finditer(pat, html, re.IGNORECASE):
        u = m.group(1).lower()
        for kw in ('sightmap.com/embed','onlineleasing.realpage','rentcafe.com','securecafe.com','.appfolio.com','myresman.com'):
            if kw in u:
                print(f'  {label:<7} {kw:<28} {m.group(1)[:90]}')
                break

# Rent / FP / unit pattern counts
print('  $NNN:',    len(re.findall(r'\$\s?\d{2,5}(?:,\d{3})?', html)))
print('  bed/br:',  len(re.findall(r'\b\d+\s?(?:br|bed|bedroom)', html, re.IGNORECASE)))
print('  sqft:',    len(re.findall(r'\b\d{2,5}\s?(?:sqft|sq\.?\s?ft|square\s?feet)', html, re.IGNORECASE)))
print('  studio:',  len(re.findall(r'\bstudio\b', html, re.IGNORECASE)))
```

### Frame enumeration — required before declaring STUB (2026-05-15)

The `urllib` live-fetch above only sees the static HTML. Many sites (Wix Visual Data, FortressTech, AppFolio iframes, SightMap JS-injected embeds) inject the data-bearing iframe AFTER hydration. Walk every Playwright frame before concluding "no data anywhere".

```python
import asyncio, re
from playwright.async_api import async_playwright

async def enumerate_data_frames(url: str) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0',
            viewport={'width': 1920, 'height': 1080},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        await asyncio.sleep(3)
        # Scroll slowly to trigger lazy / on-scroll iframe injection
        for y in range(0, 4500, 400):
            await page.evaluate(f'window.scrollTo(0, {y})')
            await asyncio.sleep(0.8)
        await asyncio.sleep(4)
        # Walk every frame
        for fr in page.frames:
            if not fr.url or 'about:blank' in fr.url or 'data:' in fr.url:
                continue
            # Skip CDN-only / asset frames
            if any(s in fr.url for s in (
                'parastorage.com', 'wixstatic.com', 'images.squarespace-cdn',
                'googleapis.com/maps', 'wix-instantsearchplus', 'filesusr.com',
            )):
                continue
            try:
                text = await fr.evaluate('document.body ? document.body.innerText : ""')
            except Exception:
                continue
            if len(text) < 50:
                continue
            # Unit-data heuristic — same logic as STUB classifier
            has_unit_data = bool(re.search(
                r'\d+\s*(?:bed|bath|sqft|sq\.?\s?ft|square)|\$\s?\d{3,5}',
                text, re.IGNORECASE
            ))
            if has_unit_data:
                print(f'*** FRAME WITH DATA: {fr.url[:200]}')
                print(f'    text first 800 chars: {text[:800]!r}')
            else:
                print(f'    frame (no data): {fr.url[:160]}')
        await browser.close()

# Example: confirm a "STUB" claim
# asyncio.run(enumerate_data_frames('https://www.16bennett.com/'))
```

When `*** FRAME WITH DATA` prints with `wix-visual-data.appspot.com/index?pageId=...` or `yourcrossstreet.com/property/...` or `embed.fortresstech.io/...`, the property is NOT a stub — the parent scraper just didn't hop into that iframe. Check `_PORTAL_URL_PATTERNS` + `_PORTAL_INFRA_BLACKLIST` semantics before recommending classification.

### Profile inspection via local proppy

```python
import pg8000.dbapi as pg
conn = pg.connect(host='localhost', port=5432, user='postgres', password='Ashu@007saxe', database='proppy')
cur = conn.cursor()
cur.execute("""
  SELECT canonical_id,
         payload->'confidence'->>'maturity' AS maturity,
         payload->'navigation'->>'winning_page_url' AS wpu,
         jsonb_array_length(coalesce(payload->'navigation'->'explored_links', '[]'::jsonb)) AS exp_n
  FROM scrape_profiles
  WHERE canonical_id = ANY(%s)
""", ([pid1, pid2, ...],))
for row in cur.fetchall(): print(row)
```

### Run pytest before any push

```bash
pytest tests/pms tests/fetch -q --tb=line                  # ~30s
pytest tests/integration --deselect tests/integration/extract/test_extract_cross_page_link_hop.py::test_h5_visited_urls_dedupe -q --tb=line  # ~30s
```

The deselect-list captures known pre-existing failures (`test_h5_visited_urls_dedupe` has been failing on `cf9c4ba` since before today's session — confirm with `git stash && pytest <test> && git stash pop` before adding new entries).

---

## Phase 15 — File location reference

| What | Where |
|---|---|
| `_PORTAL_URL_PATTERNS` (portal host substrings) | `pms/adapters/_html_extract.py:891` |
| `_extract_portal_iframe_hints` (3-pass iframe/anchor/quoted scan) | `pms/scraper.py:1146-1198` |
| `extract_embedded_blobs_from_html` (1-4 MB JSON blob extractor) | `pms/adapters/_html_extract.py:689-777` |
| `find_unit_arrays` (recursive walker, ≥2 unit-signal keys) | `pms/adapters/_api_parser.py:108-189` |
| `_item_has_unit_signals` (uses normalize_field_key) | `pms/adapters/_api_parser.py:131-165` |
| `parse_api_responses` (consumer of walker + nested `rent.min`/`sqft.min` unwrap) | `pms/adapters/_api_parser.py:503+` |
| `_jsonld_item_has_unit_signal` (Schema.org + vendor fuzzy match) | `pms/adapters/_api_parser.py:180-241` |
| `has_floor_plan_signals` (single source of truth for "is this unit data?") | `pms/signal_engine/floor_plan_signals.py` |
| `FIELD_ALIASES` (vendor-camelCase to canonical) | `pms/signal_engine/floor_plan_signals.py:151-213` |
| `_PMS_SUB_PATH_PRIORS`, `_UNIVERSAL_SUB_PATH_PRIORS` | `pms/scraper.py` |
| `_rank_internal_links` (anchor + path keyword scoring) | `pms/scraper.py:1280+` |
| `_try_link_hop` (orchestrator) | `pms/scraper.py:1465+` |
| Cold-profile retry runner-side | `scripts/runners/jugnu.py:687-727` |
| Cold-profile retry in-memory profile clone | `pms/scraper.py:2402-2459` |
| LLM_DOM FP-signal gate | `pms/adapters/generic.py:2356-2375` |
| LLM_DOM retry-on-empty (prior TIER_4 winner) | `pms/adapters/generic.py:2407-2452` |
| RC3 monolithic deferral gate (`hop_depth == 0`) | `pms/signal_engine/decider.py:118-148` |
| `AdapterContext` field set (incl. `hop_depth`) | `pms/adapters/base.py:17-55` |
| `record_explored_link` (writer side) | `services/profile_updater.py:531-547` |
| `_is_infra_api_url` (profile poisoning filter) | `services/profile_updater.py` |
| `dom_hints_saved_this_run` flag (degraded-eviction suppression) | `services/profile_updater.py:556+` |
| L1 fetcher slow mouse-wheel scroll | `fetch/fetcher.py` (search `scroll_trigger`, `_SCROLL_STEPS`) |
| SGCaptcha early-exit | `fetch/fetcher.py` (search `SGCAPTCHA_WALL`) |
| Body-hash dedup (stripped text) | `pms/scraper.py:1094-1117` + `1721-1729` + `1858-1879` |
| Subpath URL composition fix | `pms/scraper.py:1958-1972` |
| Self-fetch suppression | `pms/scraper.py:1520-1561` |
| SightMap-vs-Entrata detector tiebreaker | `pms/detector.py:365-394` |
| `analyze_cloud_run.py` (artifact mirror + report generator) | `scripts/diagnostics/analyze_cloud_run.py` |
| `local_canary.py` (canary tool) | `scripts/diagnostics/local_canary.py` |
| Profile seeder for canary (CSV → local proppy) | `C:/tmp/seed_canary_profiles.py` |
| Cloud SQL profile export query | §6.3 of this document |
| **2026-05-15 additions** | |
| `_extract_portal_iframe_hints` 6-pass scanner (incl. unknown-portal discovery, AppFolio slug synth, inline-JS PMS parser) | `pms/scraper.py:1164-1330` |
| `_PORTAL_INFRA_BLACKLIST` (open-by-default complement) | `pms/adapters/_html_extract.py:1135-1240` |
| `_INLINE_JS_INIT_PATTERNS` (inline-JS PMS init capture) | `pms/scraper.py:1228+` |
| `_read_additional_property` (Schema.org `Product + additionalProperty` reader) | `pms/adapters/_html_extract.py:119-198` |
| `_extract_product_floorplans_as_units` (JSON-LD Pass 4 — Squarespace e-commerce) | `pms/adapters/_html_extract.py:445-525` |
| `count_listing_structural_signals` + `has_listing_structure` (STUB_AGGREGATE_COPY gate) | `pms/signal_engine/floor_plan_signals.py:391-490` |
| `_fuzzy_normalize` + `_PROTECTED_CANONICAL_KEYS` (camelCase vendor-key learning) | `pms/signal_engine/floor_plan_signals.py:230-356` |
| Redirect-aware `landed_url` in `_rank_internal_links` and `_try_link_hop` | `pms/scraper.py:1401-1556` + `pms/scraper.py:2790+` |
| Decider rule-conflation fix (rationale check at HOP_TO_URL) | `pms/adapters/generic.py:2655-2670` |
| Generic SPA-shell late-render detector (content-based, no host whitelist) | `fetch/fetcher.py:903-955` |
| `EMBEDDED_PORTAL_UNKNOWN_HOST_SEEN` event for cross-run aggregation | `observability/events.py` |
| Generic portal discovery design doc | `docs/generic_portal_discovery.md` |
| **2026-05-16 additions** | |
| Partial-recovery verdict emit (Bug #1) | `scripts/runners/jugnu.py:415-465` |
| `_OUTCOME_VERDICT_PREFIX` extended for CANCELLED + 4 more (Bug #2) | `pms/scraper.py:1473-1495` |
| `extract.adapter_selected` field-name fix (Bug #3) | `scripts/diagnostics/analyze_cloud_run.py:298` |
| `_IFRAME_DATA_SRC_RE` + `_SIGHTMAP_EMBED_URL_RE` (Bug #4 — partial; see "Phase 17") | `pms/scraper.py:1149-1198` |
| `AdapterContext.candidate_portal_urls` + Entrata candidate-derived probe (Bug #5) | `pms/adapters/base.py:55-65` + `pms/adapters/entrata.py:284-317` |
| Redirect-to-entry persistence into shared_budget (Bug #6) | `pms/scraper.py:2634-2655` |
| `entry_captcha_detected` / `entry_bot_blocked` split (Bug #7 + #11) | `scripts/diagnostics/analyze_cloud_run.py:85-96, 442-465` |
| `--expected-shards` default 100 + clamped denominator (Bug #8) | `scripts/diagnostics/analyze_cloud_run.py:1115-1124, 629` |
| `count_floor_plan_signal_cardinality` tests (Bug #9) | `tests/pms/signal_engine/test_floor_plan_cardinality.py` |
| `_emit_signal_inspection` property_id parameter (Bug #10) | `pms/adapters/_api_parser.py:634-689` |
| Wedge-rescue HTTP_ONLY retry pass | `scripts/runners/jugnu.py:497-680` |
| Hop-count cap on zero-signal entry pages | `pms/scraper.py:3375-3405` |
| `_HOP_CUMULATIVE_BUDGET_MS = 240_000` cap | `pms/scraper.py:2402-2435` + accumulator at 2504-2518 |
| Browser-restart on `context.close()` timeout | `fetch/browser_pool.py:165-240` |
| Host-level `page.goto` timeout (`asyncio.wait_for`) | `fetch/fetcher.py:694-722` |
| AsyncPool capped to `MAX_CONCURRENT_BROWSERS` | `scripts/runners/jugnu.py:307-329` |
| `WEDGE_RESCUE_RETRY_STARTED` / `_RESOLVED` EventKinds | `observability/events.py` (Phase 17) |
| Alternate SightMap init patterns (Bug #4 follow-up) | `pms/scraper.py:1438+` |
| **2026-05-17 additions** | |
| `Verdict.SUCCESS_PARTIAL` (timeout-rescue success class) | `reporting/verdict.py` |
| `_SUCCESS_VERDICTS` admits SUCCESS_PARTIAL; PARTIAL stays out | `reporting/verdict.py` |
| `AVAIL_STATUS_KEYS` canonical alias table | `extraction/canonical.py` |
| `_is_available_with_rent` predicate (AVAILABLE+rent promotion) | `extraction/classify.py` |
| `Floor Plans` field in v1 output / `floor_plans[]` in v2 output (plan_summaries surface) | `scripts/runners/jugnu.py` |
| Hop-side plan_summaries propagation (had_data + key-copy list) | `pms/scraper.py` |
| `_discover_cross_host_per_plan_urls` (cross-host per-plan URL discovery) | `pms/scraper.py` |
| `_PER_FLOORPLAN_DETAIL_PATH_RE` + `_PER_FLOORPLAN_DETAIL_SLUG_TOKEN_RE` + `_PER_FLOORPLAN_MIN_QUEUE_SCORE` | `pms/scraper.py` |
| `slugged_plan_detail` URL-shape score boosted 5_000 → 6_500 | `pms/scraper.py` `_URL_SHAPE_PATTERNS` |
| `_PORTAL_INFRA_BLACKLIST` extended with `resources.yardi.com` + `www.yardi.com` | `pms/adapters/_html_extract.py` |
| Blacklist filter lifted into known-portal pattern dispatch | `pms/scraper.py` (inside `_extract_portal_iframe_hints` pass 1) |
| Anchor keywords `view details` (70) / `apply now` (60) / `only ` (75) | `pms/signal_engine/defaults.py` `DEFAULT_ANCHOR_KEYWORDS` |
| `wedge_rescue_decision` pure predicate + RETRY / SKIP_ENTRY_CAPTCHA / NO_RETRY decision | `scripts/runners/jugnu.py` |
| `entry_captcha_detected` / `entry_bot_blocked` propagated from `_fetch_diagnostic` → `_meta` | `scripts/runners/jugnu.py` `_process_one` |
| `WEDGE_RESCUE_RETRY_RESOLVED` docstring updated with `SKIPPED_ENTRY_CAPTCHA` resolution | `observability/events.py` |
| Test: AVAILABLE+rent promotion (11 cases) | `tests/extraction/test_classify_available_rent_promotion.py` |
| Test: v1 + v2 `floor_plans` emit contract (7 cases) | `tests/integration/contracts/test_floor_plans_emit.py` |
| Test: cross-host per-plan URL discovery (15 cases) | `tests/pms/test_cross_host_per_plan_discovery.py` |
| Test: wedge-rescue decision (20 cases — RETRY / SKIP_ENTRY_CAPTCHA / NO_RETRY) | `tests/scripts/test_wedge_rescue_decision.py` |
| Test: SUCCESS_PARTIAL admitted to success set, PARTIAL stays out | `tests/reporting/test_verdict_plan_level.py` |
| **2026-05-21 concession additions** | |
| `concession_clean.py` — quality classifier + best-effort cleaner | `core/concession_clean.py` |
| `concession_normalize.py` — structured parser, returns dict-or-None | `core/concession_normalize.py` |
| `_PROPERTY_CONCESSION_RE` + `_capture_concession_from_html` (script-strip + sentence-extend) | `pms/scraper.py` |
| `_SPECIALS_PATHS` + `_probe_specials_pages` (stealth fetch + NOT_MODIFIED fallback + `max_paths` cap + telemetry) | `pms/scraper.py` |
| `stealth_probe` helper (custom headers / POST / no conditional cache, identity sticky-key, captcha detect) | `fetch/probe.py` |
| `vision_banner.capture_banner` — lazy env-gated vision-LLM banner fallback | `extraction/vision_banner.py` |
| `_probe_realpage_cws` + `_probe_beacon_ajax` routed via `stealth_probe` with `telemetry_context` | `pms/adapters/generic.py` |
| Schema_v2 emit chain: `concession_text` / `concession_text_clean` / `_concession_quality` / `concession_structured` (unit + property) | `core/schema_v2.py` |
| Jugnu output surface for property-level concession trio + `concessions_source_url` | `scripts/runners/jugnu.py` |
| `_bundle_unit_concessions` — bundles raw + clean + quality + structured + value + source into `units.concessions` JSON | `data_provider/sql/stores.py` |
| `_SNAPSHOT_SOURCES["concessions"]` key chain widened to `("concession_text", "concession", "concessions")` | `data_provider/sql/stores.py` |
| xlsx export read chain `concession_text_clean → concession_text → concessions` | `scripts/email/daily_failures.py` |
| `EventKind.HOP_CAPTCHA_DETECTED` + `EventKind.CONCESSION_PROBE_RESULT` | `observability/events.py` |
| `zstandard>=0.22.0` runtime dep (closes httpx zstd-decoder gap) | `requirements.txt` |
| Concession canary (standalone, no DB, no Playwright) | `scripts/diagnostics/concession_canary.py` |
| Test: classifier + cleaner invariants (29 cases) | `tests/core/test_concession_clean.py` |
| Test: normalize offer shapes + no-match → None (27 cases) | `tests/core/test_concession_normalize.py` |
| Test: end-to-end raw-preservation invariant + real-property fixtures (32 cases) | `tests/core/test_concession_pipeline.py` |
| Test: script-strip / sentence-extend / /specials probe stealth / cap / telemetry (20+ cases) | `tests/pms/test_concession_capture.py` |
| Test: `stealth_probe` stealth headers / sticky identity / captcha detect / hop-captcha telemetry | `tests/fetch/test_probe.py` |
| Test: JSON-column bundle shape + raw-fallback when structured is None (7 cases) | `tests/data_provider/test_concession_bundle.py` |

---

## Phase 17 — Deferred follow-ups

### Bug #4 — parent-marketing-site detection (PIDs 14524, 234945, similar)

**Diagnosis from 2026-05-16 Playwright forensic:**
- **14524 venterraliving.com** — corporate parent. 130 KB HTML, 0 SightMap/Engrain references in static or rendered HTML, only GTM iframe. The detector routed to `sightmap` because `pms_detected` was carried over from an earlier cloud run, but the entry page has no inventory at all. Inventory lives on `/apartments/<city>/<property-slug>/` sub-pages that the link-hop scheduler doesn't currently surface from a corporate-tier URL.
- **234945 imtresidential.com** — corporate parent. 9.6 MB HTML, **1838 'engrain' substrings** but **zero `engrain.com/<id>` URLs**. The substring count was a misleading signal — the 'engrain' references are CSS class names / content metadata, not SDK integration. Inventory lives on per-property sub-pages.

**Why the existing fixes don't help:**
- `_extract_portal_iframe_hints` requires an actual iframe `src=` / `data-src=` / quoted-URL match. Corporate marketing pages don't have those.
- `_scan_inline_js_pms_init` requires a SightMap-init call. Corporate pages don't have those either.
- The PMS detector's substring-only check (`if "sightmap.com" in h` at `pms/detector.py:427`) is the culprit for routing label; demoting it without corroboration is risky because some legitimate properties only have the SightMap host in a CDN reference + a JS-injected iframe.

**Recommended fix path (not yet shipped):**
1. **PARENT_SITE detector**: when a page has (a) multi-state property nav (≥3 state-name anchors), (b) zero per-unit signals (rent/bed/bath count == 0), (c) iframe count ≤1 AND the only iframe is on `_PORTAL_INFRA_BLACKLIST` (GTM, reCAPTCHA, AudioEye), classify as `PARENT_SITE_NEEDS_HOP` and surface a list of property-sub-anchors as hop candidates at score 9_500 (above PMS_PRIOR, below known-portal).
2. **Detector tightening**: at `pms/detector.py:427`, require `sightmap.com/(embed|api|app)/[\w-]+` rather than the bare substring. The strong path at lines 377-395 stays. Properties that match the bare host but not the structured path get DEMOTED from `sightmap` routing and fall through to the generic cascade.
3. **Telemetry**: emit `PARENT_SITE_DETECTED` event so cross-run aggregation can quantify how many failures fall into this category. Initial estimate: 2 of 4 SightMap_SHAPE_REJECTED canary failures, ~6-15 of the 25 cloud SightMap_SHAPE_REJECTED bucket.

Deferred because: requires new heuristic + telemetry + a new hop-candidate score band. Lower ROI than the immediate post-deploy Bug #4 alternate-init-pattern fix.

### Bug #4 — CF-protected SightMap properties (PID 16139)

**Diagnosis from Playwright forensic:** chaseknollsapts.com returns CF 403 on urllib but renders fine in Playwright with the default Chrome UA. The rendered HTML contains `sightmap.com/embed/api` (the SDK endpoint, not the iframe URL — the property-specific embed-id is supplied via an init call the agent couldn't enumerate).

**Status:** Partially fixed. The 2026-05-16 patch added 3 more inline-JS init patterns to `_INLINE_JS_INIT_PATTERNS` (`embed_id` / `mapId` / `<sightmap-*>` custom element + an Engrain-context permissive last-resort). If the chaseknolls SDK uses any of those, the iframe URL will be synthesized correctly. If not, a Playwright-rendered scan is the only fix — deferred.

**To verify after next cloud deploy:** check whether 16139 appears in the new run's `TIER_1_API_SIGHTMAP` (success) or stays in `TIER_1_API_SIGHTMAP_SHAPE_REJECTED`. If still failing, dispatch a follow-up Playwright forensic with `page.evaluate("window.SightMap")` to dump the actual SDK config blob.

### Bug #5 — sibling per-plan URL synthesis (PIDs 52331 alexandriacarmel, similar cross-host portal sites) (2026-05-17)

**Diagnosis:** §8.21 cross-host per-plan URL discovery surfaces only the per-plan URLs already present in the entry-page candidate queue. For PID 52331, the entry page contained ONE per-plan anchor (`/floorplans/the-diplomat-1-br-1-ba` at score 5980), so accumulation only crawled that one. The other 5 sibling per-plan URLs (Justice, Independence, Ambassador, Constitution, Congressional) weren't surfaced as anchors on the entry HTML and were never crawled.

Each sibling per-plan detail page contains its own per-apartment inventory (5-15+ physical units per plan per the user's ground-truth observation). Without the other 5 URLs, the property emits 14 units when the real count is likely 60-100.

**Why the existing fix doesn't cover this:**
- `_discover_cross_host_per_plan_urls` only matches URLs already in the queue.
- The SecureCafe portal page's HTML may contain the sibling anchors but the same-host `_rank_internal_links` fallback (line 3191+) filters cross-host candidates.
- The first per-plan detail page (`/floorplans/the-diplomat-1-br-1-ba`) may contain "Other Floor Plans" anchors but those weren't surfaced either in the canary trace.

**Recommended fix path (not yet shipped):**
1. **URL synthesis from discovered template + plan-name list**: when at least one per-plan URL is matched, derive `{scheme}://{host}{prefix}/{slug}` from it and synthesise sibling URLs by slugifying the plan names extracted from the hop's plan_summaries. Plan-name slugification: lowercase + non-alphanumeric → `-` + strip edges. Bounded by per-property plan count (typically 6–12).
2. **Pull plan names from the SightMap recovery payload** (if the property's hop sequence includes a SightMap iframe hop, the engrain.com API response has plan names in clear form even when the SecureCafe portal extraction emits empty `floor_plan_name`).
3. **Fallback synthesis**: when no plan names are available, derive slug variants from URL pattern observations on the same host (e.g. `/floorplans/the-{n}-br-{n}-ba` where n ∈ {0..3}).

**Status:** ~50 LOC + tests. Benefits every cross-host portal property with marketing-site per-plan detail pages. Estimated impact: ~5x unit-count uplift on the affected ~40 properties per cloud run (RentCafe vanity sites with SecureCafe portals).

Deferred because: requires careful slugification rules (vendor-specific edge cases in plan-name → URL transformation) and a 600s wallclock budget audit (synthesising 6 more hops per property may not fit current budget — needs prioritisation guard or per-plan parallelism).

---

## Phase 18 — Concession data debugging (2026-05-21)

Concession capture is adjacent to FAILED_NO_DATA — a property can ship `verdict=SUCCESS` with full unit data but a blank, dirty, or un-parseable `concessions` column. This section is the diagnostic + fix reference for that class of bug.

The capture pipeline has three layers; each emits its own field at the property and unit level so a downstream reader can always recover the raw text even when normalization fails:

```
raw page HTML                              units[].concession_text  (raw, ALWAYS preserved)
        │
        ▼  _capture_concession_from_html      ── property-level: concessions
   pms/scraper.py
        │
        ▼  clean_concession_text             ── concessions_clean / concession_text_clean
   core/concession_clean.py                     _concessions_quality / _concession_quality
        │
        ▼  normalize_concession              ── concessions_structured / concession_structured
   core/concession_normalize.py                 (may be None — raw stays the source of truth)
```

**Preserve-and-flag invariant:** the raw text is *never* discarded. `concessions_structured` is None when the regex normaliser couldn't confidently parse one of the supported shapes; the cleaned text and the quality label still ship. xlsx readers fall back through `concession_text_clean → concession_text → concessions`. DB writes bundle all four into `units.concessions` JSON. If a future change touches any of these three modules, run `tests/core/test_concession_pipeline.py::TestPreserveAndFlagInvariant` before shipping.

### 18.1 Symptom decoder

| Symptom | Most likely cause | Fix reference |
|---|---|---|
| **Concessions xlsx column blank for every row** | `daily_failures.py` reading legacy plural key `concessions` while v2 emits `concession_text` | §18.3.6 — read chain is now `concession_text_clean → concession_text → concessions` |
| **Concession text contains JS function bodies / CSS rules** (`href.indexOf`, `padding:`, `Functions["abc~1"]`) | `<script>`/`<style>` BODIES not stripped before tag-flatten in the page-HTML capture | §18.3.1 — script-strip regex landed BEFORE the tag-flatten |
| **Captured `"Limited Time Offer!"` (header only, no body)** | Sentence-split discarded the body sentence; matched sentence is the banner header terminated by `!` | §18.3.1 — sentence-extend forward 1-2 sentences while staying under 300 chars |
| **Property has concessions on page but ours shows none** | Concession copy lives on `/specials` (not homepage); the URL probe missed or hit captcha | §18.3.4 — `_probe_specials_pages` with stealth-fetch + NOT_MODIFIED fallback + 4-path cap |
| **`concessions_structured` is `None` despite non-empty raw text** | Expected raw-fallback behaviour — text didn't match any supported offer shape (e.g. amenity-noise "Free WiFi", marketing prose) | NOT A BUG — verify raw is preserved at `concessions` / `concession_text` and downstream consumers fall back to it. See §18.4 invariant. |
| **`units.concessions` JSON column is `None` despite per-unit emit** | `_SNAPSHOT_SOURCES["concessions"]` was reading `("concessions",)` but schema_v2 emits under `"concession_text"` | §18.3.6 — SNAPSHOT_SOURCES key chain now `("concession_text", "concession", "concessions")` + structured bundle in `_bundle_unit_concessions` |
| **Concession found via stealth probe one day, missing next day** | L1 conditional-cache returned `NOT_MODIFIED, body=None` (carry-forward signal poisoned the probe) | §18.3.4 — NOT_MODIFIED falls through to `stealth_probe` (no conditional cache) for a fresh body |
| **`HOP_CAPTCHA_DETECTED` event count high for one domain** | The L1 stealth posture isn't sufficient on this WAF — domain needs a stealth-tier escalation | §18.5 known limitation — first inspect `concession_probe.result outcome=all_blocked` rate |
| **Garbled bytes in `concession_text` despite clean page** | Server returned `Content-Encoding: zstd` and `zstandard` was not installed | §18.5 — `zstandard>=0.22.0` is in `requirements.txt`; verify it shipped |

### 18.2 Per-property concession diagnostic — 4-question checklist

Run AFTER Phase 3 Q1-Q13 — concession capture is decoupled from unit extraction, so a SUCCESS verdict tells you nothing about concession health. For each property you investigate, answer all 4 from `events.jsonl` + the property record under `data/runs/<date>/properties.json`.

| # | Question | Where to look | What different answers tell you |
|---|---|---|---|
| **Q14** | What's the raw `concessions` value at the property level? | `data/runs/<date>/properties.json` → property record | Non-empty raw → `_capture_concession_from_html` fired; check `_concessions_quality` next. Empty + `concessions_source_url` is None → no homepage hit and either the /specials probe missed or never fired. |
| **Q15** | What does the `_concessions_quality` flag say? | property record | `clean` → text is safe to display. `unclean_script_leak` / `unclean_style_leak` / `unclean_dmapi` → the script-strip in scraper.py didn't catch this leak shape; add the marker to `_SCRIPT_LEAK_MARKERS` / `_STYLE_LEAK_MARKERS` (§18.3.2). `unclean_header_only` → header captured, body sentence dropped; check why sentence-extend didn't reach the body. `unclean_orphan_prefix` → text starts mid-statement; raw was truncated upstream. |
| **Q16** | Did the /specials probe fire? What was its outcome? | grep events.jsonl for `extract.concession_probe.result` for this PID | `outcome=found` → probe rescued the concession; `source_url` tells you which path. `outcome=exhausted` → all probed paths returned non-OK (404 etc.); concession may genuinely not exist OR may be on a path outside the canonical list — consider raising `max_paths` for this domain. `outcome=all_blocked` → every probed path was captcha-blocked; domain needs stealth-tier escalation. Missing event → homepage capture succeeded (probe was short-circuited) OR concession capture wasn't invoked at all (check that `result["concessions_text"]` was already populated). |
| **Q17** | Did any hop hit captcha? | grep events.jsonl for `extract.hop.captcha_detected` for this PID | Multiple captcha hits across the probe → same as Q16 `all_blocked`. Captcha on `context=realpage_cws_probe` → RealPage's WAF tightened, the CWS credential path is no longer viable for this property. Captcha on `context=beacon_ajax_probe` → Beacon's site is now Cloudflare-protected. Zero captcha events on a missed concession → not a stealth issue; check Q15 (quality flag) and Q16 (probe outcome). |

### 18.3 Fixes implemented (2026-05-20 + 2026-05-21)

#### 18.3.1 Property-level page-HTML capture (with script-strip + sentence-extend)

**Signal:** before this fix, ~50% of canary concession captures contained JS function bodies leaked from the ±200-char window around the regex match. ~10K of 49,677 captures hit the 300-char cap with the real offer chopped off. A subsequent batch (~46 rows for Woodland Creek) captured just `Limited Time Offer!` — the body sentence was lost to sentence-split.

**Where:** [pms/scraper.py](ma_poc/pms/scraper.py) — `_PROPERTY_CONCESSION_RE` + `_capture_concession_from_html()`. Capture runs once per scrape on `page_html` before the detector + adapter dispatch:

1. Strip `<script>` / `<style>` / `<noscript>` *bodies* via `re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE|re.DOTALL)` BEFORE the tag-flatten.
2. Search the flattened text with `_PROPERTY_CONCESSION_RE` (anchored numbers + `weeks?/months? free`, `$X off`, `limited-time offer`, `move-in special`, etc.).
3. Take a ±200-char window, sentence-split it, find the matched sentence, then walk forward 1-2 sentences while the running total stays under 300 chars. This recovers banner-header rows (`Limited Time Offer!`) by stitching the body sentence (`Move in by 6/15 and get 1 month free rent.`).

**Source-grep pins:** `tests/pms/test_concession_capture.py::test_script_strip_pattern_present_in_scraper` and `test_sentence_extend_pattern_present_in_scraper` catch refactor regressions.

#### 18.3.2 Raw-text preserve-and-flag

**Where:** [core/concession_clean.py](ma_poc/core/concession_clean.py) (new). Two helpers:

* `classify_concession_quality(text) -> str` — returns one of `clean | unclean_script_leak | unclean_style_leak | unclean_dmapi | unclean_orphan_prefix | unclean_header_only | empty`. Drives the `_concession_quality` / `_concessions_quality` field downstream.
* `clean_concession_text(text) -> str` — best-effort cleaner. Two strategies (first wins): (a) 120-char window around the first recognised offer phrase (`weeks free`, `$X off`, `move-in special`, etc.); (b) boundary-split at the last `})` / `};` / `>` followed by capital letter or digit. **Never returns empty** when the input had visible-text content — preserves the user invariant.

Header-only rows return whitespace-normalised banner (nothing to mine; the text IS the banner). The quality flag tells reporting to display with caution.

#### 18.3.3 Structured normalizer with raw fallback

**Where:** [core/concession_normalize.py](ma_poc/core/concession_normalize.py) (new). `normalize_concession(text, source="TEXT") -> dict | None`. Supported shapes (first match wins):

| Shape | Output type | Example match |
|---|---|---|
| `N weeks/months/days free` (+ inverted form) | `free_rent` with `free_period: {value, unit}` | "2 months free rent" |
| `$X off / save $X / $X welcome bonus` | `discount` with `amount: {value, currency}` | "Save $500 off" |
| `N% off` (bounded 0-99 to reject "150% off" → matching "50% off") | `percent_off` with `percent` | "10% off" |
| `Waived <kind> fee(s)` | `waived_fee` with `fee_kind` | "Waived application fee" |
| `Reduced deposit` | `reduced_deposit` | "Reduced deposit" |
| `Look and lease` | `look_and_lease` | "Look-and-lease special" |

Each result also carries `deadline` (raw date string from `Move in by | Lease by | Valid through | Expires …` — best-effort, date-pipeline owns format normalisation downstream) and `conditions` (≤80 chars of qualifier copy after the offer phrase, sentence-bounded).

**Raw-fallback invariant:** `normalize_concession` returns `None` for anything that doesn't match a supported shape. The caller MUST retain the raw text in a sibling field — `schema_v2.py` and `jugnu.py` both do this unconditionally. If you add a new offer shape, add a regex + builder + append to `_RULES`; never remove the `None`-return path.

#### 18.3.4 /specials URL probe with stealth + cap + telemetry

**Where:** [pms/scraper.py](ma_poc/pms/scraper.py) — `_probe_specials_pages()`. Fires only when the homepage capture missed. Iterates a fixed `_SPECIALS_PATHS` list (`/specials`, `/special-offers`, `/specials-offers`, `/promotions`, ...) and routes EACH candidate through `jugnu_fetch(CrawlTask)` — the L1 stealth stack with identity rotation, Chrome header set, captcha detect, proxy selection.

**Key invariants:**

* **Stealth on every hop.** Every probed URL carries `property_id` as the sticky-key — the entry-page fetch and every downstream probe present the same Chrome identity to the bot-management edge. Tests pin this (`tests/fetch/test_probe.py::test_stealth_probe_sticky_identity_by_property_id`).
* **Captcha guards.** A `captcha_detected=True` response is skipped (interstitial HTML would yield false-positive matches like "Just a moment..."). The L1 captcha_detected flag is the canonical signal.
* **NOT_MODIFIED fallback.** L1's conditional-GET cache returns `outcome=NOT_MODIFIED, body=None` when the server matches our `If-None-Match`. Correct for entry-page change-detection, WRONG for a concession probe that needs the current body. The probe falls through to `stealth_probe` (no conditional cache) on this outcome.
* **Early-exit cap.** `max_paths: int = 4` bounds the worst-case probe time. Bumping to 12 lets a captcha-blocked property burn ~180s; capping at 4 bounds it to ~60s. Configurable per-call.

#### 18.3.5 stealth_probe helper for adapter-side probes

**Where:** [fetch/probe.py](ma_poc/fetch/probe.py) (new). The L1 `fetch()` entry point is GET-only and rejects custom headers because the `CrawlTask` contract is intentionally narrow. `stealth_probe` is the slimmer surface for adapter-side hops that need custom headers (RealPage CWS `x-ws-authkey`), non-GET methods (Beacon AJAX POST), or short-timeout fire-and-forget calls outside the L1 retry loop.

Applies the same `IdentityPool.pick(sticky_key=property_id)` + `chrome_header_set(cold_visit=True)` + `looks_like_captcha` as the L1 fetcher. Skips the conditional-GET cache, the rate limiter, and the retry / identity-rotation loop — adapter probes piggy-back on the L1 budget.

**Currently wired into:**

* RealPage CWS API probe at `pms/adapters/generic.py:_probe_realpage_cws` — `telemetry_context="realpage_cws_probe"`.
* Beacon AJAX POST probe at `pms/adapters/generic.py:_probe_beacon_ajax` — `telemetry_context="beacon_ajax_probe"`.
* /specials NOT_MODIFIED fallback in `_probe_specials_pages` — `telemetry_context="specials_probe"`.

#### 18.3.6 DB persistence + xlsx export fixes

Two pre-existing bugs surfaced during the rewrite:

| Bug | Fix |
|---|---|
| `data_provider/sql/stores.py::_SNAPSHOT_SOURCES["concessions"] = ("concessions",)` — read the wrong key. v2 schema emits `concession_text` per unit, so `units.concessions` JSON column was always None. | Chain widened to `("concession_text", "concession", "concessions")`. New `_bundle_unit_concessions()` runs after the snapshot loop and bundles `{text, text_clean, quality, structured, value, source}` into the JSON column. Pre-bundled dict (carry-forward path) passes through unchanged. |
| `scripts/email/daily_failures.py` xlsx export read `u.get("concessions")` (legacy plural). v2 unit dict emits `concession_text` — the Concessions column was blank on every row. | Read chain now `concession_text_clean → concession_text → concessions`. Applied at both call sites (success-row and failed-row paths). |

#### 18.3.7 Vision-LLM banner fallback (lazy, env-gated)

**Where:** [extraction/vision_banner.py](ma_poc/extraction/vision_banner.py) (new). Fires only when text-based capture AND `/specials` probe both missed AND a Playwright `page` is available AND a vision provider is env-configured (`ANTHROPIC_API_KEY` or `AZURE_OPENAI_API_KEY + AZURE_OPENAI_DEPLOYMENT_GPT4O_VISION + AZURE_OPENAI_ENDPOINT`). No-op when any precondition fails — vision is opt-in.

Crops to top-third of viewport (where banners sit), JPEG-encodes at 70% quality, caps base64 payload at 1.5 MB. Returns a dict with the same shape as `normalize_concession()` so `schema_v2.py` and `jugnu.py` prefer the vision-parsed structure over re-normalising the text (the LLM already aggregated sentence fragments and read structured terms directly from the image).

**Cost guard:** bounded to one screenshot + one vision call per property. No retry. Failures return None — the structured-fallback chain ends here.

### 18.4 Telemetry — measuring concession capture health in production

Two new event kinds in [observability/events.py](ma_poc/observability/events.py):

| Event | Payload | Aggregation question it answers |
|---|---|---|
| `extract.hop.captcha_detected` | `url`, `provider` (`cloudflare`/`recaptcha`/`hcaptcha`/`perimeterx`/`sgcaptcha`/`unknown`), `context` (`specials_probe`/`realpage_cws_probe`/`beacon_ajax_probe`/`other`), `status` | Hop-captcha rate per hop class — distinct from the much-noisier `fetch.captcha_detected` (entry-page) firehose. Filterable without URL-pattern regex. |
| `extract.concession_probe.result` | `outcome` (`found`/`exhausted`/`all_blocked`), `paths_attempted`, `captcha_count`, `source_url` | Per-property terminal outcome of the /specials probe. `all_blocked` is the canonical signal that a domain needs a stealth-tier escalation. |

Worked queries:

```sql
-- Hop-captcha rate per hop class (over the most recent N runs).
SELECT context, provider, COUNT(*) AS hits
FROM events
WHERE kind = 'extract.hop.captcha_detected'
  AND ts > now() - interval '7 days'
GROUP BY context, provider
ORDER BY hits DESC;
```

```sql
-- /specials probe outcome distribution.
SELECT outcome, COUNT(*) AS n,
       round(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM events
WHERE kind = 'extract.concession_probe.result'
  AND ts > now() - interval '7 days'
GROUP BY outcome;
-- found > 0% : probe is recovering concessions homepage capture missed.
-- all_blocked > 5% : meaningful fraction of domains need a stealth escalation.
```

```sql
-- Per-domain unit fidelity: which hostnames lose concession copy to all_blocked?
SELECT split_part(split_part(source_url, '/', 3), ':', 1) AS host, COUNT(*) AS blocks
FROM events
WHERE kind = 'extract.concession_probe.result' AND outcome = 'all_blocked'
GROUP BY host
ORDER BY blocks DESC
LIMIT 20;
```

### 18.5 Known limitations and known-good tradeoffs

* **Quirky WordPress sites can backfire under full stealth.** Confirmed in the 2026-05-20 canary on `woodviewapartments.com/specials`: minimal Chrome UA returns 643 KB containing the offer; full stealth (Chrome UA + `Sec-Ch-Ua` client hints + `Sec-Fetch-*` suite) returns 116 KB without it. The WordPress server's caching layer / theme device-detects on the modern client-hint headers and serves a different variant. **This is a known tradeoff** — stealth helps on bot-managed sites (RentCafe, RealPage, Equity), occasionally hurts on idiosyncratic WordPress. Don't weaken stealth to chase WordPress quirks; if a specific domain matters, raise `max_paths` for that domain or add a per-domain header override.
* **`zstandard` codec gap.** `Accept-Encoding: gzip, deflate, br, zstd` is advertised in `chrome_header_set`. Without `zstandard` installed httpx CANNOT decode `Content-Encoding: zstd` responses and hands back undecoded bytes that break UTF-8 decoding and regex matching. Closed by pinning `zstandard>=0.22.0` in `requirements.txt` (2026-05-21). If you see garbled `concession_text` in production despite a 200 status, confirm `zstandard` is in the runtime image.
* **Capture is best-effort, never blocking.** Every probe wraps its core in `try/except` and the scrape continues on any failure. A property with extraction-side bugs may still ship correct units alongside a None concessions field; the inverse (units missing because of concession capture) is impossible by construction.
* **`concessions_structured` is allowed to be None.** If a property's concession text is `"Welcome to our community"`, the normaliser correctly returns None and the raw text is the system of record. Do NOT count `concessions_structured IS NULL` as a failure metric — count `concessions IS NOT NULL AND _concessions_quality != 'empty'` for "we captured something" and `concessions_structured IS NOT NULL` for "we structured it".

### 18.6 Concession files at a glance

| File | What it owns |
|---|---|
| [core/concession_clean.py](ma_poc/core/concession_clean.py) | `classify_concession_quality`, `clean_concession_text`. Quality labels + best-effort cleaner. |
| [core/concession_normalize.py](ma_poc/core/concession_normalize.py) | `normalize_concession` and the offer-shape rule table. Returns `dict` or `None`. |
| [pms/scraper.py](ma_poc/pms/scraper.py) | `_PROPERTY_CONCESSION_RE`, `_capture_concession_from_html`, `_SPECIALS_PATHS`, `_probe_specials_pages` (with `max_paths`, NOT_MODIFIED fallback, telemetry emits). |
| [fetch/probe.py](ma_poc/fetch/probe.py) | `stealth_probe` — slim L1-equivalent for adapter probes with custom headers / POST / short timeouts. |
| [extraction/vision_banner.py](ma_poc/extraction/vision_banner.py) | Vision-LLM banner fallback. Lazy + env-gated. |
| [pms/adapters/generic.py](ma_poc/pms/adapters/generic.py) | `_probe_realpage_cws` + `_probe_beacon_ajax` — wired through `stealth_probe` with telemetry contexts. |
| [core/schema_v2.py](ma_poc/core/schema_v2.py) | Emits the trio (raw + clean + quality + structured) at unit AND property level. Prefers vision-LLM structured output when present. |
| [scripts/runners/jugnu.py](ma_poc/scripts/runners/jugnu.py) | Property-record surface for `concessions_clean` / `_concessions_quality` / `concessions_structured` / `concessions_source_url`. |
| [data_provider/sql/stores.py](ma_poc/data_provider/sql/stores.py) | `_bundle_unit_concessions` — bundles the trio into `units.concessions` JSON column. |
| [scripts/email/daily_failures.py](ma_poc/scripts/email/daily_failures.py) | xlsx export read-chain. |
| [scripts/diagnostics/concession_canary.py](ma_poc/scripts/diagnostics/concession_canary.py) | Standalone canary: fetches ~12 property home pages and runs the full pipeline. No DB, no Playwright. |
| [tests/core/test_concession_clean.py](ma_poc/tests/core/test_concession_clean.py) | 29 tests — quality classifier + cleaner invariants. |
| [tests/core/test_concession_normalize.py](ma_poc/tests/core/test_concession_normalize.py) | 27 tests — offer-shape coverage + no-match → None. |
| [tests/core/test_concession_pipeline.py](ma_poc/tests/core/test_concession_pipeline.py) | 32 tests — end-to-end raw-preservation invariant + real-property fixtures. |
| [tests/pms/test_concession_capture.py](ma_poc/tests/pms/test_concession_capture.py) | 20+ tests — script-strip, sentence-extend, /specials probe stealth + cap + telemetry. |
| [tests/fetch/test_probe.py](ma_poc/tests/fetch/test_probe.py) | `stealth_probe` invariants — stealth headers, sticky identity, captcha detect, telemetry. |
| [tests/data_provider/test_concession_bundle.py](ma_poc/tests/data_provider/test_concession_bundle.py) | 7 tests — JSON-column bundle shape + raw-fallback when structured is None. |

### 18.7 Decision tree: how to handle a concession bug report

```
Property X shows blank concessions in the daily xlsx
│
├─ Q14: is properties.json[X].concessions populated?
│  │
│  ├─ YES → xlsx export bug. Check daily_failures.py read-chain (§18.3.6).
│  │        Confirm concession_text_clean → concession_text → concessions order.
│  │
│  └─ NO → capture missed. Continue.
│
├─ Q15: any concession event in events.jsonl for this PID?
│  │
│  ├─ extract.concession_probe.result outcome=found → schema_v2 emit failed.
│  │        Inspect concession_text vs concessions field name (unit vs property).
│  │
│  ├─ outcome=exhausted → probe didn't recover. Live-fetch the homepage:
│  │        - If banner copy IS in the HTML → _PROPERTY_CONCESSION_RE missed
│  │          the phrase shape. Add to the regex.
│  │        - If banner is on /specials but not in probed paths → add path
│  │          to _SPECIALS_PATHS OR raise max_paths for this domain.
│  │
│  ├─ outcome=all_blocked → domain is captcha-walled on the probe.
│  │        Inspect extract.hop.captcha_detected events for provider.
│  │        If cloudflare → consider stealth-tier escalation
│  │        (residential proxy + Playwright RENDER) for this host.
│  │
│  └─ no event → capture never invoked. Check that scrape() reached the
│        property-level capture block (page_html was non-empty, no
│        exception before line ~570).
│
└─ Q16: vision banner fired?
   │
   ├─ Property has VISION provider env vars set → grep for
   │    "vision_banner screenshot failed" or "vision_banner call failed"
   │    in logs; provider may be erroring.
   │
   └─ No env vars set → vision is opt-in, this is expected. Land env vars
        in production to enable image-only banner capture.
```

---

## Phase 19 — RentCafe / AppFolio data-quality gaps (2026-05-20 cloud run)

The 2026-05-20 cloud run audit surfaced a class of SUCCESS-with-junk-data
failures that the existing verdict layer treats as wins. The audit walked
2,367 RentCafe properties (47.5% of the run) plus 42 AppFolio properties;
the findings + fixes are below. Telemetry shipped 2026-05-20 lets the
analyzer track these going forward — pre-2026-05-20 cloud data has zero
date-shape signals and won't classify under §19.1's sub-cause split.

### 19.1 The avail-date-only-gap (406 RentCafe SUCCESS properties, 18.8%)

Properties that ship SUCCESS with bed/bath/area/rent/floor_plan_name all
populated AND `available_date` null on >50% of units. Pre-fix, every gap
property looked identical in dashboards; T1+T2+T3 split the cohort into
five sub-causes with distinct fix paths:

| Sub-cause | Definition | Fix |
|---|---|---|
| A — `API_FLOORPLANS_ONLY` | `TIER_1_API_*` won; per-unit endpoint never captured | **F7a** OneSite `/units` probe (shipped 2026-05-20) — `pms/adapters/onesite.py::_probe_realpage_units_endpoint`. Mark-Taylor + Entrata analogues deferred |
| B — `PAGE_NO_DATES` | Zero date signals in any captured HTML | **F7b** accept; per-unit info gated behind portal (CF-blocked SecureCafe). Surfaced as `DATE_GAP_PAGE_NO_DATES` issue (INFO severity) |
| C — `LLM_SECTION_MISSED` | `TIER_4_LLM_DOM` won; dates exist in HTML | **F7c** LLM section widening (shipped 2026-05-20) — `_widen_to_include_date_column` in `pms/adapters/generic.py` |
| D — `DOM_ATTRS_IGNORED` | `TIER_1_API_*` won; `data-availability` attrs exist | **F7d** OneSite DOM augmentation (shipped 2026-05-20) — `pms/adapters/onesite.py::_augment_units_with_dom_availability`. Mark-Taylor analogue deferred |
| E — `AVAILABLE_NOW_NO_FALLBACK` | "Available Now" text seen, no ISO date, fill < 50% | Investigate — likely alias drift or producer-side regression |

### 19.2 RentCafe `fp-container` data-attr extractor (~40 props, F2 + F3)

**Live evidence:** PID 60578 (1105townbrookhaven-apts.com), PID 10141
(wymberlycrossing), PID 73715 (sussexwestlife). The `/floorplans` page
renders 19 `<div class="fp-container" id="fp-container-NNN">` plan
cards, each carrying `data-floorplan-name` / `-size` (beds) / `-sqft` /
`-price` (LO-HI range, or "0" placeholder) attributes on descendant
Apply / Guided-Tour buttons.

**Why pre-fix missed it:** `_DOM_CONTAINER_SELECTORS` had `.fp-card` /
`.floorplan-card` / `.floorplanItem` but NOT `.fp-container`; the cascade
fell through to `.floorplan` (40 inner elements without rent attrs).
And `grep -nrE "data-floorplan-(price|name|size|sqft)" ma_poc/` returned
zero hits — no adapter read these canonical RentCafe carriers.

**Fixes shipped 2026-05-20:**
- `.fp-container` + `[id^='fp-container-']` selectors added to
  `_DOM_CONTAINER_SELECTORS` in `pms/adapters/_html_extract.py:1648`
- `_extract_rentcafe_data_attrs` extractor parses the four data attrs,
  rejects price="0" sentinel, wired into `_COMPACT_ROW_EXTRACTORS`.
- Test suite: `tests/pms/adapters/test_compact_row_extractor.py::TestRentCafeFpContainerDataAttrs`.

### 19.3 RentCafe Interactive Property Map (F5, best-effort)

**Live evidence:** PID 1973 (rosslynheights.com), PID 231711
(williamsburgmishawaka.com). URL path is `/interactivepropertymap`
(NOT `/interactivecommunitymap` which is the SightMap embed). DOM
uses `.nu-floor-plan` containers with `.min-rent` / `.max-rent` /
`.unit_price` / `.popover-price`.

**Status:** Class-name presence confirmed via spot-check agent grep,
NOT via live structural confirmation. `_extract_rentcafe_ipm_card` in
`pms/adapters/_html_extract.py` requires a parseable rent value before
admitting the row — conservative gate prevents false positives if
class names appear incidentally. **Must run a 5-property canary
against `/interactivepropertymap` URLs before broad rollout.**

### 19.4 AppFolio SSR `_ADDRESS_RE` regex break (F1, 42 props / 3,037 units)

**Live evidence:** PID 67736 (live210main.com → meridiapm.appfolio.com),
0/300 addresses parsed on the live page.

**Why pre-fix missed it:** The regex required a nested wrapping tag
(`>\s*<TAG>([^<]+)<`). Real AppFolio templates put the address text
directly inside the span: `<span class="u-pad-rm js-listing-address">3749 Arbor Green Way</span>`.
Pre-fix `parse_appfolio_listings_ssr` fell back to
`floor_plan_name=f"AppFolio listing {listing_id}"` — a unique-per-unit
junk string. bed/bath/sqft/rent/availability ARE extracted correctly;
only address + plan_name are lost.

**Fix shipped 2026-05-20:** optional middle-tag group
`(?:<[^>]+>)?` accepts both shapes. Test pin:
`tests/pms/adapters/test_appfolio_f11.py::test_f1_ssr_parser_extracts_meridiapm_direct_text_address`.
**Sibling regex audit:** `_RENT_RE` / `_BED_BATH_RE` / `_SQFT_RE` /
`_AVAIL_RE` at `appfolio.py:128-131` all use direct-text `>([^<]+)<`
already — no nested-tag assumption to fix.

### 19.5 SecureCafe portal demote (F8a, ~1,461 props recover or speed up)

**Live evidence:** RentCafe vanity sites synthesise `*.securecafe.com`
URLs as embedded-portal candidates at score 10120 (10_000 base + 120
host-keyword bonus). The hop hits Cloudflare ~80%+ of the time. F2+F3
showed that the marketing `/floorplans` page typically has the same
data SecureCafe gates — but link-hop spent the first slot on
SecureCafe before discovering the marketing path.

**Fix shipped 2026-05-20:** SecureCafe URLs cap at score 9_000 in
`pms/scraper.py::_extract_portal_iframe_hints` synthesis, below
`profile:winning_page_url` (10_001) AND `profile:availability_links`
(10_000). The portal still gets crawled if no other candidate
succeeds — just not first.

### 19.6 OneSite DOM `data-availability` augmentation (F7d, ~9 props)

**Live evidence:** PID 11317 (dixonatstonegate.com). 11 plan-level units
emitted from `/floorplans` API; 11 `data-availability` attrs in the
rendered DOM matching them 1:1; zero dates merged pre-fix.

**Fix shipped 2026-05-20:** `_augment_units_with_dom_availability` in
`pms/adapters/onesite.py` scans the captured page HTML for
`data-availability` / `data-available-date` / `data-move-in-date` /
`data-ready-date` attrs, pairs them with the same-element
`data-unit-id` / `data-unit-number` / `data-apartment-id` /
`data-listing-id`, and fills `availability_date` on matching units
**non-destructively** (API-set dates are authoritative — DOM only
fills empties). Tested in
`tests/pms/adapters/test_onesite_dom_augmentation.py` (10 cases).

### 19.7 MAA embedded-JSON price aliases (F6, best-effort, ~23 props)

**Live evidence:** PID 218985 (maac.com/.../maa-trinity) + 22 sibling
MAA tenants. The `TIER_1_5_EMBEDDED` walker found unit lists (44 rows
on 218985) but `rent_low=null` for every row.

**Status:** maac.com Cloudflare-blocks ad-hoc IPs so I couldn't live
forensic the exact key path. Added a batch of probable MAA / Cortland /
Bell aliases to `FIELD_ALIASES` at
`pms/signal_engine/floor_plan_signals.py`: `loweffectiverent`,
`lowestrent`, `lowmarketrent`, `highesteffectiverent`, `highmarketrent`,
`effectiverent`, `netrent`, `marketrent`, etc. **Must run a 5-property
canary against a known MAA tenant before broad rollout.** If a key
doesn't match the live payload the alias is a no-op; if it matches it
recovers rent.

### 19.8 Telemetry shipped 2026-05-20 (T1 / T2 / T4 / T5 / F8b)

Six observability changes to make the next round of triage faster:

| Telemetry | Source site | Consumer |
|---|---|---|
| **T1** — `date_iso_count` / `date_us_count` / `date_named_count` / `available_now_count` / `move_in_keyword_count` / `data_avail_attr_count` on `extract.html_characterized` | `pms/scraper.py::_characterize_html` | T3 analyzer + cross-event aggregation |
| **T2** — `extract.date_presence_summary` per-property roll-up | `scripts/runners/jugnu.py::_emit_date_presence_summary` | T3 analyzer |
| **T3** — `Date-completeness sub-cause split` section in `summary.md` | `scripts/diagnostics/analyze_cloud_run.py::classify_date_gap` | manual triage |
| **T4** — `DATE_GAP_PAGE_NO_DATES` issue (INFO severity) | `scripts/runners/jugnu.py` (inside `_emit_date_presence_summary`) | `jq 'select(.code=="DATE_GAP_PAGE_NO_DATES")' issues.jsonl` |
| **T5** — `extract.date_extraction_drop` when v2 formatter sees a date-shaped value under an unknown key | `scripts/runners/jugnu.py::_format_v2_unit` | weekly alias-drift report |
| **F8b** — `extract.rent_gated_by_portal` when null-rent SUCCESS coincides with SecureCafe CF block | `scripts/runners/jugnu.py::_emit_date_presence_summary` | dashboard split |

### 19.9 State Diff is no longer hard-coded zero (F4)

`scripts/reports/per_property.py::_phase7_section` now suppresses the
State Diff section entirely when the caller passes an all-empty
`unit_diff` dict (which is the current Jugnu reality — the real diff
lives in a different scope). Pre-fix every property report rendered
"State Diff: new=0, updated=0, unchanged=0, disappeared=0" which
misled every debugging session that read it (the per-property report
on PID 67736 / 2026-05-20 cost real triage time before the misdirection
was caught). Companion guard in `_summary_box` skips the State Diff
table row too.

### 19.10 Anti-pattern #17 — `urllib` lies about JS-hydrated PMS sites

**What I did wrong on 2026-05-20:** Concluded "no rent on this page"
from a `urllib.request.urlopen` fetch of
`1105townbrookhaven-apts.com/floorplans` and reported the property as
SecureCafe-CF-gated. The user pushed back; a Playwright render showed
19 fp-container cards with `data-floorplan-price="1660-2199"` in plain
attributes. The page IS public, our extractor missed the carriers.

**What to do instead:** Any "page has no X" claim that drives a fix
decision must use Playwright (`networkidle` + scroll). RentCafe / G5 /
modern PMS sites are JS-hydrated; urllib gets the shell only. AND grep
for `data-*` attributes alongside visible text — modern PMS templates
push canonical values into data attributes for analytics tracking.

The Playwright snippet in §14 "Frame enumeration snippet" is the right
starting template; for data-attr verification add:
```python
import re
attrs = sorted(set(re.findall(r'data-([\w-]+)=', html)))
print('data-* attrs:', attrs[:30])
```

### 19.11 Next-week priorities (queued after the 2026-05-21 deploy)

The fixes above ship together; queue these for the next post-deploy
canary cycle (in priority order):

| # | Priority | Investigation / fix |
|---|---|---|
| 1 | **MUST** | Validate F5 (Interactive Property Map) against 5 live `/interactivepropertymap` properties — confirm `.nu-floor-plan` is the right container class on rosslynheights + 4 siblings. Without this, F5 is a no-op or worse. |
| 2 | **MUST** | Validate F6 (MAA aliases) against a known MAA tenant page. If maac.com still CF-blocks the canary box, route the live fetch via the residential proxy that the cloud worker uses. Expected outcome: rent_low/rent_high populated on PID 218985 + ≥20 sibling MAA properties. |
| 3 | **MUST** | Re-run `analyze_cloud_run.py --date <next-run>` and verify the new `## Date-completeness sub-cause split` section populates. If B is the dominant bucket (≥50% of the avail-gap cohort), accept and document; F7b. If C is dominant, F7c shipped — the canary should show the cohort shrinking. |
| 4 | should | Implement F7a analogues for **Mark-Taylor** (3 props, `mark-taylor.com` API pattern needs forensic) and **Entrata** (1 prop, widget endpoint URL synthesis). The OneSite probe shipped 2026-05-20 covers ~9 properties; the analogues cover ~4 more. |
| 5 | should | Investigate the **143 RentCafe `SUCCESS_PLAN_LEVEL` properties** — these ship zero per-unit rows by design. Check whether `extraction.post_process.classify` is over-aggressively demoting per-unit rows to `plan_summaries`. The playbook §8.20 (AVAILABLE+rent promotion) was the last major fix; a 143-property cohort suggests more rows are demoted than should be. |
| 6 | should | Investigate the **275 multi-field-gap RentCafe properties** — not surfaced in any of §19.1's buckets because they have ≥2 fields below threshold. 67 are TIER_3_DOM, 68 have no recorded tier. Likely a mix of cascade-pickup failures. |
| 7 | should | Implement **F8c residential proxy for SecureCafe** if post-F8a metrics show SecureCafe still dominates the avail-gap cohort. Don't ship preemptively — F8a's demote + F2/F3's marketing-site extraction may eliminate the need. |
| 8 | nice-to-have | Investigate the **578 minor-gap RentCafe properties** (single non-rent / non-avail field <95%). Long tail; probably each has its own small story. |
| 9 | nice-to-have | Aggregate `extract.date_extraction_drop` events weekly to drive the alias table additions (T5 — analogous to the existing rent-key alias-drift report at `extract.signal_inspection`). |
| 10 | nice-to-have | Audit AppFolio sibling regexes (`_RENT_RE`/`_BED_BATH_RE`/`_SQFT_RE`/`_AVAIL_RE` at `appfolio.py:128-131`) against the meridiapm fixture — they LOOK fine (direct-text form already) but a deliberate live grep against 5 AppFolio tenants would close out the regex-shape risk for the cohort. |

---

## Phase 20 — Cross-origin proxy gap + platform-wide adapter telemetry (2026-05-22)

The 2026-05-21 post-merge audit surfaced one structural failure mode that ate almost every benefit of the May-13 feature-branch merge to main, plus three concrete adapter-level fixes whose root causes had been invisible for weeks. The investigation also produced the first **platform-wide per-adapter telemetry** — events.jsonl now carries an `adapter_exit` event for every PMS adapter dispatch, regardless of which adapter was selected, so future failures of this class can be diagnosed in 5 minutes instead of 4 hours.

### 20.1 The discovery — same code, 4.5× different outcome by env var

The May-13 feature branch ported a RentCafe SecureCafe-portal drill-down (`_try_rentcafe_securecafe_probe` at [rentcafe.py:903+](../pms/adapters/rentcafe.py#L903)) that was supposed to recover ~1,000 of the 1,738 RentCafe-detected properties currently falling through to the LLM_DOM cascade. Post-merge, the cloud run on 2026-05-21 showed **0** `TIER_1_API_RENTCAFE_SECURECAFE` wins across 1,885 RentCafe-detected properties, vs **259** wins on a proxy-enabled canary (`jugnu-canary-failedstrict-6b30f18`) running an *older* image on a *harder* property basket. Same code shape. Same adapter. Same scrape pipeline. The only difference between the two runs:

| Job | `PROBE_PROXY_URL` | SecureCafe wins | RentCafe-Nestin wins |
|---|---|---:|---:|
| `jugnu-adhoc-production` | **not set** | 0 / 1885 | 319 |
| `jugnu-canary-failedstrict-6b30f18` | set (BrightData secret) | 259 / 2561 | 124 |

The Nestin path stayed working in both because it uses `page.evaluate(fetch())` from the patchright browser session (which already cleared CF on the property's *own* origin). The SecureCafe / WP / Hosted probes all use `curl_cffi` via `_probe.py:probe_get` which goes through `PROBE_PROXY_URL` when set, or direct from the Cloud Run egress IP when not. Direct GCP-egress requests to `*.securecafe.com` get a CF challenge interstitial 100% of the time.

### 20.2 The proof — Test A / A2 / C probe-experiment from a real Cloud Run egress

I shipped `ma_poc/scripts/diagnostics/asn_ipv6_probe.py` and overrode the `canary-introspect` Cloud Run job to run it inside the same egress path as `jugnu-adhoc-production`. The probe ran 20 SecureCafe URLs that succeed locally with curl_cffi + chrome120 impersonation, plus 5 control URLs (Cortland/Irvine/AvalonBay/Apts247/Google), plus an IPv6 lookup on every SC hostname.

**Egress identity confirmed**: IPv4 `136.124.32.68`, ASN `AS396982 Google LLC`, ISP "Google Cloud (us-central1)".

| Test | Result | Interpretation |
|---|---|---|
| **A** — 20 SC URLs over IPv4 from GCP | **20/20 `blocked_403`** | uniform 6,207-6,293 byte body, 12 CF challenge markers per row, `server: cloudflare` on every response, latency p50=88.5 ms (consistent with CF edge POP, not origin) |
| **A2** — 5 control URLs from GCP | google.com 200, avalonbay.com 200, cortland.com 200, **irvinecompanyapartments.com 403**, apts247.com timeout | proves the probe stack works against most hosts; irvine's homepage is also CF-blocked from GCP but the brand adapter doesn't hit the homepage — see §20.3 |
| **C** — IPv6 reachability | **0/20 hosts have AAAA records** | no v6 escape hatch on the SecureCafe tenant |

Tightness of the 6.2KB body distribution + sub-100ms latencies + uniform CF marker counts → the blocks are happening at CF's edge based on IP-or-ASN reputation, not at the origin Yardi server. Closing details in `c:/tmp/probe_test_a.jsonl` (preserved at `gs://jugnu-canary/diagnostics/asn_ipv6_probe/test-a-2026-05-22/results.jsonl`).

### 20.3 The cross-origin clearance asymmetry — why Cortland/Irvine succeed but SecureCafe fails over the SAME GCP egress

This is the single most-important insight from the investigation, and it directly explains why the platform's "GCP IPs are blocked" surface narrative is wrong:

The brand-API adapters (Cortland, Irvine, AvalonBay, MAAC, Apts247) succeed from GCP **not** because their hosts treat GCP IPs better, but because they reuse the CF clearance cookies that patchright already minted during the property's entry-page fetch. The Irvine adapter at [irvine.py:202](../pms/adapters/irvine.py#L202) calls `probe_get(base, ...)` against the community page on the *property's own marketing origin*, then `probe_post(_RANK_URL, ...)` against `search.irvinecompanyapartments.com` — and the `_with_clearance` helper at [_probe.py:73-86](../pms/adapters/_probe.py#L73-L86) automatically attaches the cookies the patchright render established.

The SecureCafe drill hits `<sub>.securecafe.com/onlineleasing/<slug>/availableunits.aspx` — a **different CF zone** from the property's marketing site. The clearance cookies minted by patchright for `www.theblackhawkapartments.com` don't apply to `theblackhawkapartments.securecafe.com`. The probe arrives unauthenticated against Yardi's CF zone and gets the 403 interstitial.

This is the same reason the **Entrata ProspectPortal probe** needs a residential proxy: `havenatsouthmountainapts.com` (marketing site) and `havenatsouthmountain.prospectportal.com` (data origin) are separate CF zones; the clearance doesn't transfer.

**Rule of thumb**: any adapter that probes a **cross-origin** endpoint via `probe_get`/`probe_post` from production needs `PROBE_PROXY_URL` set. Any adapter that stays on the property's own origin or sub-paths works without it.

### 20.4 The fix — `_adapter_telemetry.py` shared module + platform-wide `adapter_exit`

Pre-2026-05-22, every PMS adapter returned silently on failure. `events.jsonl` carried zero `extract.tier_attempted` events from inside the adapter, so an `_log_attempt("rentcafe:sc_probe", "ok")` event looked indistinguishable from "the SecureCafe probe never fired" or "fired and CF-blocked it" or "fired and parser dropped the units." Diagnosing the 2026-05-22 regex bug took 4+ hours of manual fetching. With the new telemetry, the same bug would be ONE jq query against a future run.

Three building blocks at [`ma_poc/pms/adapters/_adapter_telemetry.py`](../pms/adapters/_adapter_telemetry.py):

| Helper | Purpose | Emit shape |
|---|---|---|
| `log_adapter_stage(adapter, pid, stage, outcome, **kw)` | Per-stage event. Fires once per recovery path attempted. | `extract.tier_attempted` with `tier_key=<adapter>:<stage>`, plus `via_proxy`, `via_unlocker` env-derived booleans, `ran_units`, `reason`. |
| `log_adapter_diag(adapter, pid, stage, body, **extra_signals)` | Raw structural signal dump on silent-empty parser failures. | Same envelope, `tier_key=<adapter>:<stage>_diag`, payload carries `signal_caption_samples`, `signal_heading_samples`, `signal_table_ids`, `signal_data_label_inventory`, `signal_floorplan_ids_seen`, `signal_first_row_ctx`, `signal_vendor_markers`, `signal_cf_marker_counts`. |
| `classify_probe_body(status, body, success_marker=...)` | Categorises probe responses into `ok` / `cf_challenge_shell` / `blocked_status_403/429/503` / `no_success_marker` / `status_NNN`. | Returns `(outcome, reason)` tuple; callers pass to `log_adapter_stage`. |

Platform-wide `adapter_exit` lives in [`scraper.py:933+`](../pms/scraper.py#L933) at the adapter-dispatch site. Every adapter — instrumented or not — emits one `adapter_exit` event per dispatch with `tier_used`, `units`, `plan_summaries`, `confidence`, and a one-line error summary. New adapters get this for free without writing any telemetry code.

### 20.5 Per-adapter stage reference — what events.jsonl now carries

After 2026-05-22 the playbook's diagnostic workflow (§3 Q-questions) gains a new tier_attempted event-type set. Adapters with per-stage instrumentation (as of 2026-05-22):

| Adapter | Stages | Notes |
|---|---|---|
| `rentcafe` | `xhr_capture`, `wp_property_id`, `wp_probe`, `wp_parse`, `sc_search`, `sc_homepage_refetch`, `sc_probe`, `sc_parse`, `hosted_dom`, `nestin_recover`, `cascade_exit` | + `*_diag` variants on parser-silent-empty paths |
| `g5` | `urn_pick`, `graphql_fetch`, `apollo_fallback`, `cascade_exit` | `urn_pick` carries `urn_picked` (chosen URN literal), `urn_cdn_anchored` (boolean — did the deterministic anchor fire), `urn_total`/`urn_distinct` (candidate universe) |
| `onesite` | `xhr_capture`, `cascade_exit` | `floorplans_matched` + `units_matched` counts + `realpage_property_id` |
| `realpage_oll` | `xhr_capture` | `n_workflow_matched`, `n_floorplans_matched`, `n_units_matched` |
| `entrata` | `prospectportal_probe` | One event per link-hop iteration; `outcome=ran_empty` when PP iframe missing OR probe returned 0 rows |
| `sightmap` | `xhr_capture` | `total_raw_units` + `join_dropped` (for the partial-join SLO check) |
| `apts247` | `origin_resolve`, `api_key_resolve`, `api_fetch` | `key_source` indicates `static_body` vs `homepage_refetch` |
| `appfolio` | `xhr_capture` | `matched_shape` count |
| `knock` | `ids_search` | `has_static_init` boolean |

**Diagnostic-first query**: filter `events.jsonl` for the `tier_key` field starting with `<adapter>:`. Each event has `via_proxy` and `via_unlocker` booleans — the single most-actionable signal added in this update.

### 20.6 SecureCafe new-template regex relaxation (5 PIDs lost units silently for weeks)

Live-verified 2026-05-22 on PIDs 72944 / 24561 / 6550 / 40584 / 67750 — each `availableunits.aspx` page had 4-17 `AvailUnitRow` rows but ZERO header matches under the old `_SECURECAFE_FP_HDR_RE` regex. The newer SecureCafe template wraps the floor-plan grouping in:

```
<caption class="sr-only">Apartment Details and Selection for Floor Plan: 2 Bed - 1 Bath - 2 Bedrooms, 1 Bathroom</caption>
```

The visual-name segment `2 Bed - 1 Bath` contains literal dashes, which the previous `[^<\-]` character class rejected. Fix at [rentcafe.py:790-808](../pms/adapters/rentcafe.py#L790-L808) is a one-character relaxation: `[^<\-]` → `[^<]`. The trailing `- N Bedroom[s], N.N Bathroom` anchor still bounds the non-greedy capture so the regex can't slide past the bed/bath suffix.

PID 119798 (829garfield) — which previously parsed 0 units locally — now parses **13**. The 5 sample PIDs each have 2-25 floor-plan group headers under the new regex.

### 20.7 G5 deterministic URN picker — Cloudinary CDN anchor replaces `max(matches, key=len)`

The G5 adapter's URN selector at [g5.py:92+](../pms/adapters/g5.py#L92) shipped wrong-property data for 5/5 live-verified sample PIDs because the old `max(matches, key=len)` heuristic picks parent-company switcher URNs or sibling-property URNs in preference to the property's own URN. Live evidence:

| Property | Old (longest) | Live API result |
|---|---|---|
| Anson Burlingame CA | `…-anson-gaithersburg-md` | shipped MD data for a CA property |
| Central Park Park Forest IL | `…-saint-petersburg-fl` | wrong sibling |
| Brook Hollow Wichita Falls TX | `g5-clw-h59cwfh0t6-brook-hollow-eb883a…` | 404 — `g5-clw-` variant is a parent-company switcher, not an inventory URN |
| Ten68 West | `g5-clw-gqgzrdf1jy-ten68-west-…` | 404 |
| Westgate Village | `g5-clw-6pncm85-first-montgomery-…-hash` | 404 |

**Deterministic anchor**: G5's CMS routes every asset upload to the tenant's company+property folder under `/g5/g5-c-<companyId>/g5-cl-<propertyUrn>/uploads/`. The favicon / og:image / apple-touch-icon URLs all reference THIS property's folder; sibling URNs in switcher menus live under their own `g5-c-…` folder. Pattern at [g5.py:98-103](../pms/adapters/g5.py#L98-L103):

```python
_G5_CDN_PROPERTY_RE = re.compile(
    r"/g5/g5-c[a-z]?-[a-z0-9]+/(g5-cl[a-z]?-[a-z0-9-]+?)/uploads/",
    re.IGNORECASE,
)
```

Three-step picker at [g5.py:105-160](../pms/adapters/g5.py#L105-L160): (1) Cloudinary CDN regex, (2) frequency tie-break inside CDN matches, (3) fallback to most-frequent `g5-cl-*` anywhere on the page. Live-verified 5/5 correct on the same sample where `max(len)` was 0/5.

**Open question from the 2026-05-22 canary**: `urn_cdn_anchored=False` on all 3 canary G5 PIDs — meaning the CDN regex didn't fire in the patchright-rendered HTML, but the most-frequent fallback still picked the right URN. The CDN anchor probably needs a tweak to handle the rendered DOM (Cloudinary URL is in a `<link rel="icon">` tag that may not survive Vue/React hydration into patchright's content snapshot). Tracked as nice-to-have follow-up.

The adapter also gains **rendered-HTML access** at [g5.py:264-284](../pms/adapters/g5.py#L264-L284) — was reading only `ctx.fetch_result.body` (static SSR), now pulls `page.content()` first via the same `_get_page_html` helper GenericAdapter uses. This fixes the case where the URN is JS-injected and absent from SSR.

### 20.8 OneSite negative gate — `static2.apts247.info` / `doorway.knck.io` competing CDNs

The OneSite STRONG-marker detector branch at [detector.py:500+](../pms/detector.py#L500) fired on any page containing the bare substring `onlineleasing.realpage.com`, routing 80/312 OneSite-detected production properties to TIER_4_LLM_DOM because many Apts247 / apartments247.com / Knock-fronted sites carry a stale "Apply Now" anchor pointing at `*.onlineleasing.realpage.com` from a previous platform.

Live-verified 2026-05-22 on 3 misroute + 3 real-OneSite PIDs + 3 validation PIDs: `static2.apts247.info` (the Apts247 widget JS CDN) is present 22/22/22 times on misroutes and 0/0/0 on real OneSite. Same separation for `apartments247_api.min.js`. The Apts247 widget cannot legitimately appear on a real RealPage portal page; it only loads on apartments247.com-template sites.

```python
# detector.py — inside the existing "if 'onlineleasing.realpage.com' in h" block
_has_apts247_widget = "static2.apts247.info" in h or "apartments247_api.min.js" in h
_has_knock_loader   = "doorway.knck.io" in h or "knockdoorway" in h
_has_competing_primary_for_knock = (".securecafe.com" in h or ...)

if _has_knock_loader and not _has_competing_primary_for_knock:
    return "knock", 0.85, [...]                       # short-circuit Knock
if not (_has_apts247_widget or _has_knock_loader):
    return "onesite", 0.85, [...]                     # real OneSite
# apts247-widget case falls through to the apts247 branch downstream
```

**One gotcha caught mid-canary**: simply skipping the OneSite return for the Knock-loader case lets the page fall through to the `realpage_oll` branch (which lives ABOVE the Knock branch in source order). PID 19245 (tenzenapartments.com) demonstrated this — went from production's `TIER_4_LLM` (4 units) to canary's `TIER_1_API_REALPAGE_OLL` (0 units). The fix is to EXPLICITLY return `"knock"` from inside the OneSite block when the Knock loader is present, not just skip the OneSite return. Captured in test `test_onesite_negative_gate_knock_doorway_routes_to_knock` and companion `test_onesite_knock_demotion_yields_when_competing_primary_pms`.

### 20.9 Entrata ProspectPortal probe — restored from git history (8b1bfa4 → reverted in 4c9dbf8)

The `_probe_prospectportal` helper + its call site existed at commit `8b1bfa4` (2026-05-18 springriver canary) but was lost in the regression-revert `4c9dbf8`. The parser (`parse_prospectportal_unit_spaces` at [entrata.py:360](../pms/adapters/entrata.py#L360)) and the URL-component regexes (`_PP_HOST_RE`, `_PP_PROPID_RE`, `_PP_FPID_RE` at [entrata.py:342-349](../pms/adapters/entrata.py#L342-L349)) survived the revert — only the orchestrator method went missing.

Live-verified 2026-05-22 on 5 sample PIDs (19939, 243704, 30775, 34777, 297737): every marketing page embeds exactly one `*.prospectportal.com` iframe whose `src` carries `property[id]={pid}` baked into the query string. The iframe alone yields `(host, property_id)` deterministically — no slug needed. Probe URL family:

```
landing:   https://{host}/?module=check_availability&is_secure=1
per-fp:    https://{host}/?module=check_availability&is_secure=1
           &property[id]={pid}&action=view_unit_spaces
           &property_floorplan[id]={fpid}&move_in_date={today}
           &occupancy_type=conventional
```

Restored at [entrata.py:614-687](../pms/adapters/entrata.py#L614-L687), wired into `extract()` at line 540 with per-stage `prospectportal_probe` telemetry emitted on every call (whether it found rows or not — `via_proxy` flag tells you whether CF blocked it).

**Cloudflare-fronted** — requires `PROBE_PROXY_URL=brightdata` to bypass. Without it, the probe emits `outcome=ran_empty via_proxy=False` events that surface the gate explicitly in events.jsonl.

### 20.10 The RentCafe→SightMap misroute hypothesis — DISPROVED by live verification

A prior agent investigation proposed a detector co-occurrence rule for the 385 RentCafe→SightMap misroutes: when entry HTML has BOTH `securecafe.com/onlineleasing` AND a SightMap embed/api URL, demote to sightmap (mirroring the existing entrata→sightmap rule at [detector.py:540-557](../pms/detector.py#L540-L557)).

**Live-verified 2026-05-22 on 6 sample misroute PIDs (118750, 119144, 1994, 217358, 21589, 218580): 6/6 have the RentCafe portal marker in entry HTML. 0/6 have any SightMap signal in entry HTML.** SightMap is discovered only at link-hop depth ≥ 2-3 — per earlier event-log analysis on PID 1994, SightMap's API body arrives at hop_index=3, well after the detector's confirm_detection cycle completes.

A co-occurrence detector rule would fire on **zero** of these 385 properties. The proposal was a no-op disguised as a fix.

**What WOULD work**: hop-aware re-detection — re-run `detect_pms` on each link-hopped HTML body and re-classify if a stronger marker appears later in the cascade. This is a structural change to the scrape lifecycle, not a 15-line detector rule. **Deferred.** The telemetry added in §20.4 (specifically the platform-wide `adapter_exit` events + the `signal_vendor_markers` field in diagnostics) will capture this pattern in production from the next run, giving us the data to design the re-detection properly when we pick it back up.

### 20.11 New anti-patterns (#18 - #20)

| # | Anti-pattern | What I did wrong | What to do instead |
|---|---|---|---|
| **18** | **Trusting an agent's proposed fix without live verification** | Believed the earlier agent's "co-occurrence rule will fix the 385 misroutes" claim. Would have shipped a no-op. Live-fetched 6 sample PIDs and found 0 of them had the SightMap signal in entry HTML. | After any agent proposes a detector rule, fetch ≥5 sample PIDs with curl_cffi chrome120 and grep for the proposed signals BEFORE writing the code. If 5/5 are missing, the rule won't fire — investigate why before shipping. |
| **19** | **Cross-host clearance asymmetry** | Assumed brand-API adapters working from GCP meant probe_get works for any host from GCP. Reality: brand adapters reuse patchright clearance for the property's *own* origin; cross-origin probes (SecureCafe, ProspectPortal) get no clearance and CF-403. | Any adapter that probes a host different from the property's marketing origin needs `PROBE_PROXY_URL` set in production. Check by URL host comparison — if probe URL host ≠ marketing-site host, mark the adapter "proxy-dependent" in the file docstring. |
| **20** | **Detector branch ordering — fall-through reaches the wrong adapter** | Wrote a negative gate that SKIPPED the OneSite return when a Knock-loader was present, expecting the page to fall through to the Knock branch later in `_detect_html_markers`. But the RealPage OLL branch (line 512) lives BETWEEN OneSite (line 500) and Knock (line 655), so the page fell into RealPage OLL and stopped. | When introducing a negative gate that demotes one PMS in favor of another, EXPLICITLY return the intended PMS literal from inside the gate. Don't rely on source-order fall-through; the order is fragile and unreviewable. The fix: `if _has_knock_loader: return "knock", 0.85, [...]` instead of `pass`. |

### 20.12 The diagnostic-from-events.jsonl workflow

The 2026-05-22 SecureCafe regex bug took 4+ hours to find: had to live-fetch 5 PIDs, dump HTML, eyeball the new template, hand-write a regex. With the new platform-wide telemetry, the same bug would surface as the following query against the NEXT run's events.jsonl:

```python
# 1. Find rentcafe-detected props where sc_parse silently returned empty
import json
from pathlib import Path
parse_fails = []
for shard in sorted(Path("c:/tmp/run-2026-05-22").iterdir()):
    ev = shard / "events.jsonl"
    if not ev.exists(): continue
    for line in ev.read_text(encoding="utf-8", errors="ignore").splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get("kind") != "extract.tier_attempted": continue
        if e.get("tier_key") != "rentcafe:sc_parse": continue
        if e.get("outcome") != "parse_returned_empty": continue
        parse_fails.append(e)
print(f"{len(parse_fails)} sc_parse silent empties — clustering by signals")

# 2. For each one, the SUBSEQUENT event with tier_key=rentcafe:sc_parse_diag
#    has the raw signals. Cluster by signal_caption_samples regex shape.
from collections import Counter
caption_shapes = Counter()
for shard in sorted(Path("c:/tmp/run-2026-05-22").iterdir()):
    ev = shard / "events.jsonl"
    for line in ev.read_text(encoding="utf-8", errors="ignore").splitlines():
        try: e = json.loads(line)
        except: continue
        if e.get("tier_key") != "rentcafe:sc_parse_diag": continue
        captions = e.get("signal_caption_samples") or []
        for c in captions:
            # Normalise to a shape — replace specifics with placeholders
            shape = c[:50]  # first 50 chars usually contain "Floor Plan:" or not
            caption_shapes[shape] += 1
print("Top 10 caption shapes across silent-empty cohort:")
for shape, n in caption_shapes.most_common(10):
    print(f"  {n:>4}  {shape!r}")
```

Two queries against events.jsonl would have surfaced the new SecureCafe caption format (`Apartment Details and Selection for Floor Plan: ...`) within minutes of the first cloud run after the template change shipped. No live-fetches, no manual eyeballing — the signal is in the events.

**This is the diagnostic capability the playbook now provides.** Every silent-empty adapter failure carries enough structural signal in events.jsonl to:
1. Identify which stage in which adapter failed (`tier_key=<pms>:<stage>` + `outcome`)
2. Detect environmental gates (`via_proxy`, `via_unlocker` booleans)
3. Diff between template variants (`signal_caption_samples`, `signal_data_label_inventory`, `signal_table_ids`, `signal_first_row_ctx`)
4. Detect cross-PMS misroutes (`signal_vendor_markers` shows whether another PMS's CDN is also on the page)
5. Distinguish CF challenges from genuine empty responses (`classify_probe_body` + `signal_cf_marker_counts`)

### 20.13 Files touched 2026-05-22 (for future reference)

| File | Change |
|---|---|
| `ma_poc/pms/adapters/_adapter_telemetry.py` | NEW — 219 LOC shared module |
| `ma_poc/pms/adapters/rentcafe.py` | regex fix + diagnostic + refactor to shared module (~289 LOC modified) |
| `ma_poc/pms/adapters/g5.py` | deterministic URN picker + rendered HTML access + per-stage telemetry |
| `ma_poc/pms/adapters/onesite.py` | xhr_capture + cascade_exit telemetry |
| `ma_poc/pms/adapters/entrata.py` | restored `_probe_prospectportal` (from git ref `8b1bfa4`) + prospectportal_probe telemetry |
| `ma_poc/pms/adapters/sightmap.py` | xhr_capture telemetry + join-dropped count |
| `ma_poc/pms/adapters/apts247.py` | origin_resolve + api_key_resolve + api_fetch stages |
| `ma_poc/pms/adapters/appfolio.py` | xhr_capture telemetry |
| `ma_poc/pms/adapters/knock.py` | ids_search telemetry |
| `ma_poc/pms/adapters/realpage_oll.py` | xhr_capture telemetry with shape-match counts |
| `ma_poc/pms/detector.py` | OneSite negative gate + Knock explicit demotion |
| `ma_poc/pms/scraper.py` | platform-wide adapter_exit telemetry at line 933+ |
| `ma_poc/scripts/diagnostics/asn_ipv6_probe.py` | NEW — egress-probe diagnostic |
| `.github/workflows/probe-experiment.yml` | NEW — Cloud Run job override workflow |
| `ma_poc/tests/pms/test_detector.py` | +5 OneSite gate regression tests |
| `ma_poc/tests/pms/adapters/test_rentcafe.py` | +7 SC parser + diagnostic tests |
| `ma_poc/tests/pms/adapters/test_g5_marketing_cloud_synth.py` | +5 deterministic URN tests |

Test result: **1,676 pass / 2 skipped / 0 fail** in `ma_poc/tests/pms/`.

---

## Phase 16 — Closing checklist before shipping a fix

1. **Code change** has file:line citations in the commit message.
2. **Unit test** exercising the change via a real ctx/profile (not Mocks).
3. **Live-fetch verification** if the change references specific URLs/hosts/patterns.
4. **Pytest** `tests/pms tests/fetch` green; deselect pre-existing failures explicitly.
5. **Cold canary** on a 32-PID sample from yesterday's failures + 4 sentinels. REGRESSED == 0.
6. **Warm canary** (with prod profiles seeded) — if your fix is profile-dependent, this is mandatory.
7. **Verify on at least one specifically-diagnosed PID** that the new event trace matches the expected tier sequence.
8. **No half-finished changes** in the diff (TODO comments, commented-out code, unused imports).
9. **Anti-patterns audit:** for each Phase 0 anti-pattern, ask yourself "did I avoid this in my diff?"
10. **Concession-pipeline audit** (if your change touches `concession_clean.py` / `concession_normalize.py` / `_capture_concession_from_html` / `_probe_specials_pages` / schema_v2 concession emit):
    a. Run `tests/core/test_concession_pipeline.py::TestPreserveAndFlagInvariant` — pins the raw-text-never-discarded contract.
    b. Run `python ma_poc/scripts/diagnostics/concession_canary.py` against ≥4 properties known to advertise concessions; spot-check that `raw` and `_quality` and `structured` are all populated as expected.
    c. If the change touches `_PROPERTY_CONCESSION_RE` or the script-strip regex, confirm the source-grep pins (`test_script_strip_pattern_present_in_scraper` + `test_sentence_extend_pattern_present_in_scraper`) still match — these catch refactor regressions.
    d. Verify `_concessions_quality` distribution didn't regress (∼50% clean baseline pre-fix; should be ≥90% clean post-fix). Look at the canary JSON output's quality histogram.

---

## Appendix A — Glossary

- **Cold profile** — `confidence.maturity == COLD`, typically `winning_page_url == None`; no prior-run state.
- **Warm/Hot profile** — `confidence.maturity ∈ {WARM, HOT}`; has at least one prior SUCCESS run.
- **PMS prior** — sub-path guessed from the detected PMS (e.g. `/floorplans` for RentCafe). Score 5000-5095.
- **Anchor-discovered link** — `<a href>` found by `_rank_internal_links`. Score 0-5600 depending on keywords.
- **Portal URL** — third-party PMS leasing widget (SightMap embed, OneSite portal, AppFolio listings, etc.). Score 10000 when found by §8.2 portal scan.
- **STUB_URL** — proposed verdict for properties with no unit data on ANY reachable page (§11). Not yet shipped.
- **ENV_MISMATCH** — local canary failure that the cloud run handles correctly (usually CF/bot-block with residential proxy).
- **Self-fetch** — `profile.winning_page_url == entry_url`; symptom of homepage being mistakenly tagged as the unit-data URL.
- **Hop_depth bug pattern** — a gate that reads `ctx.X` where `X` was never wired into the upstream `AdapterContext`; gate silently always-True or always-False.
- **SUCCESS_PARTIAL** (2026-05-17) — verdict emitted when the per-property 600s wallclock fires and the link-hop accumulator has buffered ≥1 unit before the timeout. Counts as success in `_SUCCESS_VERDICTS`. Distinct from `PARTIAL` (validation-majority-rejected) which is NOT a success.
- **PARTIAL** (2026-05-17 semantics) — validation-majority-rejected. The schema gate dropped >50% of extracted rows on validity grounds. Surviving rows ship but are suspect; the verdict is EXCLUDED from `_SUCCESS_VERDICTS`. Tracked under `properties_partial_validation_rejected` in the analyzer.
- **plan_summaries partition** (2026-05-17) — the second output of `extraction.post_process.post_process`. Rows admitted by Stage-1 validity but classified as plan-level (no per-unit identity). Ships under `Floor Plans` (v1) / `floor_plans` (v2). Before §8.18 fix these were silently dropped at the v2 formatter.
- **Cross-host per-plan URL** (2026-05-17) — per-floorplan detail page URL whose host differs from the just-finished hop's host. Discovered by `_discover_cross_host_per_plan_urls` via URL SHAPE (`/floor[-]?plans?/{slug-with-br-ba}`). Required when a portal host (`*.securecafe.com`) yields plan-summary rows while per-apartment inventory lives on a different host (`{property}.com/floorplans/{slug}`).
- **Unit-fidelity check (Q13)** — compare emitted `units` count to the extractor's reported `units_found`. A SUCCESS verdict with units lost in transit is a §8.18-8.20 silent regression, NOT a real fix. Always run before celebrating an "IMPROVED" cluster.
- **Preserve-and-flag invariant** (2026-05-21) — the property-level + per-unit concession pipeline ALWAYS retains the raw text (`concessions` / `concession_text`) alongside the cleaned variant (`concessions_clean` / `concession_text_clean`), the quality label (`_concessions_quality` / `_concession_quality`), and the structured object (`concessions_structured` / `concession_structured` — may be `None`). When normalization fails, raw is the system of record. Pinned by `tests/core/test_concession_pipeline.py::TestPreserveAndFlagInvariant`.
- **_concession_quality** (2026-05-21) — short label classifying the raw concession text. Values: `clean` / `unclean_script_leak` / `unclean_style_leak` / `unclean_dmapi` / `unclean_orphan_prefix` / `unclean_header_only` / `empty`. Each `unclean_*` value tells you something specific about the upstream capture; see §18.1 symptom decoder.
- **concessions_structured** (2026-05-21) — regex-derived structured object. Shapes: `free_rent` / `discount` / `percent_off` / `waived_fee` / `reduced_deposit` / `look_and_lease`. Always carries `text` (whitespace-normalized source), `source` (`TEXT` / `IMAGE_BANNER` / `URL_PROBE` / `API`), `deadline`, `conditions`. `None` is a valid value — DO NOT count `IS NULL` as a failure metric.
- **stealth_probe** (2026-05-21) — adapter-side HTTP probe helper at `fetch/probe.py`. Applies `IdentityPool.pick(sticky_key=property_id)` + `chrome_header_set(cold_visit=True)` + `looks_like_captcha`. Used when the L1 `fetch()` entry point can't be reached (custom request headers, non-GET method, or fire-and-forget probe outside the L1 retry loop). Sticky identity ensures a property's entry-page fetch and every adapter-side probe present the same Chrome identity to the bot-management edge.
- **HOP_CAPTCHA_DETECTED** (2026-05-21) — new event kind at `observability/events.py`. Fires when a hop (concession `/specials` probe, RealPage CWS probe, Beacon AJAX probe) hits a captcha. Payload `context` distinguishes the hop class without URL-pattern regex. Distinct from the noisier entry-page `FETCH_CAPTCHA_DETECTED`.
- **CONCESSION_PROBE_RESULT** (2026-05-21) — per-property terminal outcome of `_probe_specials_pages`. `outcome` is `found` / `exhausted` / `all_blocked`. `all_blocked` is the canonical signal that a domain needs a stealth-tier escalation.
- **`adapter_exit` event** (2026-05-22) — platform-wide telemetry emitted at `pms/scraper.py:933+` after every adapter dispatch, regardless of which adapter ran or whether it has any internal instrumentation. `tier_key=<pms>:adapter_exit`, payload has `outcome=<tier_used>`, `ran_units`, `plan_summaries`, `confidence`. Use as a guaranteed floor — diagnose any adapter's behaviour without per-adapter code changes. See §20.4.
- **Per-adapter stage event** (2026-05-22) — `extract.tier_attempted` with `tier_key=<pms>:<stage>`. Stages are adapter-specific; reference table at §20.5. Common ones: `xhr_capture` (how many network responses matched the adapter's shape), `cascade_exit` (final outcome of the adapter's internal cascade), probe-stage events (`sc_probe`, `wp_probe`, `prospectportal_probe`, `urn_pick`, etc.).
- **`via_proxy` / `via_unlocker` booleans** (2026-05-22) — every adapter-stage event carries these read from `PROBE_PROXY_URL` and `WEB_UNLOCKER_KEY` env. `False` on a cross-origin probe (sc_probe, prospectportal_probe, wp_probe) explains a CF-403 in one query — see §20.3 cross-origin clearance asymmetry.
- **`*_diag` event** (2026-05-22) — companion event fired alongside a stage event when the parent stage emitted a silent-empty outcome (parse returned 0 despite visible markup). Payload carries raw structural signals (`signal_caption_samples`, `signal_heading_samples`, `signal_data_label_inventory`, `signal_table_ids`, `signal_floorplan_ids_seen`, `signal_first_row_ctx`, `signal_vendor_markers`, `signal_cf_marker_counts`). Cluster across PIDs to identify template variants without re-fetching pages. See §20.4 + §20.12.
- **Cross-origin clearance asymmetry** (2026-05-22) — explained in §20.3. Adapters that probe a host different from the property's marketing origin (e.g. SecureCafe drill, ProspectPortal probe, RentCafe WP probe) cannot reuse the patchright CF clearance cookies; they need `PROBE_PROXY_URL=brightdata` set in production env. Adapters that stay on the property's own origin (Cortland, Irvine, AvalonBay) succeed from GCP without proxy because `_with_clearance` at `_probe.py:73-86` automatically attaches the patchright-minted cookies for the same origin.
- **G5 deterministic URN picker** (2026-05-22) — `find_g5_urn_for_property(html, base_url)` at `g5.py:105+`. Replaces the broken `max(matches, key=len)`. Anchors on the Cloudinary asset-folder path `/g5/g5-c-<companyId>/g5-cl-<propertyUrn>/uploads/` (G5's CMS guarantees property-specific folders per tenant). Fallback: most-frequent `g5-cl-*` on the page. Live-verified 5/5 correct on samples where `max(len)` was 0/5 (Anson Maryland-sibling-for-California, etc.).
- **OneSite negative gate** (2026-05-22) — `static2.apts247.info` / `apartments247_api.min.js` / `doorway.knck.io` competing-CDN demotion in the OneSite STRONG-marker branch at `detector.py:500+`. Returns `knock` directly when the Knock loader is present (avoiding the `realpage_oll` branch that lives between OneSite and Knock in source order). See §20.8 + §20.11.
