# Jugnu Fetch Tier Escalation Ladder — Implementation Plan

**Version:** v1.0  ·  **Target layer:** L1 Fetch (`ma_poc/fetch/`)  ·  **Owner:** Phase A

---

## 1. Goal

Add a network-cost-tiered fetch ladder to Jugnu so that properties scrape at the **cheapest viable tier** by default, escalate to more expensive tiers **only on failure**, and **persist** what tier worked so the next run starts there. The 75–80% of properties that don't need any anti-bot help must keep running through the free path.

## 2. Non-goals

- Replacing the existing extraction cascade (Tier 1–7). This plan is purely about the **fetch path**, not the extraction tiers.
- Adding a CAPTCHA solver. Sites that fail at the top of the ladder go to DLQ with `anti_bot_unsolvable`.
- Stealth fingerprinting work. Stealth is assumed always-on at every tier (it's free).

## 3. Design principles

- **Default path stays free.** A new property with no profile → tier `T0_DIRECT`. No proxy fee, no unlocker fee.
- **Escalation is per-property, not global.** Site A blocking us doesn't change anything for Site B.
- **Profile remembers.** Once a property succeeds at tier `T_N`, the next run starts at `T_N`, not `T0`. We don't re-pay the discovery cost daily.
- **Escalation has a hard ceiling per property per run.** A property cannot burn through all 5 tiers every day — that defeats the cost model.
- **Demotion exists.** Sites lift their bot protection; we periodically probe one tier below the persisted floor to recover the cheapest path.
- **Outcome-driven, not URL-pattern-driven.** Escalation is triggered by `BOT_BLOCKED` / `PROXY_ERROR` outcomes, not by domain hardcoding.

---

## 4. The Ladder

| Tier | Name | Network path | Approx cost/req | When it's chosen |
|---|---|---|---|---|
| **T0** | `DIRECT` | No proxy. Local egress. | $0 | Default for COLD profiles, persisted floor for unprotected sites |
| **T1** | `STEALTH_LOCAL` | No proxy. `curl_cffi` impersonation for GET; `patchright` stealth for RENDER. | $0 | Auto-applied as a stealth-patch layer on T0 — not a separate tier in practice. **See §4.1.** |
| **T2** | `DC_PROXY` | Datacenter proxy pool (existing pool with sticky-by-`property_id`). | ~$0.0005 | First escalation step from T0 |
| **T3** | `RESIDENTIAL` | Bright Data / Zyte residential proxy, sticky session, geo-matched to property. | ~$0.008–0.015 | Escalation after T2 fails on `BOT_BLOCKED` |
| **T4** | `UNLOCKER` | Bright Data Web Unlocker or Zyte API — provider handles TLS, fingerprint, JS challenge. | ~$0.05–0.15 | Last network-layer attempt before park |
| **T5** | `DLQ_PARK` | Not a fetch tier. Park property with `dlq_reason="anti_bot_unsolvable_T4"`. Vision-only path remains an option for next phase. | $0 (skipped) | Triggered after T4 fails twice across separate runs |

### 4.1 Why T0 and T1 collapse

Stealth (`patchright` for browser, `curl_cffi` for httpx) is free and uniformly beneficial. We always apply it. So in code, T0 and T1 are the same tier. The ladder is really five real cost steps: `DIRECT → DC_PROXY → RESIDENTIAL → UNLOCKER → DLQ_PARK`. T1 is reserved in the enum so we can split later if we ever want to A/B stealth on/off.

### 4.2 What "escalation" means at each tier

| Outcome from tier `T_N` | Action |
|---|---|
| `OK` | Stay at `T_N`. Bump `consecutive_successes_at_tier`. Persist `fetch_tier_floor = T_N` if not already. |
| `NOT_MODIFIED` | Treat as `OK`. |
| `BOT_BLOCKED` | Promote: try `T_{N+1}` immediately, same property, same run. |
| `RATE_LIMITED` | **Do not escalate.** Honor `Retry-After`, retry at `T_N`. (Rate limiting is policy, not bot-blocking.) |
| `PROXY_ERROR` | Stay at `T_N`, pick a different proxy from the pool. If 3 consecutive `PROXY_ERROR`s → escalate to `T_{N+1}`. |
| `TRANSIENT` | Stay at `T_N`, exponential backoff retry per existing `retry_policy`. |
| `HARD_FAIL` | No escalation. The site doesn't exist or robots-denies us. Carry-forward branch handles it. |

---

## 5. Data model changes

### 5.1 New enum — `ma_poc/models/fetch_tier.py`

```python
from enum import IntEnum

class FetchTier(IntEnum):
    DIRECT = 0
    STEALTH_LOCAL = 1   # reserved; collapses into DIRECT in v1
    DC_PROXY = 2
    RESIDENTIAL = 3
    UNLOCKER = 4
    DLQ_PARK = 5        # terminal — not actually fetched
```

`IntEnum` so we can do `tier + 1` arithmetic for escalation.

### 5.2 Extend `ScrapeProfile` — new sub-model

Add to `ma_poc/models/scrape_profile.py`:

```python
class FetchProfile(BaseModel):
    """Persisted fetch-tier state for a property."""
    tier_floor: FetchTier = FetchTier.DIRECT
    last_success_tier: FetchTier | None = None
    consecutive_successes_at_floor: int = 0
    consecutive_failures_at_floor: int = 0
    last_block_signature: str | None = None      # e.g. "cf_turnstile", "px_block"
    last_demotion_probe_at: datetime | None = None
    promoted_at: datetime | None = None           # when floor last went up
    total_escalations: int = 0                    # lifetime — for cost auditing

class ScrapeProfile(BaseModel):
    # ... existing fields ...
    fetch: FetchProfile = FetchProfile()
```

Profile version bumps; existing profiles deserialize with `fetch=FetchProfile()` defaults.

### 5.3 Extend `FetchResult`

Add three fields:

```python
class FetchResult(BaseModel):
    # ... existing ...
    fetch_tier_used: FetchTier
    fetch_tier_attempts: list[FetchTier]   # ordered, e.g. [DIRECT, DC_PROXY, RESIDENTIAL]
    block_signature: str | None = None     # filled when outcome=BOT_BLOCKED
```

`fetch_tier_attempts` lets observability count escalations even on success.

### 5.4 New event types

In `ma_poc/observability/events.py`, add:

- `FETCH_TIER_ESCALATED` — fired on each tier bump within a run
- `FETCH_TIER_PERSISTED` — fired when `tier_floor` is updated on the profile
- `FETCH_TIER_DEMOTED` — fired when probe succeeds at `floor - 1`
- `FETCH_LADDER_EXHAUSTED` — fired when T4 fails; precedes DLQ park

---

## 6. Differentiating "needs escalation" from "doesn't"

This is the core of your requirement. The differentiation comes from **two places**, neither of which is a hardcoded list.

### 6.1 First-run differentiation (online)

A new property has `tier_floor = DIRECT`. The fetcher tries T0. Three things can happen:

1. **`OK` returned** → site is unprotected, or stealth alone is enough. Profile saves `tier_floor = DIRECT`. **This is the path 70–80% of properties stay on forever.** Zero proxy cost.
2. **`BOT_BLOCKED` returned** → site is protected. Escalate within the run (see §7). The tier that succeeds becomes the persisted floor.
3. **`HARD_FAIL` returned** → not a bot problem. Carry-forward branch handles it; no escalation.

There is no "is this site protected?" classifier. The first `BOT_BLOCKED` outcome **is** the classifier.

### 6.2 Subsequent-run differentiation (offline)

On every run after the first, the scheduler reads `profile.fetch.tier_floor` and starts the fetcher at that tier directly. Properties that landed at `DIRECT` start at `DIRECT`. Properties that landed at `RESIDENTIAL` start at `RESIDENTIAL`. **Cost is spent only on properties that actually need it.**

### 6.3 The probe (avoids permanent over-paying)

If we never demote, a property that briefly tripped Cloudflare three months ago will pay the unlocker fee forever. So:

- Once every `PROBE_INTERVAL_DAYS` (default **14**) per property at floor ≥ T2, the scheduler picks `tier = floor - 1` for that one fetch as a probe.
- If the probe returns `OK` → demote: `tier_floor -= 1`. Emit `FETCH_TIER_DEMOTED`. Reset `consecutive_successes_at_floor`.
- If the probe returns `BOT_BLOCKED` → no demotion. Update `last_demotion_probe_at`. Re-fetch at the original floor (the property still gets its data this run; the probe doesn't cost it a daily failure).
- Probes are budget-capped: max **2% of daily property volume**, randomized so we don't probe the same 10 properties every cycle.

---

## 7. Within-run escalation flow

This goes inside `ma_poc/fetch/fetcher.py::fetch`. Pseudocode:

```python
async def fetch(task: CrawlTask, profile: ScrapeProfile) -> FetchResult:
    tier = profile.fetch.tier_floor
    attempts: list[FetchTier] = []
    last_result: FetchResult | None = None

    # Probe path (occasional demotion attempt)
    if _should_probe(profile):
        tier = max(FetchTier.DIRECT, tier - 1)
        emit("FETCH_TIER_PROBE", property_id=task.property_id, tier=tier)

    # Within-run escalation budget — never burn through every tier on one property
    max_tiers_this_run = _max_escalations_for(task)  # default 3 from current floor

    while tier < FetchTier.DLQ_PARK and len(attempts) < max_tiers_this_run:
        attempts.append(tier)

        # The retry_policy still runs INSIDE the tier (TRANSIENT/PROXY_ERROR/RATE_LIMITED)
        result = await _fetch_at_tier(task, tier, profile)

        if result.outcome in (OK, NOT_MODIFIED):
            result.fetch_tier_used = tier
            result.fetch_tier_attempts = attempts
            return result

        if result.outcome == BOT_BLOCKED:
            emit("FETCH_TIER_ESCALATED",
                 from_tier=tier, to_tier=tier+1,
                 block_signature=result.block_signature)
            tier = FetchTier(tier + 1)
            continue

        # TRANSIENT / HARD_FAIL / RATE_LIMITED already handled by _fetch_at_tier's
        # internal retry_policy; if we got here, no further tier escalation helps.
        last_result = result
        break

    # Exhausted. Either return last_result (HARD_FAIL etc) or wrap as ladder-exhausted.
    if attempts and attempts[-1] >= FetchTier.UNLOCKER:
        emit("FETCH_LADDER_EXHAUSTED", attempts=attempts)
        # Caller (jugnu_runner) will route to DLQ.

    return last_result or FetchResult(
        outcome=BOT_BLOCKED,
        fetch_tier_used=attempts[-1],
        fetch_tier_attempts=attempts,
        error_sig="LADDER_EXHAUSTED",
    )
```

### 7.1 `_fetch_at_tier(task, tier, profile)` — the per-tier fetch

This is where the existing retry/proxy/identity logic lives, parameterized by tier:

| Tier | What `_fetch_at_tier` does |
|---|---|
| `DIRECT` | Existing flow: no proxy. For RENDER → patchright stealth. For GET → curl_cffi with `impersonate="chrome120"`. |
| `DC_PROXY` | Pick from existing datacenter proxy pool, sticky by `property_id`. Health scoring already handled. |
| `RESIDENTIAL` | Pick from residential pool, sticky session, geo = property's state. **Per-pool RPS cap is lower** (residential pools are slower). |
| `UNLOCKER` | Single API call to provider (Bright Data Web Unlocker / Zyte API). No internal retry — provider does that. Body returned as if it were a normal response. **Hard cost cap per property per day applies (see §9).** |

The existing `retry_policy.decide(outcome, attempt, retry_after)` runs **inside** each tier — handling the up-to-3-attempts within-tier retry loop for `TRANSIENT` and `PROXY_ERROR`. The escalation loop wraps that.

---

## 8. Profile updates after fetch (in `profile_updater.py`)

The existing `profile_updater` runs after extraction. Add a parallel hook that runs after **fetch** to update the `FetchProfile`:

```python
def update_fetch_profile_after_fetch(
    profile: ScrapeProfile,
    result: FetchResult,
) -> ScrapeProfile:
    fp = profile.fetch

    if result.ok():
        fp.last_success_tier = result.fetch_tier_used

        if result.fetch_tier_used > fp.tier_floor:
            # Promotion: this property needs more than its floor today
            fp.tier_floor = result.fetch_tier_used
            fp.promoted_at = datetime.utcnow()
            fp.total_escalations += 1
            fp.consecutive_successes_at_floor = 1
            emit("FETCH_TIER_PERSISTED",
                 property_id=profile.canonical_id,
                 new_floor=fp.tier_floor)
        elif result.fetch_tier_used == fp.tier_floor:
            fp.consecutive_successes_at_floor += 1
            fp.consecutive_failures_at_floor = 0
        else:
            # Probe demotion succeeded
            fp.tier_floor = result.fetch_tier_used
            fp.consecutive_successes_at_floor = 1
            emit("FETCH_TIER_DEMOTED",
                 property_id=profile.canonical_id,
                 new_floor=fp.tier_floor)
    else:
        fp.consecutive_failures_at_floor += 1
        if result.outcome == "BOT_BLOCKED":
            fp.last_block_signature = result.block_signature

    return profile
```

**Auto-demotion rule (gentler than probe):**  
If `consecutive_successes_at_floor >= 30` and `tier_floor >= DC_PROXY`, schedule a probe on the next run. Sites that have been stably scraping for a month at residential should be probed for residential→datacenter demotion.

---

## 9. Cost guardrails

Anti-bot mitigation can run away. Hard caps:

| Cap | Default | Where enforced |
|---|---|---|
| Max tier escalations per property per run | 3 from current floor | `fetch()` — `max_tiers_this_run` |
| Max **lifetime** unlocker calls per property per day | 1 | `_fetch_at_tier(UNLOCKER, ...)` — checked against today's ledger |
| Max % of daily property volume on UNLOCKER | 5% | Run report — emits warning if exceeded; hard-stops escalation if exceeded by 2× |
| Max daily $ spend on residential + unlocker combined | $50 (POC budget) | Cost ledger — when crossed, all subsequent escalations to T3+ are blocked for the day |
| Max DLQ revival escalations | 1 per revival | DLQ retry path — revival doesn't get to climb the whole ladder again |

The cost ledger lives in the existing per-property cost accounting (mentioned in your run report — "Total proxy MB" line). Add `proxy_tier` and `cost_usd_estimated` fields per fetch event.

---

## 10. Integration points (file-by-file)

### 10.1 New files

- `ma_poc/models/fetch_tier.py` — `FetchTier` enum (§5.1)
- `ma_poc/fetch/tier_escalator.py` — the escalation loop (§7) factored out
- `ma_poc/fetch/providers/unlocker.py` — Bright Data / Zyte API client (single class, provider-agnostic interface)
- `ma_poc/fetch/providers/residential.py` — residential pool client (wraps existing proxy pool with geo+sticky logic)
- `tests/test_tier_escalator.py`
- `tests/test_fetch_profile_updater.py`
- `tests/test_unlocker_provider.py` (with mocked HTTP)

### 10.2 Modified files

| File | Change |
|---|---|
| `ma_poc/models/scrape_profile.py` | Add `FetchProfile` sub-model; embed in `ScrapeProfile` |
| `ma_poc/fetch/fetcher.py` | Replace single-tier fetch with escalation loop calling `_fetch_at_tier` |
| `ma_poc/fetch/response_classifier.py` | Extract `block_signature` (`"cf_turnstile"`, `"px_block"`, `"datadome"`, `"hcaptcha"`, etc.) when outcome is `BOT_BLOCKED` |
| `ma_poc/fetch/retry_policy.py` | Confirm it does NOT retry `BOT_BLOCKED` — escalator handles that |
| `services/profile_updater.py` | Add `update_fetch_profile_after_fetch` hook |
| `ma_poc/scheduler/scheduler.py` | When building `CrawlTask`, read `profile.fetch.tier_floor` and pass as `task.starting_tier`; sample ~2% of T2+ properties for probe |
| `ma_poc/observability/events.py` | Add 4 new event types |
| `scripts/daily_runner.py` & `scripts/retry_runner.py` | Surface tier metrics in run report |
| `config/profiles/_audit/...` | Audit copies pick up new schema automatically (Pydantic handles this) |

### 10.3 Deletions

None. The escalation layer is purely additive.

---

## 11. Block signature detection

`response_classifier.classify` already returns `BOT_BLOCKED` on Cloudflare/PerimeterX/captcha signals. Extend it to **fingerprint** which one:

```python
BLOCK_SIGNATURES = [
    ("cf_challenge",    [b"Just a moment", b"cf-chl-bypass", b"cdn-cgi/challenge-platform"]),
    ("cf_turnstile",    [b"challenges.cloudflare.com/turnstile"]),
    ("px_block",        [b"px-captcha", b"_pxhd", b"PerimeterX"]),
    ("datadome",        [b"datadome", b"dd_cookie"]),
    ("akamai",          [b"ak_bmsc", b"_abck", b"reference #"]),
    ("hcaptcha",        [b"hcaptcha.com/captcha"]),
    ("recaptcha",       [b"recaptcha/api2"]),
    ("imperva",         [b"_Incapsula_Resource"]),
    ("generic_403",     []),  # fallback — 403 with no recognized signature
]
```

The signature feeds two things:
1. `profile.fetch.last_block_signature` for observability / debugging
2. **Skip-tier rules**: e.g. `cf_turnstile` is known not to be solvable by datacenter proxies, so when we see this signature at T0, jump straight to T3 (residential), skipping T2 (datacenter). This saves a known-doomed escalation step.

```python
TIER_SKIP_RULES = {
    "cf_turnstile": FetchTier.RESIDENTIAL,    # skip DC, go straight to residential
    "datadome":     FetchTier.UNLOCKER,        # known-hard, jump to unlocker
    "px_block":     FetchTier.UNLOCKER,
}
```

---

## 12. Run report additions

Add a section to the daily run report:

```
## Fetch tier distribution
| Tier         │ Properties │ Successes │ Cost   │ Avg attempts │
| DIRECT       │   384      │   382     │ $0.00  │ 1.00         │
| DC_PROXY     │    62      │    59     │ $0.18  │ 1.04         │
| RESIDENTIAL  │    38      │    36     │ $4.20  │ 1.12         │
| UNLOCKER     │    11      │    10     │ $1.05  │ 1.00         │
| DLQ_PARK     │     5      │     -     │ $0.00  │ -            │

## Escalation events today
- New promotions:           7  (DIRECT → DC_PROXY: 4, DC_PROXY → RESIDENTIAL: 2, RESIDENTIAL → UNLOCKER: 1)
- Successful demotions:     3  (DC_PROXY → DIRECT)
- Ladder exhaustions:       1  (cid 7782 — datadome at T4)
- Probes attempted:        14
- Probe success rate:      21%
```

---

## 13. Tests

### 13.1 Unit tests (`tests/test_tier_escalator.py`)

- `test_direct_success_no_escalation` — T0 returns OK, no further tiers tried
- `test_bot_blocked_escalates_to_next_tier` — T0 returns BOT_BLOCKED, T1 attempted
- `test_max_escalations_respected` — escalator stops after 3 tiers even if all fail
- `test_proxy_error_does_not_escalate_immediately` — 3 PROXY_ERRORs at same tier required
- `test_rate_limited_no_escalation` — RATE_LIMITED honors Retry-After, stays at tier
- `test_hard_fail_no_escalation` — HARD_FAIL returns immediately
- `test_skip_rule_for_cf_turnstile` — block_signature triggers tier-skip
- `test_probe_demotion_path` — probe at floor-1 succeeds, floor decreases
- `test_unlocker_daily_cap_enforced` — second unlocker call same property/day rejected

### 13.2 Profile tests (`tests/test_fetch_profile_updater.py`)

- `test_floor_promotes_after_higher_tier_success`
- `test_floor_demotes_after_probe_success`
- `test_consecutive_failures_increment`
- `test_block_signature_persisted`
- `test_legacy_profile_loads_with_default_fetch_block`

### 13.3 Integration test

End-to-end with mocked HTTP responses simulating: 5 properties unprotected (stay at T0), 2 at Cloudflare (escalate to T2), 1 at Datadome (jumps T2→T4 via skip rule), 1 unsolvable (DLQ at T5). Assert run report numbers match expected.

---

## 14. Rollout (one PR per phase)

| PR | Scope | Gate |
|---|---|---|
| **PR-A** | Data model: `FetchTier`, `FetchProfile`, `FetchResult` extension. No behavior change. | All existing tests pass; new profiles serialize correctly. |
| **PR-B** | Block-signature detection in `response_classifier`. T0-only (escalator stub returns at first tier). | Run on 50-property sample; verify signature labels in event log are sensible. |
| **PR-C** | Tier escalator with T0→T2 (datacenter only, no residential/unlocker yet). Profile persistence active. | Run full 500. Compare vs baseline: success rate up, T0 % stays ~80%, no cost spike. |
| **PR-D** | T3 residential provider integration. Skip-rule table active. | Same gate. Cost cap monitor must show <$5/day total. |
| **PR-E** | T4 unlocker provider + DLQ_PARK. Probe-demotion logic. Run report sections. | Phase A weekly gate validation. |

Each PR ships behind a config flag (`ENABLE_TIER_ESCALATION`) so rollback is one env-var flip during the first week of production observation.

---

## 15. Open questions for review

1. **Provider commitment.** §8 of `Jugnu_Robust_Crawler_Architecture.docx` flagged Bright Data vs Zyte as undecided. PR-D forces a decision. Recommend Bright Data for residential (mature pool, geo control); Zyte API for unlocker (simpler billing model). Or wrap both behind a `Provider` ABC — costs ~half a day extra.
2. **Geo matching for residential.** Sticky session bound to property's state (`WA` for Seattle, `NY` for New York). Confirm Bright Data plan supports state-level geo — required for residential tier or accept country-level only.
3. **Probe budget.** 2% daily volume = ~10 probes on 500 properties. Acceptable, or reduce to 1% during POC to be conservative on cost?
4. **DLQ revival via tier escalation.** Current spec: DLQ revival starts at the persisted floor, no escalation. Should a revival get one shot at floor+1 to handle "site changed protection"?
5. **Vision tier interaction.** A property at `DLQ_PARK` for `anti_bot_unsolvable` could still be visually scrapeable via screenshots from the unlocker. Out of scope for this PR but worth tracking.

---

*End of plan.*