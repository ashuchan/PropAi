# Graph-Based Hop Traversal

**Status:** Planned  
**File to touch:** `pms/scraper.py` only (no interface changes)  
**Root causes fixed:**

| # | Bug |
|---|---|
| 1 | OK-but-empty hop pages never contribute discovered links — only successful pages did, via the floor-plan accumulation path |
| 2 | Two distinct requested URLs that redirect to the same `final_url` both run full extraction (double LLM budget) |
| 3 | `dynamic_appended` cap prevented prop_subpath and portal discovery from firing once the initial queue grew |
| 4 | Floor-plan accumulation sub-pages could be interleaved with lower-priority queue entries when both arrived at the same queue position |

---

## Problem statement

`_try_link_hop` builds one flat sorted candidate list at entry, then walks it
sequentially. Three invariants break in production:

1. **Dead hop kills discovery.** If hop 1 is DEAD_URL and hop 2 fetches OK but
   returns no units, the links on hop 2's page are never added to the candidate
   list. The existing code only discovers new links from *successful* pages (via
   the floor-plan accumulation path and portal-hint harvesting).

2. **No cross-URL redirect dedup.** `visited` tracks *requested* URLs. When
   `/availability` and `/pricing` both redirect to the same destination, both
   run full extraction and consume LLM budget twice.

3. **No re-ranking after discovery.** Links found mid-traversal are appended to
   the end of the queue regardless of their score. A discovered `/floorplans`
   link with score 5600 sits behind guessed universal priors with score 4500 that
   were already queued.

---

## Solution: min-heap traversal with outgoing-edge discovery

Replace the flat `queue: list` with a min-heap keyed on accumulated edge cost
(`1 / score`). After every OK-outcome fetch — whether or not units were found —
score the page's outgoing links and push them to the heap. The heap ordering
ensures that a high-scoring link discovered at depth 2 surfaces before a
low-scoring candidate that was seeded at depth 0.

---

## Data structures

### New variables inside `_try_link_hop` (no dataclass)

```python
import heapq

# Replaces: queue: list[tuple[str, int, str]]
#           queue_idx: int
#           dynamic_appended: int
heap: list[tuple[float, int, str, int, str, int]] = []
seq:  int = 0   # monotonically-increasing tiebreaker; prevents str comparison at position 3

# Supplementary dedup guard (added alongside existing `visited` set).
# `visited`       — tracks requested URLs; prevents re-fetching.
# `visited_final` — tracks post-redirect final_urls; prevents re-extracting
#                   the same destination page reached from two different URLs.
# Neither set replaces the other.
visited_final: set[str] = set()
```

Heap entry layout:

```
(accumulated_cost: float, seq: int, url: str, depth: int, anchor: str, raw_score: int)
```

`accumulated_cost` is the sum of edge weights from the entry node to this node.
`depth` tracks structural depth for budget logging (never exposed in result
metadata — see §Fixed semantics below). `raw_score` is preserved so the loop
body can test `score < _LLM_HINT_SCORE` without recomputing.

### Module-level helpers (add near `_rank_internal_links`)

```python
_EPSILON_COST: float = 1e-9
# Used when floor-plan accumulation sub-pages must surface before all other
# heap entries. 1e-9 < 1/10_001 (profile winner cost), so EPSILON entries
# always pop first.


def _score_to_edge_weight(score: int) -> float:
    """Higher score → smaller weight → dequeued first.

    Score range in practice (after _rank_internal_links filters score <= 0):
      profile:winning_page_url  10_001  → weight 0.00009999
      LLM / portal hint         10_000  → weight 0.0001
      PMS prior                  5_000  → weight 0.0002
      keyword anchor+path        5_600  → weight 0.000178
      keyword anchor only        5_100  → weight 0.000196
      universal prior            4_500  → weight 0.000222
      keyword internal link    100-200  → weight 0.005–0.01
    """
    return 1.0 / max(score, 1)
```

---

## New pure function: `_build_initial_candidates`

Extract lines 1388–1565 of `_try_link_hop` verbatim into a standalone
non-async function. It has no I/O, no side effects, and currently cannot be
tested in isolation because it is embedded inside the async function body.

