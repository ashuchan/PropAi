#!/usr/bin/env python3
"""Current read-only MRI ProspectConnect deep probe for three unknown sites."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "unknown_residual_lane/local_b"
OUT = LANE / "evidence_mri_three_current_probe.json"
E2E = LANE / "current_live_scraper_e2e.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
TARGETS = {
    "53567": {
        "name": "Trafalgar Square Apartments",
        "configured_url": "https://princetonmanagement.com/communities/trafalgar-square-apartments/",
        "provider_index": "https://princeton.mriprospectconnect.com/Search/Index/065",
        "community": "065",
        "expected_address_tokens": ("33210", "TRAFALGAR", "WESTLAND", "MI", "48186"),
    },
    "74523": {
        "name": "Charter Club",
        "configured_url": "https://charterclubapts.com/",
        "provider_index": "https://smg.mriprospectconnect.com/Search/Index/CCA",
        "community": "CCA",
        "expected_address_tokens": ("1040", "WINDWARD", "LONDON", "OH", "43140"),
    },
    "232583": {
        "name": "Custer Crossing Apartments",
        "configured_url": "https://princetonmanagement.com/communities/custer-crossing-apartments/",
        "provider_index": "https://princeton.mriprospectconnect.com/Search/Index/404",
        "community": "404",
        "expected_address_tokens": ("1763", "CARROLL", "DICKINSON", "ND", "58601"),
    },
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: str) -> float:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", value or "")
    return float(match.group(1).replace(",", "")) if match else 0.0


def _provider_probe(pid: str, spec: dict[str, object]) -> dict[str, object]:
    index_url = str(spec["provider_index"])
    host = f"{urljoin(index_url, '/').rstrip('/')}"
    session = requests.Session(impersonate="chrome116")
    index = session.get(index_url, timeout=40, allow_redirects=True)
    assert index.status_code == 200
    index_soup = BeautifulSoup(index.text or "", "html.parser")
    token_node = index_soup.find("input", {"name": "__RequestVerificationToken"})
    property_node = index_soup.find(attrs={"data-propertyid": True})
    heading_node = index_soup.find("h1")
    assert token_node and property_node and heading_node
    assert str(property_node.get("data-propertyid") or "").upper() == str(spec["community"]).upper()
    heading = " ".join(heading_node.get_text(" ", strip=True).split())
    assert all(
        token.lower() in " ".join(index_soup.get_text(" ", strip=True).split()).lower()
        for token in spec["expected_address_tokens"]
    )
    if pid == "74523":
        assert "CHARTER CLUB APARTMENT HOMES" in heading.upper()
    else:
        assert str(spec["name"]).lower() in heading.lower()

    search_url = urljoin(index_url, "/Search/Search")
    searched = session.post(
        search_url,
        data={
            "__RequestVerificationToken": token_node.get("value") or "",
            "Community": spec["community"],
            "MarketId": "",
            "Bedroom": "",
            "ApartmentNumber": "",
            "MoveInDate": "",
        },
        headers={"Referer": str(index.url), "Origin": host},
        timeout=40,
        allow_redirects=True,
    )
    assert searched.status_code == 200
    result_soup = BeautifulSoup(searched.text or "", "html.parser")
    native_rows: list[dict[str, object]] = []
    for button in result_soup.select("button[data-unitid]"):
        unit = str(button.get("data-unitid") or "").strip()
        building = str(button.get("data-bldgid") or "").strip()
        row = button.find_parent("tr")
        assert row is not None
        rent_node = row.select_one("[data-rent-range]")
        rent = _number(str(rent_node.get("data-rent-range") or "") if rent_node else "")
        if not rent:
            rent = _number(row.get_text(" ", strip=True))
        assert unit and rent > 0
        native_rows.append({
            "provider_unit_id": f"{building}:{unit}" if building else unit,
            "unit_number": unit,
            "building_id": building,
            "unit_address": str(button.get("data-unit-address") or "").strip(),
            "available_date": str(button.get("data-available-date") or "").strip(),
            "available_end_date": str(button.get("data-available-end-date") or "").strip(),
            "lease_term_months": str(button.get("data-term") or "").strip(),
            "rent": rent,
            "source_url": search_url,
        })
    assert len({row["provider_unit_id"] for row in native_rows}) == len(native_rows)
    plan_titles = [
        " ".join(node.get_text(" ", strip=True).split())
        for node in result_soup.select("h3.pc-card-title")
    ]
    return {
        "property_id": int(pid),
        "property_name": spec["name"],
        "configured_url": spec["configured_url"],
        "provider_index_url": index_url,
        "provider_search_url": search_url,
        "provider_property_id": spec["community"],
        "provider_heading": heading,
        "provider_identity_text": " ".join(index_soup.get_text(" ", strip=True).split())[:1200],
        "index_status": index.status_code,
        "search_status": searched.status_code,
        "index_body_sha256": hashlib.sha256((index.text or "").encode()).hexdigest(),
        "search_body_sha256": hashlib.sha256((searched.text or "").encode()).hexdigest(),
        "plan_cards": len(plan_titles),
        "sample_plan_titles": plan_titles[:8],
        "native_identity_rows": len(native_rows),
        "native_positive_rent_rows": len(native_rows),
        "native_rows": native_rows,
        "property_identity_match": True,
        "contamination_verdict": "single_exact_mri_property_session",
    }


def main() -> None:
    ledger_before = _sha(LEDGER)
    with (ROOT / "strict_recovery_remaining_current.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        residual = {
            row["property_id"]: row
            for row in csv.DictReader(handle)
            if row.get("property_id") in TARGETS
        }
    assert set(residual) == set(TARGETS)
    assert all(row["current_detected_adapter"] == "unknown" for row in residual.values())
    e2e_by_id = {
        str(row["property_id"]): row
        for row in json.loads(E2E.read_text())["results"]
    }
    results = []
    for pid, spec in TARGETS.items():
        row = _provider_probe(pid, spec)
        e2e = e2e_by_id[pid]
        assert int(e2e.get("units") or 0) == 0
        row["current_configured_route_scraper_e2e"] = {
            "adapter": e2e.get("adapter"),
            "tier": e2e.get("tier"),
            "outcome": e2e.get("outcome"),
            "units": int(e2e.get("units") or 0),
            "plans": int(e2e.get("plans") or 0),
            "errors": e2e.get("errors") or [],
        }
        if row["native_positive_rent_rows"]:
            row["strict_qualifies"] = False
            row["rejection_reason"] = (
                "current_configured_route_scraper_does_not_follow_exact_mri_portal_or_emit_native_rows"
            )
            row["minimal_code_lever"] = (
                "property-scoped MRI ProspectConnect link hop; preserve session/CSRF, POST /Search/Search "
                "with the published Community ID, then parse button[data-unitid] rows"
            )
        else:
            row["strict_qualifies"] = False
            row["rejection_reason"] = "current_exact_mri_search_publishes_plan_pricing_only_no_native_unit_rows"
            row["minimal_code_lever"] = "none_without_new_native_inventory"
        results.append(row)
    assert len(results) == 3
    assert sum(int(row["native_positive_rent_rows"]) > 0 for row in results) == 1
    assert _sha(LEDGER) == ledger_before
    payload = {
        "audit": "unknown residual MRI three-site current deep probe",
        "capture_date": "2026-08-01",
        "targets": 3,
        "strict_recoveries": 0,
        "native_near_miss_ids": [
            str(row["property_id"])
            for row in results if row["native_positive_rent_rows"]
        ],
        "results": sorted(results, key=lambda row: int(row["property_id"])),
        "policy": {
            "llm_calls": 0,
            "web_unlocker_calls": 0,
            "captcha_interactions": 0,
            "hyperbrowser_sessions": 0,
            "paid_canary": False,
        },
        "ledger_snapshot": {
            "sha256": ledger_before,
            "unchanged_after_validation": True,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "targets": 3,
        "strict_recoveries": 0,
        "near_miss_ids": payload["native_near_miss_ids"],
        "output": str(OUT),
    }))


if __name__ == "__main__":
    main()
