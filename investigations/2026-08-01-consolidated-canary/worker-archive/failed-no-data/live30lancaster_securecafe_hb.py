from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.extraction.post_process import post_process
from ma_poc.fetch.captcha_detect import looks_like_captcha
from ma_poc.fetch.hyperbrowser_backend import (
    _session_options,
    hb_raw_get,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits


PID = "58390"
URL = (
    "https://live30lancaster.securecafe.com/onlineleasing/"
    "new-30-lancaster/availableunits.aspx"
)
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/live30lancaster_securecafe_hb")


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


async def main() -> None:
    assert os.environ.get("COMPLIANCE_MODE") == "1"
    assert os.environ.get("HB_USE_STEALTH") == "0"
    assert os.environ.get("HB_USE_PROXY") == "1"
    assert os.environ.get("HYPERBROWSER_MAX_CALLS_PER_PROPERTY") == "1"
    assert not os.environ.get("PROBE_PROXY_URL", "").strip()
    options = _session_options("render")
    assert options == {
        "solveCaptchas": False,
        "useStealth": False,
        "useProxy": True,
        "adblock": True,
    }
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        metadata = next(row for row in csv.DictReader(handle) if row["apartmentid"] == PID)

    reset_hyperbrowser_property_counts()
    status, text = await hb_raw_get(URL, PID, max_calls_per_property=1)
    body = text.encode("utf-8", "replace")
    parsed = parse_securecafe_availableunits(text, URL)
    processed = post_process(parsed, property_id=PID)
    strict = [
        row
        for row in processed.admitted
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    ROOT.mkdir(parents=True, exist_ok=True)
    raw_path = ROOT / "availableunits.html.gz"
    with gzip.open(raw_path, "wb") as handle:
        handle.write(body)
    low = text.casefold()
    payload = {
        "guardrails": {
            "compliance_mode": True,
            "session_options": options,
            "hyperbrowser_max_calls_per_property": 1,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "llm": False,
            "paid_canary": False,
        },
        "property_id": int(PID),
        "property": metadata["name"],
        "configured_identity": {
            key: metadata[key] for key in ("address", "city", "state", "zip")
        },
        "url": URL,
        "status": status,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "captcha": looks_like_captcha(body),
        "identity_visible": {
            "name": metadata["name"].casefold() in low,
            "street": " ".join(metadata["address"].casefold().split()[:2]) in low,
            "city": metadata["city"].casefold() in low,
            "zip": metadata["zip"] in text,
        },
        "hb_calls": hyperbrowser_property_call_count(PID),
        "raw_parsed_rows": len(parsed),
        "admitted_rows": processed.n_admitted,
        "strict_native_positive_rows": len(strict),
        "plan_summaries": len(processed.plan_summaries),
        "rows": [
            {
                key: row.get(key)
                for key in (
                    "unit_number",
                    "floor_plan_name",
                    "bedrooms",
                    "bathrooms",
                    "sqft",
                    "market_rent_low",
                    "market_rent_high",
                    "availability_status",
                    "availability_date",
                    "source_ids",
                    "source_api_url",
                )
            }
            for row in strict
        ],
        "artifact": {
            "path": str(raw_path),
            "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
    }
    output = ROOT / "discovery.json"
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload))
    print(json.dumps({"output": str(output)}))


if __name__ == "__main__":
    asyncio.run(main())
