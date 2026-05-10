# Quality-tiered DOM-hint eviction

The loosened save-side gate persists degraded selectors (`quality < 0.4`) clamped to `0.4`. That trades save-time strictness for replay-time give-it-a-chance. But the existing eviction policy (`consecutive_misses >= 3`) treats those low-quality saves the same as fully-validated `quality=1.0` saves — waiting 3 misses before eviction.

Net effect: a degraded entry that misses on its first replay attempt sticks around for 2 more days, costing a DOM cascade attempt each day, before finally being evicted.

## What the gate does today

`services/profile_updater.py:826-854`:

```python
if profile.dom_hints.consecutive_misses >= 3:
    profile.dom_hints.field_selectors = FieldSelectorMap()
    profile.dom_hints.consecutive_misses = 0
    emit(EventKind.DOM_HINTS_EVICTED, ...)
```

3-strikes is the right call for a `quality=1.0` entry that proved itself once — transient page issues (CDN hiccup, loading-state DOM) shouldn't prematurely lose a working hint. But for a `quality=0.4` PR-6 save that already failed self-validation against its own source HTML, the first miss IS the second strike — pretending otherwise wastes runs.

## Fix (PR 8): quality-tiered eviction

```python
threshold = 3 if profile.dom_hints.field_selectors_quality >= 0.8 else 1
if profile.dom_hints.consecutive_misses >= threshold:
    evict
```

- Validated selectors (`quality >= 0.8` — original PR-3 success branch behavior): unchanged, 3-strike resilience.
- Soft / degraded selectors (`0.4 <= quality < 0.8` — PR 6 territory + flaky validations): 1-strike.

Boundary chosen at `0.8` because that's the comment at `pms.adapters.generic.py:1867` (`ratio >= 0.8 → high quality, persist as-is`). Anything between 0.4 and 0.8 was already labelled "flaky" by the adapter at save time.

## Why this is right

PR 6 says: "save the selector even if it failed self-validation; tomorrow's page might be slightly different and the selector might work." That's the give-it-a-chance argument.

PR 8 says: "if it failed self-validation AND it failed on the next run too, it's not coming back." That's the cut-your-losses argument. The two PRs together: liberal save, aggressive prune.

For high-quality saves: 3-strike is preserved (transient issues don't destroy good work).

## Tests

1. `quality=1.0` entry, miss once: NOT evicted. (Resilience preserved.)
2. `quality=1.0` entry, miss 3 times: evicted. (Existing behavior unchanged.)
3. `quality=0.4` entry, miss once: evicted. (PR 8 aggressive prune.)
4. `quality=0.4` entry, hit once: consecutive_misses reset to 0; not evicted on subsequent miss. (Hit-resets-streak behavior preserved.)
5. `quality=0.79` (just below the 0.8 boundary): single-strike threshold applies. (Boundary check.)
6. `quality=0.80` (at boundary): 3-strike threshold applies. (Boundary check, `>=`.)

## What this PR does NOT do

- Touch the save-time gate. PR 6 owns that.
- Promote selectors from low-quality to high after a successful replay. That'd belong to PR 9 (promote-on-hint).
- Cross-property cluster-aware eviction (one property's eviction informs another's).
