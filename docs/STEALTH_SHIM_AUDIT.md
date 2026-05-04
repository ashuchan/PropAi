# Stealth Shim Audit — PR-1

## Pre-PR audit

### 1. Static scan — production code imports

```
$ grep -rn 'patchright\|rebrowser_playwright\|playwright_stealth\|stealth_async\|stealth_sync' ma_poc/ --include='*.py'
```

**Result:** Zero matches. No shim was wired in production code before this PR.
`ma_poc/fetch/browser_pool.py` imported `from playwright.async_api import async_playwright`
directly — vanilla Playwright, no anti-automation patching.

### 2. Installed packages

```
$ pip list 2>/dev/null | grep -iE 'playwright|patchright|rebrowser|stealth'
```

**Result (pre-PR):**
```
patchright         1.59.1
playwright         1.52.0
```

`playwright-stealth` and `rebrowser-playwright` are **not** present.

### 3. Runtime — which package does BrowserContextPool launch from?

```python
import inspect
from ma_poc.fetch.browser_pool import BrowserContextPool
src = inspect.getsource(BrowserContextPool)
for i, line in enumerate(src.splitlines(), 1):
    if 'import' in line or 'async_playwright' in line or '.launch' in line:
        print(f"L{i}: {line}")
```

**Pre-PR result:**
```
L20:     from playwright.async_api import async_playwright
L22:     pw = await async_playwright().start()
L23:     self._browser = await pw.chromium.launch(headless=True)
```

**Conclusion:** vanilla Playwright was wired. No stealth shim active.

---

## Decision: consolidate to patchright

### Why patchright over alternatives

| Option | Assessment |
|---|---|
| `playwright` (vanilla) | `navigator.webdriver === true`, missing Chrome runtime markers — detected by every modern bot-management edge. |
| `playwright-stealth` | JS-injection approach. Patches are themselves detectable (e.g., `navigator.plugins` override shape, `window.chrome` structure mismatches). Conflicts with patchright at the injection layer. |
| `rebrowser-playwright` | Patches CDP markers via browser binary modification. Valid approach but less actively maintained than patchright as of 2026-05. |
| `patchright` ✅ **chosen** | Drop-in replacement for `playwright`. Patches Chrome's CDP automation markers at the binary level rather than via JS injection. Actively maintained against Chrome anti-automation telemetry. Import surface is identical — no call-site changes needed beyond the import line. |

### Conditions for revisiting this decision

- If patchright stops tracking Chrome's automation counter-measures (check release cadence every 3 months).
- If F2 verdict changes to TLS_FINGERPRINT — then patchright is also needed for the Playwright RENDER path to avoid CDP detection alongside `curl_cffi` on the HTTP path.
- If a third shim with demonstrably better Cloudflare bypass rates emerges.

### Change made

`ma_poc/fetch/browser_pool.py` line 17 (TYPE_CHECKING block) and line 44 (runtime import inside `_ensure_browser`):

```python
# Before
from playwright.async_api import async_playwright   # (runtime)
from playwright.async_api import Browser, BrowserContext, Page  # (TYPE_CHECKING)

# After
from patchright.async_api import async_playwright   # (runtime)
from patchright.async_api import Browser, BrowserContext, Page  # (TYPE_CHECKING)
```

No other code changes are required — patchright's API surface is a drop-in superset of playwright's.
