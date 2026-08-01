from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_scully_portal_probe")
TARGETS = {
    "43995": "https://hamiltonhall.scullycompany.com/Apartments/module/application_authentication/",
    "63191": "https://avenir.scullycompany.com/Apartments/module/application_authentication/",
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
