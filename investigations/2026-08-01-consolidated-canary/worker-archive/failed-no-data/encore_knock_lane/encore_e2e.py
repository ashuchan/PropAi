from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/encore_knock_lane")
PROBE = ROOT / "encore_probe.json"
OUTPUT = ROOT / "encore_exact_route_current_e2e.json"
TARGET_IDS = {42571, 59649, 228341, 252116, 258789, 275898}


def load_metadata() -> dict[int, dict[str, str]]:
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["apartmentid"]): row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "").isdigit()
            and int(row["apartmentid"]) in TARGET_IDS
        }


def fetch(url: str) -> tuple[int, str, str, str]:
    try:
        response = probe_get(url, timeout=35, unlocker=False, retries=1)
        return (
            int(getattr(response, "status_code", 0) or 0),
            str(getattr(response, "url", "") or url),
            str(getattr(response, "text", "") or ""),
            "",
        )
    except Exception as exc:
        return 0, url, "", f"{type(exc).__name__}: {exc}"


def fetch_result(url: str, status: int, final_url: str, body: str) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK if status == 200 and body else FetchOutcome.ERROR,
        status=status,
        body=body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(key), (int, float))
        and not isinstance(unit.get(key), bool)
        and unit[key] > 0
        for key in (
            "market_rent_low", "market_rent_high", "rent_low", "rent_high",
            "asking_rent", "rent",
        )
    )


def anchor(unit: dict) -> str:
    source_ids = unit.get("source_ids")
    if isinstance(source_ids, dict):
        for key, value in sorted(source_ids.items()):
            if value not in (None, "") and not key.endswith(("floorplan_id", "floor_plan_id")):
                return f"{key}:{value}"
    return str(unit.get("unit_id") or unit.get("native_unit_id") or unit.get("unit_number") or "")


