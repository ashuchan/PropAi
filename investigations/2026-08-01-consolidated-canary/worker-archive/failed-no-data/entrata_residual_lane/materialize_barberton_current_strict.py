#!/usr/bin/env python3
"""Fetch and materialize strict current Barberton RentCafe evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""

from ma_poc.core.identity import unit_has_real_anchor  # noqa: E402
from ma_poc.pms.adapters._probe import probe_get  # noqa: E402
from ma_poc.pms.adapters._rentcafe_nestin import (  # noqa: E402
    parse_nestin_detail_page,
)


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
EVIDENCE = OUT / "evidence_barberton_46915_current_strict.json"
PROPERTY_ID = 46915
PROPERTY_NAME = "Barberton"
WEBSITE = "https://www.barbertonapt.com/"
INVENTORY_URL = "https://www.barbertonapt.com/availableunits"
CANONICAL_ADDRESS = "605 Barberton Dr"
POSTAL_CODE = "23451"


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and math.isfinite(float(row[key]))
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


def fetch(url: str) -> tuple[str, dict[str, Any]]:
    response = probe_get(
        url,
        timeout=35,
        unlocker=False,
        proxies={},
        verify=True,
    )
    html = str(getattr(response, "text", "") or "")
    final_url = str(getattr(response, "url", "") or url)
    metadata = {
        "requested_url": url,
        "status": int(getattr(response, "status_code", 0) or 0),
        "final_url": final_url,
        "same_origin": urlsplit(final_url).hostname == urlsplit(WEBSITE).hostname,
        "body_bytes": len(html.encode("utf-8", "replace")),
        "body_sha256": hashlib.sha256(
            html.encode("utf-8", "replace")
        ).hexdigest(),
        "challenge_detected": bool(
            re.search(
                r"just a moment|verify you are human|checking your browser|cf-chl-",
                html,
                re.I,
            )
        ),
        "transport": {
            "backend": "direct_curl_cffi_probe_get",
            "unlocker": False,
            "proxies": {},
            "captcha_solving": False,
            "fingerprint_rotation": False,
        },
    }
    return html, metadata


def main() -> None:
    root_html, root_fetch = fetch(WEBSITE)
    inventory_html, inventory_fetch = fetch(INVENTORY_URL)
    assert root_fetch["status"] == 200 and inventory_fetch["status"] == 200
    assert root_fetch["same_origin"] is True
    assert inventory_fetch["same_origin"] is True
    assert root_fetch["challenge_detected"] is False
    assert inventory_fetch["challenge_detected"] is False

    for html in (root_html, inventory_html):
        text = normalized(
            " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
        )
        assert normalized(PROPERTY_NAME) in text
        assert normalized(CANONICAL_ADDRESS) in text
        assert POSTAL_CODE in set(text.split())

    root_capture = OUT / "direct_barberton_46915_root_current.html.gz"
    inventory_capture = OUT / "direct_barberton_46915_inventory_current.html.gz"
    with gzip.open(root_capture, "wt", encoding="utf-8") as handle:
        handle.write(root_html)
    with gzip.open(inventory_capture, "wt", encoding="utf-8") as handle:
        handle.write(inventory_html)

    parsed = parse_nestin_detail_page(inventory_html, INVENTORY_URL)
    native_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parsed:
        unit_number = str(row.get("unit_number") or "").strip()
        key = unit_number.casefold()
        if (
            not unit_number
            or key in seen
            or not unit_has_real_anchor(row)
            or not positive_rent(row)
            or str(row.get("source_api_url") or "") != INVENTORY_URL
        ):
            continue
        seen.add(key)
        native_rows.append(row)

    assert [str(row["unit_number"]) for row in native_rows] == [
        "B107",
        "06",
        "C207",
        "B101",
    ]

    result = {
        "property_id": PROPERTY_ID,
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_same_origin_published_rentcafe_inventory_"
            "native_unique_id_positive_rent"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "current_root_name_street_zip_match": True,
            "current_inventory_name_street_zip_match": True,
            "same_origin_inventory_route": True,
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "source_urls": [INVENTORY_URL],
        },
        "native_samples": [
            {
                "identity": {"unit_number": str(row["unit_number"])},
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "source_api_url": INVENTORY_URL,
            }
            for row in native_rows
        ],
        "native_rows": native_rows,
        "current_capture": {
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "root_capture": str(root_capture),
            "root_capture_sha256": hashlib.sha256(root_capture.read_bytes()).hexdigest(),
            "inventory_capture": str(inventory_capture),
            "inventory_capture_sha256": hashlib.sha256(
                inventory_capture.read_bytes()
            ).hexdigest(),
            "root_fetch": root_fetch,
            "inventory_fetch": inventory_fetch,
        },
    }
    payload = {
        "summary": {
            "result_type": "strict_current_rentcafe_same_origin_inventory_direct",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [PROPERTY_ID],
            "native_positive_rent_rows": len(native_rows),
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "unlocker": False,
            "proxies": {},
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": [result],
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest(),
                "property_id": PROPERTY_ID,
                "native_positive_rent_rows": len(native_rows),
                "unit_ids": [row["unit_number"] for row in native_rows],
            }
        )
    )


if __name__ == "__main__":
    main()