```python
def _build_initial_candidates(
    entry_url: str,
    entry_page_html: str,
    profile: Any,
    detected: DetectedPMS,
    llm_navigation_hints: list[str] | None,
    embedded_portal_hints: list[tuple[str, str]] | None,
    visited_urls: set[str],
) -> tuple[list[tuple[str, int, str]], set[str]]:
    """Merge all candidate sources into a single ranked, deduped list.

    Returns:
        candidates    — [(url, score, anchor), ...] SourceRanker-ordered,
                        filtered against visited_urls and the profile dead-link TTL.
        explored_skip — URLs to exclude that the profile recorded as dead ends.

    Pure function — callers can unit-test seed-phase priority without mocking
    jugnu_fetch or running the full async traversal.
    """
    # [verbatim: profile_top, explored_skip, pms_priors, portal_candidates,
    #  _rank_internal_links call, combined merge+sort, SourceRanker pass,
    #  visited/explored_skip filter, cap calculation]
    ...
    return candidates, explored_skip
```

`_try_link_hop` seed phase becomes two lines:

```python
candidates, explored_skip = _build_initial_candidates(
    entry_url, entry_page_html, profile, detected,
    llm_navigation_hints, embedded_portal_hints, visited,
)
if not candidates:
    return None
```

---

## Revised algorithm

### Seed phase

```python
for url, score, anchor in candidates:
    if url in session_blocked:
        continue
    heapq.heappush(heap, (_score_to_edge_weight(score), seq, url, 0, anchor, score))
    seq += 1
```

### Traversal loop

