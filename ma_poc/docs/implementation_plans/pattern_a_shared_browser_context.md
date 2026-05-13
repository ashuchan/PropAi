# Pattern A — Shared BrowserContext Across Hops

**Status:** Planned  
**Root cause fixed:** Every hop creates a fresh `BrowserContext` → no session cookies, no referrer chain → securecafe/RealPage/ResMan portals block the request as a bot.

---

## Files to touch

| File | Change |
|---|---|
| `fetch/crawl_task.py` (or wherever `CrawlTask` is defined) | Add `reuse_page: Any | None = None` field |
| `fetch/fetcher.py` | `_do_render()` — skip pool acquire/release when `task.reuse_page` is set |
| `pms/scraper.py` | `_try_link_hop()` — accept `browser_page` param; pass it into `CrawlTask` |
| `pms/scraper.py` | `scrape()` — pass existing `page` into `_try_link_hop` |

---

## Step-by-step

### 1. `CrawlTask` — add `reuse_page` field

```python
# In CrawlTask dataclass:
reuse_page: Any | None = None   # live Playwright Page; skips pool acquire when set
```

### 2. `fetch/fetcher.py` — `_do_render()` conditional pool acquire

```python
if task.reuse_page is not None:
    page = task.reuse_page
    ctx_to_release = None          # caller owns the context; we must NOT close it
else:
    page, ctx_to_release = await self._pool.acquire(identity)

try:
    await page.goto(task.url, wait_until="domcontentloaded", timeout=20_000)
    # ... all existing scroll / late-render / stability / body-read logic unchanged ...
finally:
    if ctx_to_release is not None:
        await self._pool.release(ctx_to_release)
```

Key invariant: the response listener (`_on_response`) is registered on `page` before `page.goto()`.  
When we reuse the page, the listener is already registered from the entry fetch — it keeps capturing across all subsequent `page.goto()` hop navigations automatically.

### 3. `pms/scraper.py` — `_try_link_hop` signature

```python
async def _try_link_hop(
    entry_url: str,
    entry_page_html: str,
    ...,
    browser_page: Any | None = None,   # NEW — live Playwright page from entry render
) -> list[dict]:
```

When building each hop's `CrawlTask`:
```python
sub_task = CrawlTask(
    url=sub_url,
    property_id=property_id,
    priority=0,
    budget_ms=35_000,
    reason=TaskReason.SCHEDULED,
    render_mode=RenderMode.RENDER,
    parent_task_id=None,
    reuse_page=browser_page,       # NEW — pass existing page
)
```

### 4. `pms/scraper.py` — `scrape()` pass-through

`scrape()` already receives a `page` argument from the entry render path. Pass it to `_try_link_hop`:

```python
link_hop_units = await _try_link_hop(
    ...,
    browser_page=page,             # NEW
)
```

---

## Response listener continuity

No change needed. `page.on("response", _on_response)` stays registered across all `page.goto()` calls within the same Playwright Page object.  
All XHRs fired during hop navigations are captured into the same `network_log` as the entry page.

---

## Context lifetime

The `BrowserContext` is owned by the entry-page `_do_render` call and released in its `finally` block — AFTER `_try_link_hop` returns. Hop fetches never release the context.  
Stack:
```
scrape()
  └── jugnu_fetch(entry)          ← acquires context here
        └── _do_render()
              └── _try_link_hop() ← uses same page; no acquire/release
        [finally: release context] ← released here, after all hops done
```

---

## Tests to write before shipping

| Test | What it verifies |
|---|---|
| `test_hop_reuses_page_context_not_new_context` | Mock `_pool.acquire`; assert called exactly once (entry), not N+1 |
| `test_hop_response_listener_captures_xhr_from_hop_page` | XHRs fired during hop URL appear in `network_log` |
| `test_hop_cookies_from_entry_visible_on_hop_page` | Cookie set on entry page is readable on hop page JavaScript |
| `test_hop_with_none_browser_page_still_works` | `browser_page=None` falls back to pool acquire (backward compat) |
| `test_context_released_after_all_hops_complete` | `_pool.release` called once, after final hop |

---

## Why this fixes Pattern G (RealPage CWS)

When `/floor-plans.aspx` loads in the same context as the entry page, the RealPage widget initialises with the session cookies already set. The widget calls `api.ws.realpage.com/v2/property/{id}/units` with valid auth. The `_on_response` listener captures that API response. No separate `realpage_cws` credential-extraction hack needed — the authenticated XHR is captured passively.
