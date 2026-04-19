"""
Live validation of PMS adapters against real property websites.

Launches Playwright, navigates to each property URL, captures API responses,
runs detect_pms + the appropriate adapter, and reports results.

Usage:
    python scripts/validate_adapters_live.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.detector import detect_pms

# Real property URLs grouped by expected PMS.
# Each tuple: (canonical_id, expected_pms, url)
TEST_PROPERTIES = [
    # Entrata
    ("257356", "entrata", "https://www.hackneyhouseapartments.com/"),
    ("252511", "entrata", "https://www.introcleveland.com/"),
    # SightMap
    ("268836", "sightmap", "https://www.hawthorneattraditions.com/"),
    ("256856", "sightmap", "https://livethevive.com/"),
    # RentCafe (via Brookfield/Yardi)
    ("35593", "rentcafe", "https://www.mercdallas.com/"),
    # OneSite / RealPage
    ("293707", "onesite", "https://www.deltastreetapts.com/"),
    # AvalonBay
    ("238997", "avalonbay", "https://www.avaloncommunities.com/new-jersey/west-windsor-apartments/avalon-w-squared-at-princeton-junction/"),
    # AppFolio
    ("12807", "appfolio", "https://www.sagecanyonaz.com/"),
]

# Known API patterns to capture (same as scripts/entrata.py).
_API_PATTERNS = (
    "/api/", "/availab", "/floor", "/pricing", "/units", "/apartments",
    "/floorplans", "/floorPlan", "/listings", "/propertyunits",
    "getFloorplans", "getUnits", "sightmap.com/app/api",
    "/Apartments/module/", "wp-json/middleware",
    "realpage.com", "rentcafe.com", "securecafe.com",
)

_FALSE_POSITIVE_HOSTS = frozenset({
    "googleapis.com", "maps.googleapis.com", "go-mpulse.net",
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "connect.facebook.net", "hotjar.com", "sentry.io",
    "meetelise.com", "sierra.chat", "theconversioncloud.com",
    "nestiolistings.com", "rentgrata.com", "g5marketingcloud.com",
    "userway.org", "omni.cafe", "visitor-analytics.io",
})

_FALSE_POSITIVE_PATHS = frozenset({
    "/tag-manager/", "/mapsjs/", "/gen_204", "/analytics/", "/gtag/",
    "/pixel", "/beacon", "/widget/inbox", "/widget/contact",
})


def _looks_like_api(url: str) -> bool:
    url_lower = url.lower()
    host = urllib.parse.urlparse(url_lower).hostname or ""
    if any(fp in host for fp in _FALSE_POSITIVE_HOSTS):
        return False
    if any(fp in url_lower for fp in _FALSE_POSITIVE_PATHS):
        return False
    return any(p in url_lower for p in _API_PATTERNS)


async def scrape_one(cid: str, expected_pms: str, url: str) -> dict:
    """Scrape a single property and return validation results."""
    from playwright.async_api import async_playwright

    result = {
        "canonical_id": cid,
        "url": url,
        "expected_pms": expected_pms,
        "detected_pms": "unknown",
        "detection_confidence": 0.0,
        "adapter_used": "",
        "units_extracted": 0,
        "adapter_confidence": 0.0,
        "sample_unit": None,
        "api_urls_captured": [],
        "errors": [],
    }

    # Step 1: Offline detection
    detection = detect_pms(url)
    result["detected_pms"] = detection.pms
    result["detection_confidence"] = detection.confidence

    api_responses: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # Capture API responses
            async def _on_response(response):
                try:
                    req_url = response.url
                    if not _looks_like_api(req_url):
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct and "javascript" not in ct:
                        return
                    try:
                        body = await response.json()
                    except Exception:
                        return
                    api_responses.append({"url": req_url, "body": body})
                except Exception:
                    pass

            page.on("response", _on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(2.0)
            except Exception as e:
                result["errors"].append(f"Navigation: {type(e).__name__}: {str(e)[:100]}")

            # Get page HTML for re-detection
            try:
                html = await page.content()
                strong_detection = detect_pms(url, page_html=html)
                if strong_detection.confidence > detection.confidence:
                    detection = strong_detection
                    result["detected_pms"] = detection.pms
                    result["detection_confidence"] = detection.confidence
            except Exception:
                pass

            result["api_urls_captured"] = [r["url"][:120] for r in api_responses]

            # Step 2: Run adapter
            adapter = get_adapter(detection.pms)
            result["adapter_used"] = adapter.pms_name

            ctx = AdapterContext(
                base_url=url,
                detected=detection,
                profile=None,
                expected_total_units=None,
                property_id=cid,
            )
            ctx._api_responses = api_responses  # type: ignore[attr-defined]

            try:
                adapter_result = await adapter.extract(page, ctx)
                result["units_extracted"] = len(adapter_result.units)
                result["adapter_confidence"] = adapter_result.confidence
                if adapter_result.units:
                    result["sample_unit"] = adapter_result.units[0]
                if adapter_result.errors:
                    result["errors"].extend(adapter_result.errors[:3])
            except Exception as e:
                result["errors"].append(f"Adapter: {type(e).__name__}: {str(e)[:100]}")

            # Step 3: If primary adapter got 0 units, try generic
            if result["units_extracted"] == 0 and detection.pms != "unknown":
                generic = get_adapter("generic")
                ctx_generic = AdapterContext(
                    base_url=url,
                    detected=detection,
                    profile=None,
                    expected_total_units=None,
                    property_id=cid,
                )
                ctx_generic._api_responses = api_responses  # type: ignore[attr-defined]
                try:
                    generic_result = await generic.extract(page, ctx_generic)
                    if generic_result.units:
                        result["units_extracted"] = len(generic_result.units)
                        result["adapter_confidence"] = generic_result.confidence
                        result["adapter_used"] += " -> generic"
                        result["sample_unit"] = generic_result.units[0]
                except Exception as e:
                    result["errors"].append(f"Generic fallback: {type(e).__name__}: {str(e)[:100]}")

            await context.close()
        finally:
            await browser.close()

    return result


def _print_result(r: dict) -> None:
    pms_match = "OK" if r["detected_pms"] == r["expected_pms"] else f"MISMATCH (got {r['detected_pms']})"
    units_ok = "OK" if r["units_extracted"] > 0 else "FAIL (0 units)"

    print(f"\n{'='*70}")
    print(f"Property: {r['canonical_id']}  |  URL: {r['url'][:60]}")
    print(f"  Expected PMS:  {r['expected_pms']}")
    print(f"  Detected PMS:  {r['detected_pms']} (conf={r['detection_confidence']:.2f})  [{pms_match}]")
    print(f"  Adapter used:  {r['adapter_used']}")
    print(f"  Units found:   {r['units_extracted']}  [{units_ok}]")
    print(f"  Confidence:    {r['adapter_confidence']:.2f}")
    print(f"  APIs captured: {len(r['api_urls_captured'])}")
    for api_url in r["api_urls_captured"][:5]:
        print(f"    - {api_url}")
    if r["sample_unit"]:
        u = r["sample_unit"]
        print(f"  Sample unit:   {u.get('floor_plan_name', '?')} | "
              f"beds={u.get('bedrooms', '?')} | "
              f"rent={u.get('rent_range', '?')} | "
              f"sqft={u.get('sqft', '?')}")
    if r["errors"]:
        for err in r["errors"][:3]:
            print(f"  ERROR: {err[:100]}")


async def main() -> int:
    print("=" * 70)
    print("LIVE ADAPTER VALIDATION")
    print(f"Testing {len(TEST_PROPERTIES)} properties across PMS types")
    print("=" * 70)

    results = []
    for cid, expected, url in TEST_PROPERTIES:
        try:
            r = await scrape_one(cid, expected, url)
        except Exception as e:
            r = {
                "canonical_id": cid, "url": url, "expected_pms": expected,
                "detected_pms": "error", "detection_confidence": 0.0,
                "adapter_used": "", "units_extracted": 0,
                "adapter_confidence": 0.0, "sample_unit": None,
                "api_urls_captured": [], "errors": [str(e)[:200]],
            }
        results.append(r)
        _print_result(r)

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    total = len(results)
    pms_correct = sum(1 for r in results if r["detected_pms"] == r["expected_pms"])
    units_found = sum(1 for r in results if r["units_extracted"] > 0)
    print(f"  PMS detection correct: {pms_correct}/{total}")
    print(f"  Units extracted:       {units_found}/{total}")

    for r in results:
        status = "PASS" if r["units_extracted"] > 0 else "FAIL"
        pms_ok = "ok" if r["detected_pms"] == r["expected_pms"] else "MISMATCH"
        print(f"  [{status}] {r['canonical_id']:>8} {r['expected_pms']:>15} -> "
              f"{r['detected_pms']:>15} ({pms_ok}) units={r['units_extracted']}")

    # Write results to file
    out_path = ROOT / "data" / "adapter_validation.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nFull results: {out_path.relative_to(ROOT)}")

    return 0 if units_found == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