```python
while heap and hop_count < max_hops:

    acc_cost, _, sub_url, depth, anchor, score = heapq.heappop(heap)

    # Dedup: same URL may have been pushed from multiple parent nodes.
    # FIX #3 (explored gap): always record so profile_updater sees every attempt.
    if sub_url in visited:
        explored.setdefault(sub_url, False)
        continue
    if sub_url in session_blocked:
        explored.setdefault(sub_url, False)
        continue

    visited.add(sub_url)
    hop_count += 1
    idx = hop_count

    # Existing skip: drain low-priority candidates once profile winner satisfied.
    if _winning_page_satisfied and score < _LLM_HINT_SCORE and not _in_floorplan_accumulation:
        continue

    # ── FETCH ─────────────────────────────────────────────────────────────────
    try:
        sub_fetch = await jugnu_fetch(CrawlTask(url=sub_url, ...))
    except Exception as exc:
        emit(LINK_HOP_FETCHED, error=str(exc)[:200], hop_index=idx)
        continue

    outcome_val = sub_fetch.outcome.value if hasattr(sub_fetch.outcome, "value") \
                  else str(sub_fetch.outcome)
    emit(LINK_HOP_FETCHED, url=sub_url, outcome=outcome_val,
         elapsed_ms=sub_fetch.elapsed_ms,
         body_bytes=len(sub_fetch.body) if sub_fetch.body else 0,
         hop_index=idx, score=score, anchor=anchor[:60])

    # ── NON-OK BRANCH ─────────────────────────────────────────────────────────
    # DEAD_URL / HARD_FAIL have no parseable body.
    # No discovery. No heap pushes. continue is the complete action.
    if outcome_val != "OK":
        explored[sub_url] = False
        if shared_budget is not None:
            shared_budget.setdefault("_session_blocked_urls", set()).add(sub_url)
        _body_bytes = len(sub_fetch.body) if sub_fetch.body else 0
        if outcome_val in ("DEAD_URL", "HARD_FAIL") and _body_bytes < 2048 and profile is not None:
            import datetime as _dt
            try:
                profile.navigation.dead_links[sub_url] = \
                    _dt.datetime.now(_dt.timezone.utc).isoformat()
            except Exception:
                pass
        if anchor == "profile:winning_page_url" and shared_budget is not None:
            shared_budget["_winning_page_url_hop_outcome"] = "profile:winning_page_url:failed"
        continue

    # ── BODY DECODE — ONCE ────────────────────────────────────────────────────
    # All downstream consumers read from sub_html. No second decode anywhere.
    _body = sub_fetch.body
    if isinstance(_body, bytes):
        sub_html = _body.decode("utf-8", errors="replace")
    elif isinstance(_body, str):
        sub_html = _body
    else:
        sub_html = ""

    # ── COST-CAP REFRESH (unchanged) ─────────────────────────────────────────
    is_portal_hint = anchor.startswith(_EMBEDDED_PORTAL_ANCHOR_PREFIX)
    is_llm_hint = (
        (anchor.startswith(_LLM_HINT_ANCHOR_PREFIX) or score == _LLM_HINT_SCORE)
        and not is_portal_hint
    )
    if shared_budget is not None and (
        _link_hop_is_rich(sub_fetch) or is_llm_hint or is_portal_hint
    ):
        _refresh_cost_cap_for_hop(shared_budget, property_id=property_id,
                                  sub_url=sub_url, hop_index=idx)

    # ── POST-REDIRECT DEDUP ───────────────────────────────────────────────────
    # visited_final is supplementary — it catches the case where two distinct
    # requested URLs redirect to the same destination. visited (above) catches
    # the case where the same URL is requested twice.
    _sub_final_url = getattr(sub_fetch, "final_url", None) or sub_url
    if _sub_final_url != sub_url and _sub_final_url in visited_final:
        explored[sub_url] = False
        continue
    visited_final.add(_sub_final_url)

    # ── SILENT HOMEPAGE REDIRECT GUARD (unchanged) ────────────────────────────
    try:
        _entry_p = urllib.parse.urlparse(entry_url)
        _sub_p   = urllib.parse.urlparse(_sub_final_url)
        _same_host = (_sub_p.hostname or "") == (_entry_p.hostname or "")
        _entry_path = (_entry_p.path or "/").rstrip("/") or "/"
        _sub_path   = (_sub_p.path   or "/").rstrip("/") or "/"
        _redirected_to_entry = _same_host and _sub_path in (_entry_path, "/", "")
    except Exception:
        _redirected_to_entry = False

    if _redirected_to_entry and _sub_final_url != sub_url:
        explored[sub_url] = False
        log.debug("link-hop %s: silent redirect to homepage (%s) — skipping",
                  sub_url, _sub_final_url)
        continue

    # ── PROP_SUBPATH DISCOVERY — AFTER OK CHECK, BEFORE SCRAPE ───────────────
    # Derived from the fetched URL's path structure, not from page HTML.
    # Runs on every OK-outcome page regardless of whether it yields units.
    # No dynamic_appended guard — hop_count < max_hops bounds total fetches;
    # entries pushed here but never dequeued cost only a heappush.
    try:
        _parts = [p for p in urllib.parse.urlparse(sub_url).path.split("/") if p]
        if len(_parts) >= 3:
            for _psp in ("/floorplans", "/floor-plans", "/pricing", "/apartments-pricing"):
                _psp_url = sub_url.rstrip("/") + _psp
                if _psp_url not in visited and _psp_url not in session_blocked:
                    _psp_cost = acc_cost + _score_to_edge_weight(_PMS_PRIOR_SCORE + 200)
                    heapq.heappush(
                        heap,
                        (_psp_cost, seq, _psp_url, depth + 1,
                         f"prop_subpath:{_psp}", _PMS_PRIOR_SCORE + 200),
                    )
                    seq += 1
    except Exception:
        pass

    # ── LLM MONOLITHIC REFRESH (unchanged) ───────────────────────────────────
    if shared_budget is not None and is_llm_hint:
        _refresh_monolithic_budget_for_llm_hint(
            shared_budget, property_id=property_id,
            sub_url=sub_url, hop_index=idx,
        )

    # ── EXTRACTION ───────────────────────────────────────────────────────────
    try:
        sub_result = await scrape(
            base_url=sub_url, profile=profile,
            expected_total_units=expected_total_units,
            page=None, fetch_result=sub_fetch,
            csv_row=csv_row, property_id=property_id,
            shared_budget=shared_budget,
        )
    except Exception as exc:
        log.warning("link-hop scrape failed for %s: %s", sub_url, exc)
        explored[sub_url] = False
        continue

    had_data = bool(sub_result.get("units"))
    explored[sub_url] = had_data

    # ── OUTGOING-EDGE DISCOVERY ───────────────────────────────────────────────
    # Runs for every OK-outcome page — with or without units.
    # DEAD_URL/HARD_FAIL already continued above; this block is never reached
    # from a non-OK outcome.
    # Uses sub_html decoded once above — no second decode.
    if sub_html:
        for new_url, new_score, new_anchor in _rank_internal_links(
            sub_html, sub_url, limit=20
        ):
            if new_url not in visited and new_url not in session_blocked:
                _child_cost = acc_cost + _score_to_edge_weight(new_score)
                heapq.heappush(
                    heap,
                    (_child_cost, seq, new_url, depth + 1, new_anchor, new_score),
                )
                seq += 1

    # Portal hints discovered during scrape() (unchanged logic, heap version).
    for hint in (sub_result.get("_embedded_portal_hints") or []):
        try:
            url_s, portal_name = hint
        except Exception:
            continue
        url_s = str(url_s or "").strip()
        if url_s and url_s not in visited and url_s not in session_blocked:
            _portal_cost = acc_cost + _score_to_edge_weight(_EMBEDDED_PORTAL_SCORE)
            heapq.heappush(
                heap,
                (_portal_cost, seq, url_s, depth + 1,
                 f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}{portal_name}",
                 _EMBEDDED_PORTAL_SCORE),
            )
            seq += 1

    if not had_data:
        continue

    # ── SUCCESS PATH ─────────────────────────────────────────────────────────
    unit_count = len(sub_result.get("units") or [])
    if unit_count > _best_units_page[1]:
        _best_units_page = (sub_url, unit_count)

    if anchor.startswith("profile:winning_page_url") and unit_count > 1:
        _conf = getattr(profile, "confidence", None) if profile else None
        _maturity = str(getattr(_conf, "maturity", "COLD") or "COLD").upper()
        _failures = int(getattr(_conf, "consecutive_failures", 99) or 0)
        if _maturity in ("WARM", "HOT") and _failures == 0:
            _winning_page_satisfied = True

    # ── FLOOR-PLAN ACCUMULATION ───────────────────────────────────────────────
    fp_hints = sub_result.get("_embedded_floorplan_subpage_hints") or []
    if not fp_hints and not _in_floorplan_accumulation:
        # HTML fallback — uses already-decoded sub_html, no second decode.
        _HASH_PATH_RE_LOCAL = re.compile(
            r"/(?:unit|apt|apartment)-[0-9a-f]{16,}/", re.IGNORECASE
        )
        for lnk_url, lnk_score, lnk_anchor in _rank_internal_links(
            sub_html, sub_url, limit=20
        ):
            if lnk_score < 88 or lnk_url in visited:
                continue
            if _HASH_PATH_RE_LOCAL.search(lnk_url):
                continue
            try:
                import urllib.parse as _up
                _lnk_p  = _up.urlparse(lnk_url)
                _base_p = _up.urlparse(sub_url)
                _sub_path_l  = (_lnk_p.path  or "").lower()
                _base_path_l = (_base_p.path  or "").lower()
                if not _sub_path_l.startswith(_base_path_l.rstrip("/")):
                    _path_kw_match = any(
                        kw in _sub_path_l
                        for kw in ("/floorplan", "/floor-plan",
                                   "/availability", "/units",
                                   "/conventional", "/apartments")
                    )
                    if not _path_kw_match:
                        continue
            except Exception:
                pass
            fp_hints.append((lnk_url, "html_subpage"))

    if fp_hints and not _in_floorplan_accumulation:
        _in_floorplan_accumulation = True
        _first_successful_result = sub_result
        _accumulated_units.extend(sub_result.get("units") or [])
        # Checkpoint partial results for timeout recovery (unchanged).
        if shared_budget is not None:
            shared_budget["_partial_units"] = list(_accumulated_units)
            shared_budget["_partial_result"] = sub_result
            _ext_ref = shared_budget.get("_external_partial_ref")
            if isinstance(_ext_ref, dict):
                _ext_ref["units"] = list(_accumulated_units)
        if _fp_llm_selectors is None:
            _css = (sub_result.get("_llm_hints") or {}).get("css_selectors")
            if isinstance(_css, dict) and _css.get("container"):
                _fp_llm_selectors = _css
                if shared_budget is not None:
                    shared_budget["_fp_css_hint"] = _css

        # FIX: push fp-hint sub-pages at EPSILON_COST so they surface before
        # all other heap entries. Accumulation requires these pages to be
        # processed as a contiguous batch; the heap does not serialise them
        # automatically, so EPSILON forces them to the front.
        for fp_url, fp_kind in fp_hints:
            if fp_url not in visited and fp_url not in session_blocked:
                heapq.heappush(
                    heap,
                    (_EPSILON_COST, seq, fp_url, depth + 1,
                     f"{_EMBEDDED_PORTAL_ANCHOR_PREFIX}{fp_kind}",
                     _EMBEDDED_PORTAL_SCORE),
                )
                seq += 1

        emit(LINK_HOP_RECOVERED, property_id, entry_url=entry_url,
             sub_url=sub_url, units=unit_count,
             tier=sub_result.get("extraction_tier_used"),
             hop_index=idx, score=score)
        continue  # keep accumulating

    elif _in_floorplan_accumulation:
        _accumulated_units.extend(sub_result.get("units") or [])
        if shared_budget is not None:
            shared_budget["_partial_units"] = list(_accumulated_units)
            shared_budget["_partial_result"] = _first_successful_result or sub_result
            _ext_ref = shared_budget.get("_external_partial_ref")
            if isinstance(_ext_ref, dict):
                _ext_ref["units"] = list(_accumulated_units)
        if _fp_llm_selectors is None:
            _css = (sub_result.get("_llm_hints") or {}).get("css_selectors")
            if isinstance(_css, dict) and _css.get("container"):
                _fp_llm_selectors = _css
                if shared_budget is not None:
                    shared_budget["_fp_css_hint"] = _css
        emit(LINK_HOP_RECOVERED, property_id, entry_url=entry_url,
             sub_url=sub_url, units=unit_count,
             tier=sub_result.get("extraction_tier_used"),
             hop_index=idx, score=score)
        continue

    # Non-accumulation success — return immediately.
    sub_result["_link_hop_from"]   = entry_url
    sub_result["_link_hop_depth"]  = 1     # invocation-level; always 1 (see §Fixed semantics)
    sub_result["_link_hop_score"]  = score
    sub_result["_link_hop_anchor"] = anchor
    existing = sub_result.get("_explored_links") or {}
    existing.update(explored)
    sub_result["_explored_links"]  = existing
    sub_result["_best_units_page"] = _best_units_page[0] or sub_url
    sub_result["_best_units_count"] = _best_units_page[1]
    emit(LINK_HOP_RECOVERED, property_id, entry_url=entry_url,
         sub_url=sub_url, units=unit_count,
         tier=sub_result.get("extraction_tier_used"),
         hop_index=idx, score=score)
    return sub_result
```

