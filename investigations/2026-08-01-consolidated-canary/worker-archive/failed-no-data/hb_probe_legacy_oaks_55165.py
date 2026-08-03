from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get_then
from ma_poc.pms.adapters.rentmanager import parse_rentmanager_wp_cards


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_ID = "55165"
START_URL = "https://legacyoaksapts.com/"


def _select_inventory(body: str) -> str:
    soup = BeautifulSoup(body, "lxml")
    candidates: list[tuple[int, str]] = []
    expected_host = (urlparse(START_URL).hostname or "").removeprefix("www.")
    for anchor in soup.find_all("a", href=True):
        candidate = urljoin(START_URL, str(anchor.get("href") or ""))
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").removeprefix("www.")
        if host != expected_host:
            continue
        path = (parsed.path or "").casefold()
        label = anchor.get_text(" ", strip=True).casefold()
        if "unit-availability" in path or "available-units" in path:
            priority = 0
        elif "availability" in path or "availability" in label:
            priority = 1
        elif "floor-plans" in path or "floorplans" in path:
            priority = 2
        else:
            continue
        candidates.append((priority, candidate))
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1] if candidates else ""


def _summarize(url: str, status: int, body: str) -> dict[str, object]:
    soup = BeautifulSoup(body or "", "lxml")
    rows = parse_rentmanager_wp_cards(body, url)
    strict_rows = [
        row
        for row in rows
        if str(row.get("unit_number") or "").strip()
        and isinstance(row.get("market_rent_low"), (int, float))
        and not isinstance(row.get("market_rent_low"), bool)
        and row["market_rent_low"] > 0
    ]
    return {
        "url": url,
        "status": status,
        "bytes": len((body or "").encode("utf-8")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "captcha_or_challenge": bool(
            re.search(r"captcha|challenge-platform|cf-chl|sgcaptcha", body or "", re.I)
        ),
        "published_inventory_url": _select_inventory(body),
        "parsed_rows": len(rows),
        "native_positive_rent_rows": len(strict_rows),
        "sample_rows": strict_rows[:3],
    }


async def main() -> None:
    first, followup = await hb_raw_get_then(
        START_URL,
        PROPERTY_ID,
        _select_inventory,
    )
    first_status, first_body = first
    (ROOT / "hb_legacy_oaks_55165_root.html").write_text(
        first_body,
        encoding="utf-8",
    )
    results = [_summarize(START_URL, first_status, first_body)]
    if followup is not None:
        followup_url, followup_status, followup_body = followup
        (ROOT / "hb_legacy_oaks_55165_inventory.html").write_text(
            followup_body,
            encoding="utf-8",
        )
        results.append(_summarize(followup_url, followup_status, followup_body))
    summary = {
        "property_id": int(PROPERTY_ID),
        "property_name": "Legacy Oaks",
        "captcha_solving": False,
        "session_count": 1,
        "results": results,
    }
    (ROOT / "hb_legacy_oaks_55165_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


asyncio.run(main())
