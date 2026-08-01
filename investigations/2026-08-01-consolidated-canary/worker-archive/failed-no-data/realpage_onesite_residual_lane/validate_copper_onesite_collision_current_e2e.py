from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.knock import find_published_onesite_portals
from ma_poc.pms.adapters.onesite import (
    _XYZ_USER_AGENT,
    _generate_xyz_token,
    _onesite_workflowstartup_url,
)
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
SOURCE = Path("ma_poc/pms/adapters/knock.py")
OUTPUT = LANE / "evidence_copper_onesite_collision_current_e2e.json"
PROPERTY_ID = 261116
EXPECTED_PORTAL = "https://9131096aff.onlineleasing.realpage.com/"
EXPECTED_SITE_ID = "4629273"
SESSION_RE = re.compile(r"(ClientSessionID=)[^&\"'\s]+", re.IGNORECASE)
SITE_ID_RE = re.compile(r"/ollr/widgetLoader\.js\?siteId=(\d+)", re.IGNORECASE)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def sanitize_url(value: str) -> str:
    return SESSION_RE.sub(r"\1<redacted>", str(value or ""))


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(key), (int, float))
        and not isinstance(unit.get(key), bool)
        and unit.get(key) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


def page_identity(row: dict[str, str], body: str) -> dict[str, object]:
    visible = normalize(BeautifulSoup(body, "lxml").get_text(" ", strip=True))
    tokens = set(visible.split())
    name = normalize(row.get("name") or "")
    address_tokens = normalize(row.get("address") or "").split()
    ignored = {
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
        "blvd",
        "boulevard",
    }
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token
        for token in address_tokens[1:]
        if token not in ignored and not token.isdigit()
    ]
    return {
        "canonical_name": row.get("name") or "",
        "canonical_address": row.get("address") or "",
        "canonical_city": row.get("city") or "",
        "canonical_state": row.get("state") or "",
        "canonical_zip": row.get("zip") or "",
        "name_visible_exact_normalized": bool(name and name in visible),
        "street_number_and_words_visible": bool(
            street_number
            and street_number in tokens
            and street_words
            and all(token in tokens for token in street_words)
        ),
        "city_visible": normalize(row.get("city") or "") in visible,
        "state_visible": normalize(row.get("state") or "") in tokens,
        "zip_visible": str(row.get("zip") or "") in tokens,
    }


def workflow_unit_ids(payload: object) -> set[str]:
    ids: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            unit_ids = value.get("UnitIds")
            if isinstance(unit_ids, list):
                ids.update(str(item) for item in unit_ids if str(item).strip())
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return ids