### Finalize (unchanged)

```python
# Floor-plan accumulation cleanup — unchanged logic.
if _in_floorplan_accumulation and _first_successful_result is not None:
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for u in _accumulated_units:
        key = "|".join([
            u.get("unit_id") or u.get("unit_number") or "",
            u.get("floor_plan_name") or u.get("floor_plan_id") or "",
            str(u.get("rent_low") or u.get("market_rent_low") or ""),
        ])
        if key not in seen_ids:
            seen_ids.add(key)
            deduped.append(u)
    _first_successful_result["units"] = deduped
    existing = _first_successful_result.get("_explored_links") or {}
    existing.update(explored)
    _first_successful_result["_explored_links"] = existing
    if _best_units_page[0]:
        _first_successful_result["_best_units_page"]  = _best_units_page[0]
        _first_successful_result["_best_units_count"] = _best_units_page[1]
    return _first_successful_result

if explored:
    return {"_units_empty": True, "_explored_links": explored}
return None
```

---

## Fixed semantics

### `_link_hop_depth` stays `1`

`depth` is carried in the heap entry for budget logging only. The
`_link_hop_depth` key written into the result dict remains `1` on all code
paths. It signals "this result came from a single `_try_link_hop` invocation",
not the number of graph hops inside it. Changing the value would break
`profile_updater` consumers that check `result.get("_link_hop_depth") == 1`.

