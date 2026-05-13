# Pattern E — Wire `floor_plan_signals` into the Extraction Pipeline

**Status:** Planned  
**Module already created:** `pms/signal_engine/floor_plan_signals.py` (167 tests passing)

## Shared utility — one import pattern for all call sites

Every file that performs a floor-plan signal check imports:

```python
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals,
    SIGNAL_THRESHOLD_ANY,
    SIGNAL_THRESHOLD_STRUCTURAL,
)
```

`has_floor_plan_signals(text, threshold)` is the only function called at implementation sites — never `count_floor_plan_signals` directly, never a raw integer threshold. This is the same import used by every B1 call site; see `pattern_b1_floor_plan_signal_count.md` for the full threshold semantics.


**Root cause fixed:**  
1. `_extract_rent_dom_section` picks DOM sections by `$NNN` count — selects marketing banners as often as unit tables.  
2. `has_unit_signals()` in `_merge_fns.py` does exact key matching — Entrata's `no_of_bedroom`, `square_footage`, `no_of_bathroom` miss if the alias is absent from `_UNIT_SIGNAL_KEYS`.  
3. `SourceSignal.__post_init__` only lowercases field keys — `no_of_bedroom` becomes `no_of_bedroom` (not `bedrooms`), so `FieldCombination` cross-group checks fail for the bed group even though the data is present.

---

## Files to touch

| File | Change |
|---|---|
| `pms/adapters/generic.py` | `_extract_rent_dom_section()` — combined rent + floor-plan score |
| `pms/adapters/_merge_fns.py` | `has_unit_signals()` — normalize keys before intersection |
| `pms/signal_engine/models.py` | `SourceSignal.__post_init__` — apply `normalize_field_key` |

---

## Step 1 — `_extract_rent_dom_section` (generic.py ~line 435)

### Current logic (broken)
Picks the smallest DOM element with ≥ 2 matches of `$NNN`. A marketing banner with two price mentions beats a unit table with one price but rich structural data.

### New logic — combined score

```python
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals,
    count_floor_plan_signals,   # used only here for scoring; not at call sites
    SIGNAL_THRESHOLD_ANY,
    SIGNAL_THRESHOLD_STRUCTURAL,
)

# Replace the selection loop body:
best: Any = None
best_len: int = 10**9
best_combined: int = -1

for el in soup.find_all(True):
    try:
        text = el.get_text(" ", strip=True)
    except Exception:
        continue

    _rent_count = len(_re_rent.findall(text))
    _fp_score   = count_floor_plan_signals(text)
    _combined   = _rent_count + _fp_score

    # Accept if: ≥2 rent signals  OR  (≥1 rent AND page is structurally rich)
    if _rent_count < 2 and not (
        _rent_count >= 1
        and has_floor_plan_signals(text, SIGNAL_THRESHOLD_STRUCTURAL)
    ):
        continue

    s = str(el)
    if len(s) < 500 or len(s) > max_bytes:
        continue

    # Prefer higher combined score; break ties by smaller element size
    if _combined > best_combined or (_combined == best_combined and len(s) < best_len):
        best, best_len, best_combined = el, len(s), _combined
```

Note: `count_floor_plan_signals` is used inside this loop purely for scoring (to compute `_combined`). It is not a call-site use — the acceptance gate uses `has_floor_plan_signals` with the named threshold.

### Score comparison examples

| DOM element content | rent_count | fp_score | combined | Selected? |
|---|---|---|---|---|
| "Save $200 this month! Ask about $500 specials" | 2 | 0 | 2 | Yes (only candidate) |
| "1BR/1BA — 750 sqft — $1,500/mo" | 1 | 4 | 5 | **Yes (beats banner)** |
| "1 Bed / 1 Bath starting at $1,200" | 1 | 2 | 3 | Yes (beats banner, loses to full card) |
| "Floor Plans — Studio, 1BR, 2BR available" | 0 | 2 | 2 | No (rent_count=0) |

---

## Step 2 — `has_unit_signals()` (`_merge_fns.py`)

### Current logic (broken)
```python
valued_signal_keys = sum(
    1 for k in (item.keys() & _UNIT_SIGNAL_KEYS)
    if item[k] not in (None, "", 0)
)
```
`item.keys() & _UNIT_SIGNAL_KEYS` is exact — `"no_of_bedroom"` is in `_UNIT_SIGNAL_KEYS` but `"no_of_bathroom"` is now added; `"square_footage"` is also now in the set. However field-name normalization should be the single gate, not set membership.

