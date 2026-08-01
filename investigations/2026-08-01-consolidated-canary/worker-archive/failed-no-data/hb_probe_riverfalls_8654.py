from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import (
    hb_raw_get,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)


PROPERTY_ID = "8654"
URL = "https://www.riverfallsatbellmar.com/"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_riverfalls_8654")


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    raw = body.encode("utf-8", "replace")
    if raw:
        with gzip.open(ROOT / "root.html.gz", "wb") as handle:
            handle.write(raw)
    text = body.casefold()
    markers = [
        marker
        for marker in (
            "rentcafe",
            "securecafe",
            "onlineleasing",
            "realpage",
            "entrata",
            "resman",
            "sightmap",
            "apts247",
            "availableunits",
            "myolepropertyid",
        )
        if marker in text
    ]
    urls = sorted(
        {
            value.rstrip("\\'\"),.;")
            for value in re.findall(r"https?://[^\s<>\"']+", body, re.I)
            if any(
                token in value.casefold()
                for token in (
                    "avail",
                    "unit",
                    "floorplan",
                    "floor-plan",
                    "apply",
                    "lease",
                    "resident",
                    "portal",
                )
            )
        }
    )
    payload = {
        "property_id": int(PROPERTY_ID),
        "url": URL,
        "status": status,
        "body_bytes": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest() if raw else "",
        "identity": {
            "name_visible": "riverfalls at bellmar" in text,
            "street_visible": "10570 stone canyon" in text,
            "city_zip_visible": "dallas" in text and "75230" in text,
        },
        "provider_markers": markers,
        "candidate_urls": urls,
        "hyperbrowser_sessions": hyperbrowser_property_call_count(PROPERTY_ID),
        "session_options": {
            "solve_captchas": False,
            "stealth": False,
            "fingerprint_rotation": False,
            "residential_proxy": True,
        },
        "llm": False,
        "paid_canary": False,
    }
    (ROOT / "summary.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
