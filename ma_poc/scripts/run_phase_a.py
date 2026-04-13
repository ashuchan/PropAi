"""
scripts/run_phase_a.py — Phase A entrypoint.

Runs the full daily scrape over all properties in config/properties.csv.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from scraper.fleet import ScrapeFleet, load_properties  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "api_catalogue.json"
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))


async def main() -> int:
    properties = load_properties(ROOT / "config" / "properties.csv")
    print(f"Loaded {len(properties)} properties")
    catalogue: dict[str, Any] = {}
    if CONFIG.exists():
        try:
            catalogue = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("WARNING: api_catalogue.json is corrupted, using empty catalogue")
            catalogue = {}

    headless = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "yes")
    fleet = ScrapeFleet(
        properties=properties,
        data_dir=DATA,
        api_catalogue=catalogue,
        headless=headless,
    )
    events = await fleet.run_once(only_due=False)

    success = sum(1 for e in events if e.scrape_outcome == "SUCCESS")
    skipped = sum(1 for e in events if e.scrape_outcome == "SKIPPED")
    failed = sum(1 for e in events if e.scrape_outcome == "FAILED")
    print(f"DONE: success={success} skipped={skipped} failed={failed} total={len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
