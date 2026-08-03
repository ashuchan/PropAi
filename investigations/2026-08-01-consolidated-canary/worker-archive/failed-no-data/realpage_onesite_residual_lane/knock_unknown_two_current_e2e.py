from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.knock import find_knock_ids
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_knock_unknown_two_current_e2e.json"
TARGET_IDS = (19245, 71962)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_url(value: str) -> str:
    value = value.strip()
    return value if "://" in value else f"https://{value}"


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
        )
    )


def visible_identity(row: dict[str, str], body: str) -> dict[str, object]:
    from bs4 import BeautifulSoup

    text = normalized(BeautifulSoup(body, "lxml").get_text(" ", strip=True))
    name = normalized(row.get("name") or "")
    address_tokens = normalized(row.get("address") or "").split()
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token
        for token in address_tokens[1:]
        if token
        not in {
            "n",
            "s",
            "e",
            "w",
            "se",
            "sw",
            "ne",
            "nw",
            "st",
            "street",
            "ave",
            "avenue",
            "rd",
            "road",
            "dr",
            "drive",
        }
    ]
    tokens = set(text.split())
    return {
        "canonical_name": row.get("name") or "",
        "canonical_address": row.get("address") or "",
        "name_visible_exact_normalized": bool(name and name in text),
        "street_number_and_words_visible": bool(
            street_number
            and street_number in tokens
            and street_words
            and all(word in tokens for word in street_words)
        ),
    }


