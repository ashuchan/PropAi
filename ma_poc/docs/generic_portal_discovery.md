# Generic Portal Discovery — runtime learning of unknown vendor iframes

**Status:** Phase 1+2 shipped 2026-05-15 · Phase 3 planned.

## Why this exists

Before this change, the scraper's portal-detection layer used two hardcoded
allow-lists:

1. `_PORTAL_URL_PATTERNS` in [pms/adapters/_html_extract.py](ma_poc/pms/adapters/_html_extract.py) — substring → portal name
2. `_LATE_RENDER_HOSTS` in [fetch/fetcher.py](ma_poc/fetch/fetcher.py) — hosts that get an 8-12s wait

Both lists block runtime learning: when a property uses a NEW leasing vendor
(SightMap, AppFolio, FortressTech, Wix Visual Data, etc.), every property
on that vendor fails until someone adds the pattern + ships a deploy.
Each new vendor we've discovered required a hand-edit:
- 2026-05-13 sightmap.com `/embed/api.js` exclusion (PIDs 68284 / 16139 / 20959)
- 2026-05-13 fortresstech.io late-render whitelist (PID 1713)
- 2026-05-15 wix-visual-data.appspot.com pattern (PIDs 46179 / 118965)
- 2026-05-15 yourcrossstreet.com pattern (PID 292955)

## Architecture

Three layers, in order of confidence:

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 1 — KNOWN PORTALS (hardcoded allow-list, score 10_000)        │
│   _PORTAL_URL_PATTERNS = ("sightmap.com/embed/", "sightmap"), ...   │
│   Fast prior; precision-tuned. Promote frequent unknowns here.      │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 2 — UNKNOWN PORTAL DISCOVERY (open by default, score 9_000)   │
│   Any cross-origin iframe NOT on _PORTAL_INFRA_BLACKLIST.           │
│   Capped at 3 unknowns/property. Emits embedded_portal              │
│   .unknown_host_seen telemetry for cross-run aggregation.           │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 3 — INFRA BLACKLIST (closed list, NEVER a portal)             │
│   _PORTAL_INFRA_BLACKLIST = analytics, maps, chat, social, ...     │
│   Maintenance-only list; one-line addition when a noise host shows  │
│   up frequently in unknown_host_seen telemetry.                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Phase 1 — Open-by-default discovery (shipped)

Added pass 6 to `_extract_portal_iframe_hints` in [pms/scraper.py](ma_poc/pms/scraper.py):
walks every `<iframe src>` in the entry HTML. For each:
- skip if already queued (passes 1-5)
- skip if host on `_PORTAL_INFRA_BLACKLIST` ([_html_extract.py](ma_poc/pms/adapters/_html_extract.py))
- skip if same-origin as entry
- otherwise queue at `_UNKNOWN_PORTAL_SCORE = 9_000` with anchor `unknown:{host}`

Cap: 3 unknown iframes per property. Prevents social-embed pages from
overflowing the hop queue with video/share widgets.

## Phase 2 — Content-based late-render (shipped)

The existing SPA-shell detector in [fetcher.py:903-955](ma_poc/fetch/fetcher.py)
already fires on:
- body ≥ 50KB
- body/text ratio > 25
- SPA framework marker present (React/Vue/Angular/Nuxt/Wix)
- anchor count < 20
- no fp_signals

This now ALSO fires on hop URLs (cross-origin iframe fetches) because the
detector is content-based, not host-based. An unknown vendor iframe that
ships an AngularJS / React shell will get the 6s late-render wait
automatically.

## Phase 3 — Profile-persisted learned hosts (planned)

When an unknown-portal URL yields units (`extract.link_hop_recovered`),
write the host to `profile.api_hints.learned_portal_hosts: [host, ...]`.
On the property's next run, the host is treated as a known portal
(score 10_000) without needing the discovery probe.

Storage: extend [models/scrape_profile.py:ApiHints](ma_poc/models/scrape_profile.py) with `learned_portal_hosts: list[str]`.
Writer: [services/profile_updater.py](ma_poc/services/profile_updater.py)
hooks into the link-hop success path.
Reader: `_extract_portal_iframe_hints` (or its caller) prepends these
hosts to the hint list at score 10_000.

## Phase 4 — Cross-run promotion (planned)

A batch script reads `embedded_portal.unknown_host_seen` events across the
last N runs. Hosts with:
- Frequency ≥ 5 properties
- Success rate (hop yielded units) ≥ 50%

become PROMOTION candidates. The script writes a `_PROMOTION_CANDIDATES`
report (`data/reports/portal_promotions_{date}.md`) listing each host
with stats. A human reviews and adds the pattern to `_PORTAL_URL_PATTERNS`
with a label like `("vendor.com", "vendor")` plus a one-line comment
citing the promotion report. This gates the precision tier on human
judgment without blocking discovery.

## Maintenance — when do we update the blacklist?

The blacklist captures hosts we're 100% confident are NOT portals.
Update only when a host shows up frequently in
`embedded_portal.unknown_host_seen` AND zero properties on it ever
yield units. Example flow:

1. Run aggregator (Phase 4 script).
2. Top 20 unknown hosts → sort by `(frequency × (1 - success_rate))`.
3. The top entries are noise candidates (high frequency, zero success).
4. Add one-line entry to `_PORTAL_INFRA_BLACKLIST` with a comment
   citing the report.

The blacklist should grow slower than the allow-list — false negatives
in the blacklist cost one wasted fetch per property; false positives
in the blacklist permanently mask real portals.

## Operational checks

The 6th pass + telemetry runs on every property. Cost:
- Regex match: ~5ms per property
- Telemetry emit: cheap append-only event ledger write
- Cap of 3 hop slots ensures bounded fetch budget

Pre-deploy gate: `embedded_portal.unknown_host_seen` count should
increase modestly (catch real new vendors) without exploding (no
blacklist gap). If counts spike >100 per run, audit the top-10 hosts
in the telemetry — likely a new infra host needs to be added.

## Files

- [pms/adapters/_html_extract.py](ma_poc/pms/adapters/_html_extract.py) — `_PORTAL_INFRA_BLACKLIST`, `_is_portal_infra_blacklisted`, `_iframe_host`
- [pms/scraper.py](ma_poc/pms/scraper.py) — 6th pass in `_extract_portal_iframe_hints`; telemetry emit in entry-iframe-hint surfacer
- [observability/events.py](ma_poc/observability/events.py) — `EventKind.EMBEDDED_PORTAL_UNKNOWN_HOST_SEEN`
- [fetch/fetcher.py](ma_poc/fetch/fetcher.py) — generic SPA-shell late-render (Phase 2, already in place since 2026-05-15)
