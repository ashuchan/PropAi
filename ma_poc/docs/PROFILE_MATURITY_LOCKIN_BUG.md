# Profile Maturity Lock-In Bug — Investigation & Fix Plan

**Status:** identified, not fixed.
**Severity:** high — affects 84+ properties currently, mechanism is self-perpetuating, masks data-loss as success.
**Discovered:** 2026-05-08, while comparing local jugnu vs cloud-prod outputs for the 50-property subset.
**Independent of:** the `llm_api_rescue` factory refactor (already merged separately).

---

## What this doc is

A self-contained briefing for the next Claude Code session that picks up this work. You can land cold and execute. Read top-to-bottom, then start at "Open questions to resolve before coding."

---

## Symptom (the user-visible failure)

For property 56775 (Avalon Hill Country, `https://www.avaloncommunities.com/texas/austin-apartments/avalon-hill-country/`):

- Cloud-prod run today: **TIER_1_API_AVALONBAY, 54 units extracted.**
- Local jugnu run today: **TIER_4_LLM_DOM, 3 units extracted.**

Same code, same property, same day, same `LLM_PROVIDER=openrouter`. The local run *correctly detects* the property as AvalonBay (`pms_detected=avalonbay confidence=0.95`, fingerprints `['sightmap', 'avalonbay']`), but never invokes `AvalonBayAdapter.extract()`. Instead, the dispatcher swaps to `GenericAdapter`, which falls through to LLM_DOM and recovers only 3 of 54 units.

Local property profile state at run time (`config/profiles/56775.json`):

```
maturity: HOT
consecutive_successes: 10
last_unit_count: 3
preferred_tier: 1   (claims API works)
last_success_tier: 4 (LLM_DOM actually used)
```

The profile is stuck. Every run extracts 3, increments the streak, stays HOT, runs in GET mode, can't capture the AvalonBay XHR, falls through to LLM_DOM, gets 3, repeats.

---

## Root-cause causal chain (verified)

Reproduced from event ledger + frontier SQLite + profile JSON. Each step was confirmed by trace events for property 56775 in `data/v2/runs/2026-05-07/events.jsonl`.

1. **Scheduler reads profile maturity = HOT.** [`discovery/scheduler.py:117-146`](../discovery/scheduler.py)
2. **`change_detector.decide()` rule 3:** `if profile_maturity == "HOT" and days_since_full_render < 1: return GET`. [`discovery/change_detector.py:66-67`](../discovery/change_detector.py)
3. **GET via httpx — no Playwright, no XHR interception.** `network_log` is empty.
4. **Router invariant** [`pms/scraper.py:369-398`](../pms/scraper.py) calls `confirm_detection(detection, _api_responses)`. With empty `_api_responses`, no AvalonBay envelope exists to confirm → detection demotes from `avalonbay` → `unknown` → `get_adapter("unknown")` returns `GenericAdapter`.
5. **Generic cascade** runs. DOM_SCAN gets 6, LLM_DOM gets 3 (the LLM is correctly going through OpenRouter via the factory — that part is healthy). Final tier = TIER_4_LLM_DOM, units = 3.
6. **`profile_updater.py:268-287`** records the result:
   ```python
   if units_extracted > 0 and tier and tier != "FAILED":
       profile.confidence.consecutive_successes += 1
       ...
   if profile.confidence.consecutive_successes >= 3:
       profile.confidence.maturity = ProfileMaturity.HOT
   ```
   ANY non-zero count counts as success. 3 units → +1 streak → still HOT → loop.

The only path out of the loop today is rule 2 of `decide()`: `days_since_full_render > 7 → RENDER`. So once a week the profile gets a forced render. But if that render *also* falls through to LLM_DOM (because the AvalonBay XHR was already learned-skipped in `explored_links`, or because the page changed slightly), the loop just resets. There is no "we extracted half of expected, demote" mechanism.

---

## Where the bug lives (single file, two missing checks)

**Primary site:** [`services/profile_updater.py:268-287`](../services/profile_updater.py).

What's missing:

