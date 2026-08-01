from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


PROPERTY_ID = "244756"
URL = "https://harvest-properties.com/project/town-commons-apartments/"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9")


def _canonical_record() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if str(row.get("apartmentid") or "").strip() == PROPERTY_ID:
                return row
    return {}


async def main() -> None:
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    raw_path = ROOT / "hb_town_commons_244756.html"
    raw_path.write_text(body, encoding="utf-8")
    soup = BeautifulSoup(body, "html.parser")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    candidate_links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        full = urljoin(URL, str(anchor.get("href") or "").strip())
        low = full.casefold()
        if not re.search(
            r"avail|floor.?plan|apply|lease|rentcafe|securecafe|resident|unit",
            low,
        ):
            continue
        if full in seen:
            continue
        seen.add(full)
        candidate_links.append(full)
    canonical = _canonical_record()
    target_host = urlparse(URL).hostname or ""
    summary = {
        "property_id": int(PROPERTY_ID),
        "property_name": str(canonical.get("name") or ""),
        "property_address": str(canonical.get("address") or ""),
        "url": URL,
        "status": status,
        "bytes": len(body.encode("utf-8")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "target_host": target_host,
        "candidate_links": candidate_links[:30],
        "provider_markers": sorted(
            marker
            for marker in (
                "rentcafe",
                "securecafe",
                "entrata",
                "realpage",
                "appfolio",
                "yardi",
                "knock",
                "resman",
            )
            if marker in body.casefold()
        ),
        "captcha_or_challenge": bool(
            re.search(r"captcha|challenge-platform|cf-chl", body, re.I)
        ),
        "address_present": "1600 Town Commons".casefold() in body.casefold(),
        "text_prefix": page_text[:500],
        "raw_path": str(raw_path),
    }
    metadata_path = ROOT / "hb_town_commons_244756_summary.json"
    metadata_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


asyncio.run(main())
