# Pattern B1 — Replace Rent Signal Checks with Floor Plan Signals

**Status:** Planned  
**Root cause fixed:** `page_has_content_signals` reads `ctx.rent_signal_count` which is never populated — always 0, always False. More broadly, rent-signal (`$NNN`) counting is used as a proxy for "does this page have unit data?" in six different places. A marketing banner with two price mentions scores identically to a rich unit table. All these checks must be replaced by `count_floor_plan_signals()` from the signal engine.

---

## Shared utility (already created, tests passing)

`pms/signal_engine/floor_plan_signals.py` is the **single source of truth** for all floor-plan signal checks across the codebase.

Every call site imports exactly two things and no others:

```python
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals,
    SIGNAL_THRESHOLD_ANY,        # = 1  (at least one structural type)
    SIGNAL_THRESHOLD_STRUCTURAL, # = 2  (two or more distinct structural types)
)
```

`has_floor_plan_signals(text, threshold)` is the **only function** called at implementation sites.  
`count_floor_plan_signals` is an internal building block — only `has_floor_plan_signals` and the tests call it directly.  
No call site outside `floor_plan_signals.py` should import `count_floor_plan_signals` or write a bare integer threshold.

---

## Complete removal / replacement map

### 1. `pms/signal_engine/floor_plan_signals.py` — remove dead constant

**Line 101:** `_RE_RENT_STRICT` was added "for completeness" but has no callers inside this module and contradicts the goal of keeping rent patterns out of the signal engine.

```python
# DELETE these 2 lines:
# Rent / price signal (kept here for completeness — used by _characterize_html)
_RE_RENT_STRICT = re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?")
```

---

### 2. `pms/scraper.py` — `_RICH_HOP_RENT_TOKEN_RE` and `_RICH_HOP_MIN_RENT_TOKENS`

**Lines 202-203:** Rent-token heuristic for hop richness detection.

```python
# DELETE:
_RICH_HOP_RENT_TOKEN_RE = re.compile(r"\$\d{3,4}")
_RICH_HOP_MIN_RENT_TOKENS = 5
```

**Line 236** in `_link_hop_is_rich()`:

```python
# DELETE:
rent_hits = sum(1 for _ in _RICH_HOP_RENT_TOKEN_RE.finditer(body_str))
return rent_hits >= _RICH_HOP_MIN_RENT_TOKENS

# REPLACE WITH:
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals, SIGNAL_THRESHOLD_STRUCTURAL,
)
return has_floor_plan_signals(body_str, SIGNAL_THRESHOLD_STRUCTURAL)
```

Rationale: 5 `$NNN` tokens appear on marketing "starting at" pages. `SIGNAL_THRESHOLD_STRUCTURAL` (≥ 2 structural types) is a reliable proxy for an actual pricing page.

---

### 3. `pms/scraper.py` — `_RENT_SIGNAL_RE` and `_characterize_html`

**Line 975:** Definition.

```python
# DELETE:
_RENT_SIGNAL_RE = re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?", re.IGNORECASE)
```

**Lines 1020, 1029, 1042** in `_characterize_html()`:

```python
# DELETE line 1020:
rent_signals = len(_RENT_SIGNAL_RE.findall(page_html))

# Line 1029 — REPLACE the SPA score condition:
# Before:
if rent_signals == 0 and text_bytes < 5000:
# After:
if floor_plan_signals == 0 and text_bytes < 5000:

# Line 1042 — REPLACE in return dict:
# Before:
"rent_signal_count": rent_signals,
# After:
"floor_plan_signal_count": floor_plan_signals,
# (remove rent_signal_count entirely — no callers use the value after this change)
```

**Add before the return statement:**

```python
from ma_poc.pms.signal_engine.floor_plan_signals import count_floor_plan_signals

clean_text = re.sub(r"<[^>]+>", "", stripped)
floor_plan_signals = count_floor_plan_signals(clean_text)
```

---

### 4. `fetch/fetcher.py` — scroll trigger condition (lines 642-694)

**Line 642:** `import re as _re_scroll` — DELETE (the import exists only for the rent pattern search).

**Lines 645-649** — scroll trigger condition:

```python
# Before:
if (
    task.render_mode == RenderMode.RENDER
    and body_text is not None
    and len(body_text) >= 50_000
    and not _re_scroll.search(r"\$\s*\d{1,3}(?:[,]\d{3})*", body_text)
):

# After:
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals, SIGNAL_THRESHOLD_ANY,
)
if (
    task.render_mode == RenderMode.RENDER
    and body_text is not None
    and len(body_text) >= 50_000
    and not has_floor_plan_signals(body_text, SIGNAL_THRESHOLD_ANY)
):
```

