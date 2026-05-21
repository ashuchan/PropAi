# Block-wall cookie-mint strategy — 300-property A/B test
Date: 2026-05-21
Properties probed: 300

## Headline

| Verdict | Count | % |
|---|---:|---:|
| helped | 0 | 0.0% |
| hurt | 0 | 0.0% |
| no_change_both_ok | 179 | 59.7% |
| no_change_both_blocked | 12 | 4.0% |
| no_cookies | 109 | 36.3% |
| l1_failed | 0 | 0.0% |

**Net effect: cookie-mint helped 0, hurt 0, no-op 191.**

## NO-cookie block distribution (baseline)

| Provider | Count | % |
|---|---:|---:|
| none | 248 | 82.7% |
| http_4xx | 26 | 8.7% |
| fetch_error | 22 | 7.3% |
| cf_botfight | 1 | 0.3% |
| http_403 | 1 | 0.3% |
| http_429 | 1 | 0.3% |
| cf_iuam | 1 | 0.3% |

## WITH-cookie block distribution

| Provider | Count | % |
|---|---:|---:|
| none | 179 | 59.7% |
| http_4xx | 12 | 4.0% |

## Cookie-mint effectiveness PER block class

This is the actionable view: for each block provider seen in the baseline, what % of properties did cookie-mint help vs hurt?

| Baseline block | n | helped | hurt | no_change_ok | no_change_blocked | no_cookies | l1_failed |
|---|---:|---:|---:|---:|---:|---:|---:|
| none | 248 | 0 | 0 | 179 | 0 | 69 | 0 |
| http_4xx | 26 | 0 | 0 | 0 | 12 | 14 | 0 |
| fetch_error | 22 | 0 | 0 | 0 | 0 | 22 | 0 |
| cf_botfight | 1 | 0 | 0 | 0 | 0 | 1 | 0 |
| http_403 | 1 | 0 | 0 | 0 | 0 | 1 | 0 |
| http_429 | 1 | 0 | 0 | 0 | 0 | 1 | 0 |
| cf_iuam | 1 | 0 | 0 | 0 | 0 | 1 | 0 |

## By source sheet

| Sheet | n | helped | hurt | no_change | l1_failed |
|---|---:|---:|---:|---:|---:|
| T3_No_extraction | 173 | 0 | 0 | 126 | 0 |
| T4_No_body_antibot | 94 | 0 | 0 | 46 | 0 |
| T4_Edge_RentCafe | 22 | 0 | 0 | 17 | 0 |
| T4_Edge_Knock | 11 | 0 | 0 | 2 | 0 |

## L1 render outcomes

| Outcome | Count |
|---|---:|
| OK | 231 |
| TRANSIENT | 36 |
| DEAD_URL | 24 |
| BOT_BLOCKED | 7 |
| HARD_FAIL | 2 |

## Strategy (derived from numbers)

### 1. Per-provider routing decision

### 2. Concrete implementation

Patch `_probe._with_clearance` to gate cookie-attach by:

```python
# Only attach minted clearance cookies if the block-class
# at this host is known to benefit. Default: do not attach.
_CLEARANCE_REUSE_ALLOWLIST = {
}
```

Plus: on `cf_iuam` / `cf_botfight` response detected on a `probe_get`, retry once WITHOUT cookies before escalating. Mirrors what `_rentcafe_nestin.py:630` already does defensively for Nestin's separate CF zone.

### 3. The L1-failure cohort

109 properties had L1 succeed but mint zero clearance cookies — the host has no CF/DataDome challenge to solve, OR the challenge wasn't the type that drops a clearance cookie (e.g. JS-side mitigation). These don't benefit from cookie-mint either way.

### 4. Camoufox status

Run with patchright (default). Camoufox path is broken in this worktree per the earlier 5-property test (`TypeError` on every L1). Don't reach for ENABLE_CAMOUFOX as a CF bypass until that's fixed.

