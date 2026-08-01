#!/usr/bin/env python3
"""Independently validate and materialize strict Lakewood Entrata evidence.

This is an audit-only helper.  It reads the archived failed-run metadata and
the prior bounded Hyperbrowser sweep, then replays only Lakewood's exact public
Entrata index/detail URLs with a bounded direct HTTP session.  It never writes
the shared recovery ledger.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from curl_cffi import requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import (
    find_entrata_pp_plan_links,
    parse_entrata_modern_units_data,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SWEEP = ROOT / "hb_entrata_sweep_root_remaining14.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = "1375"
INDEX_URL = (
    "https://www.lakewood-apartments.net/tomball/"
    "lakewood-apartments/conventional/"
)
ARTIFACT = OUT / "evidence_entrata_1375_lakewood_current_strict.json"
LEDGER_ROWS = OUT / "strict_entrata_lakewood_net_new_ledger_rows.csv"
SUMMARY = OUT / "strict_entrata_lakewood_net_new_summary.json"
LEDGER_FIELDS = [
    "property_id",
    "property_name",
    "website",
    "evidence_lane",
    "artifact",
    "units",
    "property_identity_match",
    "contamination_verdict",
    "native_identity_rows",
    "native_positive_rent_rows",
    "source_urls",
    "sample_native_unit_ids",
    "local_validation",
]
RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_row() -> dict[str, str]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("apartmentid") or "").strip() == PROPERTY_ID:
                return row
    raise AssertionError(f"canonical property {PROPERTY_ID} missing")


def prior_sweep_row() -> dict[str, Any]:
    payload = json.loads(SWEEP.read_text(encoding="utf-8"))
    for row in payload.get("results", []):
        if str(row.get("property_id")) == PROPERTY_ID:
            return row
    raise AssertionError(f"sweep property {PROPERTY_ID} missing")


def ledger_state() -> tuple[str, list[dict[str, str]], set[str]]:
    digest = file_sha256(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = {str(row.get("property_id") or "").strip() for row in rows}
    return digest, rows, ids


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def exact_lakewood_url(url: str, *, index: bool = False) -> bool:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "www.lakewood-apartments.net"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    if index:
        return parsed.path == "/tomball/lakewood-apartments/conventional/"
    return bool(
        re.fullmatch(
            r"/floorplans/tomball-TX/lakewood-apartments/"
            r"[a-z0-9-]+-\d+-\d+/",
            parsed.path,
            re.IGNORECASE,
        )
    )


def body_looks_real(html: str, marker: str) -> bool:
    low = html.casefold()
    challenge = any(
        token in low
        for token in ("just a moment", "verify you are human", "cf-chl-")
    )
    return bool(html) and marker.casefold() in low and not challenge


def bounded_get(
    session: requests.Session,
    url: str,
    *,
    marker: str,
    referer: str = "",
) -> tuple[str, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    accepted_html = ""
    accepted_url = ""
    for attempt in range(1, 3):
        headers = {"Referer": referer} if referer else None
        response = session.get(
            url,
            headers=headers,
            timeout=45,
            allow_redirects=True,
        )
        html = response.text
        accepted = response.status_code == 200 and body_looks_real(html, marker)
        attempts.append(
            {
                "attempt": attempt,
                "status_code": response.status_code,
                "final_url": str(response.url),
                "body_bytes": len(response.content),
                "body_sha256": sha256_bytes(response.content),
                "accepted": accepted,
            }
        )
        if accepted:
            accepted_html = html
            accepted_url = str(response.url)
            break
    if not accepted_html:
        raise AssertionError(f"exact direct fetch did not yield inventory: {url}")
    return accepted_html, {
        "requested_url": url,
        "accepted_url": accepted_url,
        "attempts": attempts,
    }


def positive_rent(row: dict[str, Any]) -> bool:
    for key in RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and float(value) > 0:
            return True
    return False


def native_identity(row: dict[str, Any]) -> bool:
    source_ids = row.get("source_ids")
    return bool(
        unit_has_real_anchor(row)
        and str(row.get("unit_number") or "").strip()
        and isinstance(source_ids, dict)
        and str(source_ids.get("entrata_uid") or "").strip()
        and str(source_ids.get("entrata_fpid") or "").strip()
    )


def json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    canonical = canonical_row()
    sweep = prior_sweep_row()
    ledger_hash_before, ledger_rows, ledger_ids = ledger_state()
    assert PROPERTY_ID not in ledger_ids, "Lakewood is no longer net-new"

    session = requests.Session(impersonate="chrome120")
    index_html, index_fetch = bounded_get(
        session,
        INDEX_URL,
        marker="fp-card",
    )
    assert exact_lakewood_url(index_fetch["accepted_url"], index=True)

    page_text = normalized_text(index_html)
    canonical_name = str(canonical.get("name") or "Lakewood")
    canonical_address = str(canonical.get("address") or "11000 Gatesden Dr")
    assert "lakewood apartments" in page_text
    assert normalized_text(canonical_address) in page_text
    assert "tomball tx 77377" in page_text

    plan_rows = parse_entrata_prospectportal_html(index_html, INDEX_URL)
    plan_links = find_entrata_pp_plan_links(index_html, INDEX_URL)
    assert len(plan_links) == 9
    assert all(exact_lakewood_url(url) for url in plan_links)
    assert len(plan_rows) == 9

    detail_fetches: list[dict[str, Any]] = []
    parsed_rows: list[dict[str, Any]] = []
    parser_counts: dict[str, dict[str, int]] = {}
    for url in plan_links:
        detail_html, fetch = bounded_get(
            session,
            url,
            marker="unit-details",
            referer=INDEX_URL,
        )
        assert exact_lakewood_url(fetch["accepted_url"])
        detail_fetches.append(fetch)
        counts: dict[str, int] = {}
        for parser in (
            parse_entrata_pp_unit_cards,
            parse_entrata_pp_jd_fp_cards,
            parse_entrata_modern_units_data,
        ):
            rows = parser(detail_html, url)
            counts[parser.__name__] = len(rows)
            parsed_rows.extend(rows)
        parser_counts[url] = counts

    strict_rows = [
        row for row in parsed_rows if native_identity(row) and positive_rent(row)
    ]
    unit_numbers = [str(row["unit_number"]).strip() for row in strict_rows]
    entrata_uids = [str(row["source_ids"]["entrata_uid"]) for row in strict_rows]
    source_urls = sorted({str(row.get("source_api_url") or "") for row in strict_rows})
    assert len(strict_rows) == 19
    assert len(unit_numbers) == len(set(unit_numbers)) == 19
    assert len(entrata_uids) == len(set(entrata_uids)) == 19
    assert all(exact_lakewood_url(url) for url in source_urls)
    assert set(source_urls).issubset(set(plan_links))
    assert all(native_identity(row) for row in strict_rows)
    assert all(positive_rent(row) for row in strict_rows)

    # The row's Entrata floor-plan id must agree with the exact detail slug.
    for row in strict_rows:
        match = re.search(r"-(\d+)-\d+/$", str(row["source_api_url"]))
        assert match is not None
        assert str(row["source_ids"]["entrata_fpid"]) == match.group(1)

    assert sweep.get("outcome") == "UNIT_QUALIFIED"
    assert int(sweep.get("units") or 0) == 19
    assert sweep.get("property_identity_match") is True
    assert sweep.get("contamination_verdict") == (
        "pass_strict_property_boundary_and_native_positive_rent"
    )
    by_unit = {str(row["unit_number"]): row for row in strict_rows}
    for sample in sweep.get("native_samples", []):
        unit_number = str(sample.get("identity", {}).get("unit_number") or "")
        current = by_unit[unit_number]
        assert current["source_ids"] == sample.get("source_ids")
        assert current.get("market_rent_low") == sample.get(
            "positive_rent_evidence", {}
        ).get("market_rent_low")
        assert current.get("availability_date") == sample.get("availability_date")

    gates = {
        "canonical_property_id": PROPERTY_ID,
        "canonical_name": canonical_name,
        "canonical_address": canonical_address,
        "canonical_city_state_zip": "Tomball, TX 77377",
        "index_exact_property_url": True,
        "index_name_match": True,
        "index_address_match": True,
        "all_detail_urls_same_origin_and_property_slug": True,
        "all_row_sources_from_published_exact_plan_links": True,
        "rows_with_native_identity": len(strict_rows),
        "rows_with_positive_rent": len(strict_rows),
        "distinct_unit_numbers": len(set(unit_numbers)),
        "distinct_entrata_uids": len(set(entrata_uids)),
        "floorplan_id_slug_joins": len(strict_rows),
        "sibling_or_cross_property_rows": 0,
        "saved_hb_sweep_count_and_samples_confirmed": True,
    }
    evidence = {
        "result_type": "strict_current_direct_revalidation",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "property": {
            "property_id": int(PROPERTY_ID),
            "property_name": canonical_name,
            "website": canonical.get("website") or "http://www.lakewood-apartments.net/",
            "address": canonical_address,
            "city": canonical.get("city") or "Tomball",
            "state": canonical.get("state") or "TX",
            "zip": canonical.get("zip") or "77377",
        },
        "provider": "entrata_prospectportal",
        "prior_bounded_hyperbrowser_evidence": {
            "artifact": str(SWEEP),
            "reported_units": sweep.get("units"),
            "reported_session_calls": sweep.get("session_calls"),
            "reported_winning_url": sweep.get("winning_url"),
            "sample_rows": sweep.get("native_samples", []),
        },
        "current_direct_validation": {
            "hyperbrowser_sessions_used": 0,
            "index_fetch": index_fetch,
            "published_plan_links": plan_links,
            "index_plan_rows": json_safe(plan_rows),
            "detail_fetches": detail_fetches,
            "parser_counts": parser_counts,
        },
        "strict_gates": gates,
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_property_native_ids_positive_rents_no_sibling_contamination"
        ),
        "native_identity_rows": len(strict_rows),
        "native_positive_rent_rows": len(strict_rows),
        "source_urls": source_urls,
        "native_rows": json_safe(strict_rows),
        "shared_ledger": {
            "path": str(LEDGER),
            "sha256_before": ledger_hash_before,
            "row_count_before": len(ledger_rows),
            "unique_property_ids_before": len(ledger_ids),
            "property_1375_present_before": False,
        },
    }
    ARTIFACT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    ledger_row = {
        "property_id": PROPERTY_ID,
        "property_name": canonical_name,
        "website": canonical.get("website") or "http://www.lakewood-apartments.net/",
        "evidence_lane": "entrata_residual_exact_direct_revalidation",
        "artifact": str(ARTIFACT),
        "units": str(len(strict_rows)),
        "property_identity_match": "True",
        "contamination_verdict": (
            "pass_exact_property_native_ids_positive_rents_no_sibling_contamination"
        ),
        "native_identity_rows": str(len(strict_rows)),
        "native_positive_rent_rows": str(len(strict_rows)),
        "source_urls": " | ".join(source_urls),
        "sample_native_unit_ids": " | ".join(
            f"{row['unit_number']}:{row['source_ids']['entrata_uid']}"
            for row in strict_rows[:5]
        ),
        "local_validation": (
            "saved_hb_revalidated_by_current_exact_direct_session_no_paid_canary"
        ),
    }
    with LEDGER_ROWS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerow(ledger_row)

    ledger_hash_after, ledger_rows_after, ledger_ids_after = ledger_state()
    assert ledger_hash_after == ledger_hash_before
    assert len(ledger_rows_after) == len(ledger_rows)
    assert ledger_ids_after == ledger_ids
    summary = {
        "result_type": "net_new_strict_entrata_residual",
        "net_new_properties": 1,
        "net_new_property_ids": [int(PROPERTY_ID)],
        "net_new_native_positive_rent_rows": len(strict_rows),
        "overlap_with_latest_ledger": 0,
        "latest_ledger_path": str(LEDGER),
        "latest_ledger_sha256": ledger_hash_after,
        "latest_ledger_rows": len(ledger_rows_after),
        "latest_ledger_unique_property_ids": len(ledger_ids_after),
        "shared_ledger_modified": False,
        "hyperbrowser_sessions_used_for_revalidation": 0,
        "evidence_artifact": str(ARTIFACT),
        "net_new_ledger_rows_artifact": str(LEDGER_ROWS),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
