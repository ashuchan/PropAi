#!/usr/bin/env python3
"""Validate Canyon Springs from its current canonical Entrata routes."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import (
    _extract_vus_urls,
    _find_pp_conventional_index,
    parse_prospectportal_unit_spaces,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = 40398
WEBSITE = "https://www.canyonspringsfresno.com/"
EXPECTED_INDEX = (
    "https://www.canyonspringsfresno.com/"
    "fresno-ca-apartments/canyon-springs/conventional/"
)
ARTIFACT = OUT / "evidence_entrata_40398_canyon_springs_current_strict.json"


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical() -> dict[str, str]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("apartmentid") == str(PROPERTY_ID):
                return row
    raise AssertionError("Canyon Springs canonical row missing")


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def same_origin(left_url: str, right_url: str) -> bool:
    left = urlsplit(left_url)
    right = urlsplit(right_url)
    return bool(
        left.scheme == right.scheme == "https"
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
    xhr: bool = False,
) -> tuple[str, str, list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    for attempt in range(1, 3):
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "text/html, */*; q=0.01"
        response = session.get(
            url,
            headers=headers or None,
            timeout=45,
            allow_redirects=True,
        )
        body = response.text
        is_challenge = any(
            token in body.casefold()
            for token in ("just a moment", "verify you are human", "cf-chl-")
        )
        accepted = response.status_code == 200 and bool(body) and not is_challenge
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


def native_id_map(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for anchor in soup.select("a.unit-button"):
        uid_value = anchor.get("data-unit") or anchor.get("rel") or ""
        if isinstance(uid_value, list):
            uid_value = uid_value[0] if uid_value else ""
        uid = str(uid_value).strip()
        parent = anchor.find_parent(class_="unit-row-wrapper") or anchor.find_parent(
            class_="unit-row"
        )
        node = parent.select_one(".unit-col.unit .unit-col-text") if parent else None
        unit = node.get_text(strip=True) if node else ""
        if unit and uid:
            out[unit] = uid
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = canonical()
    ledger_sha_before = file_sha(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger_ids = {row.get("property_id") for row in ledger_rows}
    assert str(PROPERTY_ID) not in ledger_ids

    session = requests.Session(impersonate="chrome120")
    home_html, home_url, home_attempts = bounded_get(session, WEBSITE)
    assert home_url == WEBSITE
    discovered_indexes = _find_pp_conventional_index(home_html, home_url)
    assert discovered_indexes == [EXPECTED_INDEX]

    index_html, index_url, index_attempts = bounded_get(session, EXPECTED_INDEX)
    assert index_url == EXPECTED_INDEX
    assert same_origin(index_url, WEBSITE)
    text = normalized(index_html)
    assert "canyon springs" in text
    assert normalized(meta["address"]) in text
    assert normalized(meta["zip"]) in set(text.split())

    published_vus = [
        url
        for _, url in _extract_vus_urls([(index_url, index_html)], index_url)
        if same_origin(url, index_url)
    ]
    assert len(published_vus) == 1
    detail_url = published_vus[0]
    query = parse_qs(urlsplit(detail_url).query)
    assert query.get("property[id]") == ["673205"]
    assert query.get("property_floorplan[id]") == ["777076"]

    detail_html, detail_final_url, detail_attempts = bounded_get(
        session,
        detail_url,
        referer=index_url,
        xhr=True,
    )
    assert detail_final_url == detail_url
    parsed = parse_prospectportal_unit_spaces(detail_html, detail_url)
    native_ids = native_id_map(detail_html)
    assert len(parsed) == 1
    assert set(native_ids) == {str(row["unit_number"]) for row in parsed}

    rows: list[dict[str, object]] = []
    for row in parsed:
        unit = str(row.get("unit_number") or "")
        assert unit_has_real_anchor(row)
        rents = [row.get("market_rent_low"), row.get("market_rent_high")]
        assert any(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0
            for value in rents
        )
        copy = dict(row)
        copy["source_ids"] = {
            "entrata_uid": native_ids[unit],
            "entrata_fpid": "777076",
            "entrata_property_id": "673205",
        }
        rows.append(copy)
    assert [row["unit_number"] for row in rows] == ["226"]
    assert [row["source_ids"]["entrata_uid"] for row in rows] == ["4476796"]
    assert [row["market_rent_low"] for row in rows] == [1750]
    assert [row["availability_date"] for row in rows] == ["2026-08-01"]

    evidence = {
        "result_type": "strict_current_canonical_route_direct_vus",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "property": {
            "property_id": PROPERTY_ID,
            "property_name": meta["name"],
            "website": meta["website"],
            "address": meta["address"],
            "city": meta["city"],
            "state": meta["state"],
            "zip": meta["zip"],
        },
        "provider": "entrata_prospectportal",
        "hyperbrowser_sessions_used": 0,
        "homepage": {
            "requested_url": WEBSITE,
            "final_url": home_url,
            "attempts": home_attempts,
            "published_conventional_urls": discovered_indexes,
        },
        "index": {
            "requested_url": EXPECTED_INDEX,
            "final_url": index_url,
            "attempts": index_attempts,
            "published_vus_urls": published_vus,
        },
        "detail": {
            "url": detail_url,
            "attempts": detail_attempts,
            "entrata_property_id": "673205",
            "entrata_floorplan_id": "777076",
        },
        "property_identity_match": True,
        "strict_gates": {
            "canonical_name_match": True,
            "canonical_address_match": True,
            "canonical_zip_match": True,
            "index_url_published_by_canonical_homepage": True,
            "index_url_same_origin": True,
            "detail_url_published_by_exact_current_grid": True,
            "detail_url_same_origin": True,
            "rows_with_native_unit_number": 1,
            "rows_with_native_entrata_uid": 1,
            "rows_with_positive_rent": 1,
            "distinct_unit_numbers": 1,
            "distinct_entrata_uids": 1,
            "sibling_or_cross_property_rows": 0,
        },
        "contamination_verdict": (
            "pass_exact_property_published_vus_native_ids_positive_rents"
        ),
        "native_identity_rows": 1,
        "native_positive_rent_rows": 1,
        "source_urls": [detail_url],
        "native_rows": rows,
        "shared_ledger": {
            "path": str(LEDGER),
            "sha256_before": ledger_sha_before,
            "rows_before": len(ledger_rows),
            "property_present_before": False,
        },
    }
    ARTIFACT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    assert file_sha(LEDGER) == ledger_sha_before
    print(
        json.dumps(
            {
                "property_id": PROPERTY_ID,
                "native_positive_rent_rows": 1,
                "artifact": str(ARTIFACT),
                "shared_ledger_modified": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
