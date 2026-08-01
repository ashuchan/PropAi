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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/securecafe_hb_remainder_lane")
OUTPUT = ROOT / "securecafe_hb3_discovery.json"
TARGETS = {
    "5782": (
        "https://springfieldrenton.securecafe.com/onlineleasing/"
        "springfield-apartments-5/availableunits.aspx"
    ),
    "231543": (
        "https://autumnhills-bestrentnj.securecafe.com/onlineleasing/"
        "village-at-autumn-hills/availableunits.aspx"
        "?myolepropertyid=1026013&floorPlans=3429401"
    ),
    "266766": (
        "https://101oxford.securecafe.com/onlineleasing/"
        "101-w-oxford/availableunits.aspx"
    ),
}


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def metadata() -> dict[str, dict[str, str]]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        return {
            row["apartmentid"]: row
            for row in csv.DictReader(handle)
            if row["apartmentid"] in TARGETS
        }


async def main() -> None:
    assert os.environ.get("COMPLIANCE_MODE") == "1"
    assert os.environ.get("HB_USE_STEALTH") == "0"
    assert os.environ.get("HB_USE_PROXY") == "1"
    assert os.environ.get("HYPERBROWSER_MAX_CALLS_PER_PROPERTY") == "1"
    assert not os.environ.get("PROBE_PROXY_URL", "").strip()
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False
    assert options["useProxy"] is True
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    by_pid = metadata()
    results: list[dict[str, object]] = []
    for pid, url in TARGETS.items():
        status, text = await hb_raw_get(
            url,
            pid,
            max_calls_per_property=1,
        )
        body = text.encode("utf-8", "replace")
        artifact = ROOT / f"{pid}_securecafe.html.gz"
        with gzip.open(artifact, "wb") as handle:
            handle.write(body)
        parsed = parse_securecafe_availableunits(text, url)
        pp = post_process(parsed, property_id=pid)
        strict = [
            row
            for row in pp.admitted
            if unit_has_real_anchor(row) and positive_rent(row)
        ]
        low = text.casefold()
        meta = by_pid[pid]
        results.append(
            {
                "property_id": int(pid),
                "property": meta["name"],
                "configured_identity": {
                    key: meta[key]
                    for key in ("address", "city", "state", "zip")
                },
                "url": url,
                "status": status,
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "captcha": looks_like_captcha(body),
                "identity_visible": {
                    "name": meta["name"].casefold() in low,
                    "street": " ".join(meta["address"].casefold().split()[:2])
                    in low,
                    "city": meta["city"].casefold() in low,
                    "zip": meta["zip"] in text,
                },
                "hb_calls": hyperbrowser_property_call_count(pid),
                "raw_parsed_rows": len(parsed),
                "admitted_rows": pp.n_admitted,
                "strict_native_positive_rows": len(strict),
                "plan_summaries": len(pp.plan_summaries),
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
                    "path": str(artifact),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                },
            }
        )
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
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload))
    print(json.dumps({"output": str(OUTPUT)}))


if __name__ == "__main__":
    asyncio.run(main())
