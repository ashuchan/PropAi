#!/usr/bin/env python3
"""Materialize strict La Vina and Kelly Farms current modal evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import parse_prospectportal_unit_spaces


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
EVIDENCE = OUT / "evidence_lavina_kelly_current_strict.json"
TARGETS: tuple[dict[str, Any], ...] = (
    {
        "property_id": 22552,
        "property_name": "La Vina Apartments",
        "page_name": "La Vina",
        "website": "https://www.lavinaapts.com/",
        "canonical_address": "4601 Gerrilyn Way",
        "postal_code": "94550",
        "listing_url": (
            "https://www.lavinaapts.com/livermore/la-vina/conventional/"
        ),
        "provider_property_id": "100172727",
        "provider_floorplan_id": "801354",
        "expected_units": ["208", "212"],
        "expected_floorplan": "B1",
        "rent_provenance": "unit_button_data_rent",
    },
    {
        "property_id": 242976,
        "property_name": "Kelly Farms",
        "page_name": "Kelly Farms Apartments",
        "website": (
            "http://www.kellyfarms.com/?fbclid="
            "IwAR3Ht2UTDJGEhVJqHMlcBmpL_RXbUH4u9mVSJ-Z1gopj5mzcXzrLI4u3ilk"
        ),
        "canonical_address": "1280 Cinnamon Hill Lane",
        "postal_code": "65201",
        "listing_url": (
            "https://www.kellyfarms.com/columbia/"
            "kelly-farms-apartments/conventional/"
        ),
        "provider_property_id": "648323",
        "provider_floorplan_id": "278303",
        "expected_units": ["201"],
        "expected_floorplan": "Kiernan - Style 2",
        "rent_provenance": "exact_floorplan_modal_header_and_page_jsonld",
        "expected_floorplan_rent": 1250,
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


def modal_header_rent(html: str) -> int:
    text = " ".join(BeautifulSoup(html, "lxml").get_text(" ", strip=True).split())
    match = re.search(r"\bRent\*?\s*\$([\d,]+(?:\.\d+)?)\s*/month\b", text, re.I)
    assert match
    return int(round(float(match.group(1).replace(",", ""))))


def jsonld_floorplan_rent(html: str, floorplan_name: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            payload = json.loads(script.get_text(" ", strip=True))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        plans = payload.get("accommodationFloorPlan") if isinstance(payload, dict) else []
        for plan in plans or []:
            if not isinstance(plan, dict) or plan.get("name") != floorplan_name:
                continue
            pricing = plan.get("additionalProperty") or {}
            value = pricing.get("minValue") if isinstance(pricing, dict) else None
            if isinstance(value, (int, float)) and value > 0:
                return int(round(float(value)))
    return 0


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

    endpoint = str(capture.get("published_endpoint") or "")
    assert f"property[id]={target['provider_property_id']}" in endpoint
    assert f"property_floorplan[id]={target['provider_floorplan_id']}" in endpoint
    assert "action=view_unit_spaces" in endpoint
    assert "is_availability_alert=true" not in endpoint
    assert urlsplit(endpoint).hostname == urlsplit(listing_url).hostname

    page_html = str(capture.get("page_html") or "")
    endpoint_html = str(capture.get("endpoint_html") or "")
    text = normalized(page_html)
    assert normalized(str(target["page_name"])) in text
    assert normalized(str(target["canonical_address"])) in text
    assert str(target["postal_code"]) in set(text.split())

    parsed = parse_prospectportal_unit_spaces(endpoint_html, endpoint)
    native_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for original in parsed:
        row = dict(original)
        unit_number = str(row.get("unit_number") or "").strip()
        key = unit_number.casefold()
        if not unit_number or key in seen or not unit_has_real_anchor(row):
            continue
        if target["rent_provenance"] == "exact_floorplan_modal_header_and_page_jsonld":
            expected_rent = int(target["expected_floorplan_rent"])
            assert modal_header_rent(endpoint_html) == expected_rent
            assert (
                jsonld_floorplan_rent(page_html, str(target["expected_floorplan"]))
                == expected_rent
            )
            row["market_rent_low"] = expected_rent
            row["market_rent_high"] = expected_rent
            row["rent_range"] = f"${expected_rent:,}"
            row["positive_rent_provenance"] = target["rent_provenance"]
        if not positive_rent(row) or str(row.get("source_api_url") or "") != endpoint:
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
            "positive_rent_provenance": target["rent_provenance"],
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
                    "provenance": target["rent_provenance"],
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