Rationale: scroll fires when the page has NO floor-plan structural signals at all. Using `SIGNAL_THRESHOLD_ANY` (≥ 1) means "don't bother scrolling if even a single structural element is already present."

**Lines 675-680** — post-scroll `_rent_appeared` check:

```python
# Before:
_rent_appeared = bool(
    _re_scroll.search(r"\$\s*\d{1,3}(?:[,]\d{3})*", _body_after_scroll_text or "")
)

# After:
_fp_appeared = has_floor_plan_signals(_body_after_scroll_text or "", SIGNAL_THRESHOLD_ANY)
```

**Line 686** — update log message:

```python
# Before:  " grew=%s rent_appeared=%s",  _body_grew, _rent_appeared
# After:   " grew=%s fp_appeared=%s",    _body_grew, _fp_appeared
```

---

### 5. `fetch/fetcher.py` — portal late-render condition (lines 799-804)

**Lines 799-804:**

```python
# Before:
import re as _re_q
has_dollar_rent = bool(_re_q.search(r"\$\s?\d{3,4}(?:[,.]\d{3})?", body_text))
if not has_dollar_rent:

# After:
# (has_floor_plan_signals already imported above from scroll-trigger section)
if not has_floor_plan_signals(body_text, SIGNAL_THRESHOLD_STRUCTURAL):
```

DELETE the `import re as _re_q` line and `has_dollar_rent` variable entirely.

Rationale: the late-render wait fires when "we don't have data yet." `SIGNAL_THRESHOLD_STRUCTURAL` (≥ 2 structural types) skips the 12s wait only when the page is genuinely rich — it avoids triggering the wait on pages that already have two or more distinct floor-plan signal types.

---

### 6. `pms/adapters/generic.py` — `_re_rent` in `_extract_rent_dom_section` (line 483)

**Line 97** — `_re_rent` definition. Do NOT delete — it is also used in the LLM gate at line 1927 (handled below). After step 7 replaces that usage, `_re_rent` can be removed.

**Line 483** in `_extract_rent_dom_section()`:

```python
# Before:
if len(_re_rent.findall(text)) < 2:

# After — per Pattern E plan:
_rent_count = len(_re_rent.findall(text))
_fp_score   = count_floor_plan_signals(text)
if _rent_count < 2 and not (_rent_count >= 1 and _fp_score >= 2):
    continue
```

(Full replacement is in the Pattern E plan — `_extract_rent_dom_section` combined scoring.)

---

### 7. `pms/adapters/generic.py` — LLM gate relaxation (lines 1927, 1961)

**Lines 1927 and 1937** — `_rent_hits` in the `skip_llm` gate:

```python
# Before (lines 1927, 1937):
_rent_hits = len(_re_rent.findall(html))
...
strict_match = _text_bytes >= 5000 and _rent_hits >= 1

# After:
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals, SIGNAL_THRESHOLD_ANY,
)
# _text is the script/style-stripped version of html (already computed)
strict_match = _text_bytes >= 5000 and has_floor_plan_signals(_text, SIGNAL_THRESHOLD_ANY)
```

`broad_match` and `tiny_marketing_match` use `_kw_hits` (keyword string matching) — those are unaffected.

**Line 1961** — event emission:

```python
# Before:
rent_signals=_rent_hits,

# After:
floor_plan_signals=_fp_hits,
```

---

### 8. `pms/adapters/generic.py` — `page_has_content_signals` (line 2596-2597)

```python
# Before:
page_has_content_signals=(
    (getattr(ctx, "rent_signal_count", 0) or 0) > 0
),

# After:
page_has_content_signals=(
    (getattr(ctx, "floor_plan_signal_count", 0) or 0) >= SIGNAL_THRESHOLD_STRUCTURAL
),
```

Import at the top of `generic.py` (alongside the existing signal engine imports):
```python
from ma_poc.pms.signal_engine.floor_plan_signals import (
    has_floor_plan_signals,
    SIGNAL_THRESHOLD_ANY,
    SIGNAL_THRESHOLD_STRUCTURAL,
)
```

---

### 9. `pms/adapters/generic.py` — delete `_re_rent` and `_re_rent_loose` module-level constants

