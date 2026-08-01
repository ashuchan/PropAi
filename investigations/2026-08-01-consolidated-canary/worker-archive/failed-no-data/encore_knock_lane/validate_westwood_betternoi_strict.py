from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import DetectedPMS


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "encore_knock_lane/evidence_encore_42571_betternoi_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTY_ID = 42571
INVENTORY_URL = "https://westwoodvillageapthomes.com/en/floor-plans/"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("apartmentid") == str(PROPERTY_ID):
                return row
    raise AssertionError("canonical property missing")


def get(url: str, referer: str = "") -> tuple[int, str, str]:
    response = probe_get(
        url,
        timeout=40,
        unlocker=False,
        retries=1,
        headers={"Referer": referer} if referer else None,
    )
    return (
        int(getattr(response, "status_code", 0) or 0),
        str(getattr(response, "url", "") or url),
        str(getattr(response, "text", "") or ""),
    )


def norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def norm_address(value: str) -> str:
    aliases = {
        "avenue": "ave", "street": "st", "boulevard": "blvd",
        "road": "rd", "parkway": "pkwy", "drive": "dr",
        "lane": "ln", "court": "ct", "place": "pl",
    }
    return " ".join(aliases.get(token, token) for token in norm(value).split())


def visible_identity(body: str, meta: dict[str, str]) -> dict[str, bool]:
    soup = BeautifulSoup(body, "lxml")
    text = norm(soup.get_text(" ", strip=True))
    words = set(text.split())
    name_tokens = [
        token for token in norm(meta["name"]).split()
        if token not in {"apartments", "apartment", "the", "at", "of"}
    ]
    address = norm_address(meta["address"])
    return {
        "name_match": bool(name_tokens) and all(token in words for token in name_tokens),
        "street_number_match": address.split()[0] in words,
        "city_match": norm(meta["city"]) in text,
        "state_match": norm(meta["state"]) in text,
        "zip_match": meta["zip"] in words,
    }


def pairs(body: str) -> list[tuple[str, str]]:
    pattern = re.compile(
        r"data-property\s*=\s*[\\\"']+([0-9a-f-]{36})[\\\"']+"
        r".{0,600}?data-fpcode\s*=\s*[\\\"']+([0-9a-f-]{36})[\\\"']+",
        re.I | re.S,
    )
    found: list[tuple[str, str]] = []
    for match in pattern.finditer(body):
        pair = (match.group(1), match.group(2))
        if pair not in found:
            found.append(pair)
    return found


