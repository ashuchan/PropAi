#!/usr/bin/env python3
"""Materialize strict current evidence for The Boro - Phipps."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import parse_prospectportal_unit_spaces


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
CAPTURE = OUT / "hb_modal_direct_26736_current.json"
EVIDENCE = OUT / "evidence_boro_phipps_26736_current_strict.json"
PROPERTY_ID = 26736
PROPERTY_NAME = "The Boro - Phipps"
WEBSITE = (
    "https://www.theboroapartments.com/?switch_cls%5Bid%5D=64893&"
    "utm_source=gmb&utm_medium=organic&oll_switch_cls%5Bid%5D=64893&"
    "oll_utm_source=gmb&oll_utm_medium=organic"
)
CANONICAL_ADDRESS = "3460 Kingsboro Rd NE"
LISTING_URL = (
    "https://www.theboroapartments.com/atlanta/phipps-place/conventional/"
)


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


def main() -> None:
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    assert capture.get("property_id") == PROPERTY_ID
    assert capture.get("outcome") == "CURRENT_PUBLISHED_MODAL_ENDPOINT_CAPTURED"
    assert capture.get("probe_mode") == "click"
    assert int(capture.get("endpoint_status") or 0) == 200
    assert capture.get("endpoint_challenge") is False
    options = capture.get("session_options") or {}
    assert options.get("solveCaptchas") is False
    assert options.get("useStealth") is False
    assert capture.get("captcha_solving") is False
    assert str(capture.get("listing_url") or "") == LISTING_URL
    assert urlsplit(LISTING_URL).hostname == "www.theboroapartments.com"

    button = capture.get("published_button") or {}
    endpoint = str(capture.get("published_endpoint") or "")
    assert "available" in str(button.get("text") or "").casefold()
    assert "property[id]=1140727" in endpoint
    assert "property_floorplan[id]=772398" in endpoint
    assert "action=view_unit_spaces" in endpoint
    assert urlsplit(endpoint).hostname == urlsplit(LISTING_URL).hostname

    page_html = str(capture.get("page_html") or "")
    endpoint_html = str(capture.get("endpoint_html") or "")
    text = normalized(page_html)
    assert "phipps place" in text
    assert normalized(CANONICAL_ADDRESS) in text
    assert "30326" in set(text.split())

    parsed = parse_prospectportal_unit_spaces(endpoint_html, endpoint)
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
            or str(row.get("source_api_url") or "") != endpoint
        ):
            continue
        seen.add(key)
        native_rows.append(row)

    assert [str(row["unit_number"]) for row in native_rows] == ["542", "106", "837"]
    assert all(
        row.get("floor_plan_name")
        == "One Bedroom One Bath with Den (1049 SF)"
        for row in native_rows
    )

    result = {
        "property_id": PROPERTY_ID,
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_same_property_published_entrata_modal_"
            "native_unique_id_positive_rent"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "current_page_name_match": True,
            "current_page_street_zip_match": True,
            "current_listing_provider_property_binding": "1140727",
            "current_listing_property_floorplan_binding": "772398",
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "source_urls": [endpoint],
        },
        "native_samples": [
            {
                "identity": {"unit_number": str(row["unit_number"])},
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "source_api_url": endpoint,
            }
            for row in native_rows
        ],
        "native_rows": native_rows,
        "current_capture": {
            "capture_timestamp_utc": capture.get("capture_timestamp_utc"),
            "capture_artifact": str(CAPTURE),
            "capture_artifact_sha256": hashlib.sha256(CAPTURE.read_bytes()).hexdigest(),
            "listing_html_sha256": capture.get("listing_html_sha256"),
            "page_html_sha256": capture.get("page_html_sha256"),
            "endpoint_html_sha256": capture.get("endpoint_html_sha256"),
            "published_endpoint": endpoint,
            "endpoint_status": capture.get("endpoint_status"),
            "session_options": options,
            "captcha_solving": False,
        },
    }
    payload = {
        "summary": {
            "result_type": "strict_current_entrata_published_modal_same_session",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [PROPERTY_ID],
            "native_positive_rent_rows": len(native_rows),
            "captcha_solving": False,
            "fingerprint_rotation": False,
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
