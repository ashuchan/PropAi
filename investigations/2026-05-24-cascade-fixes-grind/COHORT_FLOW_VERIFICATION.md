# Cohort flow verification — 2026-05-24

Step-wise verification of each adapter / recovery path shipped today,
showing where it sits in the cascade priority and how the fallback
chain triggers when the previous step returns empty.

---

## Overall cascade priority

```
Per property fetch:
  ↓
1. Fetcher: DIRECT (httpx)
   ├─ 403/HARD_FAIL?
   │   └─ AUTO: curl_cffi chrome120 retry            (commit 59b9102)
   │       └─ still fails → escalate
   ├─ Proxy tier (DC → RESIDENTIAL → UNLOCKER)
   │   └─ RESIDENTIAL: per-property sticky session    (commit 0bd31d4)
   │       └─ BOT_BLOCKED → force-rotate salt + retry once
   ↓ (body delivered)
   ↓
Detector picks primary PMS adapter
   ↓
Primary adapter (RentCafe / Entrata / SightMap / G5 / etc.)
   ├─ G5 adapter:
   │   └─ curl_cffi (not httpx)                       (commit 642c41b)
   │   └─ try up-to-3 ranked URN candidates
   ├─ RentManager-vanity:                              (commit 89c6c02)
   │   └─ generic.py sub-tier 2.79 (.suite-group)
   ↓ 0 units?
   ↓
Step 7b: Entrata→SightMap secondary (entrata only)
   ↓ 0 units?
   ↓
Step 8: generic fallback adapter
   ↓ 0 units?
   ↓
Step 8b: universal_recovery cascade (6 paths in order):
   1. appfolio_embed
   2. leaseleads_embed
   3. pms_portal_hop
   4. generic_dom_floorplans
      └─ Track A (live DOM via page.evaluate)
      └─ Track B (static HTML, BeautifulSoup)         (commit aad05af)
   5. sightmap_subpage                                 (commit 4eab4d9)
      └─ /floorplans/ → embed → /sightmaps/{id} API
   6. g5_recovery                                      (commit cbe18a7)
      └─ Re-runs G5Adapter when body has g5-cl URN
   ↓ first non-empty wins; rest are skipped
   ↓
F1.5 cross-page enrichment                             (commit 2910a6e)
   ├─ Direction A: units have area, no rent → probe for rent
   ├─ Direction B: units have rent, no sqft → probe for sqft  (NEW)
   └─ Partial: probe for the underrepresented dim
   ↓
F2: LLM rescue (deterministic-tier failures only)
```

---

## Per-cohort flow verification

Executed live via `step_wise_flow_test.py` — all 10 cohort flows pass:

