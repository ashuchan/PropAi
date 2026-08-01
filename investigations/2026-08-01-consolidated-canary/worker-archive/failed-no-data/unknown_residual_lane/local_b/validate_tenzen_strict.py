#!/usr/bin/env python3
"""Strict current Tenzen recovery evidence; read-only and LLM-free."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "unknown_residual_lane/local_b"
OUT = LANE / "evidence_19245_tenzen_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTY_ID = "19245"
PAGE_URL = "https://tenzenapartments.com/"
COMMUNITY_URL = (
    "https://doorway-api.knockrentals.com/v1/property/community/"
    "5d11eef5c367a4b5"
)
UNITS_URL = "https://doorway-api.knockrentals.com/v1/property/2022654/units"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _positive_rent(row: dict) -> float:
    for key in (
        "market_rent_low", "market_rent_high", "rent_low", "rent_high",
        "asking_rent", "rent",
    ):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return 0.0


def _canonical() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(
            item for item in csv.DictReader(handle)
            if item.get("apartmentid") == PROPERTY_ID
        )
    assert row["name"] == "Tenzen"
    assert row["address"] == "4117 124th Ave SE"
    assert row["city"] == "Bellevue" and row["state"] == "WA"
    assert row["zip"] == "98006"
    return row


async def main() -> None:
    ledger_before = _sha(LEDGER)
    canonical = _canonical()
    with (ROOT / "strict_recovery_remaining_current.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        residual = next(
            row for row in csv.DictReader(handle)
            if row.get("property_id") == PROPERTY_ID
        )
    assert residual["current_detected_adapter"] == "unknown"

    page_response = probe_get(PAGE_URL, timeout=40, unlocker=False, retries=1)
    assert page_response.status_code == 200
    page_body = page_response.text or ""
    page_final = str(page_response.url)
    assert (urlparse(page_final).hostname or "").lower() in {
        "tenzenapartments.com", "www.tenzenapartments.com"
    }
    soup = BeautifulSoup(page_body, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript"]):
        tag.decompose()
    visible = " ".join(soup.get_text(" ", strip=True).split())
    assert "Tenzen Apartments" in visible
    assert "4117 124th Avenue SE Bellevue, WA 98006" in visible

    init = re.search(
        r"knockDoorway\.init\s*\(\s*['\"](?P<token>[^'\"]+)['\"]\s*,"
        r"\s*['\"]community['\"]\s*,\s*['\"](?P<community>[^'\"]+)['\"]",
        page_body,
        re.IGNORECASE,
    )
    assert init and init.group("community") == "5d11eef5c367a4b5"

    community_response = probe_get(
        COMMUNITY_URL, timeout=40, unlocker=False, retries=1
    )
    assert community_response.status_code == 200
    community = community_response.json()["property"]
    location = community["data"]["location"]
    address = location["address"]
    assert int(community["id"]) == 2022654
    assert int(community["data"]["property_id"]) == 2022654
    assert community["data"]["id"] == init.group("community")
    assert location["name"] == "Tenzen Apartments"
    assert address["street"] == canonical["address"]
    assert address["city"] == canonical["city"]
    assert address["state"] == canonical["state"]
    assert address["zip"] == canonical["zip"]

    units_response = probe_get(UNITS_URL, timeout=40, unlocker=False, retries=1)
    assert units_response.status_code == 200
    raw_units = units_response.json()["units_data"]["units"]
    strict_raw = []
    for row in raw_units:
        native_id = str(row.get("id") or "").strip()
        unit_number = str(row.get("name") or "").strip()
        rent = float(row.get("price") or row.get("displayPrice") or 0)
        assert native_id and unit_number and rent > 0
        assert int(row.get("propertyId") or 0) == 2022654
        assert row.get("available") is True and row.get("hidden") is False
        strict_raw.append({
            "native_unit_id": native_id,
            "unit_number": unit_number,
            "property_id": str(row["propertyId"]),
            "floor_plan_name": str(row.get("layoutName") or ""),
            "rent": rent,
            "available_on": str(row.get("availableOn") or ""),
            "source_url": UNITS_URL,
        })
    assert len(strict_raw) == 2
    assert len({row["native_unit_id"] for row in strict_raw}) == 2
    assert len({row["unit_number"] for row in strict_raw}) == 2

    fetch_result = FetchResult(
        url=PAGE_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=page_body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=page_final,
        attempts=1,
        elapsed_ms=0,
    )
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 1,
        "_cost_cap_usd": 0,
    }
    csv_row = {
        "apartmentid": PROPERTY_ID,
        "name": canonical["name"],
        "proj_name": canonical["name"],
        "address": canonical["address"],
        "city": canonical["city"],
        "state": canonical["state"],
        "zip": canonical["zip"],
        "website": canonical["website"],
    }
    result = await asyncio.wait_for(
        scraper_mod.scrape(
            PAGE_URL,
            page=None,
            fetch_result=fetch_result,
            csv_row=csv_row,
            property_id=PROPERTY_ID,
            shared_budget=budget,
        ),
        timeout=180,
    )
    assert result.get("_adapter_used") == "knock"
    assert result.get("extraction_tier_used") == "TIER_1_KNOCK_API"
    e2e_units = list(result.get("units") or [])
    strict_e2e = [
        row for row in e2e_units
        if unit_has_real_anchor(row) and _positive_rent(row) > 0
    ]
    assert len(strict_e2e) == 2
    raw_by_id = {row["native_unit_id"]: row for row in strict_raw}
    e2e_rows = []
    for row in strict_e2e:
        source_ids = row.get("source_ids") or {}
        assert isinstance(source_ids, dict)
        native_id = str(source_ids.get("knock_unit_id") or "")
        assert native_id in raw_by_id
        raw = raw_by_id[native_id]
        assert str(row.get("source_property_id") or "") == "2022654"
        assert str(row.get("source_api_url") or "") == UNITS_URL
        assert str(row.get("unit_number") or "") == raw["unit_number"]
        assert _positive_rent(row) == raw["rent"]
        e2e_rows.append({
            "native_unit_id": native_id,
            "unit_id": str(row.get("unit_id") or ""),
            "unit_number": str(row.get("unit_number") or ""),
            "floor_plan_name": str(row.get("floor_plan_name") or ""),
            "rent": _positive_rent(row),
            "availability_date": str(row.get("availability_date") or ""),
            "source_property_id": str(row.get("source_property_id") or ""),
            "source_api_url": str(row.get("source_api_url") or ""),
            "source_ids": source_ids,
        })
    assert {row["native_unit_id"] for row in e2e_rows} == set(raw_by_id)
    assert budget["llm_api_calls"] == 0
    assert budget["llm_dom_calls"] == 0
    assert budget["llm_monolithic"] == 0
    assert _sha(LEDGER) == ledger_before

    ledger_ids = {
        row["property_id"]
        for row in csv.DictReader(LEDGER.open(encoding="utf-8", newline=""))
    }
    payload = {
        "audit": "current strict Tenzen Knock recovery",
        "capture_date": "2026-08-01",
        "property_id": PROPERTY_ID,
        "property_name": canonical["name"],
        "website": residual["website"],
        "strict_qualifies": True,
        "net_new_vs_current_ledger": PROPERTY_ID not in ledger_ids,
        "property_identity_match": True,
        "contamination_verdict": "exact_property_page_to_single_knock_property_id",
        "property_boundary_evidence": {
            "page_final_url": page_final,
            "page_title": " ".join(
                (BeautifulSoup(page_body, "html.parser").title.get_text(" ", strip=True))
                .split()
            ),
            "visible_name": "Tenzen Apartments",
            "visible_address": "4117 124th Avenue SE Bellevue, WA 98006",
            "published_knock_community_id": init.group("community"),
            "knock_property_id": "2022654",
            "knock_property_name": location["name"],
            "knock_property_address": address,
            "single_source_property_ids": ["2022654"],
        },
        "source_urls": [PAGE_URL, COMMUNITY_URL, UNITS_URL],
        "native_identity_rows": len(strict_raw),
        "native_positive_rent_rows": len(strict_raw),
        "native_rows": strict_raw,
        "current_full_scraper_e2e": {
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "winning_url": result.get("_winning_page_url") or "",
            "emitted_units": len(e2e_units),
            "strict_native_positive_rent_rows": len(e2e_rows),
            "rows": e2e_rows,
            "errors": list(result.get("errors") or []),
        },
        "policy": {
            "llm_calls": 0,
            "web_unlocker_calls": 0,
            "captcha_interactions": 0,
            "hyperbrowser_sessions": 0,
            "paid_canary": False,
        },
        "ledger_snapshot": {
            "sha256": ledger_before,
            "rows": len(ledger_ids),
            "unchanged_after_validation": True,
        },
        "local_validation": "exact page/API identity + native IDs/rents + full scraper E2E assertions passed",
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "property_id": PROPERTY_ID,
        "strict_qualifies": True,
        "native_positive_rent_rows": len(strict_raw),
        "net_new": PROPERTY_ID not in ledger_ids,
        "output": str(OUT),
    }))


if __name__ == "__main__":
    asyncio.run(main())