### No cost cap

The original plan proposed `accumulated_cost > max_hops` as a secondary guard.
This is dead code: real-world scores are ≥ 100 (`_rank_internal_links` filters
`score <= 0`), so accumulated cost after 7 such hops is ≤ 0.07. The cap would
never fire. `hop_count < max_hops` is the real constraint and is sufficient.

### `dynamic_appended` removed

`dynamic_appended` capped total dynamically-discovered entries across the whole
traversal. With the heap, `hop_count < max_hops` already bounds total fetches.
Entries pushed to the heap but never dequeued (because `hop_count` reaches
`max_hops` first) cost only a `heappush` — negligible. The cap is no longer
needed and its removal simplifies the prop_subpath and portal-hint push sites.

---

## Functions changed vs unchanged

| Function | Change |
|---|---|
| `_build_initial_candidates` | **New pure function** — extracts lines 1388–1565 verbatim |
| `_score_to_edge_weight` | **New helper** — 3 lines at module level |
| `_try_link_hop` | **Traversal loop rewritten** — seed phase → `_build_initial_candidates`; `queue`+`queue_idx`+`dynamic_appended` → `heap`+`seq`; add `visited_final`; add outgoing-edge discovery; prop_subpath uses heap push; fp-hints pushed at EPSILON cost; body decoded once |
| `_rank_internal_links` | Unchanged |
| `_augment_ranked_with_hints` | Unchanged (consumed inside `_build_initial_candidates`) |
| `_pms_priors_for` | Unchanged (consumed inside `_build_initial_candidates`) |
| `_link_hop_is_rich` | Unchanged |
| `_refresh_cost_cap_for_hop` | Unchanged |
| `_refresh_monolithic_budget_for_llm_hint` | Unchanged |
| `scrape`, `scrape_jugnu`, `AdapterContext` | Unchanged — `_try_link_hop` signature identical |

