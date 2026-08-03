from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
FULL_SCAN = LANE / "scan_remaining_current_pipeline.json"
OUTPUT = LANE / "evidence_edgewater_entrata_current_strict_replay.json"
PROPERTY_ID = "48092"


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


def street_matches(canonical: object, source: object) -> bool:
    ignored = {
        "n", "s", "e", "w", "ne", "nw", "se", "sw",
        "north", "south", "east", "west", "st", "street",
        "rd", "road", "ave", "avenue", "dr", "drive",
        "cir", "circle", "blvd", "boulevard", "ln", "lane",
    }
    canonical_tokens = normalize(canonical).split()
    source_tokens = set(normalize(source).split())
    if not canonical_tokens:
        return False
    number = canonical_tokens[0]
    core = {
        token
        for token in canonical_tokens[1:]
        if token not in ignored and len(token) > 1
    }
    return bool(number in source_tokens and core and core <= source_tokens)


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


def direct_fetch(url: str) -> FetchResult:
    response = probe_get(url, timeout=30, unlocker=False, retries=1)
    status = int(response.status_code or 0)
    body = (response.text or "").encode()
    return FetchResult(
        url=url,
        outcome=(
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.HARD_FAIL
        ),
        status=status,
        body=body,
        headers={},
        render_mode=RenderMode.GET,
        final_url=str(response.url or url),
        attempts=1,
        elapsed_ms=0,
    )


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

    canonical = next(
        row for row in read_csv(PROPERTIES) if row.get("apartmentid") == PROPERTY_ID
    )
    ledger_ids = {row["property_id"] for row in read_csv(LEDGER)}
    if PROPERTY_ID in ledger_ids:
        raise RuntimeError(f"{PROPERTY_ID} already exists in captured ledger")

    scan_payload = json.loads(FULL_SCAN.read_text())
    scan_row = next(
        row
        for row in scan_payload.get("results") or []
        if str(row.get("property_id") or "") == PROPERTY_ID
    )
    configured_capture_gates = {
        "scan_guardrails_all_prohibited_paths_off": all(
            scan_payload.get("guardrails", {}).get(key) is False
            for key in (
                "llm_enabled", "web_unlocker", "flaresolverr", "hyperbrowser",
                "captcha_solving", "fingerprint_rotation", "paid_canary",
                "shared_ledger_modified", "shared_source_modified",
            )
        ),
        "configured_capture_http_200": scan_row.get("configured_status") == 200,
        "configured_capture_adapter_entrata": scan_row.get("adapter") == "entrata",
        "configured_capture_tier_unit_level": scan_row.get("tier")
        == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
        "configured_capture_native_positive_rows": int(
            scan_row.get("strict_native_priced_rows") or 0
        )
        == 23,
        "configured_capture_no_errors": not (scan_row.get("errors") or []),
        "configured_capture_exact_final_host": (
            urlparse(str(scan_row.get("configured_final_url") or "")).hostname or ""
        ).lower()
        == "www.edgewatervillage-apts.com",
    }

    configured_fetch = await asyncio.to_thread(direct_fetch, canonical["website"])
    configured_body = (configured_fetch.body or b"").decode("utf-8", "replace")
    soup = BeautifulSoup(configured_body, "lxml")
    visible = normalize(soup.get_text(" ", strip=True))
    route_candidates = {
        urljoin(configured_fetch.final_url, str(tag.get("href") or ""))
        for tag in soup.find_all("a", href=True)
        if "/greensboro/edgewater-village/conventional/"
        in str(tag.get("href") or "").lower()
    } | {
        urljoin(configured_fetch.final_url, str(tag.get("action") or ""))
        for tag in soup.find_all("form", action=True)
        if "/greensboro/edgewater-village/conventional/"
        in str(tag.get("action") or "").lower()
    }
    if len(route_candidates) != 1:
        raise RuntimeError(f"Expected one exact conventional route: {route_candidates}")
    route_url = next(iter(route_candidates))
    route_fetch = await asyncio.to_thread(direct_fetch, route_url)
    route_body = (route_fetch.body or b"").decode("utf-8", "replace")
    result = await scraper_mod.scrape(
        route_url,
        page=None,
        fetch_result=route_fetch,
        csv_row=canonical,
        property_id=PROPERTY_ID,
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )
    emitted = [item for item in result.get("units") or [] if isinstance(item, dict)]
    qualified = [
        item for item in emitted if unit_has_real_anchor(item) and positive_rent(item)
    ]
    native_ids = {
        str((item.get("source_ids") or {}).get("entrata_uid") or "")
        for item in qualified
    }
    floorplan_ids = {
        str((item.get("source_ids") or {}).get("entrata_fpid") or "")
        for item in qualified
    }
    replay_gates = {
        "configured_live_http_200": configured_fetch.status == 200,
        "configured_live_exact_final_host": (
            urlparse(configured_fetch.final_url).hostname or ""
        ).lower()
        == "www.edgewatervillage-apts.com",
        "configured_live_name_visible": name_key(canonical["name"]) in name_key(visible),
        "configured_live_street_exact_normalized": street_matches(
            canonical["address"], visible
        ),
        "configured_live_city_visible": normalize(canonical["city"]) in visible,
        "configured_live_state_visible": normalize(canonical["state"]) in visible,
        "configured_live_zip_visible": normalize(canonical["zip"]) in visible,
        "configured_live_publishes_sole_exact_route": len(route_candidates) == 1,
        "published_route_http_200": route_fetch.status == 200,
        "published_route_adapter_entrata": result.get("_adapter_used") == "entrata",
        "published_route_tier_unit_level": result.get("extraction_tier_used")
        == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
        "published_route_no_errors": not (result.get("errors") or []),
        "all_emitted_rows_strict_native_positive": len(qualified) == len(emitted) > 0,
        "all_native_ids_present": bool(native_ids) and "" not in native_ids,
        "all_floorplan_ids_present": bool(floorplan_ids) and "" not in floorplan_ids,
        "all_unit_numbers_present": all(
            str(item.get("unit_number") or "").strip() for item in qualified
        ),
        "all_source_urls_exact_published_route": all(
            str(item.get("source_api_url") or "").rstrip("/")
            == route_url.rstrip("/")
            for item in qualified
        ),
        "every_native_id_floorplan_id_and_unit_replayed_in_route_body": all(
            str((item.get("source_ids") or {}).get("entrata_uid") or "") in route_body
            and str((item.get("source_ids") or {}).get("entrata_fpid") or "") in route_body
            and str(item.get("unit_number") or "") in route_body
            for item in qualified
        ),
    }
    all_gates = {**configured_capture_gates, **replay_gates}
    if not all(all_gates.values()):
        raise RuntimeError(
            "Edgewater strict gates failed: "
            + json.dumps({key: value for key, value in all_gates.items() if not value})
        )

    recovery = {
        "property_id": int(PROPERTY_ID),
        "property_name": canonical["name"],
        "website": canonical["website"],
        "strict_verdict": (
            "pass_exact_configured_current_e2e_capture_and_published_route_replay"
        ),
        "native_identity_rows": len(qualified),
        "native_positive_rent_rows": len(qualified),
        "source_urls": [canonical["website"], configured_fetch.final_url, route_url],
        "property_boundary_evidence": {
            "canonical_address": canonical["address"],
            "canonical_city": canonical["city"],
            "canonical_state": canonical["state"],
            "canonical_zip": canonical["zip"],
            "configured_final_url": configured_fetch.final_url,
            "published_route": route_url,
            "full_configured_capture_artifact": str(FULL_SCAN),
            "full_configured_capture_sha256": sha256(FULL_SCAN),
            "full_configured_capture_generated_at_utc": scan_payload.get(
                "generated_at_utc"
            ),
            "gates": all_gates,
        },
        "current_full_pipeline_capture": scan_row,
        "current_published_route_replay": {
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
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
                "source_ids": item.get("source_ids") or {},
            }
            for item in qualified
        ],
    }
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_label": "edgewater-entrata-current-configured-capture-route-replay",
        "ledger_snapshot": {
            "path": str(LEDGER),
            "sha256": sha256(LEDGER),
            "rows": len(read_csv(LEDGER)),
            "net_new_ids": [int(PROPERTY_ID)],
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
        "recoveries": [recovery],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "ledger_rows": payload["ledger_snapshot"]["rows"],
                "net_new_ids": payload["ledger_snapshot"]["net_new_ids"],
                "strict_native_positive_rent_rows": len(qualified),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
