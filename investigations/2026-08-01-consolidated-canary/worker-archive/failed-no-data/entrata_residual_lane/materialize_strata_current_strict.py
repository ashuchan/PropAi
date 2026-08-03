#!/usr/bin/env python3
"""Materialize strict current evidence for Strata on California."""

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
CAPTURE = OUT / "hb_modal_direct_239274_current.json"
EVIDENCE = OUT / "evidence_strata_239274_current_strict.json"
PROPERTY_ID = 239274
PROPERTY_NAME = "Strata on California"
WEBSITE = "http://www.strataoncalifornia.com"
CANONICAL_ADDRESS = "6312 California Ave SW"
LISTING_URL = (
    "https://www.strataoncalifornia.com/seattle/"
    "strata-on-california/conventional/"
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
    assert capture.get("outcome") == "CURRENT_PUBLISHED_MODAL_ENDPOINT_CAPTURED"
    assert capture.get("probe_mode") == "click"
    assert int(capture.get("endpoint_status") or 0) == 200
    assert capture.get("endpoint_challenge") is False
    assert (capture.get("session_options") or {}).get("solveCaptchas") is False
    assert str(capture.get("listing_url") or "") == LISTING_URL
    assert urlsplit(LISTING_URL).hostname == "www.strataoncalifornia.com"

    button = capture.get("published_button") or {}
    endpoint = str(capture.get("published_endpoint") or "")
    assert str(button.get("text") or "") == "3 Available"
    assert "property[id]=100136434" in endpoint
    assert "property_floorplan[id]=856131" in endpoint
    assert "action=view_unit_spaces" in endpoint

    page_html = str(capture.get("page_html") or "")
    endpoint_html = str(capture.get("endpoint_html") or "")
    text = normalized(page_html)
    assert normalized(PROPERTY_NAME) in text
    assert "6312 california avenue southwest" in text
    assert "seattle" in set(text.split())
    assert "98136" in set(text.split())

    parsed = parse_prospectportal_unit_spaces(endpoint_html, LISTING_URL)
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
            or str(row.get("source_api_url") or "") != LISTING_URL
        ):
            continue
        seen.add(key)
        native_rows.append(row)

    assert {str(row["unit_number"]) for row in native_rows} == {
        "109",
        "101",
        "115",
        "307",
    }
    assert len(native_rows) == 4
    assert all(row.get("floor_plan_name") == "1 Bedroom" for row in native_rows)

    result = {
        "property_id": PROPERTY_ID,
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_same_property_published_entrata_modal_"
            "native_ids_positive_rents"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "current_page_name_match": True,
            "current_page_full_address_match": True,
            "current_listing_provider_property_binding": "100136434",
            "current_listing_property_floorplan_binding": "856131",
            "current_listing_immediate_unit_count": 3,
            "provider_modal_current_native_rows_including_future": len(native_rows),
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "source_urls": [LISTING_URL],
        },
        "native_samples": [
            {
                "identity": {"unit_number": str(row["unit_number"])},
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "source_api_url": LISTING_URL,
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
            "session_options": capture.get("session_options"),
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