1. **No tier-quality gate.** A TIER_4_LLM_DOM extraction promotes the profile equally to a TIER_1_API extraction. But Tier 4 is the *fallback path*; promoting on it locks the profile away from ever discovering the real Tier-1 path.
2. **No expected-count comparison.** A 3-of-54 extraction (94% miss) still counts toward HOT promotion.

Secondary issue same file, **line 274**: `preferred_tier` ratchets down on the *first* Tier-1 hit and never resets. So a single one-off API success at unit count 1 permanently pins `preferred_tier=1` even when every subsequent run fell through to vision (Tier 5). See profile 12617 for an extreme case (`preferred_tier=1` but `last_success_tier=5`).

---

## Blast radius (already measured)

Audit query against `config/profiles/*.json` (1,034 total profiles in the local store):

```python
HOT profiles with last_unit_count < 10:  84
```

Sample of the worst stuck profiles:

| PID | units | streak | preferred_tier | last_success_tier |
|---|---|---|---|---|
| 10898 | 1 | 6 | 1 | 1 |
| 7572 | 2 | 7 | 1 | 1 |
| 12617 | 2 | 7 | **1** | **5** (vision — preferred_tier stale) |
| 285770 | 2 | 10 | 3 | 3 |
| 56775 (Avalon) | 3 | 10 | 1 | 4 |
| ... 79 more | | | | |

These 84 represent ~8% of the local profile store. Cloud-prod likely has a similar fraction stuck — but with much more historical data, so the proportions may differ. **One of the open questions below is to confirm the prod blast radius before shipping a fix.**

---

## Suggested fix (sketch — needs review)

Pseudocode for [`services/profile_updater.py:268-287`](../services/profile_updater.py). Treat as a starting point, not a final design.

```python
TIER_API = 1
TIER_JSONLD = 2

# Treat extraction as "high-quality" only when it came from a deterministic
# adapter path (API, JSON-LD), not from an LLM/vision fallback. LLM_DOM/LLM
# tiers are recovery paths and should not pin the profile.
high_quality = tier_num is not None and tier_num <= TIER_JSONLD

# Compare against expected_total_units when known. The 0.5 floor is a
# starting heuristic — needs validation. See open questions.
expected = scrape_result.get("_expected_total_units")
realistic = expected is None or units_extracted >= max(1, expected * 0.5)

if units_extracted > 0 and tier and tier != "FAILED":
    profile.confidence.consecutive_successes += 1
    profile.confidence.consecutive_failures = 0
    if tier_num:
        profile.confidence.last_success_tier = tier_num
        # Only ratchet preferred_tier down on high-quality + realistic runs.
        # Otherwise a one-off Tier-1 success at 1 unit pins preferred_tier=1
        # forever even when every subsequent run uses Tier 5.
        if high_quality and realistic:
            if (profile.confidence.preferred_tier is None
                    or tier_num < profile.confidence.preferred_tier):
                profile.confidence.preferred_tier = tier_num
    profile.confidence.last_unit_count = units_extracted
else:
    profile.confidence.consecutive_failures += 1
    profile.confidence.consecutive_successes = 0

# Promotion requires high-quality + realistic. Demotion still triggers on
# 3 consecutive failures.
if profile.confidence.consecutive_successes >= 3 and high_quality and realistic:
    profile.confidence.maturity = ProfileMaturity.HOT
elif profile.confidence.consecutive_successes >= 1 and (high_quality or realistic):
    profile.confidence.maturity = ProfileMaturity.WARM
elif profile.confidence.consecutive_failures >= 3:
    profile.confidence.maturity = ProfileMaturity.COLD
```

Plus a **one-time cleanup script**: demote any HOT profile where `last_success_tier > TIER_JSONLD` to COLD so the next run re-renders and re-discovers the real tier. That recovers the 84 stuck profiles without further code changes.

---

## Open questions to resolve before coding

The pseudocode above is a sketch. Don't merge until each of these is answered. Each question has a concrete way to investigate.

### Q1 — What's the right threshold for "realistic"?

The 0.5×expected floor is a guess. Need to look at real data: across the cloud-prod `units` table, when an extraction returned `< X%` of `expected_total_units`, did the next run with the same property recover more units? If yes, the threshold is justified — these were bad extractions worth re-rendering. If no, the threshold is just noise and we'd be churning fetches for nothing.

