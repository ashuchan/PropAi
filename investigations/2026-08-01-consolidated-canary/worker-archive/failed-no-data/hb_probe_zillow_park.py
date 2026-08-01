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


PROPERTY_ID = "38378"
URL = "https://www.zillow.com/apartments/richmond-va/park-northside-apartments/5Xr2GJ/"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_zillow_park_38378")


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    body_bytes = body.encode("utf-8", "replace")
    if body_bytes:
        with gzip.open(ROOT / "page.html.gz", "wb") as handle:
            handle.write(body_bytes)
    summary = {
        "property_id": int(PROPERTY_ID),
        "url": URL,
        "status": status,
        "body_bytes": len(body_bytes),
        "sha256": hashlib.sha256(body_bytes).hexdigest(),
        "hyperbrowser_sessions": hyperbrowser_property_call_count(PROPERTY_ID),
        "solve_captchas": False,
    }
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
