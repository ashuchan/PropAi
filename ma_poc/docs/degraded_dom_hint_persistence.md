# Degraded DOM-hint persistence

The DOM-selector cache (`dom_hints.field_selectors`) stays empty in the DB despite `LLM_DOM_TARGETED` extracting selectors successfully on most runs. The save-time self-validation gate is too strict — selectors that fail to 1:1 reproduce against their own source HTML get dropped, even when the failure is due to dynamic DOM state rather than a hallucination.

The fix: behind a feature flag, persist degraded selectors at the replay-gate floor (quality=0.4) so they get a chance on the next run when the page state is fresh.

## What the gate does

`pms/adapters/generic.py:1860-1925` (the LLM_DOM_TARGETED success branch):

1. The LLM returns `dom_units` AND `css_selectors`.
2. Adapter immediately replays the selectors against the same `dom_section_html` the LLM saw.
3. `quality_score = min(1.0, len(replayed) / len(dom_units))`.
4. **`if quality_score < 0.4:` → drop selectors completely**, emit `DOM_HINTS_MISS`.

The replay-side gate at `generic.py:1330` then refuses to use saved hints with `field_selectors_quality < 0.4`. So even if a low-quality save squeaked through (it can't, because of the save-side drop), the replay path rejects it.

Comment at the gate cites the rationale: "Cheaper to re-LLM than to chase a known-bad hint cycle."

## Why this is wrong in practice

The self-validation runs on the SAME HTML the LLM analyzed. A `quality_score < 0.4` here means the selectors fail to reproduce against their own source, which happens when:

1. **The LLM hallucinated a class name** that doesn't exist in the page → genuine miss, dropping is correct.
2. **The LLM's selector targets a list rendered server-side, but the LLM analyzed the inline JSON envelope** that includes more units than the DOM displays. Common on SPAs where DOM is paginated. Self-validation says 0.3 because only 30% of the API-discovered units are in the DOM at first paint.
3. **HTML had stale DOM nodes** (loading state, ghost rows) that interfere with selector matching but won't be there next run.

Cases 2 and 3 produce selectors that ARE useful tomorrow — they just don't 1:1 reproduce against today's exact HTML. Today's strict gate drops them.

## Symmetric to PR 1

PR 1 added `ENABLE_DEGRADED_MAPPING_PERSIST` so degraded mappings (no `json_paths` but with `response_envelope`) persist with `quality_score=0.5` instead of being silently dropped. Same pattern applies here: degraded DOM hints (low self-validation but present selectors) should persist behind a similar flag, default ON.

## Fix (PR 6)

1. **Add `enable_degraded_dom_persist()` to `config/feature_flags.py`**, default `True` (desired post-fix behavior; flag exists for kill-switch).
2. **In `generic.py:1898` save-side gate**: when flag is ON, persist with `max(quality_score, 0.4)` so the replay gate admits them. When flag is OFF, current strict behavior.
3. **No replay-gate change**: keep `>= 0.4` so non-bypassed degraded saves work; the flag-gated bypass clamps to 0.4 specifically so they pass.
4. **Telemetry**: when degraded-persist saves, emit `DOM_HINTS_DEGRADED_SAVED` so the analyser can count the divergence between PR-6-on and PR-6-off behavior.

## Tests

1. quality_score < 0.4 with flag ON → persists at 0.4
2. quality_score < 0.4 with flag OFF → drops as before
3. quality_score >= 0.4 → unaffected
4. quality_score = 0 (all selectors broken) with flag ON → still saves at 0.4 (operator can audit the `DOM_HINTS_DEGRADED_SAVED` events to spot truly-broken hints)
5. flag default is ON

## What this PR does NOT do

- Adapt the replay gate to retire selectors with consecutive misses — that's PR 8 ("single-miss invalidation").
- Promote hints automatically when they replay-hit on next run — that's PR 9 territory.
- Touch the LLM_DOM extraction itself.