**How to investigate:**
- Query postgres: for each property, look at the variance of `last_units_count` over the last 30 days against a stable expected count (e.g., median of last 30 days, or CSV-provided `Total Units (Est.)`).
- Pick a threshold that catches the 84 stuck profiles without flagging healthy properties whose true unit count fluctuates ±20% day-to-day due to vacancy.

### Q2 — Where does `expected_total_units` come from, and is it reliable?

`scrape_result.get("_expected_total_units")` — verify this key is actually populated. From [`pms/scraper.py:285-292`](../pms/scraper.py), it reads CSV columns `Total Units` / `Total Units (Est.)` / `total_units`. The current `properties.csv` doesn't have this column for most rows.

**How to investigate:**
- `grep -c "Total Units" config/properties.csv` and check what fraction of rows have it.
- If most rows lack it, the `realistic` check defaults to True and provides no signal. Need an alternative: median historical unit count from `units` table, or a heuristic from JSON-LD `numberOfAvailableAccommodationUnits`, or just disable the realistic check when expected is unknown.
- Decision: should we fall back to `last_unit_count` from the profile itself? That would catch sudden drops (e.g., 54 → 3) but not slow drift.

### Q3 — Should we also patch `change_detector.decide()`?

`profile_updater` controls *what gets promoted*. `change_detector` controls *what HOT means in fetch behavior*. The bug has two faces, and you could fix it from either side.

- Option A: only fix `profile_updater` (don't promote bad runs to HOT). Pro: minimal blast. Con: existing 84 stuck profiles still need cleanup, and a single bad promotion still locks the profile until the 7-day forced-render rule fires.
- Option B: fix both. Add a "force RENDER every Nth visit" override in `change_detector.decide()` even for HOT profiles, so even legitimately-HOT profiles get a periodic Tier-1 sanity check. Pro: defense in depth. Con: increases Playwright cost; needs tuning of N.

**Recommendation in open question form:** start with Option A + cleanup script. Measure prod cost-per-render after rollout. If the stuck-profile rate creeps back up over 30 days, escalate to Option B.

### Q4 — Are there profiles where HOT-on-Tier-4 is *correct*?

Possible counterexample: a property with no API at all (e.g., a Wix or Squarespace site), where TIER_4_LLM_DOM is genuinely the best achievable tier. Demoting these would force a needless re-render every week.

**How to investigate:**
- Cross-reference the 84 stuck profiles against `dom_hints.platform_detected`. If `platform_detected in {wix, squarespace, custom}`, TIER_4_LLM_DOM may legitimately be the ceiling.
- Decision: should the `high_quality` gate include "or `platform_detected` is a known no-API platform"? This keeps Wix sites HOT on Tier 4, while Avalon (which has an API) gets demoted.

### Q5 — Test coverage

What tests exist today for `profile_updater.update_profile_after_extraction`? Look in `tests/services/`. The fix needs:

- Tier-4 extraction does NOT promote to HOT.
- Tier-1 extraction with realistic count DOES promote to HOT.
- Tier-1 extraction with unrealistic count (e.g., 1 of 54) does NOT promote.
- Existing successful Tier-1 properties don't get demoted by the cleanup script.
- The cleanup script is idempotent.

### Q6 — Rollout safety

The cleanup script touches every HOT profile in `config/profiles/`. That's 1,034 files locally; in prod it's probably much larger. Need:

- Dry-run mode that prints what would change without writing.
- An audit log of which profiles got demoted and why.
- A way to roll back (the profile JSON has a `version` field — check whether `_audit/` snapshots are sufficient for rollback).
- Coordination with the cloud-prod sync: don't run the cleanup script locally if it'll get overwritten by the next prod sync, and don't run it in prod without first confirming the change won't break ongoing scrapes.

---

## How to verify the fix once written

In order, smallest to largest:

1. **Unit tests** for the six cases in Q5 above. All must pass before integration.
2. **Local re-run on Avalon Hill Country (56775).** With the cleanup script applied, the next run should re-render with Playwright, capture the AvalonBay XHR, dispatch to `AvalonBayAdapter.extract()`, and return ~54 units. Confirm `extract.adapter_selected = avalonbay` and `extract.tier_won = TIER_1_API_AVALONBAY` in the event ledger.
3. **Local re-run on a 20-property sample** drawn from the 84 stuck profiles. Compare unit counts before and after. Net unit count should go up substantially; verdict count (SUCCESS/FAILED) should not regress.
4. **Compare against cloud-prod's run for the same day**, filtered to `units.last_seen_at = today` (see `_apply_retention()` constraints in `sync_run_to_pg.py`). Local should now match prod's unit counts within ~10% on properties with deterministic adapters (Tier 1, JSON-LD).
5. **Run the full test suite** under `tests/`. Especially `tests/services/test_profile_updater.py` (if it exists — create if not) and any integration tests that exercise the maturity state machine.

---

## Constraints — don't break these

- **Do not touch `services/llm_api_rescue.py`.** That refactor shipped separately and is unrelated to this bug.
- **Do not change the `ProfileMaturity` enum or the profile JSON schema.** Both are persisted on disk and consumed by the cloud-prod sync. Schema changes would require a migration.
- **Do not bundle this fix with the LLM refactor PR.** They have different blast radii and different rollback profiles. Separate PRs.
- **Do not assume `_expected_total_units` is always present.** Most CSV rows don't carry it. Whatever the fix does in its absence must be explicit (preferably: don't enforce the `realistic` check when expected is unknown).
- **Do not write a fix that demotes *all* HOT profiles.** Many are legitimately HOT (true Tier-1 successes at full unit count). The fix must distinguish.
- **Do not skip hooks (`--no-verify`) when committing.** If a pre-commit hook fails, fix the root cause.

