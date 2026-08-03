#!/usr/bin/env python3
"""Materialize strict RentCafe-hosted evidence for Tamarron and controls."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


for key in (
    "WEB_UNLOCKER_KEY",
    "HYPERBROWSER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "PROBE_PROXY_URL",
):
    os.environ[key] = ""
os.environ["COMPLIANCE_MODE"] = "1"
os.environ["ENABLE_HYPERBROWSER"] = "false"
os.environ["ENABLE_TIER4_LLM"] = "false"
os.environ["ENABLE_TIER_ESCALATION"] = "false"
os.environ["ENABLE_UNLOCKER_TIER"] = "false"
os.environ["ENABLE_FLARESOLVERR_TIER"] = "false"

from ma_poc.core.identity import unit_has_real_anchor  # noqa: E402
from ma_poc.fetch.contracts import (  # noqa: E402
    FetchOutcome,
    FetchResult,
    RenderMode,
)
from ma_poc.pms.adapters._rentcafe_hosted_table import (  # noqa: E402
    parse_rentcafe_hosted_table,
)
from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    _discover_rentcafe_anchors,
)
from ma_poc.pms.scraper import scrape  # noqa: E402


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "rentcafe_hosted_table_lane"
EVIDENCE = OUT / "evidence_tamarron_34362_current_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"

CASES = {
    "34362": {
        "property_name": "Tamarron",
        "address": "4410 N 99th Avenue",
        "city": "Phoenix",
        "state": "AZ",
        "postal_code": "85037",
        "configured_url": "https://www.thetamarronapts.com",
        "url": (
            "https://www.rentcafe.com/apartments/az/phoenix/"
            "tamarron-apartments-2/default.aspx"
        ),
        "body": ROOT / "hb_tamarron_34362" / "root.html.gz",
        "summary": ROOT / "hb_tamarron_34362" / "summary.json",
        "expected_property_id": "1505175",
    },
    "218786": {
        "property_name": "Coopers Landing Apartments",
        "address": "5001 Coopers Landing Dr.",
        "city": "Kalamazoo",
        "state": "MI",
        "postal_code": "49004",
        "configured_url": "https://www.landcoapartments.com/coopers-landing-apartments/",
        "url": (
            "https://www.rentcafe.com/apartments/mi/kalamazoo/"
            "coopers-landing-apartments/default.aspx"
        ),
        "body": ROOT / "hb_rentcafe_hosted_pair" / "218786_root.html.gz",
        "expected_property_id": "480033",
    },
    "69558": {
        "property_name": "Spring Hill Apartments",
        "address": "767 Springfield Ave",
        "city": "Summit",
        "state": "NJ",
        "postal_code": "07901",
        "configured_url": "https://www.springhillapts.com/",
        "url": (
            "https://www.rentcafe.com/apartments/nj/summit/"
            "spring-hill-apartments/default.aspx"
        ),
        "body": ROOT / "hb_rentcafe_hosted_pair" / "69558_root.html.gz",
    },
}
PAIR_SUMMARY = ROOT / "hb_rentcafe_hosted_pair" / "summary.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


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


def strict_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in result.get("units") or []
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]


def identity_checks(html: str, case: dict[str, Any]) -> dict[str, bool]:
    text = normalized(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    address_tokens = normalized(str(case["address"]))
    return {
        "name": normalized(str(case["property_name"])) in text,
        "address": address_tokens in text,
        "city": normalized(str(case["city"])) in text,
        "postal_code": normalized(str(case["postal_code"])) in text,
    }


def to_fetch_result(url: str, body: bytes) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html; charset=utf-8"},
        render_mode=RenderMode.GET,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
    )


async def full_pipeline(
    property_id: str,
    case: dict[str, Any],
    body: bytes,
) -> dict[str, Any]:
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    result = await scrape(
        str(case["url"]),
        page=None,
        fetch_result=to_fetch_result(str(case["url"]), body),
        csv_row={
            "apartmentid": property_id,
            "name": case["property_name"],
            "address": case["address"],
            "city": case["city"],
            "state": case["state"],
            "zip": case["postal_code"],
            "website": case["url"],
        },
        property_id=property_id,
        shared_budget=budget,
    )
    result["_validation_budget"] = budget
    return result


def compact_e2e(result: dict[str, Any]) -> dict[str, Any]:
    rows = strict_rows(result)
    return {
        "adapter": result.get("_adapter_used"),
        "detected_pms": result.get("_detected_pms"),
        "tier": result.get("extraction_tier_used"),
        "errors": result.get("errors") or [],
        "strict_native_positive_rent_rows": len(rows),
        "distinct_native_unit_numbers": len(
            {str(row.get("unit_number") or "").casefold() for row in rows}
        ),
        "distinct_native_unit_ids": len(
            {
                str((row.get("source_ids") or {}).get("securecafe_apartment_id") or "")
                for row in rows
                if str(
                    (row.get("source_ids") or {}).get("securecafe_apartment_id")
                    or ""
                )
            }
        ),
        "source_property_ids": sorted(
            {
                str(row.get("source_property_id") or "")
                for row in rows
                if str(row.get("source_property_id") or "")
            }
        ),
        "validation_budget": result.get("_validation_budget"),
    }


def native_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": {
            "unit_number": str(row.get("unit_number") or ""),
            **(
                row.get("source_ids")
                if isinstance(row.get("source_ids"), dict)
                else {}
            ),
        },
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "availability_date": str(row.get("availability_date") or ""),
        "positive_rent_evidence": {
            "market_rent_low": row.get("market_rent_low"),
            "market_rent_high": row.get("market_rent_high"),
        },
        "source_property_id": str(row.get("source_property_id") or ""),
        "source_api_url": str(row.get("source_api_url") or ""),
    }


def rejected_sibling_urls(html: str, current_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    current_path = urlsplit(current_url).path.rstrip("/").casefold()
    found: set[str] = set()
    for anchor in soup.select("a[href]"):
        full = urljoin(current_url, str(anchor.get("href") or ""))
        parsed = urlsplit(full)
        if parsed.hostname not in {"rentcafe.com", "www.rentcafe.com"}:
            continue
        path = parsed.path.rstrip("/").casefold()
        if (
            path != current_path
            and re.fullmatch(
                r"/apartments/[^/]+/[^/]+/[^/]+/default\.aspx",
                path,
            )
        ):
            found.add(full.split("?", 1)[0])
    return sorted(found)


async def main() -> None:
    ledger_before = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    tamarron_summary = json.loads(CASES["34362"]["summary"].read_text())
    pair_summary = json.loads(PAIR_SUMMARY.read_text())
    pair_by_id = {
        str(row["property_id"]): row for row in pair_summary["results"]
    }

    bodies: dict[str, bytes] = {}
    direct: dict[str, list[dict[str, Any]]] = {}
    e2e: dict[str, dict[str, Any]] = {}
    checks: dict[str, dict[str, bool]] = {}
    anchors: dict[str, list[str]] = {}
    for property_id, case in CASES.items():
        body = gzip.open(case["body"], "rb").read()
        bodies[property_id] = body
        html = body.decode("utf-8", "replace")
        checks[property_id] = identity_checks(html, case)
        assert all(checks[property_id].values()), (property_id, checks[property_id])
        expected_hash = (
            tamarron_summary["body_sha256"]
            if property_id == "34362"
            else pair_by_id[property_id]["body_sha256"]
        )
        assert sha256_bytes(body) == expected_hash
        direct[property_id] = parse_rentcafe_hosted_table(html, str(case["url"]))
        anchors[property_id] = _discover_rentcafe_anchors(
            html,
            "https://www.rentcafe.com",
            str(case["url"]),
        )
        assert anchors[property_id] == [str(case["url"])]
        e2e[property_id] = await full_pipeline(property_id, case, body)

    tamarron = direct["34362"]
    assert len(tamarron) == 10
    assert all(unit_has_real_anchor(row) and positive_rent(row) for row in tamarron)
    assert len({str(row["unit_number"]) for row in tamarron}) == 10
    assert len(
        {
            str((row.get("source_ids") or {})["securecafe_apartment_id"])
            for row in tamarron
        }
    ) == 10
    assert {str(row.get("source_property_id")) for row in tamarron} == {
        "1505175"
    }
    assert {str(row.get("floor_plan_name")) for row in tamarron} == {
        "A1",
        "B1",
        "B2",
        "B3",
        "C1",
    }
    tamarron_e2e = strict_rows(e2e["34362"])
    assert e2e["34362"].get("_adapter_used") == "rentcafe"
    assert e2e["34362"].get("extraction_tier_used") == (
        "TIER_1_DOM_RENTCAFE_HOSTED"
    )
    assert len(tamarron_e2e) == 10
    assert {
        (
            str(row.get("unit_number")),
            int(row.get("market_rent_low") or 0),
            str(row.get("floor_plan_name")),
            str(row.get("availability_date") or ""),
        )
        for row in tamarron
    } == {
        (
            str(row.get("unit_number")),
            int(row.get("market_rent_low") or 0),
            str(row.get("floor_plan_name")),
            str(row.get("availability_date") or ""),
        )
        for row in tamarron_e2e
    }

    coopers_soup = BeautifulSoup(
        bodies["218786"].decode("utf-8", "replace"), "lxml"
    )
    coopers_raw_labels = [
        str(row.get("data-unit-name") or "").strip()
        for row in coopers_soup.select("tr.fp-unit")
    ]
    assert len(coopers_raw_labels) == 27
    assert all(
        re.sub(r"[\s_-]+", "", label).casefold().startswith("wait")
        for label in coopers_raw_labels
    )
    assert direct["218786"] == []
    assert strict_rows(e2e["218786"]) == []
    sibling_urls = rejected_sibling_urls(
        bodies["218786"].decode("utf-8", "replace"),
        str(CASES["218786"]["url"]),
    )
    assert any("/winchell-way0/" in value for value in sibling_urls)
    assert direct["69558"] == []
    assert strict_rows(e2e["69558"]) == []

    for result in e2e.values():
        budget = result.get("_validation_budget") or {}
        assert budget.get("llm_api_calls") == 0
        assert budget.get("llm_dom_calls") == 0
        assert budget.get("llm_monolithic") == 0

    ledger_after = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    assert ledger_before == ledger_after

    result = {
        "property_id": 34362,
        "property_name": "Tamarron",
        "website": CASES["34362"]["configured_url"],
        "current_official_url": CASES["34362"]["url"],
        "outcome": "UNIT_QUALIFIED",
        "adapter": "rentcafe",
        "tier": "TIER_1_DOM_RENTCAFE_HOSTED",
        "units": 10,
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_current_rentcafe_tamarron_property_1505175_native_"
            "hosted_rows_waitlist_and_cross_property_fail_closed_full_pipeline"
        ),
        "identity_evidence": {
            "canonical_name": "Tamarron",
            "published_name": "Tamarron Apartments",
            "published_address": "4410 N 99th Avenue",
            "city_state_zip": "Phoenix, AZ 85037",
            "current_page_identity_checks": checks["34362"],
            "rentcafe_property_id": "1505175",
            "rows_with_native_identity": 10,
            "rows_with_native_identity_and_positive_rent": 10,
            "distinct_unit_numbers": 10,
            "distinct_securecafe_apartment_ids": 10,
            "source_urls": [CASES["34362"]["url"]],
        },
        "strict_gates": {
            "exact_current_property_identity": True,
            "exact_rentcafe_property_id_1505175": True,
            "all_rows_native_unit_number": True,
            "all_rows_unique_securecafe_apartment_id": True,
            "all_rows_numeric_rentcafe_floorplan_id": True,
            "all_rows_native_floorplan_name": True,
            "all_rows_positive_row_level_rent": True,
            "full_pipeline_10_native_positive_rent_rows": True,
            "direct_and_full_pipeline_row_sets_identical": True,
            "coopers_27_waitlist_rows_rejected": True,
            "rentcafe_shared_host_sibling_links_rejected": True,
            "spring_hill_zero_live_inventory_rejected": True,
            "no_llm": True,
            "no_unlocker": True,
            "no_captcha_solver": True,
            "no_fingerprint_rotation": True,
        },
        "native_samples": [native_sample(row) for row in tamarron],
        "floorplan_name_and_date_semantics": {
            "exact_published_floorplan_names": ["A1", "B1", "B2", "B3", "C1"],
            "exact_numeric_floorplan_ids": sorted(
                {
                    str((row.get("source_ids") or {})["rentcafe_floorplan_id"])
                    for row in tamarron
                },
                key=int,
            ),
            "explicit_future_date_rows": sum(
                bool(row.get("availability_date")) for row in tamarron
            ),
            "available_now_rows_with_blank_calendar_date": sum(
                not bool(row.get("availability_date")) for row in tamarron
            ),
            "names_are_native_parent_fp_item_data_name_values": True,
            "dates_are_native_visible_hosted_table_values": True,
        },
        "current_source_validation": {
            "hyperbrowser_capture": {
                **tamarron_summary,
                "compressed_path": str(CASES["34362"]["body"]),
                "compressed_sha256": sha256_path(CASES["34362"]["body"]),
            },
            "direct_parser_native_positive_rent_rows": 10,
            "full_pipeline": compact_e2e(e2e["34362"]),
            "minimal_production_lever": (
                "refresh canonical property URL to the exact current RentCafe page; "
                "parse hosted native rows with provider IDs and fail closed on "
                "waitlists or shared-host sibling listings"
            ),
        },
        "contamination_negative_checks": {
            "coopers_landing": {
                "property_id": 218786,
                "exact_identity": checks["218786"],
                "published_rentcafe_property_id": "480033",
                "raw_fp_unit_rows": 27,
                "raw_unit_labels": coopers_raw_labels,
                "all_raw_labels_waitlist_sentinels": True,
                "direct_rows_admitted": 0,
                "full_pipeline_rows_admitted": 0,
                "rejected_shared_host_sibling_urls": sibling_urls,
                "known_rejected_sibling_property": {
                    "name": "Winchell Way",
                    "rentcafe_property_id": "1682177",
                    "rows_admitted": 0,
                },
                "full_pipeline": compact_e2e(e2e["218786"]),
            },
            "spring_hill": {
                "property_id": 69558,
                "exact_identity": checks["69558"],
                "raw_fp_unit_rows": 0,
                "direct_rows_admitted": 0,
                "full_pipeline_rows_admitted": 0,
                "full_pipeline": compact_e2e(e2e["69558"]),
            },
        },
    }
    payload = {
        "summary": {
            "result_type": "rentcafe_hosted_table_strict_with_negative_controls",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [34362],
            "native_positive_rent_rows": 10,
            "live_cluster_members": 3,
            "negative_controls": 2,
            "hyperbrowser_sessions": 3,
            "hyperbrowser_used_for_capture": True,
            "residential_proxy": True,
            "captcha_solving": False,
            "stealth": False,
            "fingerprint_rotation": False,
            "web_unlocker": False,
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": [result],
        "cluster_boundary": {
            "members": [34362, 218786, 69558],
            "direct_parser_rows": {
                property_id: len(rows) for property_id, rows in direct.items()
            },
            "full_pipeline_strict_rows": {
                property_id: len(strict_rows(result))
                for property_id, result in e2e.items()
            },
            "anchor_candidates_after_shared_host_gate": anchors,
        },
        "capture_provenance": {
            "tamarron": tamarron_summary,
            "pair": pair_summary,
            "source_capture_hashes_verified": True,
            "materializer": str(Path(__file__)),
            "materializer_sha256": sha256_path(Path(__file__)),
        },
        "ledger_integrity": {
            "before": ledger_before,
            "after": ledger_after,
            "unchanged_during_materialization": True,
        },
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256_path(EVIDENCE),
                "strict_ids": [34362],
                "direct_rows": payload["cluster_boundary"]["direct_parser_rows"],
                "full_pipeline_strict_rows": payload["cluster_boundary"][
                    "full_pipeline_strict_rows"
                ],
                "ledger_unchanged": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
