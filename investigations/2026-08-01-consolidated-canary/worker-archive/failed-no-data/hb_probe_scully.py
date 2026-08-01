import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


ITEMS = {
    "63191": "https://avenir.scullycompany.com/philadelphia/avenir/?is_responsive_snippet=1&snippet_type=website&occupancy_type=1&move_in_date=05/30/2024&locale_code=&is_collapsed=0&include_paragraph_content=1&host_domain=www.scullycompany.com",
    "43995": "https://hamiltonhall.scullycompany.com/norristown/hamilton-hall/?is_responsive_snippet=1&snippet_type=website&occupancy_type=1&move_in_date=05/30/2024&locale_code=&is_collapsed=0&include_paragraph_content=1&host_domain=www.scullycompany.com",
}
OUTDIR = Path("/private/tmp/propai-fnd-vBkmT9")


async def main() -> None:
    pairs = await asyncio.gather(
        *(hb_raw_get(url, property_id) for property_id, url in ITEMS.items())
    )
    summary = []
    for (property_id, url), (status, body) in zip(ITEMS.items(), pairs, strict=True):
        raw_path = OUTDIR / f"hb_scully_{property_id}.html"
        raw_path.write_text(body, encoding="utf-8")
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        page_text = soup.get_text(" ", strip=True)
        summary.append(
            {
                "property_id": int(property_id),
                "url": url,
                "status": status,
                "bytes": len(body.encode("utf-8")),
                "title": title,
                "raw_path": str(raw_path),
                "text_prefix": re.sub(r"\s+", " ", page_text)[:240],
            }
        )
    metadata_path = OUTDIR / "hb_scully_63191_43995_summary.json"
    metadata_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


asyncio.run(main())
