import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


PROPERTY_ID = "8119"
URL = "https://standrews.scullycompany.com/pompano-beach/st.-andrews/?is_responsive_snippet=1&snippet_type=website&occupancy_type=1&move_in_date=08/01/2026&is_collapsed=0&include_paragraph_content=1&host_domain=www.scullycompany.com"
OUTDIR = Path("/private/tmp/propai-fnd-vBkmT9")


async def main() -> None:
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    raw_path = OUTDIR / "hb_scully_8119.html"
    raw_path.write_text(body, encoding="utf-8")
    soup = BeautifulSoup(body, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = soup.get_text(" ", strip=True)
    summary = {
        "property_id": int(PROPERTY_ID),
        "url": URL,
        "status": status,
        "bytes": len(body.encode("utf-8")),
        "title": title,
        "raw_path": str(raw_path),
        "text_prefix": re.sub(r"\s+", " ", page_text)[:240],
    }
    metadata_path = OUTDIR / "hb_scully_8119_summary.json"
    metadata_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


asyncio.run(main())
