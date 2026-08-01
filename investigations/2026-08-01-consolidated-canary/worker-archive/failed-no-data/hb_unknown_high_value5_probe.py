from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_unknown_high_value5_probe")
TARGETS = {
    "1617": "https://www.crossingatriverlake.com",
    "34362": "https://www.thetamarronapts.com",
    "34785": "http://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55",
    "75314": "https://pettinaro.com/village-park-paladin/",
    "268888": "https://arthaus.mov/building-community.php?slug=arthaus-jack-london",
}


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for property_id, url in TARGETS.items():
        status, body = await hb_raw_get(url, property_id)
        if body:
            with gzip.open(ROOT / f"{property_id}.html.gz", "wb") as handle:
                handle.write(body.encode("utf-8", "replace"))
        results.append(
            {
                "property_id": int(property_id),
                "url": url,
                "status": status,
                "body_bytes": len(body.encode("utf-8", "replace")),
                "saved": bool(body),
            }
        )
    (ROOT / "summary.json").write_text(
        json.dumps({"solve_captchas": False, "results": results}, indent=2) + "\n"
    )
    print(json.dumps(results))


if __name__ == "__main__":
    asyncio.run(main())
