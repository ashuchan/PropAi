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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_rentcafe_hosted_pair")
TARGETS = {
    "218786": {
        "name": "Coopers Landing Apartments",
        "url": "https://www.rentcafe.com/apartments/mi/kalamazoo/coopers-landing-apartments/default.aspx",
        "name_token": "coopers landing apartments",
        "street_token": "5001 coopers landing",
        "city_token": "kalamazoo",
        "zip_token": "49004",
    },
    "69558": {
        "name": "Spring Hill Apartments",
        "url": "https://www.rentcafe.com/apartments/nj/summit/spring-hill-apartments/default.aspx",
        "name_token": "spring hill apartments",
        "street_token": "767 springfield",
        "city_token": "summit",
        "zip_token": "07901",
    },
}


async def probe(property_id: str, target: dict[str, str]) -> dict[str, object]:
    status, body = await hb_raw_get(target["url"], property_id)
    raw = body.encode("utf-8", "replace")
    if raw:
        with gzip.open(ROOT / f"{property_id}_root.html.gz", "wb") as handle:
            handle.write(raw)
    text = body.casefold()
    return {
        "property_id": int(property_id),
        "property_name": target["name"],
        "url": target["url"],
        "status": status,
        "body_bytes": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest() if raw else "",
        "identity": {
            "name_visible": target["name_token"] in text,
            "street_visible": target["street_token"] in text,
            "city_zip_visible": target["city_token"] in text and target["zip_token"] in text,
        },
        "provider_markers": [
            marker
            for marker in (
                "rentcafe",
                "onlineleasing",
                "myolepropertyid",
                "unitid",
                "floorplanid",
                "fp-unit",
            )
            if marker in text
        ],
        "property_ids": sorted(
            set(re.findall(r"myolepropertyid(?:=|\\u0026myolepropertyid=)(\d+)", text))
        ),
        "unit_ids": sorted(
            set(re.findall(r"unitid(?:=|\\u0026unitid=)(\d+)", text)), key=int
        ),
        "floorplan_ids": sorted(
            set(re.findall(r"floorplanid(?:=|\\u0026floorplanid=)(\d+)", text)), key=int
        ),
        "hyperbrowser_sessions": hyperbrowser_property_call_count(property_id),
        "session_options": {
            "solve_captchas": False,
            "stealth": False,
            "fingerprint_rotation": False,
            "residential_proxy": True,
        },
        "llm": False,
        "paid_canary": False,
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    results = await asyncio.gather(
        *(probe(property_id, target) for property_id, target in TARGETS.items())
    )
    payload = {
        "results": results,
        "summary": {
            "targets": len(results),
            "http_200": sum(row["status"] == 200 for row in results),
            "identity_exact": sum(all(row["identity"].values()) for row in results),
            "hyperbrowser_sessions": sum(row["hyperbrowser_sessions"] for row in results),
            "captcha_solving": False,
            "stealth": False,
            "fingerprint_rotation": False,
            "llm": False,
            "paid_canary": False,
        },
    }
    (ROOT / "summary.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