### New logic — normalize before intersection

```python
from ma_poc.pms.signal_engine.floor_plan_signals import normalize_field_key

def has_unit_signals(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    sample = items[: min(5, len(items))]
    quorum = max(1, len(sample) // 2)
    items_with_values = 0
    for item in sample:
        if not isinstance(item, dict):
            continue
        # Normalise keys so no_of_bedroom → bedrooms, squareFeet → sqft, etc.
        normalised = {normalize_field_key(k): v for k, v in item.items()}
        valued_signal_keys = sum(
            1
            for k in (normalised.keys() & _UNIT_SIGNAL_KEYS)
            if normalised[k] not in (None, "", 0)
        )
        if valued_signal_keys >= 2:
            items_with_values += 1
    return items_with_values >= quorum
```

---

## Step 3 — `SourceSignal.__post_init__` (`models.py`)

### Current logic (insufficient)
```python
object.__setattr__(
    self, "field_keys",
    frozenset(k.lower() for k in self.field_keys),
)
```
`"no_of_bedroom".lower()` → `"no_of_bedroom"` — still does not match the bed group in `FieldCombination` (`{"beds", "bedrooms", "no_of_bedroom"}`).  
Wait — `"no_of_bedroom"` IS in the bed group. So the current lowercase is sufficient for Entrata beds. But `"no_of_bathroom"` is NOT in the bath group until the `defaults.py` fix (already shipped). The normalization in `__post_init__` provides defense-in-depth for any future camelCase variant.

### New logic — normalize, not just lowercase

```python
from ma_poc.pms.signal_engine.floor_plan_signals import normalize_field_key

def __post_init__(self) -> None:
    # normalize_field_key() lowercases AND applies alias table (squareFeet→sqft etc.)
    object.__setattr__(
        self,
        "field_keys",
        frozenset(normalize_field_key(k) for k in self.field_keys),
    )
    # url_suffix derivation (existing, unchanged)
    if self.url_suffix is None and self.url is not None:
        ...
```

**Effect:** A `SourceSignal` built from raw Entrata API fields automatically has canonical keys by the time it reaches `FieldCombination.qualify()`. No changes needed in qualifier or defaults.

---

## Dependency order

1. `pms/signal_engine/floor_plan_signals.py` — ✅ already created  
2. `pms/signal_engine/models.py` — import `normalize_field_key`, update `__post_init__`  
3. `pms/adapters/_merge_fns.py` — import `normalize_field_key`, update `has_unit_signals`  
4. `pms/adapters/generic.py` — import `count_floor_plan_signals`, update `_extract_rent_dom_section`  

No circular imports: `floor_plan_signals` has zero imports from `adapters/` or `scraper/`.

---

## Tests to write before shipping

| Test | File | What it verifies |
|---|---|---|
| `test_extract_rent_dom_section_unit_table_beats_banner` | `test_generic_dom_section.py` | Element with 1 rent + 4 fp_score beats element with 2 rent + 0 fp_score |
| `test_extract_rent_dom_section_accepts_fp_structure_with_single_rent` | same | `_rent_count=1, _fp_score=2` accepted; `_rent_count=1, _fp_score=0` rejected |
| `test_has_unit_signals_entrata_fields_qualify` | `test_merge_fns.py` | `[{no_of_bedroom:1, no_of_bathroom:1, square_footage:750, min_rent:1500}]` → True |
| `test_has_unit_signals_camel_case_fields_qualify` | same | `[{bedRooms:2, bathRooms:1, squareFeet:900}]` → True |
| `test_has_unit_signals_null_values_rejected` | same | `[{bedrooms:None, bathrooms:None, sqft:None}]` → False |
| `test_has_unit_signals_two_signal_threshold` | same | Items with only 1 valued signal key → False |
| `test_source_signal_normalises_field_keys` | `test_qualifier.py` or new | `SourceSignal(field_keys={"squareFeet","bedRooms","bathRooms"})` → `field_keys=={"sqft","bedrooms","bathrooms"}` |
| `test_source_signal_normalised_keys_qualify` | same | Above SourceSignal qualifies via `floor_plan_bed_bath_area` combination |