---

## Files to touch (likely)

- [`services/profile_updater.py`](../services/profile_updater.py) — the fix itself.
- [`services/profile_updater.py`](../services/profile_updater.py) or new `scripts/cleanup_stuck_profiles.py` — the one-time cleanup script.
- [`tests/services/test_profile_updater.py`](../tests/services/test_profile_updater.py) — new tests (file may need to be created).

Files to **read but not edit** (load-bearing context):

- [`discovery/change_detector.py`](../discovery/change_detector.py) — to understand the GET-vs-RENDER decision rules.
- [`discovery/scheduler.py`](../discovery/scheduler.py) — to understand how maturity feeds into scheduling.
- [`pms/scraper.py`](../pms/scraper.py) — line 369-398, the router invariant that demotes detection.
- [`pms/adapters/avalonbay.py`](../pms/adapters/avalonbay.py) — to confirm the adapter has no DOM fallback (it doesn't).
- [`models/scrape_profile.py`](../models/scrape_profile.py) — for `ProfileMaturity` enum and `ExtractionConfidence` shape.

---

## Why my LLM-rescue refactor doesn't fix this

The LLM rescue refactor only changed which provider handles a specific recovery call (Azure → factory-routed OpenRouter). The Avalon symptom doesn't involve the rescue path at all:

- The router demoted `avalonbay` → `unknown` *before any adapter ran*, due to empty `_api_responses`.
- The GenericAdapter then ran the cascade and got 3 units from LLM_DOM (which already used the factory in the original pre-refactor code).
- So the routing fix was orthogonal. This bug pre-dates that refactor, persists after it, and is independent of it.

The two PRs do not depend on each other and should not be merged together.

---

## Quick start for the next session

1. `git status` — confirm you're on a clean branch off `main`.
2. Read this doc top to bottom.
3. Read [`services/profile_updater.py:255-300`](../services/profile_updater.py) and [`discovery/change_detector.py:26-82`](../discovery/change_detector.py) to ground in current behavior.
4. Inspect `config/profiles/56775.json` — that's the canonical broken example.
5. Answer Q1-Q6 above (each takes a focused Grep / postgres query — under an hour total).
6. Write the fix + tests.
7. Write the cleanup script with dry-run + audit log.
8. Run the verification checklist.
9. PR.
