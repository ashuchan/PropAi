#!/usr/bin/env python3
"""Materialize strict current evidence for Enclave on Golden Triangle."""

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
from ma_poc.pms.adapters.entrata import parse_entrata_pp_unit_cards


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
PARENT_CAPTURE = OUT / "hb_deep_inspect_35192_current.json"
CAPTURE = OUT / "hb_deep_inspect_35192_mediterranean_current.json"
EVIDENCE = OUT / "evidence_enclave_35192_current_strict.json"
PROPERTY_ID = 35192
PROPERTY_NAME = "Enclave on Golden Triangle"
WEBSITE = "http://www.enclaveongoldentriangle.com/"
CANONICAL_ADDRESS = "5001 Golden Triangle Boulevard"
EXPECTED_SOURCE = (
    "https://www.enclaveongoldentriangle.com/floorplans/fort-worth-TX/"
    "enclave-on-golden-triangle/the-mediterranean-776778-1/"
)


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


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def main() -> None:
    parent = json.loads(PARENT_CAPTURE.read_text(encoding="utf-8"))
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    html = str(capture.get("html") or "")
    final_url = str(capture.get("final_url") or "")
    assert final_url == EXPECTED_SOURCE
    assert (capture.get("session_options") or {}).get("solveCaptchas") is False
    assert urlsplit(final_url).hostname == "www.enclaveongoldentriangle.com"

    published_links = {
        str(item.get("href") or "")
        for item in (parent.get("clickable_elements") or [])
        if isinstance(item, dict)
    }
    assert EXPECTED_SOURCE in published_links

    body = normalized(html)
    assert normalized(PROPERTY_NAME) in body
    assert "5001 golden triangle blvd" in body
    assert "76244" in body

    parsed = parse_entrata_pp_unit_cards(html, final_url)
    native_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parsed:
        unit_number = str(row.get("unit_number") or "").strip()
        if (
            not unit_number
            or unit_number in seen
            or not unit_has_real_anchor(row)
            or not positive_rent(row)
            or str(row.get("source_api_url") or "") != EXPECTED_SOURCE
        ):
            continue
        seen.add(unit_number)
        native_rows.append(row)

    assert {str(row["unit_number"]) for row in native_rows} == {
        "2910",
        "2912",
        "2811",
        "1611",
    }
    assert len(native_rows) == 4
    assert all(row.get("floor_plan_name") == "The Mediterranean" for row in native_rows)

    result = {
        "property_id": PROPERTY_ID,
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_published_same_property_floorplan_"
            "native_ids_positive_rents"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": CANONICAL_ADDRESS,
            "current_page_name_match": True,
            "current_page_street_number_zip_match": True,
            "configured_domain_redirects_to_exact_same_property": True,
            "current_detail_link_published_by_exact_property_page": True,
            "rows_with_native_identity": len(native_rows),
            "rows_with_native_identity_and_positive_rent": len(native_rows),
            "source_urls": [EXPECTED_SOURCE],
        },
        "native_samples": [
            {
                "identity": {"unit_number": str(row["unit_number"])},
                "positive_rent_evidence": {
                    "market_rent_low": row.get("market_rent_low"),
                    "market_rent_high": row.get("market_rent_high"),
                },
                "source_api_url": EXPECTED_SOURCE,
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
        },
        "published_route_provenance": {
            "parent_capture_artifact": str(PARENT_CAPTURE),
            "parent_capture_sha256": hashlib.sha256(PARENT_CAPTURE.read_bytes()).hexdigest(),
            "parent_url": parent.get("final_url"),
            "published_detail_url": EXPECTED_SOURCE,
        },
    }
    payload = {
        "summary": {
            "result_type": "strict_current_entrata_exact_published_detail",
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
                "unit_ids": sorted(seen),
            }
        )
    )


if __name__ == "__main__":
    main()
