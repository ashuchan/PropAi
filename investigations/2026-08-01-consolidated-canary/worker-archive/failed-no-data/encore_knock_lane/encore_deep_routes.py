from __future__ import annotations

import asyncio
import csv
import json
import math
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import EntrataAdapter, parse_entrata_pp_unit_cards
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.detector import DetectedPMS


OUT = Path("/private/tmp/propai-fnd-vBkmT9/encore_knock_lane")
ARTIFACT = OUT / "encore_provider_deep_current_e2e.json"
TARGETS = {42571, 59649, 228341, 252116, 258789, 275898}
INVENTORY_PATH = {
    42571: "/en/floor-plans/",
    59649: "/pricing",
    228341: "/floorplans/",
    252116: "/pricing",
    258789: "/pricing",
    275898: "/en/floor-plans/",
}


def canonical() -> dict[int, dict[str, str]]:
    with Path("ma_poc/config/properties.csv").open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["apartmentid"]): row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "").isdigit()
            and int(row["apartmentid"]) in TARGETS
        }


def fetch(url: str, referer: str = "") -> tuple[int, str, str, str]:
    try:
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
            "",
        )
    except Exception as exc:
        return 0, url, "", f"{type(exc).__name__}: {exc}"


async def afetch(url: str, referer: str = "") -> tuple[int, str, str, str]:
    return await asyncio.to_thread(fetch, url, referer)


def fr(url: str, status: int, final_url: str, body: str) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK if status == 200 and body else FetchOutcome.HARD_FAIL,
        status=status,
        body=body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )


def norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def identity(html: str, meta: dict[str, str]) -> dict[str, object]:
    soup = BeautifulSoup(html, "lxml")
    metadata = " ".join(str(node.get("content") or "") for node in soup.select("meta[content]"))
    text = norm(f"{soup.get_text(' ', strip=True)} {metadata}")
    words = set(text.split())
    name_tokens = [
        token for token in norm(meta["name"]).split()
        if token not in {"the", "apartments", "apartment", "at", "on", "of", "homes", "home"}
    ]
    address = norm(meta["address"])
    return {
        "canonical_name": meta["name"],
        "canonical_address": meta["address"],
        "canonical_city": meta["city"],
        "canonical_state": meta["state"],
        "canonical_zip": meta["zip"],
        "name_match": bool(name_tokens) and all(token in words for token in name_tokens),
        "address_match": bool(address) and address in text,
        "street_number_and_zip_match": bool(address and meta["zip"])
        and address.split()[0] in words
        and meta["zip"] in words,
        "city_match": norm(meta["city"]) in text,
    }


def positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and math.isfinite(float(row[key]))
        and float(row[key]) > 0
        for key in (
            "market_rent_low", "market_rent_high", "rent_low", "rent_high",
            "asking_rent", "rent",
        )
    )


def native_id(row: dict) -> str:
    source_ids = row.get("source_ids")
    if isinstance(source_ids, dict):
        for key in (
            "entrata_uid",
            "betternoi_unit_uuid",
            "knock_unit_id",
            "sightmap_unit_id",
            "rentcafe_apartment_id",
            "securecafe_apartment_id",
        ):
            if source_ids.get(key) not in (None, ""):
                return f"{key}:{source_ids[key]}"
        for key, value in sorted(source_ids.items()):
            if value in (None, ""):
                continue
            low = key.casefold()
            if (
                "floorplan" in low
                or "floor_plan" in low
                or low.endswith("fpid")
                or low in {"property_id", "site_id"}
            ):
                continue
            return f"{key}:{value}"
    for key in ("native_unit_id", "source_unit_id"):
        if row.get(key) not in (None, ""):
            return f"{key}:{row[key]}"
    return ""


def strict_row(row: dict) -> bool:
    return bool(
        str(row.get("unit_number") or "").strip()
        and native_id(row)
        and unit_has_real_anchor(row)
        and positive_rent(row)
    )


def clean(row: dict) -> dict:
    fields = (
        "unit_id", "unit_number", "native_unit_id", "source_unit_id",
        "floor_plan_name", "floor_plan_id", "bedrooms", "bathrooms", "sqft",
        "building", "floor", "market_rent_low", "market_rent_high", "rent_low",
        "rent_high", "availability_status", "availability_date", "available_date",
        "lease_term", "source_property_id", "source_api_url", "extraction_tier",
    )
    out = {key: row.get(key) for key in fields if row.get(key) not in (None, "")}
    if isinstance(row.get("source_ids"), dict):
        out["source_ids"] = row["source_ids"]
    return out