async def one(row: dict[str, str]) -> dict[str, object]:
    pid = int(row["apartmentid"])
    url = normalize_url(row.get("website") or "")
    response = await asyncio.to_thread(
        probe_get,
        url,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    body = str(response.text or "")
    fetch_result = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=int(response.status_code or 0),
        body=body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=str(response.url or url),
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=url,
        property_id=str(pid),
        priority=0,
        budget_ms=35_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    result = await scrape_jugnu(
        task,
        fetch_result,
        page=None,
        profile=None,
        csv_row=row,
    )
    strict = [
        unit
        for unit in result.get("units") or []
        if isinstance(unit, dict)
        and unit_has_real_anchor(unit)
        and positive_rent(unit)
    ]
    _public_key, init_kind, community_hash = find_knock_ids(body)
    raw = [
        item
        for item in result.get("_raw_api_responses") or []
        if isinstance(item, dict)
    ]
    community_response = next(
        (
            item
            for item in raw
            if "/v1/property/community/" in str(item.get("url") or "")
        ),
        {},
    )
    units_response = next(
        (
            item
            for item in raw
            if re.search(r"/v1/property/\d+/units$", str(item.get("url") or ""))
        ),
        {},
    )
    community_body = community_response.get("body") or {}
    community = (
        community_body.get("property")
        if isinstance(community_body, dict)
        else {}
    ) or {}
    community_data = community.get("data") or {}
    location = community_data.get("location") or {}
    community_address = (location.get("address") or {}).get("raw") or ""
    numeric_property_id = str(community.get("id") or "")
    units_body = units_response.get("body") or {}
    units_data = (
        units_body.get("units_data") if isinstance(units_body, dict) else {}
    ) or {}
    payload_units = [
        item for item in units_data.get("units") or [] if isinstance(item, dict)
    ]
    payload_property_ids = sorted(
        {
            str(item.get("propertyId") or "")
            for item in payload_units
            if item.get("propertyId") not in (None, "")
        }
    )
    output_property_ids = sorted(
        {
            str(unit.get("source_property_id") or "")
            for unit in strict
            if unit.get("source_property_id") not in (None, "")
        }
    )
    output_source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in strict
            if unit.get("source_api_url")
        }
    )
    page_identity = visible_identity(row, body)
    expected_units_url = (
        f"https://doorway-api.knockrentals.com/v1/property/"
        f"{numeric_property_id}/units"
    )
    property_name = normalized(row.get("name") or "")
    community_name = normalized(str(location.get("name") or ""))
    canonical_address = normalized(row.get("address") or "")
    native_address = normalized(str(community_address))
    community_name_match = bool(
        property_name
        and (
            property_name in community_name
            or community_name in property_name
            or property_name.replace(" apartments", "") in community_name
        )
    )
    address_ignored = {
        "n",
        "s",
        "e",
        "w",
        "ne",
        "nw",
        "se",
        "sw",
        "st",
        "street",
        "ave",
        "avenue",
        "rd",
        "road",
        "dr",
        "drive",
    }
    canonical_address_tokens = canonical_address.split()
    canonical_street_number = (
        canonical_address_tokens[0] if canonical_address_tokens else ""
    )
    canonical_street_words = [
        token
        for token in canonical_address_tokens[1:]
        if token not in address_ignored
    ]
    native_address_tokens = set(native_address.split())
    community_address_match = bool(
        canonical_street_number
        and canonical_street_number in native_address_tokens
        and canonical_street_words
        and all(token in native_address_tokens for token in canonical_street_words)
    )
    strict_gates = {
        "configured_page_http_200": int(response.status_code or 0) == 200,
        "configured_page_name_and_address_visible": bool(
            page_identity["name_visible_exact_normalized"]
            and page_identity["street_number_and_words_visible"]
        ),
        "sole_published_knock_init": bool(
            init_kind == "community" and community_hash
        ),
        "community_route_matches_published_hash": bool(
            community_hash
            and str(community_response.get("url") or "").endswith(
                f"/community/{community_hash}"
            )
        ),
        "community_payload_hash_matches": str(community_data.get("id") or "")
        == str(community_hash or ""),
        "community_payload_name_matches": community_name_match,
        "community_payload_address_matches": community_address_match,
        "units_route_matches_resolved_numeric_property_id": str(
            units_response.get("url") or ""
        )
        == expected_units_url,
        "payload_property_ids_match_resolved_property": payload_property_ids
        == [numeric_property_id],
        "output_property_ids_match_resolved_property": output_property_ids
        == [numeric_property_id],
        "output_source_urls_match_units_route": output_source_urls
        == [expected_units_url],
        "all_output_rows_native_and_positive_rent": bool(
            strict
            and len(strict) == len(result.get("units") or [])
            and all(
                str(unit.get("unit_number") or "").strip()
                and str((unit.get("source_ids") or {}).get("knock_unit_id") or "").strip()
                for unit in strict
            )
        ),
    }
    passed = bool(all(strict_gates.values()) and len(strict) == len(payload_units))
    return {
        "property_id": pid,
        "property_name": row.get("name") or "",
        "website": row.get("website") or "",
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "property_identity_match": passed,
        "contamination_verdict": (
            "pass_exact_configured_page_published_knock_init_native_community_"
            "name_address_and_units_property_id"
            if passed
            else "reject_knock_property_binding_incomplete"
        ),
        "units": len(strict),
        "configured_final_url": str(response.url or url),
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "published_community_hash": community_hash or "",
        "resolved_numeric_property_id": numeric_property_id,
        "community_payload_name": location.get("name") or "",
        "community_payload_address": community_address,
        "strict_gates": strict_gates,
        "identity_evidence": {
            "rows_with_native_identity": len(strict),
            "rows_with_native_identity_and_positive_rent": len(strict),
            "source_urls": output_source_urls,
            "source_property_ids": output_property_ids,
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": str(unit.get("unit_number") or ""),
                    "knock_unit_id": str(
                        (unit.get("source_ids") or {}).get("knock_unit_id") or ""
                    ),
                },
                "floor_plan_name": unit.get("floor_plan_name") or "",
                "positive_rent_evidence": {
                    "market_rent_low": unit.get("market_rent_low"),
                    "market_rent_high": unit.get("market_rent_high"),
                },
                "source_property_id": unit.get("source_property_id") or "",
                "source_api_url": unit.get("source_api_url") or "",
            }
            for unit in strict
        ],
        "errors": result.get("errors") or [],
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
    }
    for name, expected in expected_env.items():
        if os.environ.get(name, "").lower() != expected:
            raise RuntimeError(f"guardrail {name} must equal {expected!r}")

    metadata = {
        int(row["apartmentid"]): row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    residual = {
        int(row["property_id"]): row
        for row in read_csv(REMAINING)
        if row.get("property_id")
    }
    if any(pid not in residual for pid in TARGET_IDS):
        raise RuntimeError("target no longer belongs to the current residual cohort")
    results = await asyncio.gather(*(one(metadata[pid]) for pid in TARGET_IDS))
    if any(row["outcome"] != "UNIT_QUALIFIED" for row in results):
        raise RuntimeError("one or more strict Knock rows failed validation")
    ledger_ids = {
        int(row["property_id"])
        for row in read_csv(LEDGER)
        if row.get("property_id")
    }
    net_new = sorted(set(TARGET_IDS) - ledger_ids)
    payload = {
        "lane": "knock_unknown_current_source_e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "production_source_modified_by_lane": False,
            "shared_ledger_modified": False,
        },
        "cohort": {
            "remaining_csv": str(REMAINING),
            "remaining_csv_sha256": sha256(REMAINING),
            "ledger_csv": str(LEDGER),
            "ledger_csv_sha256": sha256(LEDGER),
            "ledger_rows": len(ledger_ids),
        },
        "summary": {
            "current_source_full_pipeline_qualified_ids": list(TARGET_IDS),
            "current_source_net_new_ids_vs_current_ledger": net_new,
            "strict_property_count": len(results),
            "strict_native_priced_rows": sum(int(row["units"]) for row in results),
            "authoritative_ledger_delta": 0,
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "net_new": net_new,
                "units": {str(row["property_id"]): row["units"] for row in results},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
