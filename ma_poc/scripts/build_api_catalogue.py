"""
scripts/build_api_catalogue.py — Week 1 task.

Discover API URL patterns by scraping a 50-property seed set, capturing all
intercepted XHR/fetch responses, scoring them against required fields, and
appending the best matches to config/api_catalogue.json under "discovered".
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow `python scripts/build_api_catalogue.py` to import siblings
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extraction.tier1_api import _walk_for_units, matches_catalogue  # noqa: E402
from scraper.browser import BrowserFleet, BrowserSession  # noqa: E402
from scraper.fleet import load_properties  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "api_catalogue.json"
SEED_SIZE = 50


async def _seed_run() -> None:
    try:
        catalogue = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        catalogue = {}
    properties = load_properties(Path(__file__).resolve().parent.parent / "config" / "properties.csv")
    seed = properties[:SEED_SIZE]
    fleet = BrowserFleet(headless=True)
    discovered: dict[str, str] = catalogue.get("discovered", {})

    await fleet.start()
    try:
        for row in seed:
            session: BrowserSession = await fleet.scrape(row.property_id, row.url, row.pms_platform)
            best_url = None
            best_score = 0
            for resp in session.intercepted_api_responses:
                if not matches_catalogue(resp.url, catalogue):
                    continue
                if "json" not in (resp.content_type or "").lower():
                    continue
                try:
                    payload = json.loads(resp.body.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                units = _walk_for_units(payload)
                if not units:
                    continue
                # Score = how many records have the required fields
                score = sum(1 for u in units if u.get("unit_number") and u.get("asking_rent"))
                if score > best_score:
                    best_score = score
                    best_url = resp.url
            if best_url:
                discovered[row.property_id] = best_url
                print(f"DISCOVERED {row.property_id}: {best_url}")
            else:
                print(f"NO API for {row.property_id}")
    finally:
        await fleet.stop()

    catalogue["discovered"] = discovered
    CONFIG.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
    print(f"WROTE {len(discovered)} discovered API endpoints to {CONFIG}")


if __name__ == "__main__":
    asyncio.run(_seed_run())
