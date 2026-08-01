#!/usr/bin/env python3
"""Materialize strict evidence for three stale-domain current-site migrations.

This is a local, no-LLM, no-unlocker validation.  It does not run a canary and
does not mutate the recovery ledger or the repository.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


os.environ["COMPLIANCE_MODE"] = "1"
os.environ["PROBE_PROXY_URL"] = ""
os.environ["WEB_UNLOCKER_KEY"] = ""
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
from ma_poc.pms.adapters._probe import (  # noqa: E402
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.adapters.g5 import (  # noqa: E402
    _fetch_g5_units,
    find_g5_urn_candidates,
    parse_g5_apartments,
)
from ma_poc.pms.adapters.knock import (  # noqa: E402
    find_knock_ids,
    parse_knock_units,
)
from ma_poc.pms.adapters.marketapts import (  # noqa: E402
    marketapts_payload_to_units,
    marketapts_static_payload,
)
from ma_poc.pms.scraper import scrape  # noqa: E402


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "current_official_migrations_three"
EVIDENCE = OUT / "evidence_current_official_migrations_three_strict.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
SUMMARY = ROOT / "strict_recovery_ledger_current_summary.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")

SPECS: dict[str, dict[str, Any]] = {
    "246962": {
        "property_id": "246962",
        "property_name": "Portola Bridge Creek",
        "published_names": ("Portola Bridge Creek",),
        "configured_url": "http://www.bridgecreekapthomes.com/",
        "configured_probe_url": "http://www.bridgecreekapthomes.com/",
        "current_url": "https://www.portolabridgecreek.com/",
        "address": "9211 NE 15th Ave",
        "street_key": "9211 ne 15th",
        "city": "Vancouver",
        "state": "WA",
        "postal_code": "98665",
        "adapter": "marketapts",
        "tier": "TIER_1_DOM_MARKETAPTS_B_UNIT_LEVEL",
        "expected_rows": 13,
    },
    "37071": {
        "property_id": "37071",
        "property_name": "Summerwood on Towne Line",
        "published_names": ("Summerwood on Towne Line",),
        "configured_url": "www.summerwoodindy.com",
        "configured_probe_url": "https://www.summerwoodindy.com/",
        "current_url": "https://www.summerwoodontownelineapartments.com/",
        "address": "2520 Summer Dr",
        "street_key": "2520 summer",
        "city": "Indianapolis",
        "state": "IN",
        "postal_code": "46268",
        "adapter": "g5",
        "tier": "TIER_1_API_G5",
        "expected_rows": 16,
    },
    "42977": {
        "property_id": "42977",
        "property_name": "Elms at Stoney Run",
        "published_names": (
            "Elms Stoney Run Village",
            "The Elms at Stoney Run Village",
            "Elms at Stoney Run",
        ),
        "configured_url": "https://stoneyrunelmsliving.com/home#",
        "configured_probe_url": "https://stoneyrunelmsliving.com/home#",
        "current_url": "https://www.stoneyrunelmsliving.com/",
        "address": "7581 Stoney Run Drive",
        "street_key": "7581 stoney run",
        "city": "Hanover",
        "state": "MD",
        "postal_code": "21076",
        "adapter": "knock",
        "tier": "TIER_1_KNOCK_API",
        "expected_rows": 21,
    },
}

PORTOLA_PLAN_ROUTES = {
    "2x1a",
    "2x1ar",
    "2x1b",
    "2x1br",
    "2x1c",
    "2x1cr",
    "2x1d",
    "3x2a",
    "3x2ar",
}
G5_CANONICAL_URN = (
    "g5-cl-1ogmyorq1z-starwood-capital-fka-highmark-residential-"
    "scg-global-holdings-l-l-c-indianapolis-in"
)
G5_SIBLING_URN = (
    "g5-cl-1ofqzxojd0-starwood-capital-fka-highmark-residential-"
    "scg-global-holdings-l-l-c-denver-co"
)
G5_ENDPOINT = "https://inventory.g5marketingcloud.com/graphql"
KNOCK_PUBLIC_KEY = "dff7e918938011ed9e6812fdb3809b4f"
KNOCK_COMMUNITY_ID = "11ed3548d8dabea8"
KNOCK_PROPERTY_ID = "2017709"
KNOCK_COMMUNITY_URL = (
    "https://doorway-api.knockrentals.com/v1/property/community/"
    f"{KNOCK_COMMUNITY_ID}"
)
KNOCK_UNITS_URL = (
    f"https://doorway-api.knockrentals.com/v1/property/{KNOCK_PROPERTY_ID}/units"
)

CRITICAL_SOURCE_FILES = (
    REPO / "ma_poc/core/identity.py",
    REPO / "ma_poc/pms/scraper.py",
    REPO / "ma_poc/pms/adapters/marketapts.py",
    REPO / "ma_poc/pms/adapters/g5.py",
    REPO / "ma_poc/pms/adapters/knock.py",
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def snapshot(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): sha256_path(path) for path in paths}


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def visible_text(html: str) -> str:
    return normalized(BeautifulSoup(html, "lxml").get_text(" ", strip=True))


def identity_checks(html: str, spec: dict[str, Any]) -> dict[str, bool]:
    text = visible_text(html)
    return {
        "published_name": any(
            normalized(name) in text for name in spec["published_names"]
        ),
        "street": normalized(str(spec["street_key"])) in text,
        "city": normalized(str(spec["city"])) in text,
        "postal_code": normalized(str(spec["postal_code"])) in text,
    }


def identity_matches(html: str, spec: dict[str, Any]) -> bool:
    checks = identity_checks(html, spec)
    return len(checks) == 4 and all(checks.values())


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


def rent_value(row: dict[str, Any]) -> int:
    for key in (
        "market_rent_low",
        "rent_low",
        "rent",
        "market_rent_high",
        "rent_high",
    ):
        value = row.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return int(value)
    raise AssertionError(f"no positive rent: {row!r}")


def raw_rent_value(row: dict[str, Any]) -> int | None:
    for key in ("price", "displayPrice", "knockPrice"):
        value = row.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        match = re.search(r"\d[\d,]*", str(value))
        if match:
            parsed = int(match.group(0).replace(",", ""))
            if parsed > 0:
                return parsed
    return None


def iso_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() == "now":
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        month, day, year = (int(part) for part in match.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    raise AssertionError(f"unexpected availability date {text!r}")


def row_key(row: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(row.get("unit_number") or "").strip(),
        rent_value(row),
        normalized(str(row.get("floor_plan_name") or "")),
        iso_date(row.get("availability_date") or row.get("available_date")),
    )


def strict_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in (result.get("units") or [])
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]


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
        "source_urls": sorted(
            {
                str(row.get("source_api_url") or "")
                for row in rows
                if str(row.get("source_api_url") or "")
            }
        ),
        "validation_budget": result.get("_validation_budget"),
    }


def direct_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    kwargs: dict[str, Any] = {
        "timeout": 35,
        "unlocker": False,
        "retries": 2,
        "proxies": {},
        "verify": True,
    }
    if headers:
        kwargs["headers"] = headers
    response = probe_get(url, **kwargs)
    body = bytes(response.content or b"")
    text = body.decode("utf-8", "replace")
    status = int(response.status_code or 0)
    challenge = bool(
        re.search(
            r"just a moment|verify you are human|checking your browser|cf-chl-",
            text,
            re.I,
        )
    )
    assert status == 200, (url, status, response.url)
    assert body, url
    assert challenge is False, url
    metadata = {
        "requested_url": url,
        "status": status,
        "final_url": str(response.url or url),
        "body_bytes": len(body),
        "body_sha256": sha256_bytes(body),
        "challenge_detected": challenge,
        "transport": {
            "backend": "direct_curl_cffi_probe_get",
            "unlocker": False,
            "proxies": {},
            "captcha_solving": False,
            "fingerprint_rotation": False,
        },
    }
    response_headers = {
        str(key).lower(): str(value) for key, value in response.headers.items()
    }
    return body, metadata, response_headers


def archive(name: str, body: bytes) -> dict[str, Any]:
    path = OUT / name
    with gzip.open(path, "wb") as handle:
        handle.write(body)
    return {
        "path": str(path),
        "compressed_sha256": sha256_path(path),
        "raw_sha256": sha256_bytes(body),
        "raw_bytes": len(body),
    }


def archive_json(name: str, payload: Any) -> dict[str, Any]:
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return archive(name, body)


def to_fetch_result(
    url: str,
    body: bytes,
    metadata: dict[str, Any],
    headers: dict[str, str],
) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=int(metadata["status"]),
        body=body,
        headers=headers,
        render_mode=RenderMode.GET,
        final_url=str(metadata["final_url"]),
        attempts=1,
        elapsed_ms=0,
    )


async def full_pipeline(
    spec: dict[str, Any],
    url: str,
    body: bytes,
    metadata: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    result = await scrape(
        url,
        page=None,
        fetch_result=to_fetch_result(url, body, metadata, headers),
        csv_row={
            "apartmentid": spec["property_id"],
            "name": spec["property_name"],
            "address": spec["address"],
            "city": spec["city"],
            "state": spec["state"],
            "zip": spec["postal_code"],
            "website": url,
        },
        property_id=str(spec["property_id"]),
        shared_budget=budget,
    )
    result["_validation_budget"] = budget
    return result


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
        "source_api_url": str(row.get("source_api_url") or ""),
    }


def common_result(
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
    old_fetch: dict[str, Any],
    old_e2e: dict[str, Any],
    current_fetch: dict[str, Any],
    current_e2e: dict[str, Any],
    *,
    verdict: str,
    source_urls: list[str],
) -> dict[str, Any]:
    return {
        "property_id": int(spec["property_id"]),
        "property_name": spec["property_name"],
        "website": spec["configured_url"],
        "current_official_url": spec["current_url"],
        "outcome": "UNIT_QUALIFIED",
        "adapter": spec["adapter"],
        "tier": spec["tier"],
        "units": len(rows),
        "property_identity_match": True,
        "contamination_verdict": verdict,
        "identity_evidence": {
            "canonical_name": spec["property_name"],
            "published_names": list(spec["published_names"]),
            "published_address": spec["address"],
            "city_state_zip": (
                f"{spec['city']}, {spec['state']} {spec['postal_code']}"
            ),
            "current_root_identity_checks": current_fetch["identity_checks"],
            "configured_route_identity_checks": old_fetch["identity_checks"],
            "rows_with_native_identity": len(rows),
            "rows_with_native_identity_and_positive_rent": len(rows),
            "distinct_unit_numbers": len(
                {str(row.get("unit_number") or "").casefold() for row in rows}
            ),
            "source_urls": source_urls,
        },
        "configured_source_validation": {
            "configured_url": spec["configured_url"],
            "configured_probe_url": spec["configured_probe_url"],
            "configured_fetch": old_fetch,
            "configured_full_pipeline": compact_e2e(old_e2e),
            "configured_route_exact_property_identity": False,
            "current_official_fetch": current_fetch,
            "current_official_full_pipeline": compact_e2e(current_e2e),
            "minimal_production_lever": (
                "update canonical website to the exact current official property URL"
            ),
        },
        "native_samples": [native_sample(row) for row in rows[:10]],
        "native_rows": rows,
    }


def portola_result(
    spec: dict[str, Any],
    root_body: bytes,
    old_fetch: dict[str, Any],
    old_e2e: dict[str, Any],
    current_fetch: dict[str, Any],
    current_e2e: dict[str, Any],
    root_captures: dict[str, Any],
) -> dict[str, Any]:
    drill_cache: dict[str, tuple[bytes, dict[str, Any]]] = {}

    def drill_fetch(url: str, xhr: bool) -> str | None:
        request_headers = {"X-Requested-With": "XMLHttpRequest"} if xhr else None
        body, metadata, _headers = direct_fetch(url, headers=request_headers)
        drill_cache[url] = (body, metadata)
        return body.decode("utf-8", "replace")

    payload = marketapts_static_payload(
        root_body.decode("utf-8", "replace"),
        str(spec["current_url"]),
        drill_fetch,
    )
    assert payload.get("template") == "B"
    plans = [plan for plan in payload.get("plans") or [] if isinstance(plan, dict)]
    assert len(plans) == 11
    unavailable_plans = [
        plan
        for plan in plans
        if not str(plan.get("drillPath") or "") and not (plan.get("units") or [])
    ]
    assert {str(plan.get("title") or "") for plan in unavailable_plans} == {
        "1X1A",
        "1X1AR",
    }
    route_to_plan = {
        urljoin(str(spec["current_url"]), str(plan.get("drillPath") or "")): str(
            plan.get("title") or ""
        )
        for plan in plans
        if str(plan.get("drillPath") or "")
    }
    expected_routes = {
        urljoin(str(spec["current_url"]), f"unit/{slug}")
        for slug in PORTOLA_PLAN_ROUTES
    }
    assert set(route_to_plan) == expected_routes
    floorplans_url = urljoin(str(spec["current_url"]), "floorplans")
    assert set(drill_cache) == {floorplans_url, *expected_routes}
    for body, _metadata in drill_cache.values():
        assert identity_matches(body.decode("utf-8", "replace"), spec)

    apply_by_unit: dict[str, dict[str, str]] = {}
    plan_probes: list[dict[str, Any]] = []
    drill_captures: list[dict[str, Any]] = []
    for route in sorted(expected_routes):
        body, metadata = drill_cache[route]
        soup = BeautifulSoup(body.decode("utf-8", "replace"), "lxml")
        route_units: list[str] = []
        for unit_row in soup.select(".unit-table-row"):
            anchor = unit_row.select_one('a[href*="/apply-online?"]')
            assert anchor is not None
            application_url = urljoin(route, str(anchor.get("href") or ""))
            assert urlsplit(application_url).hostname == "www.portolabridgecreek.com"
            query = parse_qs(urlsplit(application_url).query)
            unit_number = str((query.get("u") or [""])[0]).strip()
            plan_code = str((query.get("t") or [""])[0]).strip()
            assert unit_number and plan_code == route_to_plan[route]
            row_text = " ".join(unit_row.stripped_strings)
            availability_match = re.search(
                r"Available:\s*(Now|\d{1,2}/\d{1,2}/\d{4})",
                row_text,
                re.I,
            )
            assert availability_match
            apply_by_unit[unit_number] = {
                "marketapts_unit_number": unit_number,
                "marketapts_plan_code": plan_code,
                "application_url": application_url,
                "source_api_url": route,
                "published_availability_text": availability_match.group(1),
            }
            route_units.append(unit_number)
        assert route_units
        plan_probes.append(
            {
                "plan_code": route_to_plan[route],
                "url": route,
                "status": metadata["status"],
                "native_positive_rent_rows": len(route_units),
                "unit_numbers": route_units,
                "same_origin_native_unit_apply_routes": True,
                "exact_property_identity_on_page": True,
            }
        )
        capture = archive(
            f"portola_direct_{route.rstrip('/').rsplit('/', 1)[-1]}.html.gz",
            body,
        )
        capture["url"] = route
        drill_captures.append(capture)

    raw_rows = marketapts_payload_to_units(payload, floorplans_url)
    rows = [
        dict(row)
        for row in raw_rows
        if isinstance(row, dict) and unit_has_real_anchor(row) and positive_rent(row)
    ]
    assert len(rows) == int(spec["expected_rows"]) == 13
    assert len(apply_by_unit) == 13
    assert {str(row.get("unit_number") or "") for row in rows} == set(apply_by_unit)
    for row in rows:
        unit_number = str(row["unit_number"])
        binding = apply_by_unit[unit_number]
        assert normalized(str(row["floor_plan_name"])) == normalized(
            binding["marketapts_plan_code"]
        )
        published = binding["published_availability_text"]
        assert iso_date(row.get("availability_date")) == iso_date(published)
        row["availability_date"] = iso_date(published)
        row["available_date"] = iso_date(published)
        row["published_availability_text"] = published
        row["source_ids"] = {
            "marketapts_unit_number": binding["marketapts_unit_number"],
            "marketapts_plan_code": binding["marketapts_plan_code"],
        }
        row["source_api_url"] = binding["source_api_url"]
        row["application_url"] = binding["application_url"]

    e2e_rows = strict_rows(current_e2e)
    assert current_e2e.get("_adapter_used") == spec["adapter"]
    assert current_e2e.get("extraction_tier_used") == spec["tier"]
    assert len(e2e_rows) == len(rows)
    assert {row_key(row) for row in rows} == {row_key(row) for row in e2e_rows}
    result = common_result(
        spec,
        rows,
        old_fetch,
        old_e2e,
        current_fetch,
        current_e2e,
        verdict=(
            "pass_exact_current_official_portola_bridge_creek_nine_same_origin_"
            "marketapts_unit_drills_native_apply_binding_full_pipeline"
        ),
        source_urls=sorted(expected_routes),
    )
    result.update(
        {
            "strict_gates": {
                "exact_current_property_identity": True,
                "marketapts_template_b": True,
                "nine_unit_drill_pages_discovered": True,
                "nine_unit_drill_pages_directly_probed": True,
                "all_drill_pages_exact_property_identity": True,
                "all_rows_same_origin_native_apply_unit_and_plan_binding": True,
                "all_rows_native_unit_number": True,
                "all_rows_positive_row_level_rent": True,
                "full_pipeline_current_root_13_native_positive_rent_rows": True,
                "full_pipeline_and_direct_unit_rent_plan_date_match": True,
                "no_llm": True,
                "no_unlocker": True,
                "no_captcha_solver": True,
                "no_fingerprint_rotation": True,
            },
            "nine_plan_direct_probes": plan_probes,
            "floorplan_name_and_date_semantics": {
                "exact_published_floorplan_names": sorted(
                    {str(row["floor_plan_name"]) for row in rows}
                ),
                "unavailable_plan_only_names": sorted(
                    {str(plan["title"]) for plan in unavailable_plans}
                ),
                "explicit_availability_date_rows": sum(
                    bool(row.get("availability_date")) for row in rows
                ),
                "available_now_rows": sum(
                    row["published_availability_text"].casefold() == "now"
                    for row in rows
                ),
                "names_are_native_plan_card_titles": True,
                "dates_are_native_unit_drill_available_values": True,
                "available_now_rows_leave_date_blank": True,
            },
            "contamination_negative_checks": {
                "all_inventory_hosts": ["www.portolabridgecreek.com"],
                "all_application_hosts": ["www.portolabridgecreek.com"],
                "all_apply_query_units_exactly_match_native_rows": True,
                "all_apply_query_plan_codes_exactly_match_plan_cards": True,
                "sibling_rows_admitted": 0,
                "direct_vs_full_pipeline_row_set_difference": [],
            },
            "current_capture": {
                **root_captures,
                "floorplans_index": archive(
                    "portola_direct_floorplans.html.gz",
                    drill_cache[floorplans_url][0],
                ),
                "unit_drill_pages": drill_captures,
            },
        }
    )
    return result


async def summerwood_result(
    spec: dict[str, Any],
    root_body: bytes,
    old_fetch: dict[str, Any],
    old_e2e: dict[str, Any],
    current_fetch: dict[str, Any],
    current_e2e: dict[str, Any],
    root_captures: dict[str, Any],
) -> dict[str, Any]:
    root_html = root_body.decode("utf-8", "replace")
    urns = find_g5_urn_candidates(root_html)
    assert urns[:2] == [G5_CANONICAL_URN, G5_SIBLING_URN]
    canonical_payload = await _fetch_g5_units(
        G5_CANONICAL_URN,
        base_url=str(spec["current_url"]),
    )
    sibling_payload = await _fetch_g5_units(
        G5_SIBLING_URN,
        base_url=str(spec["current_url"]),
    )
    assert isinstance(canonical_payload, dict)
    assert isinstance(sibling_payload, dict)
    canonical_complex = (
        (canonical_payload.get("data") or {}).get("apartmentComplex") or {}
    )
    sibling_complex = (
        (sibling_payload.get("data") or {}).get("apartmentComplex") or {}
    )
    canonical_complex_id = str(canonical_complex.get("id") or "")
    sibling_complex_id = str(sibling_complex.get("id") or "")
    assert canonical_complex_id.isdigit()
    assert sibling_complex_id.isdigit()
    assert canonical_complex_id != sibling_complex_id
    assert len(canonical_complex.get("apartments") or []) == 16
    assert len(sibling_complex.get("apartments") or []) == 28
    expected_floorplans = {
        str(floorplan.get("name") or ""): str(floorplan.get("id") or "")
        for floorplan in canonical_complex.get("floorplans") or []
        if isinstance(floorplan, dict)
    }
    assert set(expected_floorplans) == {
        "Poplar",
        "Willow",
        "Sycamore",
        "Mulberry",
        "Magnolia",
        "Cottonwood",
    }
    assert len(set(expected_floorplans.values())) == 6
    assert all(value.isdigit() for value in expected_floorplans.values())
    assert all(
        str(floorplan.get("name") or "").startswith("7575 - ")
        for floorplan in sibling_complex.get("floorplans") or []
        if isinstance(floorplan, dict)
    )

    raw_apartments = [
        row
        for row in canonical_complex.get("apartments") or []
        if isinstance(row, dict)
    ]
    raw_by_unit = {
        str(row.get("name") or row.get("displayName") or ""): row
        for row in raw_apartments
    }
    rows = [dict(row) for row in parse_g5_apartments(canonical_payload)]
    assert len(rows) == int(spec["expected_rows"]) == 16
    assert len(raw_by_unit) == 16
    api_url = f"{G5_ENDPOINT}?urn={G5_CANONICAL_URN}"
    for row in rows:
        unit_number = str(row.get("unit_number") or "")
        raw = raw_by_unit[unit_number]
        floorplan = raw.get("floorplan") or {}
        assert normalized(str(row.get("floor_plan_name") or "")) == normalized(
            str(floorplan.get("name") or "")
        )
        assert str(row.get("availability_date") or "") == str(
            raw.get("availabilityDate") or ""
        )
        assert str(floorplan.get("id") or "") == expected_floorplans[
            str(floorplan.get("name") or "")
        ]
        row["source_ids"] = {
            "g5_apartment_id": str(raw.get("id") or ""),
            "g5_floorplan_id": str(floorplan.get("id") or ""),
            "g5_apartment_complex_id": canonical_complex_id,
            "g5_location_urn": G5_CANONICAL_URN,
        }
        row["source_property_id"] = canonical_complex_id
        row["source_api_url"] = api_url
        assert unit_has_real_anchor(row) and positive_rent(row)
    assert len({row["source_ids"]["g5_apartment_id"] for row in rows}) == 16

    e2e_rows = strict_rows(current_e2e)
    assert current_e2e.get("_adapter_used") == spec["adapter"]
    assert current_e2e.get("extraction_tier_used") == spec["tier"]
    assert len(e2e_rows) == len(rows)
    assert {row_key(row) for row in rows} == {row_key(row) for row in e2e_rows}
    result = common_result(
        spec,
        rows,
        old_fetch,
        old_e2e,
        current_fetch,
        current_e2e,
        verdict=(
            "pass_exact_current_official_summerwood_canonical_datalayer_g5_"
            "native_graphql_full_pipeline_sibling_rejected"
        ),
        source_urls=[api_url],
    )
    result.update(
        {
            "strict_gates": {
                "exact_current_property_identity": True,
                "canonical_g5_datalayer_urn_ranked_first": True,
                "canonical_g5_complex_id_is_numeric_and_distinct_from_sibling": True,
                "six_exact_numeric_floorplan_ids": True,
                "all_rows_unique_native_g5_apartment_id": True,
                "all_rows_native_unit_number": True,
                "all_rows_positive_row_level_rent": True,
                "full_pipeline_current_root_16_native_positive_rent_rows": True,
                "full_pipeline_and_direct_graphql_unit_rent_plan_date_match": True,
                "denver_sibling_urn_explicitly_rejected": True,
                "no_llm": True,
                "no_unlocker": True,
                "no_captcha_solver": True,
                "no_fingerprint_rotation": True,
            },
            "g5_direct_probe": {
                "canonical_urn": G5_CANONICAL_URN,
                "canonical_complex_id": canonical_complex_id,
                "canonical_native_apartments": 16,
                "canonical_floorplans": expected_floorplans,
                "canonical_unit_numbers": [row["unit_number"] for row in rows],
                "numeric_id_semantics": (
                    "G5 numeric complex, apartment, and floorplan IDs are captured from "
                    "this live snapshot; the property boundary is the page-published "
                    "canonical G5_STORE_ID URN, not a hard-coded numeric ID"
                ),
                "sibling_urn": G5_SIBLING_URN,
                "sibling_complex_id": sibling_complex_id,
                "sibling_native_apartments": 28,
                "sibling_floorplan_prefix": "7575 - ",
            },
            "floorplan_name_and_date_semantics": {
                "exact_published_floorplan_names": sorted(expected_floorplans),
                "exact_numeric_floorplan_ids": sorted(
                    expected_floorplans.values(), key=int
                ),
                "explicit_availability_date_rows": sum(
                    bool(row.get("availability_date")) for row in rows
                ),
                "names_are_native_graphql_floorplan_names": True,
                "dates_are_native_graphql_availabilityDate_values": True,
            },
            "contamination_negative_checks": {
                "canonical_urn_source": "current property page G5_STORE_ID dataLayer",
                "canonical_complex_ids_admitted": [canonical_complex_id],
                "denver_sibling_complex_ids_admitted": [],
                "denver_sibling_rows_admitted": 0,
                "direct_vs_full_pipeline_row_set_difference": [],
            },
            "current_capture": {
                **root_captures,
                "canonical_g5_graphql_payload": archive_json(
                    "summerwood_g5_canonical_graphql.json.gz",
                    canonical_payload,
                ),
                "rejected_sibling_g5_graphql_payload": archive_json(
                    "summerwood_g5_denver_sibling_graphql.json.gz",
                    sibling_payload,
                ),
            },
        }
    )
    return result


def stoney_run_result(
    spec: dict[str, Any],
    root_body: bytes,
    old_fetch: dict[str, Any],
    old_e2e: dict[str, Any],
    current_fetch: dict[str, Any],
    current_e2e: dict[str, Any],
    root_captures: dict[str, Any],
) -> dict[str, Any]:
    root_html = root_body.decode("utf-8", "replace")
    public_key, kind, community_id = find_knock_ids(root_html)
    assert (public_key, kind, community_id) == (
        KNOCK_PUBLIC_KEY,
        "community",
        KNOCK_COMMUNITY_ID,
    )
    api_headers = {
        "Origin": "https://doorway.knck.io",
        "Accept": "application/json",
    }
    community_body, community_fetch, _ = direct_fetch(
        KNOCK_COMMUNITY_URL,
        headers=api_headers,
    )
    units_body, units_fetch, _ = direct_fetch(KNOCK_UNITS_URL, headers=api_headers)
    community_payload = json.loads(community_body)
    units_payload = json.loads(units_body)
    property_payload = community_payload.get("property") or {}
    property_data = property_payload.get("data") or {}
    location = property_data.get("location") or {}
    address = location.get("address") or {}
    social = property_data.get("social") or {}
    assert str(property_payload.get("id")) == KNOCK_PROPERTY_ID
    assert str(property_data.get("property_id")) == KNOCK_PROPERTY_ID
    assert location.get("name") == "Elms Stoney Run Village"
    assert address.get("street") == "7581 Stoney Run Drive"
    assert address.get("city") == "Hanover"
    assert address.get("state") == "MD"
    assert str(address.get("zip")) == "21076"
    assert str(social.get("website") or "").rstrip("/") == str(
        spec["current_url"]
    ).rstrip("/")

    units_data = units_payload.get("units_data") or {}
    raw_units = [
        row for row in units_data.get("units") or [] if isinstance(row, dict)
    ]
    layouts = [
        row for row in units_data.get("layouts") or [] if isinstance(row, dict)
    ]
    assert len(raw_units) == 22
    assert len(layouts) == 9
    assert {str(row.get("propertyId")) for row in raw_units} == {
        KNOCK_PROPERTY_ID
    }
    raw_by_unit = {str(row.get("name") or ""): row for row in raw_units}
    assert len(raw_by_unit) == 22
    rows = [dict(row) for row in parse_knock_units(units_payload, KNOCK_UNITS_URL)]
    assert len(rows) == int(spec["expected_rows"]) == 21
    for row in rows:
        raw = raw_by_unit[str(row.get("unit_number") or "")]
        assert str((row.get("source_ids") or {}).get("knock_unit_id") or "") == str(
            raw.get("id") or ""
        )
        assert str(row.get("source_property_id") or "") == KNOCK_PROPERTY_ID
        assert rent_value(row) == raw_rent_value(raw)
        assert str(row.get("availability_date") or "") == str(
            raw.get("availableOn") or ""
        )
        assert str(row.get("floor_plan_name") or "") == str(
            raw.get("layoutName") or ""
        )
        row["source_ids"] = {
            **(row.get("source_ids") or {}),
            "knock_layout_id": str(raw.get("layoutId") or ""),
            "knock_property_id": KNOCK_PROPERTY_ID,
            "knock_community_id": KNOCK_COMMUNITY_ID,
        }
    accepted_units = {str(row["unit_number"]) for row in rows}
    excluded = [row for row in raw_units if str(row.get("name") or "") not in accepted_units]
    assert len(excluded) == 1
    assert excluded[0].get("name") == "1622A"
    assert raw_rent_value(excluded[0]) is None
    assert all(
        excluded[0].get(key) is None
        for key in ("price", "displayPrice", "knockPrice")
    )
    assert len({str((row["source_ids"])["knock_unit_id"]) for row in rows}) == 21

    e2e_rows = strict_rows(current_e2e)
    assert current_e2e.get("_adapter_used") == spec["adapter"]
    assert current_e2e.get("extraction_tier_used") == spec["tier"]
    assert len(e2e_rows) == len(rows)
    assert {row_key(row) for row in rows} == {row_key(row) for row in e2e_rows}
    result = common_result(
        spec,
        rows,
        old_fetch,
        old_e2e,
        current_fetch,
        current_e2e,
        verdict=(
            "pass_exact_current_official_elms_stoney_run_knock_community_"
            "11ed3548d8dabea8_property_2017709_native_api_full_pipeline"
        ),
        source_urls=[KNOCK_UNITS_URL],
    )
    result.update(
        {
            "strict_gates": {
                "exact_current_property_identity": True,
                "exact_knock_public_key_and_community_id_on_property_page": True,
                "community_metadata_exact_name_address_zip_website": True,
                "numeric_knock_property_id_2017709": True,
                "all_rows_exact_property_id_2017709": True,
                "all_rows_unique_native_knock_uuid": True,
                "all_rows_native_unit_number": True,
                "all_rows_positive_row_level_rent": True,
                "full_pipeline_current_root_21_native_positive_rent_rows": True,
                "full_pipeline_and_direct_api_unit_rent_plan_date_match": True,
                "one_unpriced_raw_row_explicitly_excluded": True,
                "no_llm": True,
                "no_unlocker": True,
                "no_captcha_solver": True,
                "no_fingerprint_rotation": True,
            },
            "knock_direct_probe": {
                "public_key": KNOCK_PUBLIC_KEY,
                "community_id": KNOCK_COMMUNITY_ID,
                "property_id": KNOCK_PROPERTY_ID,
                "community_url": KNOCK_COMMUNITY_URL,
                "community_fetch": community_fetch,
                "units_url": KNOCK_UNITS_URL,
                "units_fetch": units_fetch,
                "raw_units": len(raw_units),
                "raw_layouts": len(layouts),
                "strict_native_positive_rent_rows": len(rows),
                "excluded_raw_rows": [
                    {
                        "unit_number": str(row.get("name") or ""),
                        "knock_unit_id": str(row.get("id") or ""),
                        "knock_layout_id": str(row.get("layoutId") or ""),
                        "floor_plan_name": str(row.get("layoutName") or ""),
                        "availability_date": str(row.get("availableOn") or ""),
                        "price": row.get("price"),
                        "displayPrice": row.get("displayPrice"),
                        "knockPrice": row.get("knockPrice"),
                        "strict_exclusion_reason": "no published positive rent",
                    }
                    for row in excluded
                ],
            },
            "floorplan_name_and_date_semantics": {
                "exact_published_floorplan_names": sorted(
                    {str(row["floor_plan_name"]) for row in rows}
                ),
                "exact_native_layout_ids": sorted(
                    {str(row["source_ids"]["knock_layout_id"]) for row in rows}
                ),
                "explicit_availability_date_rows": sum(
                    bool(row.get("availability_date")) for row in rows
                ),
                "names_are_native_knock_layoutName_values": True,
                "dates_are_native_knock_availableOn_values": True,
            },
            "contamination_negative_checks": {
                "community_api_exact_property_id": KNOCK_PROPERTY_ID,
                "all_raw_unit_property_ids": [KNOCK_PROPERTY_ID],
                "sibling_rows_admitted": 0,
                "unpriced_rows_admitted": 0,
                "direct_vs_full_pipeline_row_set_difference": [],
            },
            "current_capture": {
                **root_captures,
                "knock_community_payload": archive_json(
                    "stoney_run_knock_community.json.gz",
                    community_payload,
                ),
                "knock_units_payload": archive_json(
                    "stoney_run_knock_units.json.gz",
                    units_payload,
                ),
            },
        }
    )
    return result


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ledger_before = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    source_before = snapshot(CRITICAL_SOURCE_FILES)
    with REMAINING.open(newline="", encoding="utf-8-sig") as handle:
        remaining_ids = {
            str(row.get("property_id") or "") for row in csv.DictReader(handle)
        }
    assert set(SPECS).issubset(remaining_ids)

    reset_web_unlocker_call_count()
    fetches: dict[str, dict[str, Any]] = {}
    e2e: dict[str, dict[str, Any]] = {}
    root_captures: dict[str, dict[str, Any]] = {}
    root_bodies: dict[str, bytes] = {}
    for property_id, spec in SPECS.items():
        old_body, old_fetch, old_headers = direct_fetch(spec["configured_probe_url"])
        current_body, current_fetch, current_headers = direct_fetch(spec["current_url"])
        old_html = old_body.decode("utf-8", "replace")
        current_html = current_body.decode("utf-8", "replace")
        old_fetch["identity_checks"] = identity_checks(old_html, spec)
        current_fetch["identity_checks"] = identity_checks(current_html, spec)
        assert not identity_matches(old_html, spec)
        assert identity_matches(current_html, spec)
        old_result = await full_pipeline(
            spec,
            spec["configured_probe_url"],
            old_body,
            old_fetch,
            old_headers,
        )
        current_result = await full_pipeline(
            spec,
            spec["current_url"],
            current_body,
            current_fetch,
            current_headers,
        )
        assert strict_rows(old_result) == []
        assert len(strict_rows(current_result)) == int(spec["expected_rows"])
        assert current_result.get("_adapter_used") == spec["adapter"]
        assert current_result.get("extraction_tier_used") == spec["tier"]
        fetches[property_id] = {"old": old_fetch, "current": current_fetch}
        e2e[property_id] = {"old": old_result, "current": current_result}
        root_bodies[property_id] = current_body
        root_captures[property_id] = {
            "configured_route": archive(
                f"{property_id}_configured_route.html.gz", old_body
            ),
            "current_official_root": archive(
                f"{property_id}_current_official_root.html.gz", current_body
            ),
        }

    results = [
        portola_result(
            SPECS["246962"],
            root_bodies["246962"],
            fetches["246962"]["old"],
            e2e["246962"]["old"],
            fetches["246962"]["current"],
            e2e["246962"]["current"],
            root_captures["246962"],
        ),
        await summerwood_result(
            SPECS["37071"],
            root_bodies["37071"],
            fetches["37071"]["old"],
            e2e["37071"]["old"],
            fetches["37071"]["current"],
            e2e["37071"]["current"],
            root_captures["37071"],
        ),
        stoney_run_result(
            SPECS["42977"],
            root_bodies["42977"],
            fetches["42977"]["old"],
            e2e["42977"]["old"],
            fetches["42977"]["current"],
            e2e["42977"]["current"],
            root_captures["42977"],
        ),
    ]
    assert [result["property_id"] for result in results] == [246962, 37071, 42977]
    assert all(result["outcome"] == "UNIT_QUALIFIED" for result in results)
    assert web_unlocker_call_count() == 0

    source_after = snapshot(CRITICAL_SOURCE_FILES)
    ledger_after = {
        "ledger": sha256_path(LEDGER),
        "summary": sha256_path(SUMMARY),
        "remaining": sha256_path(REMAINING),
    }
    assert source_before == source_after
    assert ledger_before == ledger_after
    capture_timestamp = datetime.now(UTC).isoformat()
    payload = {
        "lane": "exact_current_official_migrations_three_strict",
        "summary": {
            "result_type": "stale_or_hijacked_domain_to_exact_current_official",
            "capture_timestamp_utc": capture_timestamp,
            "strict_unit_qualified_properties": len(results),
            "strict_unit_qualified_property_ids": [
                result["property_id"] for result in results
            ],
            "native_positive_rent_rows": sum(result["units"] for result in results),
            "unit_rows_by_property": {
                str(result["property_id"]): result["units"] for result in results
            },
            "configured_routes_with_strict_native_positive_rent": 0,
            "current_official_routes_with_strict_native_positive_rent": 3,
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "unlocker": False,
            "proxies": {},
            "hyperbrowser": False,
            "llm_used": False,
            "paid_canary_run": False,
        },
        "results": results,
        "provenance": {
            "validation_scope": "local exact-current validation; not a canary",
            "critical_source_files_before": source_before,
            "critical_source_files_after": source_after,
            "critical_source_files_unchanged": True,
            "ledger_snapshot_before": ledger_before,
            "ledger_snapshot_after": ledger_after,
            "ledger_unchanged_during_materialization": True,
            "transport_policy": {
                "compliance_mode": True,
                "llm_calls": 0,
                "web_unlocker_calls": web_unlocker_call_count(),
                "captcha_interactions": 0,
                "flaresolverr": False,
                "fingerprint_rotation": False,
                "paid_canary_run": False,
            },
        },
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256_path(EVIDENCE),
                "strict_unit_qualified_property_ids": [
                    result["property_id"] for result in results
                ],
                "unit_rows_by_property": {
                    str(result["property_id"]): result["units"]
                    for result in results
                },
                "total_native_positive_rent_rows": sum(
                    result["units"] for result in results
                ),
                "web_unlocker_calls": web_unlocker_call_count(),
                "ledger_unchanged": ledger_before == ledger_after,
                "source_unchanged": source_before == source_after,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
