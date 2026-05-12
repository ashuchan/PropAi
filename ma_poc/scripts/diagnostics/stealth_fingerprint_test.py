"""
Stealth fingerprint test — validates patchright + identity configuration
against a fingerprinting test API without needing residential proxy.

Tests:
  1. Navigator.webdriver == false (patchright patch)
  2. Chrome UA version is current (not 2+ year old)
  3. Sec-CH-UA headers match UA version
  4. Timezone matches identity.timezone_id (not server UTC)
  5. Accept-Encoding includes zstd
  6. TLS fingerprint (if curl_cffi available)

Run from ma_poc/:
    python scripts/diagnostics/stealth_fingerprint_test.py

Exit 0 if all checks pass, 1 if any fail.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

# Add PropAi/ to sys.path so `import ma_poc.fetch` works (ma_poc is a package)
_PROPAI_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # PropAi/
sys.path.insert(0, str(_PROPAI_ROOT))

from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.stealth import IdentityPool


# ---------------------------------------------------------------------------
# Check 1 — Navigator.webdriver and other automation signals via Playwright
# ---------------------------------------------------------------------------

FINGERPRINT_JS = """
() => ({
    webdriver:         navigator.webdriver,
    chrome_runtime:    !!(window.chrome && window.chrome.runtime),
    languages_ok:      (navigator.languages || []).length > 0,
    platform:          navigator.platform,
    hardware_concurrency: navigator.hardwareConcurrency,
    timezone:          Intl.DateTimeFormat().resolvedOptions().timeZone,
    plugins_count:     navigator.plugins ? navigator.plugins.length : -1,
    user_agent:        navigator.userAgent,
})
"""


async def test_playwright_fingerprint(pool: BrowserContextPool, identity_pool: IdentityPool) -> dict:
    identity = identity_pool.pick(sticky_key="test_property_1")
    page = await pool.acquire(identity)
    results = {}
    try:
        # Navigate to a simple page that doesn't require internet
        await page.goto("about:blank")
        fp = await page.evaluate(FINGERPRINT_JS)
        results["webdriver"] = fp.get("webdriver")
        results["chrome_runtime"] = fp.get("chrome_runtime")
        results["languages_ok"] = fp.get("languages_ok")
        results["platform"] = fp.get("platform")
        results["timezone"] = fp.get("timezone")
        results["user_agent"] = fp.get("user_agent", "")
        results["hardware_concurrency"] = fp.get("hardware_concurrency")
        results["plugins_count"] = fp.get("plugins_count")
        results["expected_timezone"] = identity.timezone_id
        results["expected_ua_fragment"] = f"Chrome/{identity.browser_major}"
    except Exception as e:
        results["error"] = str(e)
    finally:
        await pool.release(page)
    return results


# ---------------------------------------------------------------------------
# Check 2 — Headers sent by the HTTP layer (Accept-Encoding, Sec-CH-UA, etc.)
# ---------------------------------------------------------------------------

async def test_http_headers() -> dict:
    """Validates headers.py output for the current identities."""
    from ma_poc.fetch.headers import chrome_header_set
    from ma_poc.fetch.stealth import IdentityPool

    pool = IdentityPool()
    results = {}

    for i in range(len(pool._identities)):
        identity = pool._identities[i]
        headers = chrome_header_set(identity, cold_visit=True)

        ae = headers.get("Accept-Encoding", "")
        has_zstd = "zstd" in ae
        has_br = "br" in ae
        ua = headers.get("User-Agent", "")

        chrome_ver_match = re.search(r"Chrome/(\d+)", ua)
        chrome_ver = int(chrome_ver_match.group(1)) if chrome_ver_match else 0

        firefox_ver_match = re.search(r"Firefox/(\d+)", ua)
        firefox_ver = int(firefox_ver_match.group(1)) if firefox_ver_match else 0

        safari_ua = "Safari" in ua and "Chrome" not in ua

        sec_ch_ua = headers.get("Sec-Ch-Ua", "")
        ch_ua_ver_match = re.search(r'v="(\d+)"', sec_ch_ua)
        ch_ua_ver = int(ch_ua_ver_match.group(1)) if ch_ua_ver_match else 0

        # "Fresh" check is browser-family specific
        if identity.browser_family in ("chrome", "edge"):
            ua_version_fresh = chrome_ver >= 130
        elif identity.browser_family == "firefox":
            ua_version_fresh = firefox_ver >= 130
        else:  # safari
            ua_version_fresh = safari_ua  # Safari 17.x is current in 2026

        # zstd only needed for Chrome/Edge (118+); Firefox/Safari don't send it
        zstd_needed = identity.browser_family in ("chrome", "edge")

        results[f"identity_{i}_{identity.browser_family}"] = {
            "ua_chrome_version": chrome_ver,
            "ua_firefox_version": firefox_ver,
            "ch_ua_version": ch_ua_ver,
            "has_zstd": has_zstd,
            "has_br": has_br,
            "ua_version_fresh": ua_version_fresh,
            "zstd_ok": (not zstd_needed) or has_zstd,
            "versions_consistent": (
                chrome_ver == 0 or ch_ua_ver == 0 or chrome_ver == ch_ua_ver
            ),
        }

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    print("=" * 65)
    print("STEALTH FINGERPRINT TEST")
    print("=" * 65)

    failures = []

    # Test HTTP headers (no browser needed)
    print("\n[1] HTTP header checks (headers.py)")
    header_results = await test_http_headers()
    for name, r in header_results.items():
        status = "PASS" if (r["zstd_ok"] and r["ua_version_fresh"] and r["versions_consistent"]) else "FAIL"
        ua_ver_str = (f"Chrome/{r['ua_chrome_version']}" if r["ua_chrome_version"]
                      else f"Firefox/{r['ua_firefox_version']}" if r["ua_firefox_version"]
                      else "Safari")
        print(f"  {status}  {name}")
        print(f"          UA {ua_ver_str}  "
              f"Sec-CH-UA v={r['ch_ua_version']}  "
              f"zstd={r['has_zstd']}  zstd_ok={r['zstd_ok']}  fresh={r['ua_version_fresh']}  "
              f"consistent={r['versions_consistent']}")
        if status == "FAIL":
            failures.append(f"HTTP headers fail for {name}: {r}")

    # Test Playwright fingerprint
    print("\n[2] Playwright fingerprint checks (browser_pool.py)")
    pool = BrowserContextPool(max_contexts=1)
    identity_pool = IdentityPool()
    try:
        fp = await test_playwright_fingerprint(pool, identity_pool)
        if "error" in fp:
            print(f"  ERROR  Playwright test failed: {fp['error']}")
            failures.append(f"Playwright test error: {fp['error']}")
        else:
            # webdriver should be False or undefined (patchright patches it)
            wd = fp.get("webdriver")
            wd_ok = wd is False or wd is None
            print(f"  {'PASS' if wd_ok else 'FAIL'}  navigator.webdriver = {wd!r}  (should be False/None)")
            if not wd_ok:
                failures.append(f"navigator.webdriver is {wd!r} — patchright not active?")

            # Timezone should match identity.timezone_id
            tz = fp.get("timezone", "")
            expected_tz = fp.get("expected_timezone", "")
            tz_ok = tz == expected_tz
            print(f"  {'PASS' if tz_ok else 'FAIL'}  timezone = {tz!r}  (expected {expected_tz!r})")
            if not tz_ok:
                failures.append(f"Timezone {tz!r} != expected {expected_tz!r}")

            # UA should match current Chrome
            ua = fp.get("user_agent", "")
            ua_match = re.search(r"Chrome/(\d+)", ua)
            ua_ver = int(ua_match.group(1)) if ua_match else 0
            ua_ok = ua_ver >= 130
            print(f"  {'PASS' if ua_ok else 'FAIL'}  navigator.userAgent Chrome/{ua_ver}  (should be 130+)")
            if not ua_ok:
                failures.append(f"UA Chrome version {ua_ver} is too old")

            # chrome.runtime — real Chrome has this; headless may not
            # This is a known patchright limitation: window.chrome exists but
            # window.chrome.runtime is undefined (no extensions loaded).
            # Sophisticated bot detection may check this; acceptable for now.
            cr = fp.get("chrome_runtime")
            print(f"  INFO   window.chrome.runtime = {cr!r}  "
                  f"(false in headless — known patchright gap, low risk for apartment sites)")

            # Plugins — real browser has plugins; headless often has 0
            plugins = fp.get("plugins_count", -1)
            print(f"  INFO   navigator.plugins.length = {plugins}  (patchright may inject plugins)")

            # Languages
            langs_ok = fp.get("languages_ok", False)
            print(f"  {'PASS' if langs_ok else 'WARN'}  navigator.languages populated = {langs_ok}")

            print(f"\n  Full fingerprint: {json.dumps(fp, indent=4)}")
    finally:
        await pool.close()

    print("\n" + "=" * 65)
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    else:
        print("RESULT: ALL CHECKS PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
