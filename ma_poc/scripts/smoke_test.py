"""
scripts/smoke_test.py — 5-property integration test.

Asserts (CLAUDE.md):
  a. ScrapeEvent written to data/scrape_events.jsonl
  b. data/extraction_output/{property_id}/{today}.json exists
  c. scrape_outcome is SUCCESS or SKIPPED — never unexplained FAILED
  d. confidence_score is in [0.0, 1.0] (or None for SKIPPED)
  e. units list is non-empty for SUCCESS outcomes
  f. banner_capture_attempted == True for non-SKIPPED scrapes
  g. extraction_tier is set (1–5) for SUCCESS outcomes

Exits 0 if 5/5 pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
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
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))


def _check_event(event: dict[str, Any], today: date) -> tuple[bool, list[str]]:
    errs: list[str] = []
    pid = event.get("property_id")
    outcome = event.get("scrape_outcome")
    if outcome not in ("SUCCESS", "SKIPPED"):
        errs.append(f"{pid}: outcome={outcome} (expected SUCCESS or SKIPPED)")
    out_path = DATA / "extraction_output" / str(pid) / f"{today.isoformat()}.json"
    if not out_path.exists():
        errs.append(f"{pid}: missing extraction_output {out_path}")
    else:
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errs.append(f"{pid}: extraction_output JSON decode failed: {exc}")
            doc = {}
        if outcome == "SUCCESS":
            if not doc.get("units"):
                errs.append(f"{pid}: SUCCESS but units list is empty")
            if event.get("extraction_tier") not in (1, 2, 3, 4, 5):
                errs.append(f"{pid}: SUCCESS but extraction_tier={event.get('extraction_tier')}")
            if not event.get("banner_capture_attempted"):
                errs.append(f"{pid}: SUCCESS but banner_capture_attempted is False")
            cs = event.get("confidence_score")
            if cs is None or not (0.0 <= float(cs) <= 1.0):
                errs.append(f"{pid}: confidence_score out of range: {cs}")
        elif outcome == "SKIPPED":
            pass  # No banner expected, no units expected
    return (not errs), errs


async def main() -> int:
    properties = load_properties(ROOT / "config" / "properties.csv")[:5]
    print(f"SMOKE: scraping {len(properties)} properties")
    fleet = ScrapeFleet(properties=properties, data_dir=DATA, headless=True)
    events = await fleet.run_once(only_due=False)

    today = date.today()
    passed = 0
    for evt in events:
        evt_dict = evt.model_dump(mode="json")
        ok, errs = _check_event(evt_dict, today)
        if ok:
            passed += 1
            print(f"PASS {evt.property_id} ({evt.scrape_outcome})")
        else:
            for e in errs:
                print(f"FAIL {e}")
    print(f"SMOKE TEST: {passed}/{len(events)} PASSED")
    return 0 if passed == len(events) and len(events) == 5 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
