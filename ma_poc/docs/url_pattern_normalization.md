# URL pattern normalization for replay matching

Once the writer persists mappings and the backfill seeds them from prior runs, the new run still won't HIT them because the replay matcher does an exact-substring compare against URLs that drift between runs (rotated api_keys, session tokens, timestamps).

The fix: normalize both sides of the substring compare to `host/path` only — `host/path` survives query-param drift.

## What the matcher does today

`ma_poc/pms/adapters/generic.py:952`:

```python
for resp in api_responses:
    if pat in resp.get("url", ""):
        # match — try replay
```

`pat` is whatever the LLM extractor wrote into `LlmFieldMapping.api_url_pattern`. The writer (`services/profile_updater.py:265`) stores it raw — no normalization.

## What the LLM extractor writes

`services/llm_extractor.py:571`: `"api_url_pattern": api_url` — the full URL as captured at LLM-analysis time. If that capture had `?api_key=ABC123` or `?session=XYZ`, it goes into the saved pattern verbatim.

## Why the substring match fails

DB ground truth (2026-05-10 — 3 saved mappings across 5,054 profiles):

| Property | Saved pattern | Replay status |
|---|---|---|
| 37685 (Sweetwater FL) | `https://www.liveatsweetwaterfl.com/api/v3/floorplans/all/?api_key=2c704f12feb4f8613760dda184bd08d7e37785e4` | `consecutive_replay_failures: 1` |
| 55938 (Tech Ridge) | `https://api.ws.realpage.com/v2/property/8737993/floorplans` | `consecutive_replay_failures: 1` |
| 8166 (Wall Street) | `https://api.ws.realpage.com/v2/property/4481210/floorplans` | `consecutive_replay_failures: 0` (just-saved on 2026-05-09) |

Sweetwater is a textbook URL-drift miss: when the new run captures the same endpoint with `?api_key=DIFFERENT_VALUE` (rotated, re-fetched, whatever) the substring match fails because `"...api_key=2c704f12..."` is not contained in `"...api_key=NEW_VALUE..."`. RealPage URLs are clean (no query) so they survive URL-drift but die on envelope drift instead — that's a separate fix in PR 6.

## Why this kept hitting 38 of 41 daily LLM-API winners

Every replay miss costs an LLM API analysis at ~$0.005. PR 3's `profile_replay_hit_rate < 30%` SLO breach is mostly URL drift + envelope drift. Fixing URL drift is cheap and contained; envelope drift is its own design discussion.

## Fix (PR 5)

1. **Normalize at write time.** `save_llm_field_mapping` strips scheme + query + fragment, lowercases host. Stored pattern becomes `host/path` only.
2. **Normalize at read time.** Replay matcher normalizes both the saved pattern and the incoming `resp["url"]` before substring match. Old un-normalized patterns are tolerated — normalize-on-read collapses them too.
3. **Same helper used by `_url_pattern_from` in jugnu.py** — the inconsistency between Channel 1 (raw) and Channel 4 (normalized) is removed.

## Migration

Existing un-normalized patterns coexist with new normalized ones. Both work because the matcher normalizes both sides at compare time. After ~1 day of runs the cache converges (writers upsert by `api_url_pattern`; the new normalized form replaces the old full-URL form).

## What this PR does NOT do

- Wildcard / regex pattern matching for highly-variable URLs (path-id rewriting, etc.). Could be added in a follow-up if normalization isn't sufficient.
- Envelope drift gate loosening. Phase 6's strict equality on `source_envelope_hash` is the other half of the replay-miss story; addressed in a future PR.
- Cross-property URL-pattern sharing. Each profile still has its own `llm_field_mappings`.

## Tests

1. **Unit: `_normalize_url_pattern`** — same path different scheme normalize same; different query strings normalize same; different paths normalize different; lowercase host normalization; preserves non-ASCII path bytes; handles bare paths (no scheme).
2. **Writer:** `save_llm_field_mapping` stores normalized form regardless of input.
3. **Matcher:** Saved pattern with query string matches an incoming URL with a different query string. Saved pattern is path-only; incoming has query — matches.
4. **Backwards compat:** Existing un-normalized stored pattern still matches via normalize-on-read.
5. **Negative: different host** — does NOT match.
6. **Real-data fixture:** Use the actual Sweetwater URL pattern + a synthetic "next-run" URL with a rotated api_key. Assert match.