| # | Cohort / component | Verification | Status |
|---|---|---|---|
| 1 | **RentManager adapter** | `.suite-group` marker → parser emits units | ✓ PASS |
| 2 | **SightMap recovery (univ #5)** | /floorplans/ subpage → SightMapAdapter invoked | ✓ PASS |
| 3 | **Generic DOM static scan** | BeautifulSoup finds plan cards w/o Playwright | ✓ PASS |
| 4 | **G5 URN candidates** | dataLayer first, sibling URNs second | ✓ PASS |
| 5 | **G5 adapter URN-retry** | First URN empty → second URN tried → win | ✓ PASS |
| 6 | **G5 recovery (univ #6)** | g5-cl marker → G5Adapter invoked, tier stamped | ✓ PASS |
| 7 | **Cascade priority order** | Steps fire in exact sequence: appfolio→leaseleads→portal_hop→generic_dom→sightmap→g5 | ✓ PASS |
| 8 | **F1.5 bi-directional trigger** | rent→sqft, sqft→rent, both-present no-op | ✓ PASS |
| 9 | **Session burn tracker** | 2 consecutive failures → salt advances → success keeps salt | ✓ PASS |
| 10 | **BrightData session_salt** | salt=0 vs salt=1 produces different session-id | ✓ PASS |

---

## Per-cohort priority analysis

### Cohort 1: TIER_1_API_SIGHTMAP (131 props)

```
Primary adapter (RentCafe/Funnel/Repli360) → 0 units
  ↓
Step 7b (entrata-only) → skipped (not entrata)
  ↓
Step 8 (generic fallback) → 0 units
  ↓
Step 8b cascade:
  1-3. appfolio/leaseleads/portal_hop → 0
  4. generic_dom (live + static) → 0
  5. ★ sightmap_subpage:
     - homepage body has /floorplans/ link
     - probe /floorplans/ → finds sightmap.com/embed/{TOKEN}
     - splice body into ctx (dataclasses.replace, FetchResult is frozen)
     - SightMapAdapter discovers embed → API → JSON → units
  6. g5_recovery → not reached
```

**Verified live: 11/11 = 100% find embed on /floorplans/ subpath.**

### Cohort 2: TIER_3_DOM (62 props)

```
Primary adapter (Funnel/MarketApts/RentCafe/etc.) → 0 units
  ↓
Step 8 + 8b cascade:
  1-3. → 0 (no AppFolio/LeaseLeads/portal anchors)
  4. ★ generic_dom_floorplans:
     - Track A: page.evaluate not available (curl_cffi fetcher, no Playwright)
     - Track B (NEW): _scan_static_html_for_cards on ctx.fetch_result.body
       - Buckets DOM elements by plan-class words
       - Filters: 2..50 count, ≥2 signals, <800 chars
       - First sub-path (/floorplans, /floor-plans, /availability, /apartments)
         returning ≥2 qualifying cards wins
  5-6. → not reached (4 won or all empty)
```

**Verified live: 4/8 = 50% extract units (jfmanagement, embarcat, mountainridge, arriveseattle).**

### Cohort 3: TIER_1_API_G5 (22 props)

```
Primary adapter = G5Adapter (detector picked g5)
  ├─ find_g5_urn_candidates → [canonical, sibling1, sibling2]
  ├─ For each candidate (cap 3):
  │   └─ _fetch_g5_units(urn, base_url)
  │       └─ curl_cffi chrome120 POST to inventory.g5marketingcloud.com
  │       └─ Returns JSON | None
  │   - If apartmentComplex.apartments not empty → WIN, stop
  │   - If empty (wrong sibling) → try next URN
  ├─ All candidates exhausted → _try_apollo → _TIER_API_ERROR
```

**Verified live: 22/22 = 100% recovery, 314 strict-pass units.**

### Cross-cohort: G5 misroutes via universal_recovery #6

```
Primary adapter = Knock/RentCafe/etc. (misroute) → 0 units
  ↓
Step 8 + 8b cascade:
  1-5. → 0
  6. ★ g5_recovery:
     - body has g5-cl substring (cheap regex)
     - find_g5_urn_candidates returns ≥1 URN
     - Invoke G5Adapter.extract → uses same curl_cffi + URN-retry
```

**Verified live: 4/4 cross-cohort misroutes recovered, 75 strict-pass units.**

### Cohort 6 (revisited): TIER_MERGED_CROSS_PAGE (32 props)

The F1.5 bi-directional enrichment fires here. Trigger logic:

```python
n_with_rent = ...
n_with_area = ...
if n_with_rent == 0 and n_with_area > 0:
    missing = "rent"   # Direction A (original)
elif n_with_area == 0 and n_with_rent > 0:
    missing = "sqft"   # Direction B (NEW today)
elif n_with_rent > 0 and n_with_area > 0:
    both = ...
    if both < len(units) * 0.5:
        missing = "sqft" if n_with_area < n_with_rent else "rent"

if missing:
    probe /floorplans, /floor-plans, /availability, /apartments
    parse with generic_plan_text
    merge by name (exact + substring fallback)
    skip units already carrying the target dim
```

**Honest scope:** of the 13 PLAN_LEVEL props in this cohort, 0/10 live-probed yielded sqft via subpage probing — the cohort is dominated by JS-injected SPAs (Repli360, SightMap, Decron) where static-HTML scanning can't see the data. The bi-directional architecture is correct but real lift here requires either (a) extending RentCafe SecureCafe parser to also pick up homepage sqft, or (b) JS rendering. Deferred.

---

## How to re-run verification

Quick sanity (≈3s):
```bash
.venv/bin/python -m pytest \
  tests/pms/adapters/test_universal_recovery_cascade_priority.py \
  tests/pms/test_subpage_bidirectional_enrichment.py \
  tests/pms/test_subpage_rent_enrichment.py \
  tests/pms/adapters/test_sightmap_subpage_recovery.py \
  tests/pms/adapters/test_g5_recovery.py \
  tests/pms/adapters/test_g5.py \
  tests/pms/adapters/test_generic_dom_floorplans.py \
  tests/pms/adapters/test_universal_recovery.py \
  tests/pms/adapters/test_rentmanager_vanity.py \
  tests/fetch/test_fetcher_http_bot_blocked_escalation.py \
  tests/fetch/test_provider_residential.py \
  tests/fetch/proxy/test_session_burn_tracker.py \
  tests/fetch/proxy/test_brightdata.py \
  -q
# → 223 passed
```

End-to-end live (≈30s, requires network):
```bash
.venv/bin/python step_wise_flow_test.py
# → 10/10 cohort flows verified
```
