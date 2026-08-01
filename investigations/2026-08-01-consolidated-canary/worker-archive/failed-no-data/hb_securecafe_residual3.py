from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import (
    hb_raw_get,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_securecafe_residual3")
TARGETS = {
    "594": "https://harbinwoodbyelon.securecafe.com/onlineleasing/harbinwood/availableunits.aspx",
    "5782": "https://springfieldrenton.securecafe.com/onlineleasing/springfield-apartments-5/availableunits.aspx",
    "241538": "https://block88apts.securecafe.com/onlineleasing/block-88/availableunits.aspx",
}


async def fetch_one(property_id: str, url: str) -> dict[str, object]:
    status, body = await hb_raw_get(url, property_id)
    body_bytes = body.encode("utf-8", "replace")
    if body_bytes:
        with gzip.open(ROOT / f"{property_id}.html.gz", "wb") as handle:
            handle.write(body_bytes)
    return {
        "property_id": int(property_id),
        "url": url,
        "status": status,
        "body_bytes": len(body_bytes),
        "sha256": hashlib.sha256(body_bytes).hexdigest(),
        "hyperbrowser_sessions": hyperbrowser_property_call_count(property_id),
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    rows = await asyncio.gather(
        *(fetch_one(property_id, url) for property_id, url in TARGETS.items())
    )
    summary = {
        "guardrails": {
            "solve_captchas": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
        },
        "results": rows,
        "hyperbrowser_sessions": sum(int(r["hyperbrowser_sessions"]) for r in rows),
    }
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