---

## Edge cases

**DEAD_URL generates no heap entries.** The `outcome_val != "OK"` branch fires
before `sub_html` is decoded and before any discovery block. Zero pushes. This
is correct — there is nothing to discover from a dead URL.

**OK-but-empty page does generate heap entries.** `_rank_internal_links` runs
on `sub_html` and pushes discovered links. This is the primary new behavior: a
marketing shell with no units but a `/floorplans` link now propagates that link
into the heap where it competes on score, not on queue position.

**Two requested URLs → same `final_url`.** `visited_final` catches this.
First arrival adds `final_url` to `visited_final` and proceeds to extraction.
Second arrival finds `final_url in visited_final`, sets `explored[url] = False`,
continues. `visited` prevents both from being re-fetched in later iterations.

**Redirect loops.** `visited` prevents re-fetching any URL. `visited_final`
prevents re-extracting any destination. No infinite loop is possible.

**Accumulation mode interleaving.** fp-hint sub-pages are pushed at
`_EPSILON_COST = 1e-9`, which is smaller than any real candidate's cost
(`1/10_001 ≈ 0.0001`). Every fp-hint sub-page surfaces at the front of the heap
and is dequeued before any competing node, as long as any fp-hint remains
unprocessed. This reproduces the serialised-queue guarantee of the old code.

**`_winning_page_satisfied = True` drain.** The loop keeps popping and checking
`score < _LLM_HINT_SCORE`. Heap size is bounded at roughly
`initial_candidates + max_hops × 24 ≈ 200` entries. The per-pop check is O(1);
the total drain cost is negligible.

**Heap size.** `_rank_internal_links(limit=20)` plus at most 4 prop_subpath
entries per OK page gives at most 24 pushes per hop. With `max_hops=7`, peak
heap size is approximately `initial_candidates + 7 × 24 ≈ 200` entries. At
~150 bytes per entry, peak memory is ~30 KB. Not a concern.

**Portal URLs redirecting to a different domain.** `_rank_internal_links`
already admits off-domain links that match `_LINK_HOST_KEYWORDS`. Those links
are pushed at `acc_cost + _score_to_edge_weight(portal_score)`. Their cost is
tiny (≈ 0.0001), so they surface immediately after being discovered, identical
to their behaviour in the flat-list model.

