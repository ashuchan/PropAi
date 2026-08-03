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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_securecafe_residual5")
TARGETS = {
    "27080": "https://oxfordrealtygroup.securecafe.com/onlineleasing/easton-north/oleapplication.aspx?stepname=floorplan",
    "225886": "https://casabaywoodapts.securecafeapplicant.com/onlineleasing/content3/access/casa-baywood/floorplans",
    "231543": "https://autumnhills-bestrentnj.securecafe.com/onlineleasing/village-at-autumn-hills/availableunits.aspx?myolepropertyid=1026013&floorPlans=3429401",
    "266766": "https://101oxford.securecafe.com/onlineleasing/101-w-oxford/availableunits.aspx",
    "289338": "https://201walnut.securecafe.com/onlineleasing/201-walnut-avenue/availableunits.aspx",
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
