# Pattern B2 — `final_url` in Visited Set

**Status:** Planned  
**Root cause fixed:** pid=11112 (risewestarlington) fetches `prospectportal.com/arlington/` three times — hops 1 and 3 both request it. The `visited` set tracks requested URLs only. When two different requested URLs redirect to the same `final_url`, the redirect destination is processed twice (full extraction cascade, LLM budget consumed twice).

---

## File to touch

`pms/scraper.py` — one insertion in `_try_link_hop`, immediately after `visited.add(sub_url)` at line ~1639.

---

## Change

```python
# Existing line:
visited.add(sub_url)

# NEW — also block the resolved destination to prevent duplicate extraction
# when two different requested URLs redirect to the same page.
_sub_final = getattr(sub_fetch, "final_url", None) or ""
if _sub_final and _sub_final != sub_url:
    visited.add(_sub_final)
```

This goes **before** the `outcome_val != "OK"` check so that even failed hops
(whose `final_url` is a redirect to a captcha page) block the destination from
being re-tried in the same run.

---

## Why `visited` not `explored`

`visited` is a per-run in-memory set. `explored` / `explored_links` are persisted
to the scrape profile and used across runs. We want cross-run deduplication via
`final_url` too — but that requires storing the final URL in the profile, which is
a separate (and optional) enhancement. The in-run fix is the minimum correct change.

---

## Tests to write before shipping

| Test | What it verifies |
|---|---|
| `test_final_url_redirect_prevents_duplicate_extraction` | Two different requested URLs that both redirect to `final_url=/floorplans` → extraction runs once, not twice |
| `test_same_requested_and_final_url_no_duplicate_entry` | When `final_url == sub_url` (no redirect), `visited` not grown unnecessarily |
| `test_failed_hop_final_url_still_blocked` | `BOT_BLOCKED` hop that redirects to captcha page → captcha URL added to `visited` |