def workflow_probe(site_id: str, portal: str) -> dict[str, object]:
    raw_url = _onesite_workflowstartup_url(site_id)
    parsed = urlparse(portal)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": _XYZ_USER_AGENT,
        "xyz": _generate_xyz_token(site_id, _XYZ_USER_AGENT),
        "X-AuthToken": "",
        "X-Phased": "",
    }
    response = curl_requests.get(
        raw_url,
        headers=headers,
        timeout=20,
        impersonate="chrome116",
    )
    payload = response.json() if response.text else {}
    workflow = payload.get("Workflow") if isinstance(payload, dict) else {}
    unit_ids = workflow_unit_ids(workflow)
    return {
        "status": int(response.status_code or 0),
        "payload_site_id": (
            str(workflow.get("SiteId") or "") if isinstance(workflow, dict) else ""
        ),
        "unit_ids": unit_ids,
        "sanitized_url": sanitize_url(raw_url),
        "fixed_tls_fingerprint": "chrome116",
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
    for key, expected in expected_env.items():
        if os.environ.get(key, "").lower() != expected:
            raise RuntimeError(f"{key} must be {expected!r}")

    metadata = {
        int(row["apartmentid"]): row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    row = metadata[PROPERTY_ID]
    ledger_rows = read_csv(LEDGER)
    remaining_rows = read_csv(REMAINING)
    ledger_ids = {int(item["property_id"]) for item in ledger_rows}
    remaining_ids = {int(item["property_id"]) for item in remaining_rows}
    if PROPERTY_ID in ledger_ids or PROPERTY_ID not in remaining_ids:
        raise RuntimeError("Copper must be net-new against the captured ledger")

    configured_url = row["website"]
    response = await asyncio.to_thread(
        probe_get,
        configured_url,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    body = str(response.text or "")
    portals = find_published_onesite_portals(body)
    if portals != [EXPECTED_PORTAL]:
        raise RuntimeError(f"unexpected current portal set: {portals}")

    portal_response = await asyncio.to_thread(
        probe_get,
        EXPECTED_PORTAL,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    portal_body = html.unescape(str(portal_response.text or "")).replace("\\/", "/")
    site_ids = sorted(set(SITE_ID_RE.findall(portal_body)))
    if site_ids != [EXPECTED_SITE_ID]:
        raise RuntimeError(f"unexpected current portal SiteIds: {site_ids}")

    fetch_result = FetchResult(
        url=configured_url,
        outcome=FetchOutcome.OK,
        status=int(response.status_code or 0),
        body=body.encode(),
        headers=dict(response.headers or {}),
        render_mode=RenderMode.GET,
        final_url=str(response.url or configured_url),
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=configured_url,
        property_id=str(PROPERTY_ID),
        priority=0,
        budget_ms=45_000,
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
    strict_units = [
        unit
        for unit in (result.get("units") or [])
        if isinstance(unit, dict)
        and unit_has_real_anchor(unit)
        and positive_rent(unit)
        and str(unit.get("unit_number") or "").strip()
    ]
    output_ids = {str(unit.get("unit_number") or "").strip() for unit in strict_units}
    output_property_ids = {
        str(unit.get("source_property_id") or "").strip() for unit in strict_units
    }
    output_urls = {
        sanitize_url(str(unit.get("source_api_url") or "")) for unit in strict_units
    }
    workflow = workflow_probe(EXPECTED_SITE_ID, EXPECTED_PORTAL)
    raw_ids = set(workflow.pop("unit_ids"))
    identity = page_identity(row, body)
    gates = {
        "configured_http_200": int(response.status_code or 0) == 200,
        "configured_exact_final_host": (
            (urlparse(str(response.url or "")).hostname or "").removeprefix("www.")
            == "copperpointeapts.com"
        ),
        "configured_name_visible": identity["name_visible_exact_normalized"],
        "configured_street_visible": identity["street_number_and_words_visible"],
        "configured_city_visible": identity["city_visible"],
        "configured_state_visible": identity["state_visible"],
        "configured_zip_visible": identity["zip_visible"],
        "sole_published_portal": portals == [EXPECTED_PORTAL],
        "portal_http_200": int(portal_response.status_code or 0) == 200,
        "portal_publishes_sole_expected_site_id": site_ids == [EXPECTED_SITE_ID],
        "workflow_http_200": workflow["status"] == 200,
        "workflow_payload_site_id_exact": workflow["payload_site_id"]
        == EXPECTED_SITE_ID,
        "current_detector_knock": (result.get("_detected_pms") or {}).get("pms")
        == "knock",
        "current_adapter_knock": result.get("_adapter_used") == "knock",
        "current_tier_onesite_workflow": result.get("extraction_tier_used")
        == "TIER_1_API_ONESITE_WORKFLOW",
        "current_pipeline_no_errors": not (result.get("errors") or []),
        "current_pipeline_no_llm": not (result.get("_llm_interactions") or []),
        "all_output_rows_native_positive": bool(
            strict_units and len(strict_units) == len(result.get("units") or [])
        ),
        "output_unit_numbers_unique": len(output_ids) == len(strict_units),
        "output_property_id_exact": output_property_ids == {EXPECTED_SITE_ID},
        "output_source_url_exact_workflow": bool(
            output_urls
            and all(
                f"/workflowstartup/v1/{EXPECTED_SITE_ID}/" in value
                for value in output_urls
            )
        ),
        "raw_workflow_has_native_units": bool(raw_ids),
        "output_native_ids_equal_raw_workflow": output_ids == raw_ids,
    }
    passed = bool(gates and all(gates.values()))
    if not passed:
        failed = sorted(key for key, value in gates.items() if not value)
        raise RuntimeError(f"Copper strict gates failed: {failed}")

    units = [
        {
            "unit_number": str(unit.get("unit_number") or ""),
            "provider_unit_id": str(unit.get("unit_number") or ""),
            "floor_plan_name": str(unit.get("floor_plan_name") or ""),
            "rent": unit.get("market_rent_low") or unit.get("market_rent_high"),
            "availability_date": str(
                unit.get("availability_date") or unit.get("available_date") or ""
            ),
            "source_property_id": str(unit.get("source_property_id") or ""),
            "source_url": sanitize_url(str(unit.get("source_api_url") or "")),
        }
        for unit in strict_units
    ]
    payload = {
        "batch_label": "copper-onesite-collision-current-source-configured-e2e",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ledger_snapshot": {
            "path": str(LEDGER),
            "rows": len(ledger_rows),
            "sha256": sha256(LEDGER),
            "remaining_path": str(REMAINING),
            "remaining_rows": len(remaining_rows),
            "remaining_sha256": sha256(REMAINING),
            "net_new_ids": [PROPERTY_ID],
        },
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "workflow_transport": "fixed chrome116 curl_cffi, no rotation",
            "production_source_modified": True,
        },
        "source": {"path": str(SOURCE), "sha256": sha256(SOURCE)},
        "recoveries": [
            {
                "property_id": PROPERTY_ID,
                "property_name": row.get("name") or "",
                "website": configured_url,
                "strict_verdict": (
                    "pass_exact_configured_sole_published_onesite_portal_"
                    "native_workflow_units_after_empty_knock"
                ),
                "native_identity_rows": len(units),
                "native_positive_rent_rows": len(units),
                "source_urls": [
                    configured_url,
                    EXPECTED_PORTAL,
                    str(workflow["sanitized_url"]),
                ],
                "current_full_pipeline": {
                    "detected_pms": "knock",
                    "adapter": "knock",
                    "tier": result.get("extraction_tier_used") or "",
                    "strict_native_positive_rent_rows": len(units),
                    "errors": result.get("errors") or [],
                },
                "property_boundary_evidence": {
                    **identity,
                    "configured_final_url": str(response.url or configured_url),
                    "sole_published_portal": EXPECTED_PORTAL,
                    "portal_site_id": EXPECTED_SITE_ID,
                    "workflow_probe": workflow,
                    "raw_native_unit_count": len(raw_ids),
                    "gates": gates,
                },
                "units": units,
            }
        ],
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if re.search(r"ClientSessionID=(?!<redacted>)", serialized, re.IGNORECASE):
        raise RuntimeError("unsanitized workflow session id")
    OUTPUT.write_text(serialized)
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "net_new_ids": [PROPERTY_ID],
                "strict_rows": len(units),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
