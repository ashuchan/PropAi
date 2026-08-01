#!/usr/bin/env python3
"""Read-only public-browser audit for the three residual G5 properties."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright


TARGETS = {
    "18389": "https://www.fmgnj.com/apartments/pa/malvern/westgate-village/floor-plans",
    "220109": "https://www.fmgnj.com/apartments/pa/elkins-park/melrose-station/floor-plans",
    "6274": "https://www.fmgnj.com/apartments/nj/voorhees/the-village-apartments/floor-plans-apply",
}
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
OUT = Path("/private/tmp/propai-fnd-vBkmT9/evidence_g5_browser3.json")

APOLLO_JS = r"""
() => {
  const c = window.__APOLLO_CLIENT__;
  if (!c || !c.cache || typeof c.cache.extract !== 'function') {
    return {present: !!c, floorplans: [], units: [], typename_counts: {}};
  }
  let d;
  try { d = c.cache.extract(); } catch (e) {
    return {present: true, error: String(e), floorplans: [], units: [], typename_counts: {}};
  }
  const ents = Object.entries(d);
  const counts = {};
  const fpById = {};
  const floorplans = [];
  for (const [, o] of ents) {
    if (!o || !o.__typename) continue;
    counts[o.__typename] = (counts[o.__typename] || 0) + 1;
    if (o.__typename === 'Floorplan') {
      fpById[String(o.id)] = {name: o.name || '', beds: o.beds, baths: o.baths, sqft: o.sqft};
      floorplans.push({
        id: o.id, name: o.name || '', beds: o.beds, baths: o.baths,
        sqft: o.sqft, startingRate: o.startingRate, endingRate: o.endingRate,
        available: o.totalAvailableUnits, hasSpecials: !!o.hasSpecials,
      });
    }
  }
  const deref = (ref) => {
    const id = ref && (ref.id || ref.__ref);
    return id ? d[id] : null;
  };
  const units = [];
  for (const [k, o] of ents) {
    if (!o || o.__typename !== 'Apartment') continue;
    let fp = null;
    if (o.floorplan && o.floorplan.__ref) fp = d[o.floorplan.__ref] || null;
    if (!fp) {
      const m = k.match(/floorplanId"?\s*:\s*"?(\d+)/);
      fp = m ? fpById[m[1]] : null;
    }
    const prices = (o.prices || []).map(deref).filter(Boolean).map((p) => ({
      value: p.value, priceType: p.priceType, formattedPrice: p.formattedPrice,
    }));
    units.push({
      id: o.id, unit: o.name || o.displayName || '', avail: o.availabilityDate || '',
      building: o.building || '', prices,
      floorplan: fp ? {id: fp.id, name: fp.name, beds: fp.beds, baths: fp.baths, sqft: fp.sqft} : null,
    });
  }
  return {present: true, entry_count: ents.length, typename_counts: counts, floorplans, units};
}
"""


async def audit_one(context, property_id: str, url: str) -> dict:
    page = await context.new_page()
    network = []
    body_tasks = []

    async def capture(response):
        low = response.url.lower()
        if any(k in low for k in ("graphql", "inventory", "floorplan", "apartment", "realpage", "onesite")):
            item = {"url": response.url, "status": response.status}
            network.append(item)
            if any(k in low for k in ("graphql", "inventory", "realpage", "onesite")):
                try:
                    text = await response.text()
                    item["body_prefix"] = text[:20000]
                except Exception as exc:
                    item["body_error"] = f"{type(exc).__name__}: {exc}"

    page.on("response", lambda response: body_tasks.append(asyncio.create_task(capture(response))))
    result = {"property_id": int(property_id), "requested_url": url}
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        result["main_status"] = response.status if response else None
        await page.wait_for_timeout(25000)
        result["final_url"] = page.url
        result["title"] = await page.title()
        result["apollo"] = await page.evaluate(APOLLO_JS)
        result["g5_store_id"] = await page.evaluate(
            "() => (window.dataLayer || []).map(x => x && x.G5_STORE_ID).filter(Boolean)"
        )
        result["visible_text_prefix"] = (await page.locator("body").inner_text())[:5000]
        result["frames"] = []
        for index, frame in enumerate(page.frames):
            frame_row = {"index": index, "url": frame.url}
            try:
                frame_row["title"] = await frame.title()
                frame_row["visible_text_prefix"] = (await frame.locator("body").inner_text())[:10000]
                frame_html = await frame.content()
                frame_path = f"/private/tmp/vendor_tail_{property_id}_g5_frame_{index}.html"
                Path(frame_path).write_text(frame_html)
                frame_row["html_path"] = frame_path
            except Exception as exc:
                frame_row["error"] = f"{type(exc).__name__}: {exc}"
            result["frames"].append(frame_row)
        html = await page.content()
        Path(f"/private/tmp/vendor_tail_{property_id}_g5_rendered.html").write_text(html)
        result["rendered_html_path"] = f"/private/tmp/vendor_tail_{property_id}_g5_rendered.html"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    if body_tasks:
        await asyncio.gather(*body_tasks, return_exceptions=True)
    result["network"] = network
    await page.close()
    return result


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
        )
        rows = []
        for property_id, url in TARGETS.items():
            rows.append(await audit_one(context, property_id, url))
        await browser.close()
    OUT.write_text(json.dumps({"targets": rows}, indent=2))
    print(OUT)
    for row in rows:
        ap = row.get("apollo") or {}
        print(row["property_id"], row.get("main_status"), row.get("title"), len(ap.get("units") or []), len(ap.get("floorplans") or []))


if __name__ == "__main__":
    asyncio.run(main())
