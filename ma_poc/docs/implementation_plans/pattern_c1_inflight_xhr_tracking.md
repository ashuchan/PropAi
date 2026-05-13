# Pattern C1 — In-Flight XHR Request Tracking

**Status:** Planned  
**Root cause fixed:** The current fixed `asyncio.sleep(2.0)` after `domcontentloaded` is a guess. An XHR that fires at second 3 is missed. A page with no XHRs wastes 2 full seconds. The 12s portal wait compounds this. There is no signal for "all relevant requests have responded."

---

## Why the `in_flight` counter gives a correctness guarantee

Playwright's `page.on("request")` fires synchronously when the browser dispatches a network request — **before** any bytes are sent. `page.on("requestfinished")` and `page.on("requestfailed")` fire when the full response body is available. Maintaining a counter:
- `+1` on every relevant `request` event
- `-1` on every corresponding `requestfinished` or `requestfailed`

When `in_flight == 0` AND 500ms of silence has elapsed since the last relevant response, **every request that was ever dispatched has completed**. No XHR can be "about to arrive" without the browser having incremented the counter first.

The 500ms idle window guards against the race condition where a new request fires at the same moment we check `in_flight == 0`.

---

## Files to touch

`fetch/fetcher.py` — `_do_render()` method only.

---

## Step-by-step

### 1. Define relevant-request filter (at module level or inside `_do_render`)

```python
_STATIC_SUFFIXES: frozenset[str] = frozenset({
    ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".mp4", ".m4v", ".map",
})

_ANALYTICS_SUBSTRINGS: tuple[str, ...] = (
    "google-analytics", "googletagmanager", "facebook.com/tr",
    "doubleclick", "hotjar.com", "segment.io", "mixpanel.com",
    "heap.io", "intercom", "drift.com", "hubspot.com",
    "crisp.chat", "zopim", "tawk.to", "chatlio",
    "sentry.io", "bugsnag.com", "rollbar.com",    # error tracking — not data
    "fonts.googleapis.com", "fonts.gstatic.com",  # font CDNs
)

def _is_relevant_request(url: str) -> bool:
    """Return True if this request could carry apartment unit data."""
    lower = url.lower().split("?")[0]
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if suffix in _STATIC_SUFFIXES:
        return False
    return not any(s in lower for s in _ANALYTICS_SUBSTRINGS)
```

### 2. Register request/finish/fail handlers **before** `page.goto()`

```python
import time as _time

_in_flight: int = 0
_last_relevant_response_ts: float = _time.monotonic()

def _on_request(req: Any) -> None:
    nonlocal _in_flight
    if _is_relevant_request(req.url):
        _in_flight += 1

def _on_finish(req: Any) -> None:
    nonlocal _in_flight, _last_relevant_response_ts
    if _is_relevant_request(req.url):
        _in_flight = max(0, _in_flight - 1)
        _last_relevant_response_ts = _time.monotonic()

page.on("request",         _on_request)
page.on("requestfinished", _on_finish)
page.on("requestfailed",   _on_finish)   # same decrement — failed = done
page.on("response",        _on_response) # existing body-capture handler; keep as-is
```

### 3. Replace `asyncio.sleep(2.0)` with quiescence wait

```python
# After page.goto() returns (domcontentloaded):
_QUIESCE_IDLE_S: float = 0.5     # seconds of silence before we consider the network done
_QUIESCE_MAX_S:  float = 8.0     # absolute hard cap

_deadline = _time.monotonic() + _QUIESCE_MAX_S
while _time.monotonic() < _deadline:
    await asyncio.sleep(0.2)
    _idle_for = _time.monotonic() - _last_relevant_response_ts
    if _in_flight == 0 and _idle_for >= _QUIESCE_IDLE_S:
        break   # network quiescent — safe to read network_log
# fall through to existing scroll / portal-wait / stability logic
```

### 4. Interaction with existing wait stages

The quiescence loop replaces the `asyncio.sleep(2.0)` post-domcontentloaded settle.  
The subsequent stages (scroll-trigger, portal late-render wait, anchor stability gate) remain **unchanged** — they are fallbacks for sites where XHRs fire only after scroll or widget interaction. After the quiescence loop, those stages still run and may capture additional XHRs.

On a fast Entrata site: quiescence exits in ~1s (XHR fires and completes quickly), so the total wait drops from `2s + 12s = 14s` to `~1s + 0s`.  
On a slow or analytics-heavy site: quiescence exits at the 8s hard cap, then portal waits apply as before.

---

## Confidence analysis

| Scenario | Outcome |
|---|---|
| XHR fires at t=0.5s, completes at t=1.2s | `in_flight` → 1 at t=0.5, → 0 at t=1.2. Quiescence exits at t=1.7s (500ms after last response). Captured. ✓ |
| No XHRs at all | `in_flight` stays 0. First 500ms check exits immediately. Overhead: one 200ms poll cycle. ✓ |
| Analytics fires continuously | Filtered by `_is_relevant_request`. Does not increment `in_flight`. Quiescence unaffected. ✓ |
| Entrata widget fires at t=3s (IntersectionObserver) | Scroll-trigger (existing, unchanged) fires the observer. Widget XHR fires. `in_flight` → 1. Quiescence waits. Captured. ✓ |
| XHR fires at exactly `in_flight==0` check time | 500ms idle window: the new request increments `in_flight` before the idle check passes, so the loop continues. Captured. ✓ |
| Site has XHR that never completes (hung request) | Hard cap at 8s exits the loop. `requestfailed` eventually decrements counter. Network_log has whatever arrived. Behaviour identical to current fixed-sleep. |

---

## Tests to write before shipping

| Test | What it verifies |
|---|---|
| `test_quiescence_exits_early_when_no_inflight` | Page with no XHRs: exits loop in ≤ 700ms, not 8s |
| `test_quiescence_waits_for_inflight_to_complete` | Mock a 1s-delayed XHR; assert `network_log` has its body before `_do_render` returns |
| `test_analytics_requests_not_counted_as_inflight` | `googletagmanager` request → `_in_flight` stays 0 |
| `test_static_asset_requests_not_counted_as_inflight` | `.js` / `.css` requests → `_in_flight` stays 0 |
| `test_hard_cap_respected_when_inflight_never_zero` | Simulate hung request; assert loop exits within `_QUIESCE_MAX_S + 0.5s` |
| `test_requestfailed_decrements_inflight` | Failed request (network error) decrements counter correctly; no underflow |
| `test_inflight_no_underflow_below_zero` | Multiple `requestfailed` events for same request → `max(0, ...)` protects against negative |