def summarize_units(rows: list[dict]) -> dict:
    strict = [row for row in rows if strict_row(row)]
    units = [str(row.get("unit_number") or "") for row in strict]
    ids = [native_id(row) for row in strict]
    return {
        "all_rows": len(rows),
        "strict_native_positive_rows": len(strict),
        "distinct_unit_numbers": len(set(units)),
        "distinct_native_ids": len(set(ids)),
        "duplicate_unit_numbers": len(units) - len(set(units)),
        "duplicate_native_ids": len(ids) - len(set(ids)),
        "rows": [clean(row) for row in strict],
    }


async def scrape_e2e(
    pid: int,
    meta: dict[str, str],
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
            fetch_result=fr(url, status, final_url, body),
            api_responses=api_responses,
            csv_row=meta,
            property_id=str(pid),
            shared_budget={
                "llm_api_calls": 0,
                "llm_dom_calls": 0,
                "llm_monolithic": 0,
                "link_hop": 0,
                "_cost_cap_usd": 0,
            },
        )
        rows = list(result.get("units") or [])
        return {
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "errors": list(result.get("errors") or [])[-8:],
            **summarize_units(rows),
        }
    except Exception as exc:
        return {
            "exception": f"{type(exc).__name__}: {exc}",
            **summarize_units([]),
        }


