#!/usr/bin/env python3
"""Validate current embedded/public Entrata rosters for three residual sites."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import parse_entrata_pp_unit_cards


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
TARGET_IDS = {9473, 72391, 298586}
CONSOLIDATED = OUT / "evidence_entrata_embedded_direct_current_strict.json"
VILLAGE_ARTIFACT = OUT / "evidence_entrata_9473_village_cliffs_current_strict.json"
LUMINA_ARTIFACT = OUT / "evidence_entrata_72391_lumina_current_strict.json"
GATEWAY_ARTIFACT = OUT / "evidence_entrata_298586_gateway_lofts_current_strict.json"


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_rows() -> dict[int, dict[str, str]]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["apartmentid"]): row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "").isdigit()
            and int(row["apartmentid"]) in TARGET_IDS
        }


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def same_origin(left_url: str, right_url: str) -> bool:
    left = urlsplit(left_url)
    right = urlsplit(right_url)
    return bool(
        left.scheme.casefold() == right.scheme.casefold()
        and (left.hostname or "").casefold() == (right.hostname or "").casefold()
        and left.port == right.port
        and left.username is None
        and left.password is None
    )


def bounded_get(
    session: requests.Session,
    url: str,
    *,
    referer: str = "",
) -> tuple[str, str, list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    for attempt in range(1, 3):
        headers = {"Referer": referer} if referer else None
        response = session.get(url, headers=headers, timeout=45, allow_redirects=True)
        body = response.text
        challenge = any(
            marker in body.casefold()
            for marker in ("just a moment", "verify you are human", "cf-chl-")
        )
        accepted = response.status_code == 200 and bool(body) and not challenge
        attempts.append(
            {
                "attempt": attempt,
                "status_code": response.status_code,
                "final_url": str(response.url),
                "body_bytes": len(response.content),
                "body_sha256": hashlib.sha256(response.content).hexdigest(),
                "accepted": accepted,
            }
        )
        if accepted:
            return body, str(response.url), attempts
    raise AssertionError(f"current direct fetch failed: {url}")


def identity(html: str, meta: dict[str, str]) -> dict[str, bool]:
    soup = BeautifulSoup(html, "lxml")
    metadata_text = " ".join(
        str(node.get("content") or "") for node in soup.select("meta[content]")
    )
    text = normalized(f"{soup.get_text(' ', strip=True)} {metadata_text}")
    words = set(text.split())
    name_tokens = [
        token
        for token in normalized(meta["name"]).split()
        if token not in {"the", "apartments", "apartment", "at", "on", "of"}
    ]
    address = normalized(meta["address"])
    zip_code = normalized(meta["zip"])
    return {
        "name_match": bool(name_tokens) and all(token in words for token in name_tokens),
        "address_match": bool(address) and address in text,
        "street_number_and_zip_match": bool(address and zip_code)
        and address.split()[0] in words
        and zip_code in words,
    }


def positive_rent(row: dict[str, object]) -> bool:
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
            "asking_rent",
            "rent",
        )
    )


def validate_rows(rows: list[dict[str, object]], sources: list[str]) -> None:
    assert rows and sources
    units: list[str] = []
    uids: list[str] = []
    for row in rows:
        unit = str(row.get("unit_number") or "").strip()
        source_ids = row.get("source_ids")
        assert isinstance(source_ids, dict)
        uid = str(source_ids.get("entrata_uid") or "").strip()
        fpid = str(source_ids.get("entrata_fpid") or "").strip()
        assert unit and uid and fpid
        assert unit_has_real_anchor(row)
        assert positive_rent(row)
        assert str(row.get("source_api_url") or "") in sources
        units.append(unit)
        uids.append(uid)
    assert len(units) == len(set(units)) == len(rows)
    assert len(uids) == len(set(uids)) == len(rows)


def village_cliffs(
    meta: dict[str, str], ledger_sha: str, ledger_rows: int
) -> dict[str, object]:
    session = requests.Session(impersonate="chrome120")
    parent_html, parent_url, parent_attempts = bounded_get(session, meta["website"])
    assert parent_url == meta["website"]
    parent_identity = identity(parent_html, meta)
    assert parent_identity["name_match"]
    soup = BeautifulSoup(parent_html, "lxml")
    cards = soup.select(".unit-body[data-unit-id][data-unit-number]")
    assert cards
    published_subdomains = {
        str(card.get("data-subdomain") or "").strip() for card in cards
    }
    assert published_subdomains == {
        "https://villagecliffs.prospectportal.com/dallas/the-village-cliffs"
    }
    conventional_url = next(iter(published_subdomains)).rstrip("/") + "/conventional/"
    identity_html, identity_url, identity_attempts = bounded_get(
        session, conventional_url, referer=parent_url
    )
    entrata_identity = identity(identity_html, meta)
    assert identity_url == conventional_url
    assert all(entrata_identity.values())

    rows: list[dict[str, object]] = []
    for card in cards:
        unit = str(card.get("data-unit-number") or "").strip()
        uid = str(card.get("data-unit-id") or "").strip()
        fpid = str(card.get("data-floorplan-id") or "").strip()
        rent = float(str(card.get("data-rent") or "0"))
        assert unit and uid and fpid and rent > 0
        rows.append(
            {
                "floor_plan_name": "",
                "bedrooms": str(card.get("data-bedrooms") or ""),
                "bathrooms": str(card.get("data-bathrooms") or ""),
                "sqft": re.sub(r"\D", "", str(card.get("data-area") or "").split(".")[0]),
                "unit_number": unit,
                "building": str(card.get("data-building") or ""),
                "market_rent_low": int(rent),
                "market_rent_high": int(rent),
                "availability_status": "AVAILABLE",
                "availability_date": str(card.get("data-available-on") or ""),
                "source_api_url": parent_url,
                "extraction_tier": "TIER_1_DOM_ENTRATA_NATIVE_DATA_ATTRIBUTES",
                "source_ids": {
                    "entrata_uid": uid,
                    "entrata_fpid": fpid,
                    "entrata_property_id": "488093",
                },
                "data_gaps": [],
                "data_quality_flag": "",
            }
        )
    sources = [parent_url]
    validate_rows(rows, sources)
    payload = {
        "result_type": "strict_current_exact_property_native_entrata_attributes",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "property": {"property_id": 9473, **meta},
        "provider": "entrata_prospectportal",
        "hyperbrowser_sessions_used": 0,
        "parent_page": {
            "url": parent_url,
            "attempts": parent_attempts,
            "identity": parent_identity,
        },
        "published_entrata_identity_page": {
            "url": identity_url,
            "attempts": identity_attempts,
            "identity": entrata_identity,
        },
        "property_identity_match": True,
        "strict_gates": {
            "exact_property_parent_name": True,
            "published_entrata_twin_name_address_zip": True,
            "native_unit_numbers": len(rows),
            "native_entrata_uids": len(rows),
            "native_floorplan_ids": len(rows),
            "positive_rent_rows": len(rows),
            "distinct_units": len(rows),
            "distinct_uids": len(rows),
            "sibling_or_cross_property_rows": 0,
        },
        "contamination_verdict": (
            "pass_exact_parent_published_entrata_twin_native_ids_positive_rents"
        ),
        "native_identity_rows": len(rows),
        "native_positive_rent_rows": len(rows),
        "source_urls": sources,
        "native_rows": rows,
        "shared_ledger": {
            "path": str(LEDGER),
            "sha256_before": ledger_sha,
            "rows_before": ledger_rows,
            "property_present_before": False,
        },
    }
    VILLAGE_ARTIFACT.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def embedded_snippet(
    property_id: int,
    meta: dict[str, str],
    parent_path: str,
    artifact: Path,
    ledger_sha: str,
    ledger_rows: int,
) -> dict[str, object]:
    session = requests.Session(impersonate="chrome120")
    parent_requested = urljoin(meta["website"], parent_path)
    parent_html, parent_url, parent_attempts = bounded_get(session, parent_requested)
    parent_identity = identity(parent_html, meta)
    # The canonical marketing page is property-specific by URL and name. Some
    # Squarespace parents omit the street address; the child Entrata snippet
    # supplies the independently checked address/zip identity below.
    assert parent_identity["name_match"]
    soup = BeautifulSoup(parent_html, "lxml")
    iframe_nodes = [
        node
        for node in soup.select("iframe[src]")
        if "entratasnipit." in str(node.get("src") or "").casefold()
    ]
    assert len(iframe_nodes) == 1
    raw_iframe = str(iframe_nodes[0]["src"])
    iframe_url = urljoin(parent_url, raw_iframe)
    host_domain = urlsplit(parent_url).hostname or ""
    separator = "&" if "?" in iframe_url else "?"
    iframe_url = f"{iframe_url}{separator}host_domain={quote(host_domain)}"
    index_html, index_url, index_attempts = bounded_get(
        session, iframe_url, referer=parent_url
    )
    snippet_identity = identity(index_html, meta)
    assert snippet_identity["name_match"] and (
        snippet_identity["address_match"]
        or snippet_identity["street_number_and_zip_match"]
    )
    assert same_origin(index_url, iframe_url)
    index_soup = BeautifulSoup(index_html, "lxml")
    detail_urls: list[str] = []
    for anchor in index_soup.select(
        "a[href*='/Apartments/module/property_floorplans/']"
    ):
        url = urljoin(index_url, str(anchor.get("href") or ""))
        if same_origin(url, index_url) and url not in detail_urls:
            detail_urls.append(url)
    assert detail_urls

    rows: list[dict[str, object]] = []
    detail_fetches: list[dict[str, object]] = []
    positive_sources: list[str] = []
    for detail_url in detail_urls:
        body, final_url, attempts = bounded_get(session, detail_url, referer=index_url)
        assert same_origin(final_url, index_url)
        parsed = parse_entrata_pp_unit_cards(body, final_url)
        strict: list[dict[str, object]] = []
        for row in parsed:
            source_ids = row.get("source_ids")
            if not isinstance(source_ids, dict):
                continue
            if not (
                str(row.get("unit_number") or "").strip()
                and str(source_ids.get("entrata_uid") or "").strip()
                and str(source_ids.get("entrata_fpid") or "").strip()
                and unit_has_real_anchor(row)
                and positive_rent(row)
            ):
                continue
            strict.append(row)
        if strict:
            positive_sources.append(final_url)
            rows.extend(strict)
        detail_fetches.append(
            {
                "published_url": detail_url,
                "final_url": final_url,
                "attempts": attempts,
                "strict_native_positive_rent_rows": len(strict),
            }
        )
    rows_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        source_ids = row["source_ids"]
        key = (
            str(row["unit_number"]).casefold(),
            str(source_ids["entrata_uid"]),
        )
        rows_by_key[key] = row
    rows = list(rows_by_key.values())
    positive_sources = list(dict.fromkeys(positive_sources))
    validate_rows(rows, positive_sources)
    property_query_ids = {
        match.group(1)
        for url in detail_urls
        if (match := re.search(r"property%5Bid%5D/(\d+)", url, re.I))
    }
    assert len(property_query_ids) == 1
    payload = {
        "result_type": "strict_current_parent_published_entrata_snippet_details",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "property": {"property_id": property_id, **meta},
        "provider": "entrata_prospectportal_snippet",
        "hyperbrowser_sessions_used": 0,
        "parent_page": {
            "requested_url": parent_requested,
            "final_url": parent_url,
            "attempts": parent_attempts,
            "identity": parent_identity,
            "published_iframe_src": raw_iframe,
        },
        "snippet_index": {
            "url": index_url,
            "attempts": index_attempts,
            "identity": snippet_identity,
            "entrata_property_ids": sorted(property_query_ids),
            "published_detail_urls": detail_urls,
        },
        "detail_fetches": detail_fetches,
        "property_identity_match": True,
        "strict_gates": {
            "exact_parent_url_and_name": True,
            "snippet_name_address_or_street_zip": True,
            "iframe_published_by_exact_parent": True,
            "details_published_by_exact_snippet": True,
            "detail_routes_same_snippet_origin": True,
            "native_unit_numbers": len(rows),
            "native_entrata_uids": len(rows),
            "native_floorplan_ids": len(rows),
            "positive_rent_rows": len(rows),
            "distinct_units": len(rows),
            "distinct_uids": len(rows),
            "sibling_or_cross_property_rows": 0,
        },
        "contamination_verdict": (
            "pass_exact_parent_published_snippet_details_native_ids_positive_rents"
        ),
        "native_identity_rows": len(rows),
        "native_positive_rent_rows": len(rows),
        "source_urls": positive_sources,
        "native_rows": rows,
        "shared_ledger": {
            "path": str(LEDGER),
            "sha256_before": ledger_sha,
            "rows_before": ledger_rows,
            "property_present_before": False,
        },
    }
    artifact.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = canonical_rows()
    assert set(metadata) == TARGET_IDS
    ledger_sha = file_sha(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        ledger = list(csv.DictReader(handle))
    ledger_ids = {int(row["property_id"]) for row in ledger}
    assert not (TARGET_IDS & ledger_ids)

    payloads = [
        village_cliffs(metadata[9473], ledger_sha, len(ledger)),
        embedded_snippet(
            72391,
            metadata[72391],
            "/pricing",
            LUMINA_ARTIFACT,
            ledger_sha,
            len(ledger),
        ),
        embedded_snippet(
            298586,
            metadata[298586],
            "/pricing",
            GATEWAY_ARTIFACT,
            ledger_sha,
            len(ledger),
        ),
    ]
    assert file_sha(LEDGER) == ledger_sha
    consolidated = {
        "result_type": "strict_current_embedded_entrata_direct_consolidated",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "strict_properties": len(payloads),
        "strict_property_ids": [item["property"]["property_id"] for item in payloads],
        "native_positive_rent_rows": sum(
            int(item["native_positive_rent_rows"]) for item in payloads
        ),
        "hyperbrowser_sessions_used": 0,
        "captcha_solving": False,
        "llm_used": False,
        "paid_canary": False,
        "shared_ledger_modified": False,
        "shared_ledger_sha256": ledger_sha,
        "properties": payloads,
    }
    CONSOLIDATED.write_text(json.dumps(consolidated, indent=2) + "\n")
    print(
        json.dumps(
            {
                "strict_property_ids": consolidated["strict_property_ids"],
                "native_positive_rent_rows": consolidated[
                    "native_positive_rent_rows"
                ],
                "artifact": str(CONSOLIDATED),
                "shared_ledger_modified": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
