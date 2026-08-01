from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import hb_raw_get
from ma_poc.pms.adapters._rentcafe_hosted_table import parse_rentcafe_hosted_table
from ma_poc.pms.adapters.rentcafe import (
    parse_rentcafe_ysi_unitslist,
    parse_securecafe_availableunits,
)


PROPERTY_ID = "594"
URL = "https://www.harbinwoodbyelon.com/floorplans.aspx"
ROOT = Path("/private/tmp/propai-fnd-vBkmT9")


def _positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row.get(key) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def _native_identity(row: dict) -> bool:
    return any(
        str(row.get(key) or "").strip()
        for key in (
            "unit_number",
            "unit_id",
            "unitid",
            "native_unit_id",
            "source_unit_id",
            "_source_native_id",
        )
    )


def _canonical_record() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if str(row.get("apartmentid") or "").strip() == PROPERTY_ID:
                return row
    return {}


async def main() -> None:
    status, body = await hb_raw_get(URL, PROPERTY_ID)
    raw_path = ROOT / "hb_harbinwood_594.html"
    raw_path.write_text(body, encoding="utf-8")

    parser_results = {
        "securecafe_availableunits": parse_securecafe_availableunits(body, URL),
        "rentcafe_hosted_table": parse_rentcafe_hosted_table(body, URL),
        "rentcafe_ysi_unitslist": parse_rentcafe_ysi_unitslist(body, URL),
    }
    strongest_name, strongest_rows = max(
        parser_results.items(), key=lambda item: len(item[1])
    )
    strict_rows = [
        row for row in strongest_rows if _native_identity(row) and _positive_rent(row)
    ]
    soup = BeautifulSoup(body, "html.parser")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    canonical = _canonical_record()
    summary = {
        "property_id": int(PROPERTY_ID),
        "property_name": str(canonical.get("name") or ""),
        "property_address": str(canonical.get("address") or ""),
        "url": URL,
        "status": status,
        "bytes": len(body.encode("utf-8")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "markers": {
            "avail_unit_row": "AvailUnitRow" in body,
            "ysi_units_list": "ysi.unitsList" in body,
            "fp_unit": "fp-unit" in body,
            "contact_for_availability": "Contact for Availability" in body,
            "captcha": bool(re.search(r"captcha|challenge-platform", body, re.I)),
        },
        "parser_counts": {
            name: len(rows) for name, rows in parser_results.items()
        },
        "strongest_parser": strongest_name,
        "native_positive_rent_rows": len(strict_rows),
        "distinct_native_units": len(
            {
                str(row.get("unit_number") or row.get("unit_id") or "").strip()
                for row in strict_rows
                if str(row.get("unit_number") or row.get("unit_id") or "").strip()
            }
        ),
        "sample_rows": strict_rows[:3],
        "text_prefix": page_text[:400],
        "raw_path": str(raw_path),
    }
    metadata_path = ROOT / "hb_harbinwood_594_summary.json"
    metadata_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


asyncio.run(main())