def clean_unit(unit: dict) -> dict:
    keep = (
        "unit_id", "unit_number", "native_unit_id", "source_unit_id",
        "floor_plan_name", "floor_plan_id", "bedrooms", "bathrooms", "sqft",
        "building", "floor", "market_rent_low", "market_rent_high", "rent_low",
        "rent_high", "availability_status", "availability_date", "available_date",
        "lease_term", "source_property_id", "source_api_url", "extraction_tier",
    )
    cleaned = {key: unit.get(key) for key in keep if unit.get(key) not in (None, "")}
    if isinstance(unit.get("source_ids"), dict):
        cleaned["source_ids"] = unit["source_ids"]
    return cleaned


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def visible_identity(row: dict[str, str], body: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    for element in soup.select("script,style,noscript"):
        element.decompose()
    text = norm(soup.get_text(" ", strip=True))
    tokens = set(text.split())
    name = str(row.get("name") or "")
    address = str(row.get("address") or "")
    name_tokens = [
        token for token in norm(name).split()
        if token not in {"the", "at", "of", "apartments", "apartment", "homes", "home"}
    ]
    address_tokens = norm(address).split()
    ignored = {
        "n", "s", "e", "w", "north", "south", "east", "west", "st", "street",
        "rd", "road", "ave", "avenue", "blvd", "boulevard", "pkwy", "parkway",
        "dr", "drive", "ln", "lane", "ct", "court", "way", "pl", "place",
    }
    number = address_tokens[0] if address_tokens else ""
    words = [token for token in address_tokens[1:] if token not in ignored]
    return {
        "canonical_name": name,
        "canonical_address": address,
        "canonical_city_state_zip": f"{row.get('city')}, {row.get('state')} {row.get('zip')}",
        "name_exact_visible": bool(norm(name) and norm(name) in text),
        "name_distinctive_tokens_visible": bool(name_tokens and all(token in tokens for token in name_tokens)),
        "address_visible": bool(number and number in tokens and words and all(word in tokens for word in words)),
        "city_visible": norm(str(row.get("city") or "")) in text,
        "zip_visible": str(row.get("zip") or "") in text,
    }


def betternoi_urls(body: str) -> list[str]:
    soup = BeautifulSoup(body, "html.parser")
    pairs: list[tuple[str, str]] = []
    for element in soup.select("[data-property][data-fpcode]"):
        client = str(element.get("data-property") or "").strip()
        floorplan = str(element.get("data-fpcode") or "").strip()
        if client and floorplan and (client, floorplan) not in pairs:
            pairs.append((client, floorplan))
    return [
        "https://ares.betternoi.com/api/pub/v1/client/building/unit"
        f"?client_uuid={client}&floorplan_uuid={floorplan}&is_available=true"
        for client, floorplan in pairs
    ]


def payload_identity(api_responses: list[dict], row: dict[str, str]) -> dict:
    names: set[str] = set()
    addresses: set[str] = set()
    cities: set[str] = set()
    states: set[str] = set()
    zips: set[str] = set()
    client_ids: set[str] = set()
    native_ids: set[str] = set()
    unit_numbers: set[str] = set()
    raw_rows = 0
    for response in api_responses:
        body = response.get("body")
        try:
            payload = body if isinstance(body, (dict, list)) else json.loads(str(body or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        candidates = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if not isinstance(item, dict):
                continue
            raw_rows += 1
            for key in ("building_name", "property_name", "client_name"):
                if item.get(key): names.add(str(item[key]).strip())
            if item.get("building_address"): addresses.add(str(item["building_address"]).strip())
            if item.get("building_city"): cities.add(str(item["building_city"]).strip())
            if item.get("building_state"): states.add(str(item["building_state"]).strip())
            if item.get("building_postal_code"): zips.add(str(item["building_postal_code"]).strip())
            if item.get("client_uuid"): client_ids.add(str(item["client_uuid"]).strip())
            for key in ("uuid", "id"):
                if item.get(key) not in (None, ""): native_ids.add(str(item[key]))
            if item.get("unit_number") not in (None, ""): unit_numbers.add(str(item["unit_number"]))
    canonical_address = norm(str(row.get("address") or ""))
    return {
        "raw_rows": raw_rows,
        "published_names": sorted(names),
        "published_addresses": sorted(addresses),
        "published_cities": sorted(cities),
        "published_states": sorted(states),
        "published_zips": sorted(zips),
        "client_uuids": sorted(client_ids),
        "distinct_native_ids": len(native_ids),
        "distinct_unit_numbers": len(unit_numbers),
        "canonical_address_exact_normalized": bool(
            addresses and all(norm(address) == canonical_address for address in addresses)
        ),
        "canonical_city_exact_normalized": bool(
            cities and all(norm(city) == norm(str(row.get("city") or "")) for city in cities)
        ),
        "canonical_state_exact_normalized": bool(
            states and all(norm(state) == norm(str(row.get("state") or "")) for state in states)
        ),
        "canonical_zip_exact": bool(
            zips and all(zip_code == str(row.get("zip") or "") for zip_code in zips)
        ),
    }


async def run_scrape(
    pid: int,
    row: dict[str, str],
    url: str,
    status: int,
    final_url: str,
    body: str,
    api_responses: list[dict] | None = None,
) -> dict:
    try:
        result = await scraper_mod.scrape(
            url,
            page=None,
            fetch_result=fetch_result(url, status, final_url, body),
            api_responses=api_responses,
            csv_row=row,
            property_id=str(pid),
            shared_budget={
                "llm_api_calls": 0,
                "llm_dom_calls": 0,
                "llm_monolithic": 0,
                "link_hop": 0,
                "_cost_cap_usd": 0,
            },
        )
    except Exception as exc:
        return {
            "requested_url": url,
            "status": status,
            "final_url": final_url,
            "exception": f"{type(exc).__name__}: {exc}",
            "units": 0,
            "qualified_units": 0,
            "rows": [],
        }
    units = list(result.get("units") or [])
    native = [unit for unit in units if unit_has_real_anchor(unit)]
    qualified = [unit for unit in native if positive_rent(unit)]
    anchors = [anchor(unit) for unit in qualified]
    numbers = [str(unit.get("unit_number") or "") for unit in qualified]
    return {
        "requested_url": url,
        "status": status,
        "final_url": final_url,
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "raw_outcome": result.get("_raw_extractor_outcome"),
        "units": len(units),
        "native_units": len(native),
        "qualified_units": len(qualified),
        "distinct_anchors": len(set(anchors)),
        "distinct_unit_numbers": len(set(numbers)),
        "duplicate_anchor_count": len(anchors) - len(set(anchors)),
        "duplicate_unit_number_count": len(numbers) - len(set(numbers)),
        "errors": list(result.get("errors") or [])[-8:],
        "rows": [clean_unit(unit) for unit in qualified],
    }


async def main() -> None:
    probe = json.loads(PROBE.read_text(encoding="utf-8"))
    metadata = load_metadata()
    output_rows: list[dict] = []
    route_preference = {
        42571: "/en/floor-plans/",
        59649: "/pricing",
        228341: "/floorplans/",
        252116: "/pricing",
        258789: "/pricing",
        275898: "/en/floor-plans/",
    }
    for probed in probe["results"]:
        pid = int(probed["property_id"])
        row = metadata[pid]
        candidates = [
            route for route in probed["exact_linked_routes"]
            if route_preference[pid] in urlparse(route["final_url"]).path
        ]
        route = candidates[0] if candidates else probed["home"]
        requested = str(route["requested_url"])
        status, final_url, body, fetch_error = fetch(requested)
        api_responses: list[dict] = []
        api_fetches: list[dict] = []
        for api_url in betternoi_urls(body):
            api_status, api_final, api_body, api_error = fetch(api_url)
            api_fetches.append({
                "url": api_url,
                "status": api_status,
                "final_url": api_final,
                "body_bytes": len(api_body.encode()),
                "error": api_error,
            })
            if api_status == 200 and api_body:
                try:
                    parsed_body = json.loads(api_body)
                except json.JSONDecodeError:
                    parsed_body = api_body
                api_responses.append({"url": api_final, "status": api_status, "body": parsed_body})

        primary = await run_scrape(
            pid, row, requested, status, final_url, body,
            api_responses=api_responses if api_responses else None,
        )

        iframe_runs: list[dict] = []
        soup = BeautifulSoup(body, "html.parser")
        for frame in soup.select("iframe[src]"):
            iframe_url = urljoin(final_url, str(frame.get("src") or ""))
            if not re.search(r"entratasnipit", iframe_url, re.I):
                continue
            frame_status, frame_final, frame_body, frame_error = fetch(iframe_url)
            frame_run = await run_scrape(
                pid, row, iframe_url, frame_status, frame_final, frame_body,
            )
            frame_run["fetch_error"] = frame_error
            frame_run["parent_inventory_url"] = final_url
            iframe_runs.append(frame_run)

        output_rows.append({
            "property_id": pid,
            "canonical_name": row.get("name"),
            "canonical_address": row.get("address"),
            "configured_website": row.get("website"),
            "inventory_route": requested,
            "inventory_final_url": final_url,
            "inventory_final_host": (urlparse(final_url).hostname or "").lower(),
            "inventory_status": status,
            "inventory_fetch_error": fetch_error,
            "visible_identity": visible_identity(row, body),
            "api_fetches": api_fetches,
            "api_payload_identity": payload_identity(api_responses, row),
            "primary_e2e": primary,
            "entrata_iframe_e2e": iframe_runs,
        })

    payload = {
        "scope": "exact current linked inventory routes; local scraper E2E; direct public requests only",
        "llm_calls": 0,
        "paid_unlocker_calls": 0,
        "captcha_solving": False,
        "target_count": len(output_rows),
        "results": output_rows,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "artifact": str(OUTPUT),
        "results": [
            {
                "property_id": item["property_id"],
                "final_url": item["inventory_final_url"],
                "primary": {
                    key: item["primary_e2e"].get(key)
                    for key in ("adapter", "tier", "qualified_units", "distinct_anchors")
                },
                "iframe": [
                    {key: run.get(key) for key in ("adapter", "tier", "qualified_units", "distinct_anchors")}
                    for run in item["entrata_iframe_e2e"]
                ],
            }
            for item in output_rows
        ],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