---

## Implementation order

### Step 1 — Extract `_build_initial_candidates` (pure refactor)

Move lines 1388–1565 verbatim into the new function. Change only the function
boundary: six parameters in, `(candidates, explored_skip)` out. Run the full
test suite — zero behavioral change expected. This step is a prerequisite for
step 3 because it reduces the loop body to a readable size and makes the seed
phase independently testable.

Write one unit test:
- `test_build_initial_candidates_profile_winner_first` — profile
  `winning_page_url` always at index 0 regardless of keyword-ranked candidates.

### Step 2 — Add `_score_to_edge_weight` and `_EPSILON_COST`

Add both at module level near `_rank_internal_links`. No tests needed — the
function is a one-liner with an obvious invariant.

### Step 3 — Swap queue → heap in `_try_link_hop` (seed + loop structure only)

Replace:
```python
queue: list[tuple[str, int, str]] = list(ranked)
queue_idx = 0
dynamic_appended = 0
```
with:
```python
heap: list[...] = []
seq: int = 0
```

Seed the heap from `candidates`. Replace `while queue_idx < len(queue)` /
`queue[queue_idx]` / `queue_idx += 1` with `while heap and hop_count < max_hops`
/ `heapq.heappop(heap)` / `hop_count += 1`.

Run the full test suite. At this point behaviour is identical to the current
system for the seed phase — no discovery yet, just heap ordering instead of
sorted list.

### Step 4 — Add `visited_final` and fix `explored` gap

Add `visited_final: set[str] = set()`. Add the `_sub_final_url != sub_url and
_sub_final_url in visited_final` check immediately after the silent-redirect
guard. Add `visited_final.add(_sub_final_url)`. Add
`explored.setdefault(sub_url, False)` in the `visited` early-continue branch.

Run the test suite. Write two tests:
- `test_two_urls_same_final_url_extraction_runs_once` — two seed candidates
  that both redirect to the same destination; assert extraction runs once.
- `test_visited_early_continue_sets_explored` — URL dequeued twice; assert
  `explored` entry set on second dequeue even though extraction does not run.

### Step 5 — Decode body once

Move `sub_html` decode to immediately after the `outcome_val != "OK"` continue.
Remove any later `sub_fetch.body.decode(...)` calls in the loop body and replace
with `sub_html`. Run the test suite — no behavioral change.

### Step 6 — Add outgoing-edge discovery and heap-push prop_subpath

After `if not had_data: continue` — but placed before that `continue` — add the
`_rank_internal_links(sub_html, sub_url, limit=20)` loop with heap pushes.

Move the prop_subpath block from `queue.append(...)` to `heapq.heappush(heap, ...)`.
Remove the `dynamic_appended < max_dynamic_appends` guard and the `dynamic_appended`
variable entirely.

Run the test suite. Write the key regression test:
- `test_ok_empty_hop_discovers_and_follows_floorplans_link` — entry page has one
  link to `/dead` (DEAD_URL) and one to `/empty` (OK, no units, but links to
  `/floorplans`). Assert that `/floorplans` is fetched and its units returned.

### Step 7 — Fix accumulation mode fp-hint ordering

Replace `queue.append((fp_url, _EMBEDDED_PORTAL_SCORE, ...))` in both
accumulation-entry and mid-accumulation blocks with
`heapq.heappush(heap, (_EPSILON_COST, seq, fp_url, ...))`. Run the test suite.

Write one test:
- `test_fp_accumulation_subpages_processed_before_lower_priority_candidates` —
  seed with a low-score candidate and an fp-index page; assert fp sub-pages are
  all processed before the low-score candidate is dequeued.

### Step 8 — Final sweep

- Confirm `_link_hop_depth = 1` is present on all success-path return sites.
- Confirm `dynamic_appended` has zero remaining references (`grep dynamic_appended pms/scraper.py`).
- Confirm no `cost_cap` variable exists.
- Run `pytest tests/pms/ -v --tb=short`.
- Run `ruff check pms/scraper.py`.