async def entrata_adapter_e2e(
    pid: int,
    meta: dict[str, str],
    url: str,
    status: int,
    final_url: str,
    body: str,
) -> dict:
    detected = DetectedPMS(pms="entrata", confidence=1.0, evidence=["exact published Entrata detail route"])
    ctx = AdapterContext(
        base_url=url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=str(pid),
        fetch_result=fr(url, status, final_url, body),
        property_name=meta["name"],
        address=meta["address"],
        city=meta["city"],
        state=meta["state"],
        zip_code=meta["zip"],
        budget={"llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0, "link_hop": 0},
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    try:
        result = await EntrataAdapter().extract(None, ctx)
        return {
            "adapter": "entrata",
            "tier": result.tier_used,
            "winning_url": result.winning_url,
            "errors": result.errors,
            **summarize_units(list(result.units or [])),
        }
    except Exception as exc:
        return {
            "adapter": "entrata",
            "exception": f"{type(exc).__name__}: {exc}",
            **summarize_units([]),
        }


def betternoi_pairs(body: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pattern = re.compile(
        r"data-property\s*=\s*[\\\"']+([0-9a-f-]{36})[\\\"']+"
        r".{0,600}?data-fpcode\s*=\s*[\\\"']+([0-9a-f-]{36})[\\\"']+",
        re.I | re.S,
    )
    for match in pattern.finditer(body):
        pair = (match.group(1), match.group(2))
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def raw_betternoi_rows(api_responses: list[dict], meta: dict[str, str]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    addresses: set[str] = set()
    cities: set[str] = set()
    states: set[str] = set()
    zips: set[str] = set()
    names: set[str] = set()
    clients: set[str] = set()
    for response in api_responses:
        body = response["body"]
        payload = body if isinstance(body, dict) else json.loads(str(body))
        for raw in payload.get("results") or []:
            unit_number = str(raw.get("unit_number") or "").strip()
            uid = str(raw.get("uuid") or raw.get("id") or "").strip()
            rent_low = int(float(raw.get("min_rent") or 0)) or None
            rent_high = int(float(raw.get("max_rent") or 0)) or rent_low
            floorplan = raw.get("floor_plan") if isinstance(raw.get("floor_plan"), dict) else {}
            addresses.add(str(raw.get("building_address") or "").strip())
            cities.add(str(raw.get("building_city") or "").strip())
            states.add(str(raw.get("building_state") or "").strip())
            zips.add(str(raw.get("building_postal_code") or "").strip())
            clients.add(str(raw.get("client_uuid") or "").strip())
            for key in ("building_name", "property_name", "client_name"):
                if raw.get(key): names.add(str(raw[key]).strip())
            rows.append({
                "unit_number": unit_number,
                "floor_plan_name": str(floorplan.get("name") or ""),
                "bedrooms": str(raw.get("bedroom_count") or ""),
                "bathrooms": str(raw.get("bathroom_count") or ""),
                "sqft": str(raw.get("min_square_feet") or ""),
                "market_rent_low": rent_low,
                "market_rent_high": rent_high,
                "availability_status": str(raw.get("availability_status") or "AVAILABLE"),
                "availability_date": str(raw.get("adjusted_available_date") or ""),
                "source_api_url": response["url"],
                "extraction_tier": "TIER_1_PUBLIC_BETTERNOI_API",
                "source_ids": {
                    "betternoi_unit_uuid": uid,
                    "betternoi_unit_id": str(raw.get("id") or ""),
                    "property_id": str(raw.get("client_uuid") or ""),
                    "floor_plan_id": str(floorplan.get("uuid") or ""),
                },
            })
    addresses.discard("")
    cities.discard("")
    states.discard("")
    zips.discard("")
    clients.discard("")
    identity_evidence = {
        "published_names": sorted(names),
        "published_addresses": sorted(addresses),
        "published_cities": sorted(cities),
        "published_states": sorted(states),
        "published_zips": sorted(zips),
        "client_uuids": sorted(clients),
        "all_rows_exact_canonical_address": bool(addresses) and all(norm(value) == norm(meta["address"]) for value in addresses),
        "all_rows_exact_canonical_city": bool(cities) and all(norm(value) == norm(meta["city"]) for value in cities),
        "all_rows_exact_canonical_state": bool(states) and all(norm(value) == norm(meta["state"]) for value in states),
        "all_rows_exact_canonical_zip": bool(zips) and all(value == meta["zip"] for value in zips),
    }
    return rows, identity_evidence


async def audit_betternoi(pid: int, meta: dict[str, str]) -> dict:
    page_url = urljoin(meta["website"], INVENTORY_PATH[pid])
    status, final_url, body, error = await afetch(page_url)
    page_identity = identity(body, meta)
    pairs = betternoi_pairs(body)
    api_urls = [
        "https://ares.betternoi.com/api/pub/v1/client/building/unit"
        f"?client_uuid={client}&floorplan_uuid={floorplan}&is_available=true"
        for client, floorplan in pairs
    ]
    fetched = await asyncio.gather(*(afetch(url, final_url) for url in api_urls))
    api_responses: list[dict] = []
    fetches: list[dict] = []
    for url, (api_status, api_final, api_body, api_error) in zip(api_urls, fetched):
        fetches.append({
            "url": url,
            "status": api_status,
            "final_url": api_final,
            "body_bytes": len(api_body.encode()),
            "error": api_error,
        })
        if api_status == 200 and api_body:
            try:
                api_responses.append({"url": api_final, "status": api_status, "body": json.loads(api_body)})
            except json.JSONDecodeError:
                pass
    detected = DetectedPMS(pms="unknown", confidence=1.0, evidence=["audit-generic-api"])
    ctx = AdapterContext(
        base_url=page_url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=str(pid),
        fetch_result=fr(page_url, status, final_url, body),
        property_name=meta["name"],
        address=meta["address"],
        city=meta["city"],
        state=meta["state"],
        zip_code=meta["zip"],
        budget={"llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0, "link_hop": 0},
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    adapter_result = await GenericAdapter().extract(None, ctx)
    generic_adapter = {
        "adapter": "generic",
        "tier": adapter_result.tier_used,
        "errors": adapter_result.errors,
        **summarize_units(list(adapter_result.units or [])),
    }
    raw_rows, payload_identity = raw_betternoi_rows(api_responses, meta)
    # The generic adapter currently normalises the native label/rent but does
    # not retain BetterNOI's UUID. Crosswalk on the published unit number and
    # require a one-to-one match before treating its E2E output as strict.
    raw_by_unit = {str(row["unit_number"]): row for row in raw_rows}
    adapter_numbers = {
        str(row.get("unit_number") or "")
        for row in list(adapter_result.units or [])
        if str(row.get("unit_number") or "") and positive_rent(row)
    }
    crosswalk = {
        "adapter_positive_unit_numbers": sorted(adapter_numbers),
        "raw_native_unit_numbers": sorted(raw_by_unit),
        "exact_one_to_one": bool(adapter_numbers) and adapter_numbers == set(raw_by_unit),
    }
    return {
        "provider": "betternoi_public_units",
        "inventory_url": page_url,
        "inventory_final_url": final_url,
        "inventory_status": status,
        "inventory_error": error,
        "page_identity": page_identity,
        "published_client_floorplan_pairs": [list(pair) for pair in pairs],
        "api_fetches": fetches,
        "api_payload_identity": payload_identity,
        "generic_adapter_e2e": generic_adapter,
        "raw_strict": summarize_units(raw_rows),
        "adapter_to_raw_native_crosswalk": crosswalk,
    }


async def audit_entrata(pid: int, meta: dict[str, str]) -> dict:
    parent_requested = urljoin(meta["website"], INVENTORY_PATH[pid])
    parent_status, parent_url, parent_html, parent_error = await afetch(parent_requested)
    parent_identity = identity(parent_html, meta)
    parent_soup = BeautifulSoup(parent_html, "lxml")
    iframe_nodes = [
        node for node in parent_soup.select("iframe[src]")
        if "entratasnipit." in str(node.get("src") or "").casefold()
        and "application_authentication" not in str(node.get("src") or "").casefold()
    ]
    if len(iframe_nodes) != 1:
        return {
            "provider": "entrata_prospectportal_snippet",
            "parent_requested_url": parent_requested,
            "parent_final_url": parent_url,
            "parent_status": parent_status,
            "parent_error": parent_error,
            "parent_identity": parent_identity,
            "rejection": f"expected exactly one inventory Entrata iframe; found {len(iframe_nodes)}",
        }
    raw_iframe = str(iframe_nodes[0].get("src") or "")
    iframe_url = urljoin(parent_url, raw_iframe)
    host_domain = urlsplit(parent_url).hostname or ""
    iframe_url = f"{iframe_url}{'&' if '?' in iframe_url else '?'}host_domain={quote(host_domain)}"
    index_status, index_url, index_html, index_error = await afetch(iframe_url, parent_url)
    index_identity = identity(index_html, meta)
    index_soup = BeautifulSoup(index_html, "lxml")
    detail_urls: list[str] = []
    for anchor in index_soup.select("a[href*='/Apartments/module/property_floorplans/']"):
        url = urljoin(index_url, str(anchor.get("href") or ""))
        if urlsplit(url).hostname == urlsplit(index_url).hostname and url not in detail_urls:
            detail_urls.append(url)

    detail_fetches = await asyncio.gather(*(afetch(url, index_url) for url in detail_urls))
    detail_results: list[dict] = []
    all_e2e_rows: list[dict] = []
    all_parser_rows: list[dict] = []
    for detail_url, (status, final_url, body, error) in zip(detail_urls, detail_fetches):
        e2e = await entrata_adapter_e2e(pid, meta, detail_url, status, final_url, body)
        parser_rows = list(parse_entrata_pp_unit_cards(body, final_url))
        parser_strict = [row for row in parser_rows if strict_row(row)]
        all_e2e_rows.extend(e2e["rows"])
        all_parser_rows.extend(parser_strict)
        detail_results.append({
            "published_url": detail_url,
            "status": status,
            "final_url": final_url,
            "body_bytes": len(body.encode()),
            "fetch_error": error,
            "e2e": e2e,
            "parser_crosscheck": summarize_units(parser_rows),
        })

    def dedupe(rows: list[dict]) -> list[dict]:
        by_key: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (str(row.get("unit_number") or ""), native_id(row))
            if key[0] and key[1]:
                by_key[key] = row
        return list(by_key.values())

    e2e_rows = dedupe(all_e2e_rows)
    parser_rows = dedupe(all_parser_rows)
    return {
        "provider": "entrata_prospectportal_snippet",
        "parent_requested_url": parent_requested,
        "parent_final_url": parent_url,
        "parent_status": parent_status,
        "parent_error": parent_error,
        "parent_identity": parent_identity,
        "published_iframe_src": raw_iframe,
        "index_url": index_url,
        "index_status": index_status,
        "index_error": index_error,
        "index_identity": index_identity,
        "published_detail_urls": detail_urls,
        "detail_results": detail_results,
        "e2e_consolidated": summarize_units(e2e_rows),
        "parser_crosscheck_consolidated": summarize_units(parser_rows),
        "e2e_parser_exact_unit_id_set_match": {
            native_id(row) for row in e2e_rows
        } == {native_id(row) for row in parser_rows},
    }


async def audit_onesite(pid: int, meta: dict[str, str]) -> dict:
    page_url = urljoin(meta["website"], INVENTORY_PATH[pid])
    status, final_url, body, error = await afetch(page_url)
    page_identity = identity(body, meta)
    detected = DetectedPMS(pms="onesite", confidence=1.0, evidence=["exact widgetLoader siteId"])
    ctx = AdapterContext(
        base_url=page_url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=str(pid),
        fetch_result=fr(page_url, status, final_url, body),
        property_name=meta["name"],
        address=meta["address"],
        city=meta["city"],
        state=meta["state"],
        zip_code=meta["zip"],
        budget={"llm_api_calls": 0, "llm_dom_calls": 0, "llm_monolithic": 0, "link_hop": 0},
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    try:
        result = await OneSiteAdapter().extract(None, ctx)
        adapter = {
            "adapter": "onesite",
            "tier": result.tier_used,
            "winning_url": result.winning_url,
            "errors": result.errors,
            **summarize_units(list(result.units or [])),
        }
    except Exception as exc:
        adapter = {
            "adapter": "onesite",
            "exception": f"{type(exc).__name__}: {exc}",
            **summarize_units([]),
        }
    return {
        "provider": "realpage_onesite_workflow",
        "inventory_url": page_url,
        "inventory_final_url": final_url,
        "inventory_status": status,
        "inventory_error": error,
        "page_identity": page_identity,
        "published_widget_site_ids": sorted(set(re.findall(r"widgetLoader\.js\?siteId=(\d+)", body, re.I))),
        "onesite_adapter_e2e": adapter,
    }


async def main() -> None:
    metadata = canonical()
    assert set(metadata) == TARGETS
    results: list[dict] = []
    for pid in sorted(TARGETS):
        meta = metadata[pid]
        if pid in {42571, 275898}:
            audit = await audit_betternoi(pid, meta)
        elif pid in {59649, 252116, 258789}:
            audit = await audit_entrata(pid, meta)
        else:
            audit = await audit_onesite(pid, meta)
        results.append({
            "property_id": pid,
            "property_name": meta["name"],
            "website": meta["website"],
            "audit": audit,
        })
        progress = {
            "scope": "current exact published property inventory routes; current local adapter E2E",
            "llm_calls": 0,
            "captcha_solving": False,
            "web_unlocker_calls": 0,
            "hyperbrowser_sessions": 0,
            "paid_canary": False,
            "results": results,
        }
        (OUT / "encore_provider_deep_current_e2e.progress.json").write_text(
            json.dumps(progress, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    payload = {
        "scope": "current exact published property inventory routes; current local adapter E2E",
        "llm_calls": 0,
        "captcha_solving": False,
        "web_unlocker_calls": 0,
        "hyperbrowser_sessions": 0,
        "paid_canary": False,
        "results": results,
    }
    ARTIFACT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "artifact": str(ARTIFACT),
        "results": [
            {
                "property_id": row["property_id"],
                "provider": row["audit"].get("provider"),
                "e2e_strict": (
                    row["audit"].get("e2e_consolidated", {}).get("strict_native_positive_rows")
                    if row["audit"].get("provider") == "entrata_prospectportal_snippet"
                    else row["audit"].get("generic_adapter_e2e", {}).get("all_rows")
                    if row["audit"].get("provider") == "betternoi_public_units"
                    else row["audit"].get("onesite_adapter_e2e", {}).get("strict_native_positive_rows")
                ),
                "raw_strict": (
                    row["audit"].get("parser_crosscheck_consolidated", {}).get("strict_native_positive_rows")
                    if row["audit"].get("provider") == "entrata_prospectportal_snippet"
                    else row["audit"].get("raw_strict", {}).get("strict_native_positive_rows")
                    if row["audit"].get("provider") == "betternoi_public_units"
                    else None
                ),
            }
            for row in results
        ],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
