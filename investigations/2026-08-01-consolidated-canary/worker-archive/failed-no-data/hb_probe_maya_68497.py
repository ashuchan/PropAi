from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import (
    hb_raw_get,
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._rentcafe_hosted_table import (
    parse_rentcafe_hosted_table,
)
from ma_poc.pms.adapters.rentcafe import (
    parse_rentcafe_ysi_unitslist,
    parse_securecafe_availableunits,
)


PROPERTY_ID = "68497"
URL = (
    "https://liveatmaya.securecafe.com/onlineleasing/"
    "maya-apartments0/oleapplication.aspx?stepname=floorplan&myOlePropertyId=1805916"
)
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/maya_current_hb_probe")


def _native_identity(row: dict[str, object]) -> str:
    return next(
        (
            str(row.get(key) or "").strip()
            for key in (
                "unit_number",
                "unit_id",
                "unitid",
                "native_unit_id",
                "source_unit_id",
                "_source_native_id",
            )
            if str(row.get(key) or "").strip()
        ),
        "",
    )


def _positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    reset_hyperbrowser_property_counts()
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    raw_path = ROOT / "securecafe_floorplan.html"
    raw_path.write_text(body, encoding="utf-8")

    parsed = {
        "securecafe_availableunits": parse_securecafe_availableunits(body, URL),
        "rentcafe_hosted_table": parse_rentcafe_hosted_table(body, URL),
        "rentcafe_ysi_unitslist": parse_rentcafe_ysi_unitslist(body, URL),
    }
    strongest_name, strongest_rows = max(parsed.items(), key=lambda item: len(item[1]))
    strict = [
        row
        for row in strongest_rows
        if _native_identity(row) and _positive_rent(row)
    ]
    soup = BeautifulSoup(body, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    summary = {
        "property_id": int(PROPERTY_ID),
        "configured_property": {
            "name": "Maya",
            "address": "535 S Kingsley Dr",
            "city": "Los Angeles",
            "state": "CA",
            "zip": "90020",
        },
        "url": URL,
        "rentcafe_property_id": "1805916",
        "status": status,
        "body_bytes": len(body.encode("utf-8", "replace")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "identity_markers": {
            "maya": bool(re.search(r"\bmaya\b", text, re.I)),
            "street": bool(re.search(r"535\s+(?:south\s+|s\.?\s+)?kingsley", text, re.I)),
            "zip": "90020" in text,
            "rentcafe_property_id": "1805916" in body,
        },
        "challenge_markers": bool(
            re.search(r"captcha|challenge-platform|just a moment", body, re.I)
        ),
        "parser_counts": {name: len(rows) for name, rows in parsed.items()},
        "strongest_parser": strongest_name,
        "strongest_rows": len(strongest_rows),
        "native_positive_rent_rows": len(strict),
        "distinct_native_ids": len({_native_identity(row) for row in strict}),
        "sample_rows": strict[:5],
        "text_prefix": text[:500],
        "hyperbrowser_sessions": hyperbrowser_property_call_count(PROPERTY_ID),
        "guardrails": {
            "solve_captchas": False,
            "llm_enabled": False,
            "paid_canary": False,
        },
        "raw_path": str(raw_path),
    }
    summary_path = ROOT / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
