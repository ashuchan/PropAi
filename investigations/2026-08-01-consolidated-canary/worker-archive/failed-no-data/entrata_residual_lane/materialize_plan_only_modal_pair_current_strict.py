#!/usr/bin/env python3
"""Materialize two strict Entrata modal recoveries from plan-only results."""

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
EVIDENCE = OUT / "evidence_plan_only_modal_pair_current_strict.json"
TARGETS: tuple[dict[str, Any], ...] = (
    {
        "property_id": 7980,
        "property_name": "Tesota Morningside",
        "website": "https://www.tesotamorningside.com/",
        "canonical_address": "3958 Montgomery Blvd NE",
        "postal_code": "87109",
        "listing_url": (
            "https://www.tesotamorningside.com/albuquerque/"
            "tesota-morningside/conventional/"
        ),
        "provider_property_id": "1271493",
        "provider_floorplan_id": "844423",
        "expected_units": ["87", "166"],
        "expected_floorplan": "A1A",
    },
    {
        "property_id": 68952,
        "property_name": "Wy'East Pointe",
        "website": "https://www.livewyeast.com/",
        "canonical_address": "812 SE 136TH AVE",
        "postal_code": "98683",
        "listing_url": (
            "https://www.livewyeast.com/vancouver-vancouver/"
            "wyeast-pointe-apartments/conventional/"
        ),
        "provider_property_id": "219049",
        "provider_floorplan_id": "543242",
        "expected_units": ["G078"],
        "expected_floorplan": "Bronze",
    },
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


def materialize(target: dict[str, Any]) -> dict[str, Any]:
    property_id = int(target["property_id"])
    capture_path = OUT / f"hb_modal_direct_{property_id}_current.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    listing_url = str(target["listing_url"])

    assert capture.get("property_id") == property_id
    assert capture.get("outcome") == "CURRENT_PUBLISHED_MODAL_ENDPOINT_CAPTURED"
    assert capture.get("probe_mode") == "click"
    assert int(capture.get("endpoint_status") or 0) == 200
    assert capture.get("endpoint_challenge") is False
    options = capture.get("session_options") or {}
    assert options.get("solveCaptchas") is False
    assert options.get("useStealth") is False
    assert capture.get("captcha_solving") is False
    assert str(capture.get("listing_url") or "") == listing_url
    assert urlsplit(listing_url).hostname == urlsplit(str(target["website"])).hostname

    button = capture.get("published_button") or {}
    endpoint = str(capture.get("published_endpoint") or "")
    assert "available" in str(button.get("text") or "").casefold()
    assert f"property[id]={target['provider_property_id']}" in endpoint
    assert f"property_floorplan[id]={target['provider_floorplan_id']}" in endpoint
    assert "action=view_unit_spaces" in endpoint
    assert urlsplit(endpoint).hostname == urlsplit(listing_url).hostname

    page_html = str(capture.get("page_html") or "")
    endpoint_html = str(capture.get("endpoint_html") or "")
    text = normalized(page_html)
    assert normalized(str(target["property_name"])) in text
    assert normalized(str(target["canonical_address"])) in text
    assert str(target["postal_code"]) in set(text.split())

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

    assert [str(row["unit_number"]) for row in native_rows] == target["expected_units"]
    assert all(
        row.get("floor_plan_name") == target["expected_floorplan"]
        for row in native_rows
    )

    return {
        "property_id": property_id,
        "property_name": target["property_name"],
        "website": target["website"],
        "outcome": "UNIT_QUALIFIED",
        "units": len(native_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_same_property_published_entrata_modal_"
            "native_unique_id_positive_rent"
        ),
        "identity_evidence": {
            "canonical_name": target["property_name"],
            "canonical_address": target["canonical_address"],
            "current_page_name_match": True,
            "current_page_street_zip_match": True,
            "current_listing_provider_property_binding": target[
                "provider_property_id"
            ],
            "current_listing_property_floorplan_binding": target[
                "provider_floorplan_id"
            ],
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
            "capture_artifact": str(capture_path),
            "capture_artifact_sha256": hashlib.sha256(
                capture_path.read_bytes()
            ).hexdigest(),
            "listing_html_sha256": capture.get("listing_html_sha256"),
            "page_html_sha256": capture.get("page_html_sha256"),
            "endpoint_html_sha256": capture.get("endpoint_html_sha256"),
            "published_endpoint": endpoint,
            "endpoint_status": capture.get("endpoint_status"),
            "session_options": options,
            "captcha_solving": False,
        },
    }


def main() -> None:
    results = [materialize(target) for target in TARGETS]
    payload = {
        "summary": {
            "result_type": "strict_current_entrata_published_modal_same_session",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": len(results),
            "strict_unit_qualified_property_ids": [
                row["property_id"] for row in results
            ],
            "native_positive_rent_rows": sum(row["units"] for row in results),
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": results,
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest(),
                "property_ids": [row["property_id"] for row in results],
                "native_positive_rent_rows": sum(row["units"] for row in results),
                "unit_ids": {
                    str(row["property_id"]): [
                        item["identity"]["unit_number"]
                        for item in row["native_samples"]
                    ]
                    for row in results
                },
            }
        )
    )


if __name__ == "__main__":
    main()