def positive(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and math.isfinite(float(row[key]))
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


def adapter_context(
    meta: dict[str, str], status: int, final_url: str, body: str, api_responses: list[dict]
) -> AdapterContext:
    fetch_result = FetchResult(
        url=INVENTORY_URL,
        outcome=FetchOutcome.OK,
        status=status,
        body=body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )
    ctx = AdapterContext(
        base_url=INVENTORY_URL,
        detected=DetectedPMS(pms="unknown", confidence=1.0, evidence=["exact BetterNOI public XHR"]),
        profile=None,
        expected_total_units=None,
        property_id=str(PROPERTY_ID),
        fetch_result=fetch_result,
        property_name=meta["name"],
        address=meta["address"],
        city=meta["city"],
        state=meta["state"],
        zip_code=meta["zip"],
        budget={"llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0, "link_hop": 0},
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


async def main() -> None:
    meta = metadata()
    ledger_sha_before = sha(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger_ids = {int(row["property_id"]) for row in ledger_rows}

    status, final_url, body = get(INVENTORY_URL)
    assert status == 200 and final_url == INVENTORY_URL and body
    page_identity = visible_identity(body, meta)
    assert all(page_identity.values())

    published_pairs = pairs(body)
    assert published_pairs == [
        ("01a0e491-f0fd-4d03-9529-00d881128a10", "c508d086-25cf-493b-a3d8-cf90c7fb9a9e")
    ]
    api_url = (
        "https://ares.betternoi.com/api/pub/v1/client/building/unit"
        f"?client_uuid={published_pairs[0][0]}&floorplan_uuid={published_pairs[0][1]}"
        "&is_available=true"
    )
    api_status, api_final, api_body = get(api_url, final_url)
    assert api_status == 200 and api_body
    payload = json.loads(api_body)
    raw = payload.get("results") or []
    assert len(raw) == 1
    native = raw[0]
    floorplan = native.get("floor_plan") or {}
    native_id = str(native.get("uuid") or "").strip()
    unit_number = str(native.get("unit_number") or "").strip()
    assert native_id and unit_number
    assert str(native.get("client_uuid")) == published_pairs[0][0]
    assert norm_address(str(native.get("building_address") or "")) == norm_address(meta["address"])
    assert norm(str(native.get("building_city") or "")) == norm(meta["city"])
    assert norm(str(native.get("building_state") or "")) == norm(meta["state"])
    assert str(native.get("building_postal_code") or "") == meta["zip"]
    raw_rent = int(float(native.get("min_rent") or 0))
    assert raw_rent > 0

    api_responses = [{"url": api_final, "status": api_status, "body": payload}]
    adapter_result = await GenericAdapter().extract(
        None, adapter_context(meta, status, final_url, body, api_responses)
    )
    adapter_rows = list(adapter_result.units or [])
    assert adapter_result.tier_used == "TIER_1_API"
    assert len(adapter_rows) == 1
    adapter_row = adapter_rows[0]
    assert str(adapter_row.get("unit_number") or "") == unit_number
    assert positive(adapter_row)
    adapter_rent = int(
        adapter_row.get("market_rent_low")
        or adapter_row.get("rent_low")
        or 0
    )
    assert adapter_rent == raw_rent

    accepted_row = {
        "unit_number": unit_number,
        "floor_plan_name": str(floorplan.get("name") or ""),
        "bedrooms": str(native.get("bedroom_count") or ""),
        "bathrooms": str(native.get("bathroom_count") or ""),
        "sqft": str(native.get("min_square_feet") or ""),
        "market_rent_low": raw_rent,
        "market_rent_high": int(float(native.get("max_rent") or raw_rent)),
        "availability_status": str(native.get("availability_status") or "AVAILABLE").upper(),
        "availability_date": str(native.get("adjusted_available_date") or ""),
        "source_api_url": api_final,
        "extraction_tier": "TIER_1_PUBLIC_BETTERNOI_API_GENERIC_E2E_CROSSWALK",
        "source_ids": {
            "betternoi_unit_uuid": native_id,
            "betternoi_unit_id": str(native.get("id") or ""),
            "property_id": str(native.get("client_uuid") or ""),
            "floor_plan_id": str(floorplan.get("uuid") or ""),
        },
    }
    payload_out = {
        "result_type": "strict_current_exact_betternoi_generic_adapter_e2e_crosswalk",
        "property": {
            "property_id": PROPERTY_ID,
            "property_name": meta["name"],
            "website": meta["website"],
            "address": meta["address"],
            "city": meta["city"],
            "state": meta["state"],
            "zip": meta["zip"],
        },
        "policy": {
            "llm_calls": 0,
            "captcha_solving": False,
            "web_unlocker_calls": 0,
            "hyperbrowser_sessions": 0,
            "paid_canary": False,
        },
        "inventory_route": {
            "requested_url": INVENTORY_URL,
            "final_url": final_url,
            "status": status,
            "page_identity": page_identity,
            "published_client_floorplan_pairs": [list(pair) for pair in published_pairs],
        },
        "api_route": {
            "requested_url": api_url,
            "final_url": api_final,
            "status": api_status,
            "provider_client_uuid": published_pairs[0][0],
            "provider_address": str(native.get("building_address") or ""),
            "provider_city": str(native.get("building_city") or ""),
            "provider_state": str(native.get("building_state") or ""),
            "provider_zip": str(native.get("building_postal_code") or ""),
        },
        "generic_adapter_e2e": {
            "adapter": "GenericAdapter",
            "tier": adapter_result.tier_used,
            "errors": adapter_result.errors,
            "emitted_rows": len(adapter_rows),
            "adapter_unit_number": str(adapter_row.get("unit_number") or ""),
            "adapter_rent": adapter_rent,
        },
        "adapter_to_native_crosswalk": {
            "exact_one_to_one": True,
            "key": "unit_number",
            "unit_number": unit_number,
            "native_unit_uuid": native_id,
            "rent_exact_match": True,
        },
        "property_identity_match": True,
        "strict_gates": {
            "exact_property_page_name_address_city_state_zip": True,
            "single_published_provider_client": True,
            "provider_row_exact_address_city_state_zip": True,
            "native_unit_uuid_present": True,
            "native_unit_number_present": True,
            "positive_rent": True,
            "generic_adapter_e2e_emitted_exact_unit": True,
            "adapter_raw_unit_number_and_rent_crosswalk": True,
            "distinct_native_ids": 1,
            "distinct_unit_numbers": 1,
            "sibling_or_cross_property_rows": 0,
        },
        "contamination_verdict": "pass_exact_property_single_client_native_uuid_positive_rent_generic_e2e_crosswalk",
        "native_identity_rows": 1,
        "native_positive_rent_rows": 1,
        "source_urls": [api_final],
        "native_rows": [accepted_row],
        "shared_ledger": {
            "path": str(LEDGER),
            "sha256_before": ledger_sha_before,
            "rows_before": len(ledger_rows),
            "property_present_before": PROPERTY_ID in ledger_ids,
        },
    }
    assert sha(LEDGER) == ledger_sha_before
    OUT.write_text(json.dumps(payload_out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "artifact": str(OUT),
        "property_id": PROPERTY_ID,
        "strict_rows": 1,
        "net_new": PROPERTY_ID not in ledger_ids,
        "ledger_rows": len(ledger_rows),
        "ledger_sha256": ledger_sha_before,
    }, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
