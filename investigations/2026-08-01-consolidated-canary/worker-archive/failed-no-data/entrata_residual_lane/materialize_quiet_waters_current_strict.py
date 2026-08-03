#!/usr/bin/env python3
"""Materialize strict current evidence for Quiet Waters Landing."""

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
LISTING_CAPTURE = OUT / "hb_deep_inspect_20672_conventional_current.json"
CAPTURE = OUT / "hb_deep_inspect_20672_a1_modal_current.json"
EVIDENCE = OUT / "evidence_quiet_waters_20672_current_strict.json"
PROPERTY_ID = 20672
PROPERTY_NAME = "Quiet Waters Landing"
WEBSITE = "https://www.quietwaterslanding.com/"
CANONICAL_ADDRESS = "1293 Thom Ct"
SOURCE_URL = (
    "https://www.quietwaterslanding.com/annapolis/quiet-waters-landing/"
    "floorplans/a1-1bd1ba-817607/fp_name/occupancy_type/conventional/"
)
LISTING_URL = (
    "https://www.quietwaterslanding.com/annapolis/quiet-waters-landing/"
    "conventional/"
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
    listing = json.loads(LISTING_CAPTURE.read_text(encoding="utf-8"))
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    html = str(capture.get("html") or "")
    final_url = str(capture.get("final_url") or "")
    assert final_url == SOURCE_URL
    assert str(listing.get("final_url") or "") == LISTING_URL
    assert (capture.get("session_options") or {}).get("solveCaptchas") is False
    assert (capture.get("click_result") or {}).get("clicked") is True
    assert (capture.get("click_result") or {}).get("selector") == (
        "button.fp-availability"
    )
    assert urlsplit(final_url).hostname == "www.quietwaterslanding.com"

    published_links = {
        str(item.get("href") or "")
        for item in (listing.get("clickable_elements") or [])
        if isinstance(item, dict)
    }
    assert SOURCE_URL in published_links

    listing_availability = [
        item
        for item in (listing.get("clickable_elements") or [])
        if isinstance(item, dict)
        and str(item.get("href") or "") == ""
        and "property_floorplan[id]=817607" in str((item.get("data") or {}).get("data-url") or "")
        and "property[id]=1183189" in str((item.get("data") or {}).get("data-url") or "")
    ]
    assert listing_availability

    text = normalized(html)
    assert normalized(PROPERTY_NAME) in text
    assert normalized(CANONICAL_ADDRESS) in text
    assert "annapolis md 21403" in text

    parsed = parse_prospectportal_unit_spaces(html, final_url)
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
            or str(row.get("source_api_url") or "") != SOURCE_URL
        ):
            continue
        seen.add(key)
        native_rows.append(row)

    assert {str(row["unit_number"]).casefold() for row in native_rows} == {
        "2d",
        "3a",
        "3d",
    }
    assert len(native_rows) == 3
    assert all(row.get("floor_plan_name") == "A1-1BD/1BA" for row in native_rows)

    modal_responses = [
        response
        for response in (capture.get("responses") or [])
        if isinstance(response, dict)
        and "action=view_unit_spaces" in str(response.get("url") or "")
        and int(response.get("status") or 0) == 200
        and int(response.get("body_bytes") or 0) > 0
    ]
    assert len(modal_responses) == 1

    result = {
        "property_id": PROPERTY_ID,
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_published_same_property_entrata_modal_"
            "native_ids_positive_rents_casefold_deduped"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "current_page_name_match": True,
            "current_page_full_address_match": True,
            "current_listing_published_exact_detail_link": True,
            "current_listing_property_floorplan_binding": "817607",
            "current_listing_provider_property_binding": "1183189",
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "raw_provider_rows_before_casefold_dedupe": len(parsed),
            "source_urls": [SOURCE_URL],
        },
        "native_samples": [
            {
                "identity": {"unit_number": str(row["unit_number"])},
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "source_api_url": SOURCE_URL,
            }
            for row in native_rows
        ],
        "native_rows": native_rows,
        "current_capture": {
            "capture_timestamp_utc": capture.get("capture_timestamp_utc"),
            "capture_artifact": str(CAPTURE),
            "capture_artifact_sha256": hashlib.sha256(CAPTURE.read_bytes()).hexdigest(),
            "html_sha256": capture.get("html_sha256"),
            "title": capture.get("title"),
            "final_url": final_url,
            "session_options": capture.get("session_options"),
            "captcha_solving": False,
            "modal_response": {
                key: modal_responses[0].get(key)
                for key in ("url", "status", "body_bytes", "body_sha256")
            },
        },
        "published_route_provenance": {
            "listing_capture_artifact": str(LISTING_CAPTURE),
            "listing_capture_sha256": hashlib.sha256(
                LISTING_CAPTURE.read_bytes()
            ).hexdigest(),
            "listing_url": LISTING_URL,
            "published_detail_url": SOURCE_URL,
        },
    }
    payload = {
        "summary": {
            "result_type": "strict_current_entrata_published_detail_modal",
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
