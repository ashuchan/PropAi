from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_rentcafe_rentmanager_three_current_e2e.json"

TARGETS = {
    "60303": {
        "adapter": "rentcafe",
        "tier": "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT",
        "property_id": "1806816",
        "published_host": "ironworksindy.securecafeapplicant.com",
        "published_slug": "ironworks-at-keystone0",
        "verdict": "pass_exact_configured_published_applicant_property_native_units",
    },
    "234385": {
        "adapter": "rentcafe",
        "tier": "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT",
        "property_id": "59503",
        "published_host": "townhomesatspringvalley-mmgapts.securecafe.com",
        "published_slug": "townhomes-at-spring-valley",
        "verdict": "pass_exact_configured_published_applicant_property_native_units",
    },
    "263127": {
        "adapter": "rentmanager",
        "tier": "TIER_1_API_RENTMANAGER_ILOVELEASING_PUBLIC",
        "property_id": "",
        "published_host": "www.iloveleasing.com",
        "published_slug": "",
        "verdict": "pass_exact_configured_published_iloveleasing_property_native_units",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def name_key(value: object) -> str:
    ignored = {"apartment", "apartments", "community", "the", "at"}
    return "".join(token for token in normalize(value).split() if token not in ignored)


def street_key(value: object) -> tuple[str, set[str]]:
    ignored = {
        "n", "s", "e", "w", "ne", "nw", "se", "sw",
        "north", "south", "east", "west",
        "st", "street", "rd", "road", "ave", "avenue",
        "dr", "drive", "blvd", "boulevard", "ln", "lane",
    }
    tokens = normalize(value).split()
    if not tokens:
        return "", set()
    number = tokens[0]
    # A one-letter building/street-number suffix is commonly joined to the
    # number by Yardi (canonical ``2149 B`` -> source ``2149B``). The number
    # match above covers it; retain the actual street-name tokens here.
    core = {
        token for token in tokens[1:] if token not in ignored and len(token) > 1
    }
    return number, core


def street_matches(canonical: object, source: object) -> bool:
    canonical_number, canonical_core = street_key(canonical)
    source_tokens = set(normalize(source).split())
    source_joined = "".join(normalize(source).split())
    number_match = bool(
        canonical_number
        and (
            canonical_number in source_tokens
            or canonical_number in source_joined
        )
    )
    return bool(number_match and canonical_core and canonical_core <= source_tokens)


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


async def direct_fetch(task: CrawlTask, profile=None) -> FetchResult:
    del profile
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(
            probe_get,
            task.url,
            timeout=30,
            unlocker=False,
            retries=1,
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        return FetchResult(
            url=task.url,
            outcome=(
                FetchOutcome.OK
                if 200 <= status < 300 and body
                else FetchOutcome.HARD_FAIL
            ),
            status=status,
            body=body,
            headers={},
            render_mode=task.render_mode,
            final_url=str(response.url or task.url),
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.TRANSIENT,
            status=None,
            body=None,
            headers={},
            render_mode=task.render_mode,
            final_url=task.url,
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_signature=f"{type(exc).__name__}: {str(exc)[:160]}",
        )


def make_task(url: str, property_id: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=45_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def applicant_payload_evidence(result: dict, expected_id: str) -> dict:
    response = next(
        (
            item
            for item in (result.get("_raw_api_responses") or [])
            if isinstance(item, dict)
            and "getfloorplanandavailableunits" in str(item.get("url") or "").lower()
            and f"propertyId={expected_id}" in str(item.get("url") or "")
        ),
        {},
    )
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    floor_plan_rows = [
        item
        for item in (body.get("floorPlanList") or [])
        if isinstance(item, dict) and isinstance(item.get("floorPlan"), dict)
    ]
    floor_plans = [item["floorPlan"] for item in floor_plan_rows]
    native_units = [
        unit
        for item in floor_plan_rows
        for unit in (item.get("UnitAvailability") or [])
        if isinstance(unit, dict)
    ]
    return {
        "response_url": str(response.get("url") or ""),
        "response_status": response.get("status"),
        "body_status": body.get("status"),
        "floor_plans": floor_plans,
        "native_units": native_units,
    }


async def validate_one(
    pid: str,
    spec: dict[str, str],
    canonical: dict[str, str],
) -> dict:
    configured_url = canonical["website"]
    if "://" not in configured_url:
        configured_url = f"https://{configured_url}"
    task = make_task(configured_url, pid)
    configured_fetch = await direct_fetch(task)
    body = (configured_fetch.body or b"").decode("utf-8", "replace")
    soup = BeautifulSoup(body, "lxml")
    visible = normalize(soup.get_text(" ", strip=True))
    links = [
        str(anchor.get("href") or "")
        for anchor in soup.find_all("a", href=True)
        if str(anchor.get("href") or "").strip()
    ]
    result = await scraper_mod.scrape_jugnu(
        task,
        configured_fetch,
        page=None,
        profile=None,
        csv_row=canonical,
    )
    emitted = [item for item in result.get("units") or [] if isinstance(item, dict)]
    qualified = [
        item for item in emitted if unit_has_real_anchor(item) and positive_rent(item)
    ]
    common_gates = {
        "configured_http_200": configured_fetch.status == 200,
        "configured_name_visible": name_key(canonical["name"]) in name_key(visible),
        "configured_city_visible": normalize(canonical["city"]) in visible,
        "configured_state_visible": normalize(canonical["state"]) in visible,
        "current_pipeline_adapter_exact": result.get("_adapter_used") == spec["adapter"],
        "current_pipeline_tier_exact": result.get("extraction_tier_used") == spec["tier"],
        "current_pipeline_no_errors": not (result.get("errors") or []),
        "all_emitted_rows_strict_native_positive": len(qualified) == len(emitted) > 0,
        "all_output_unit_numbers_present": all(
            str(item.get("unit_number") or "").strip() for item in qualified
        ),
    }
    boundary: dict[str, object]
    if spec["adapter"] == "rentcafe":
        expected_id = spec["property_id"]
        published_links = [
            link
            for link in links
            if (urlparse(link).hostname or "").lower() == spec["published_host"]
            and spec["published_slug"] in link.lower()
        ]
        evidence = applicant_payload_evidence(result, expected_id)
        floor_plans = evidence["floor_plans"]
        native_units = evidence["native_units"]
        floor_plan_names = {name_key(item.get("PropertyName")) for item in floor_plans}
        floor_plan_ids = {str(item.get("PropertyID") or "") for item in floor_plans}
        floor_plan_addresses = {str(item.get("Address") or "") for item in floor_plans}
        floor_plan_cities = {normalize(item.get("City")) for item in floor_plans}
        floor_plan_states = {normalize(item.get("State")) for item in floor_plans}
        floor_plan_zips = {normalize(item.get("Zipcode")) for item in floor_plans}
        unit_addresses = [
            str(item.get("unitAddress") or "")
            for item in native_units
            if str(item.get("unitAddress") or "").strip()
        ]
        output_source_ids = {
            str(item.get("source_property_id") or "") for item in qualified
        }
        output_source_urls = {
            str(item.get("source_api_url") or "") for item in qualified
        }
        output_native_ids = {
            str((item.get("source_ids") or {}).get("securecafe_apartment_id") or "")
            for item in qualified
        }
        output_source_names = {
            name_key(item.get("source_property_name")) for item in qualified
        }
        output_source_addresses = {
            str(item.get("source_property_address") or "") for item in qualified
        }
        floor_plan_zip_match = normalize(canonical["zip"]) in floor_plan_zips
        native_unit_zip_match = bool(unit_addresses) and all(
            normalize(canonical["zip"]) in normalize(value) for value in unit_addresses
        )
        adapter_gates = {
            "configured_publishes_exact_portal": bool(published_links),
            "applicant_api_http_200": evidence["response_status"] == 200,
            "applicant_api_status_true": evidence["body_status"] is True,
            "api_property_id_exact": floor_plan_ids == {expected_id},
            "api_property_name_exact": floor_plan_names == {name_key(canonical["name"])},
            "api_property_street_exact_normalized": bool(floor_plan_addresses)
            and all(street_matches(canonical["address"], value) for value in floor_plan_addresses),
            "api_property_city_exact": floor_plan_cities == {normalize(canonical["city"])},
            "api_property_state_exact": floor_plan_states == {normalize(canonical["state"])},
            "api_zip_or_native_unit_zip_exact": floor_plan_zip_match or native_unit_zip_match,
            "output_source_property_id_exact": output_source_ids == {expected_id},
            "output_source_property_name_exact": output_source_names
            == {name_key(canonical["name"])},
            "output_source_property_street_exact_normalized": bool(output_source_addresses)
            and all(
                street_matches(canonical["address"], value)
                for value in output_source_addresses
            ),
            "all_output_native_ids_present": bool(output_native_ids)
            and "" not in output_native_ids,
            "all_output_source_urls_exact_api": output_source_urls
            == {str(evidence["response_url"])},
            "winning_url_exact_api": str(result.get("_winning_page_url") or "")
            == str(evidence["response_url"]),
        }
        boundary = {
            "published_links": published_links,
            "expected_source_property_id": expected_id,
            "api_property_names": sorted(floor_plan_names),
            "api_property_addresses": sorted(floor_plan_addresses),
            "api_property_cities": sorted(floor_plan_cities),
            "api_property_states": sorted(floor_plan_states),
            "api_property_zips": sorted(floor_plan_zips),
            "native_unit_addresses": unit_addresses,
            "api_response_url": evidence["response_url"],
        }
    else:
        luv_settings = re.findall(
            r"window\.luv_settings\s*=\s*\[\s*['\"]([^'\"]+)['\"]\s*,"
            r"\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            body,
            re.IGNORECASE,
        )
        output_native_ids = {
            str((item.get("source_ids") or {}).get("iloveleasing_unit_id") or "")
            for item in qualified
        }
        output_names = {name_key(item.get("source_property_name")) for item in qualified}
        output_addresses = {str(item.get("address") or "") for item in qualified}
        output_provenance = {
            str(item.get("source_property_provenance") or "") for item in qualified
        }
        output_source_urls = {
            str(item.get("source_api_url") or "") for item in qualified
        }
        raw_statuses = {
            str(item.get("url") or ""): item.get("status")
            for item in (result.get("_raw_api_responses") or [])
            if isinstance(item, dict)
        }
        expected_api = "https://www.iloveleasing.com/pub/wapi/api/availability/"
        adapter_gates = {
            "single_published_iloveleasing_settings": len(luv_settings) == 1,
            "published_iloveleasing_script": any(
                spec["published_host"] in str(script.get("src") or "").lower()
                for script in soup.find_all("script", src=True)
            ),
            "all_public_iloveleasing_routes_http_200": len(raw_statuses) == 3
            and all(status == 200 for status in raw_statuses.values()),
            "output_property_name_exact": output_names == {name_key(canonical["name"])},
            "output_property_address_exact_normalized": bool(output_addresses)
            and all(street_matches(canonical["address"], value) for value in output_addresses),
            "output_city_state_zip_exact": all(
                normalize(canonical[field]) in normalize(value)
                for value in output_addresses
                for field in ("city", "state", "zip")
            ),
            "output_provenance_published_widget": output_provenance
            == {"published_iloveleasing_widget"},
            "all_output_native_ids_present": bool(output_native_ids)
            and "" not in output_native_ids,
            "all_output_source_urls_exact_api": output_source_urls == {expected_api},
            "winning_url_exact_api": str(result.get("_winning_page_url") or "")
            == expected_api,
        }
        boundary = {
            "published_luv_settings": [list(item) for item in luv_settings],
            "public_api_statuses": raw_statuses,
            "output_property_names": sorted(output_names),
            "output_addresses": sorted(output_addresses),
            "api_response_url": expected_api,
        }

    gates = {**common_gates, **adapter_gates}
    if not all(gates.values()):
        raise RuntimeError(
            f"pid {pid} strict gates failed: "
            + json.dumps({key: value for key, value in gates.items() if not value})
        )
    source_urls = sorted(
        {
            configured_url,
            *(str(item.get("source_api_url") or "") for item in qualified),
            *(
                str(item.get("source_portal_url") or "")
                for item in qualified
                if item.get("source_portal_url")
            ),
        }
    )
    return {
        "property_id": int(pid),
        "property_name": canonical["name"],
        "website": canonical["website"],
        "strict_verdict": spec["verdict"],
        "native_identity_rows": len(qualified),
        "native_positive_rent_rows": len(qualified),
        "source_urls": source_urls,
        "property_boundary_evidence": {
            "canonical_address": canonical["address"],
            "canonical_city": canonical["city"],
            "canonical_state": canonical["state"],
            "canonical_zip": canonical["zip"],
            "configured_final_url": configured_fetch.final_url,
            "configured_final_host": urlparse(configured_fetch.final_url).hostname or "",
            "gates": gates,
            **boundary,
        },
        "current_full_pipeline": {
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
            "winning_page_url": result.get("_winning_page_url") or "",
            "strict_native_positive_rent_rows": len(qualified),
            "errors": result.get("errors") or [],
        },
        "units": [
            {
                "unit_number": str(item.get("unit_number") or ""),
                "floor_plan_name": str(item.get("floor_plan_name") or ""),
                "rent": item.get("market_rent_low"),
                "market_rent_high": item.get("market_rent_high"),
                "availability_date": str(item.get("availability_date") or ""),
                "source_url": str(item.get("source_api_url") or ""),
                "source_property_id": str(item.get("source_property_id") or ""),
                "source_ids": item.get("source_ids") or {},
                "source_property_name": str(item.get("source_property_name") or ""),
                "source_property_address": str(
                    item.get("source_property_address") or item.get("address") or ""
                ),
            }
            for item in qualified
        ],
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").lower()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    metadata = {
        row["apartmentid"]: row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid") in TARGETS
    }
    ledger_ids = {row["property_id"] for row in read_csv(LEDGER)}
    overlap = sorted(set(TARGETS) & ledger_ids, key=int)
    if overlap:
        raise RuntimeError(f"Targets already in captured ledger: {overlap}")

    # Preserve current source orchestration while guaranteeing direct-only,
    # no-paid-backend link-hop if a configured route needs one.
    fetch_mod.fetch = direct_fetch
    recoveries = await asyncio.gather(
        *(validate_one(pid, TARGETS[pid], metadata[pid]) for pid in TARGETS)
    )
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_label": "rentcafe-rentmanager-three-current-source-configured-e2e",
        "ledger_snapshot": {
            "path": str(LEDGER),
            "sha256": sha256(LEDGER),
            "rows": len(read_csv(LEDGER)),
            "net_new_ids": [int(pid) for pid in TARGETS],
        },
        "guardrails": {
            "llm_enabled": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "production_source_modified_by_lane": False,
            "shared_builder_modified": False,
            "shared_ledger_modified": False,
        },
        "recoveries": recoveries,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "ledger_rows": payload["ledger_snapshot"]["rows"],
                "net_new_ids": payload["ledger_snapshot"]["net_new_ids"],
                "strict_native_positive_rent_rows": {
                    str(item["property_id"]): item["native_positive_rent_rows"]
                    for item in recoveries
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