After steps 6 and 7 are complete, `_re_rent` has no remaining callers.

**Line 97** — DELETE:
```python
_re_rent = _re.compile(r"\$\s?\d{3,4}(?:[,.]\d{3})?(?:/mo|\s*/\s*month)?", _re.IGNORECASE)
```

**Lines 221-232** — DELETE `_re_rent_loose` (used only in the tertiary fallback of `_extract_rent_dom_section`, which the Pattern E change eliminates):
```python
_re_rent_loose = _re.compile(
    r"(?:starting\s+(?:at|from)|from|rent|lease|monthly|priced\s+at)\s*[:\-]?\s*"
    r"\$?\s?\d{3,4}(?:[,.]\d{3})?(?:\s*[/\-]\s*\$?\s?\d{3,4}(?:[,.]\d{3})?)?"
    r"(?:\s*/?\s*(?:mo|month|monthly))?",
    _re.IGNORECASE,
)
```

---

## Removal order (dependency-safe)

Execute in this order to avoid breaking intermediate states:

1. Add `count_floor_plan_signals` import and `floor_plan_signals` computation to `_characterize_html` (scraper.py)
2. Add `floor_plan_signal_count` field to `AdapterContext` and populate it (base.py + scraper.py ~line 582)
3. Replace `page_has_content_signals` line in generic.py (line 2596)
4. Replace scroll trigger condition in fetcher.py (line 645-649)
5. Replace post-scroll `_rent_appeared` → `_fp_appeared` in fetcher.py (line 675)
6. Replace portal late-render condition in fetcher.py (line 801)
7. Replace `_link_hop_is_rich` rent check in scraper.py (line 236)
8. Replace LLM gate `_rent_hits` → `_fp_hits` in generic.py (lines 1927, 1937, 1961)
9. Replace `_extract_rent_dom_section` selection loop in generic.py (line 483) — per Pattern E plan
10. Delete `_RENT_SIGNAL_RE` from scraper.py (line 975) — now has no callers
11. Delete `_RICH_HOP_RENT_TOKEN_RE` and `_RICH_HOP_MIN_RENT_TOKENS` from scraper.py (lines 202-203)
12. Delete `import re as _re_scroll` from fetcher.py (line 642)
13. Delete `_re_rent` from generic.py (line 97) — now has no callers
14. Delete `_re_rent_loose` from generic.py (lines 221-232) — now has no callers
15. Delete `_RE_RENT_STRICT` from floor_plan_signals.py (line 101) — never had callers

---

## Tests to write before shipping

| Test | File | What it verifies |
|---|---|---|
| `test_characterize_html_returns_fp_count_not_rent_count` | `test_scraper_characterize.py` | Return dict has `floor_plan_signal_count`, NOT `rent_signal_count` |
| `test_characterize_html_fp_zero_for_marketing` | same | Marketing-only page → `floor_plan_signal_count=0` |
| `test_characterize_html_fp_nonzero_for_unit_table` | same | "1BR/1BA 750 sqft" → `floor_plan_signal_count ≥ 3` |
| `test_link_hop_is_rich_uses_fp_signals` | `test_scraper_hop.py` | Body with 1BR/1BA → rich; body with only `$1200` → not rich |
| `test_scroll_trigger_fires_when_fp_zero` | `test_fetcher_scroll.py` | Page ≥50KB, no floor plan signals → scroll triggered |
| `test_scroll_trigger_suppressed_when_fp_nonzero` | same | Page with 1BR/1BA already in body → scroll NOT triggered |
| `test_portal_late_render_suppressed_when_fp_gte_2` | `test_fetcher_portal.py` | Portal page with floor plan signals → no 12s extra wait |
| `test_llm_gate_strict_uses_fp_signals` | `test_generic_llm_gate.py` | `strict_match` requires `_fp_hits >= 1`, not `_rent_hits >= 1` |
| `test_page_has_content_signals_true_at_threshold_2` | `test_generic_rc3.py` | `floor_plan_signal_count=2` → `page_has_content_signals=True` |
| `test_page_has_content_signals_false_below_threshold` | same | `floor_plan_signal_count=1` → `page_has_content_signals=False` |
| `test_re_rent_deleted_no_import` | `test_generic_imports.py` | `from pms.adapters.generic import _re_rent` raises `ImportError` |
| `test_rent_signal_re_deleted_no_import` | `test_scraper_imports.py` | `from pms.scraper import _RENT_SIGNAL_RE` raises `ImportError` |
