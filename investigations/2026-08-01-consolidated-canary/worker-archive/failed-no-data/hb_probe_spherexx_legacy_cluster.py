from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
CASES = (
    (
        "259386",
        "Fairmount Towers",
        "https://www.fairmount-towers.com/floor-plans",
    ),
    (
        "70255",
        "Coventry Square",
        "https://coventrysquareapartments.com/floor-plans/",
    ),
)
PORTAL_RE = re.compile(
    r"https?://clients\.spherexx\.com/[^\"'<>\s]*availability[^\"'<>\s]*",
    re.IGNORECASE,
)


async def main() -> None:
    results = []
    for property_id, property_name, url in CASES:
        status, body = await hb_raw_get(url, property_id)
        soup = BeautifulSoup(body, "html.parser")
        portals = []
        for match in PORTAL_RE.findall(body.replace("&amp;", "&")):
            if match not in portals:
                portals.append(match)
        results.append(
            {
                "property_id": int(property_id),
                "property_name": property_name,
                "url": url,
                "status": status,
                "bytes": len(body.encode("utf-8")),
                "title": soup.title.get_text(" ", strip=True) if soup.title else "",
                "published_spherexx_availability_urls": portals,
                "captcha_or_challenge": bool(
                    re.search(r"captcha|challenge-platform|cf-chl", body, re.I)
                ),
            }
        )
    output = ROOT / "hb_spherexx_legacy_cluster_summary.json"
    output.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    print(json.dumps({"results": results}, indent=2))


asyncio.run(main())
