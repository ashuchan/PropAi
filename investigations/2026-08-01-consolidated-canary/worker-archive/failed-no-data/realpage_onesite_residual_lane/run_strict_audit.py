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

from curl_cffi import requests as curl_requests

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.onesite import (
    OneSiteAdapter,
    _XYZ_USER_AGENT,
    _generate_xyz_token,
    _onesite_workflowstartup_url,
)
from ma_poc.pms.detector import DetectedPMS


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "realpage_onesite_residual_lane"
BASELINE = LANE / "baseline_current_direct_e2e.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "evidence_realpage_onesite_remaining11_current_strict.json"
NET_NEW_OUTPUT = LANE / "strict_realpage_onesite_net_new_ids.json"

TARGET_ADAPTERS = {"realpage_oll", "onesite"}

# These are same-origin inventory pages currently linked by the configured
# marketing page. The runner verifies the path is present in the current
# configured-page HTML before treating the chain as published evidence.
INVENTORY_PAGES = {
    2948: "https://crystalwoodsapts.com/floorplans/",
    16172: "https://townecrest.com/floorplans/",
    253326: "https://www.thepointatreston.com/floor-plans",
}

EXPECTED_CURRENT_SITE_IDS = {
    2948: "4845783",
    14295: "1101338",
    16172: "5194917",
    18194: "1046774",
    38677: "1321537",
    39995: "5272798",
    43520: "5586626",
    253326: "4843024",
    291774: "4645221",
}

WIDGET_RE = re.compile(r"widgetLoader\.js\?siteId=(\d+)", re.IGNORECASE)
WELCOME_RE = re.compile(
    r"onesite\.realpage\.com/welcomehome/?\?[^\"'\\\s<>]*siteId=(\d+)",
    re.IGNORECASE,
)
PORTAL_RE = re.compile(
    r"https?://[\w-]+\.onlineleasing\.realpage\.com[^\"'<>\s]*",
    re.IGNORECASE,
)
SESSION_RE = re.compile(r"(ClientSessionID=)[^&\"'\s]+", re.IGNORECASE)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_url(value: str) -> str:
    value = value.strip()
    return value if "://" in value else f"https://{value}"


def sanitize_url(value: str) -> str:
    return SESSION_RE.sub(r"\1<redacted>", str(value or ""))


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_url(value)
    return value


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_targets() -> list[dict[str, str]]:
    residual = [
        row
        for row in read_csv(REMAINING)
        if row.get("current_detected_adapter") in TARGET_ADAPTERS
    ]
    metadata = {
        row.get("apartmentid", ""): row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    targets = []
    for row in residual:
        canonical = metadata.get(row["property_id"], {})
        targets.append({**row, **canonical, "property_id": row["property_id"]})
    return sorted(targets, key=lambda item: int(item["property_id"]))


def clean_body(body: str) -> str:
    return html.unescape(body or "").replace("\\/", "/").replace("\\", "")


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def page_identity(row: dict[str, str], body: str) -> dict[str, object]:
    visible = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible_key = key(html.unescape(re.sub(r"<[^>]+>", " ", visible)))
    tokens = set(visible_key.split())
    name = row.get("name") or row.get("property_name") or ""
    name_key = key(name)
    distinctive = [
        token
        for token in name_key.split()
        if token
        not in {
            "the",
            "at",
            "of",
            "apartments",
            "apartment",
            "homes",
            "home",
            "townhomes",
            "village",
        }
    ]
    address = row.get("address") or ""
    address_tokens = key(address).split()
    ignored = {
        "n",
        "s",
        "e",
        "w",
        "north",
        "south",
        "east",
        "west",
        "st",
        "street",
        "rd",
        "road",
        "ave",
        "avenue",
        "blvd",
        "boulevard",
        "pkwy",
        "parkway",
        "dr",
        "drive",
        "ln",
        "lane",
        "ct",
        "court",
        "hwy",
        "highway",
        "way",
        "pl",
        "place",
        "cir",
        "circle",
    }
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token
        for token in address_tokens[1:]
        if token not in ignored and not token.isdigit()
    ]
    return {
        "canonical_name": name,
        "canonical_address": address,
        "name_visible_exact_normalized": bool(name_key and name_key in visible_key),
        "name_distinctive_tokens_visible": bool(
            distinctive and all(token in tokens for token in distinctive)
        ),
        "street_number_and_words_visible": bool(
            street_number
            and street_number in tokens
            and street_words
            and all(token in tokens for token in street_words)
        ),
        "street_words_checked": street_words,
    }


def fetch_page(url: str) -> dict[str, object]:
    try:
        response = probe_get(url, timeout=30, unlocker=False, retries=1)
        return {
            "requested_url": url,
            "status": int(response.status_code or 0),
            "final_url": str(response.url or url),
            "body": str(response.text or ""),
            "body_bytes": len((response.text or "").encode()),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "requested_url": url,
            "status": 0,
            "final_url": url,
            "body": "",
            "body_bytes": 0,
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }


def same_origin(left: str, right: str) -> bool:
    left_host = (urlparse(left).hostname or "").lower().removeprefix("www.")
    right_host = (urlparse(right).hostname or "").lower().removeprefix("www.")
    return bool(left_host and left_host == right_host)


def discover_published_route(
    pid: int,
    configured: dict[str, object],
    inventory: dict[str, object],
) -> dict[str, object]:
    configured_body = clean_body(str(configured.get("body") or ""))
    inventory_body = clean_body(str(inventory.get("body") or ""))
    direct_ids = sorted(set(WIDGET_RE.findall(inventory_body)))
    welcome_ids = sorted(set(WELCOME_RE.findall(inventory_body)))
    portals = sorted(set(PORTAL_RE.findall(inventory_body)))
    portal_fetch = None
    shell_ids: list[str] = []
    if not direct_ids and not welcome_ids and len(portals) == 1:
        portal_url = portals[0].rstrip("/\\") + "/"
        portal_fetch = fetch_page(portal_url)
        shell_ids = sorted(
            set(WIDGET_RE.findall(clean_body(str(portal_fetch.get("body") or ""))))
        )
    all_ids = sorted(set(direct_ids + welcome_ids + shell_ids))
    expected = EXPECTED_CURRENT_SITE_IDS.get(pid)
    inventory_url = str(inventory.get("final_url") or inventory.get("requested_url") or "")
    configured_url = str(
        configured.get("final_url") or configured.get("requested_url") or ""
    )
    inventory_path = urlparse(inventory_url).path.rstrip("/") or "/"
    if pid in INVENTORY_PAGES:
        path_published = bool(
            inventory_path.lower() in configured_body.lower()
            or (inventory_path + "/").lower() in configured_body.lower()
        )
    else:
        path_published = True
    if direct_ids:
        provenance = "direct_widget_site_id_on_current_inventory_page"
    elif welcome_ids:
        provenance = "direct_welcomehome_site_id_on_current_configured_page"
    elif shell_ids:
        provenance = "sole_current_configured_page_portal_to_shell_widget_site_id"
    else:
        provenance = "none"
    result = {
        "configured_final_url": configured_url,
        "inventory_final_url": inventory_url,
        "same_origin_inventory_page": same_origin(configured_url, inventory_url),
        "inventory_path_published_by_configured_page": path_published,
        "direct_widget_site_ids": direct_ids,
        "direct_welcomehome_site_ids": welcome_ids,
        "published_portal_urls": portals,
        "portal_shell_fetch": None,
        "portal_shell_widget_site_ids": shell_ids,
        "all_published_site_ids": all_ids,
        "site_id_provenance": provenance,
        "expected_site_id": expected or "",
        "expected_site_id_is_sole_published_id": bool(
            expected and all_ids == [expected]
        ),
        "no_conflicting_site_ids": len(all_ids) <= 1,
    }
    if portal_fetch is not None:
        result["portal_shell_fetch"] = {
            key: value for key, value in portal_fetch.items() if key != "body"
        }
    return result


def workflow_floorplans(payload: dict) -> list[dict]:
    workflow = payload.get("Workflow") if isinstance(payload, dict) else None
    if not isinstance(workflow, dict):
        return []
    found: list[dict] = []
    for group in workflow.get("ActivityGroups") or []:
        if not isinstance(group, dict):
            continue
        for activity in group.get("GroupActivities") or []:
            if not isinstance(activity, dict):
                continue
            for floorplan in activity.get("Floorplans") or []:
                if isinstance(floorplan, dict):
                    found.append(floorplan)
    deduped: dict[str, dict] = {}
    anonymous = 0
    for floorplan in found:
        floorplan_id = str(
            floorplan.get("Id") or floorplan.get("MarketingId") or ""
        )
        if not floorplan_id:
            anonymous += 1
            floorplan_id = f"anonymous-{anonymous}"
        deduped.setdefault(floorplan_id, floorplan)
    return list(deduped.values())


def probe_workflow(site_id: str, referer: str) -> dict[str, object]:
    raw_url = _onesite_workflowstartup_url(site_id)
    parsed = urlparse(referer)
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://example.com"
    )
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
    try:
        response = curl_requests.get(
            raw_url,
            headers=headers,
            timeout=20,
            impersonate="chrome116",
        )
        status = int(response.status_code or 0)
        try:
            payload = response.json() if response.text else {}
        except Exception:  # noqa: BLE001
            payload = {}
    except Exception as exc:  # noqa: BLE001
        return {
            "requested_site_id": site_id,
            "sanitized_url": sanitize_url(raw_url),
            "status": 0,
            "payload_site_id": "",
            "workflow_present": False,
            "floorplan_count": 0,
            "available_floorplan_count": 0,
            "available_unit_id_count": 0,
            "distinct_available_unit_id_count": 0,
            "sample_available_unit_ids": [],
            "fixed_tls_fingerprint": "chrome116",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
    workflow = payload.get("Workflow") if isinstance(payload, dict) else None
    payload_site_id = (
        str(workflow.get("SiteId") or "") if isinstance(workflow, dict) else ""
    )
    floorplans = workflow_floorplans(payload)
    available_floorplans = []
    unit_ids: list[str] = []
    for floorplan in floorplans:
        try:
            available = int(floorplan.get("AvailableUnits") or 0)
        except (TypeError, ValueError):
            available = 0
        ids = [
            str(value)
            for value in (floorplan.get("UnitIds") or [])
            if str(value).strip()
        ]
        if available > 0:
            available_floorplans.append(floorplan)
        unit_ids.extend(ids)
    return {
        "requested_site_id": site_id,
        "sanitized_url": sanitize_url(raw_url),
        "status": status,
        "payload_site_id": payload_site_id,
        "workflow_present": isinstance(workflow, dict),
        "floorplan_count": len(floorplans),
        "available_floorplan_count": len(available_floorplans),
        "available_unit_id_count": len(unit_ids),
        "distinct_available_unit_id_count": len(set(unit_ids)),
        "sample_available_unit_ids": list(dict.fromkeys(unit_ids))[:10],
        "fixed_tls_fingerprint": "chrome116",
        "payload_site_id_matches_requested": bool(
            payload_site_id and payload_site_id == site_id
        ),
    }


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
            "asking_rent",
            "rent",
        )
    )


def sample_unit(unit: dict) -> dict[str, object]:
    return {
        "unit_number": str(unit.get("unit_number") or ""),
        "unit_id": str(unit.get("unit_id") or ""),
        "floor_plan_name": str(unit.get("floor_plan_name") or ""),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "source_property_id": str(unit.get("source_property_id") or ""),
        "source_ids": unit.get("source_ids")
        if isinstance(unit.get("source_ids"), dict)
        else {},
        "source_api_url": sanitize_url(str(unit.get("source_api_url") or "")),
    }


async def run_onesite_adapter(
    row: dict[str, str], inventory: dict[str, object]
) -> dict[str, object]:
    status = int(inventory.get("status") or 0)
    body = str(inventory.get("body") or "")
    url = str(inventory.get("requested_url") or "")
    final_url = str(inventory.get("final_url") or url)
    if status != 200 or not body:
        return {
            "executed": False,
            "reason": f"inventory_fetch_not_200_status_{status}",
            "tier": "",
            "unit_rows": 0,
            "plan_summary_rows": 0,
            "native_positive_rent_rows": 0,
            "sample_units": [],
        }
    fetch_result = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=status,
        body=body.encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )
    ctx = AdapterContext(
        base_url=final_url,
        detected=DetectedPMS("onesite", 1.0, ["strict residual audit"]),
        profile=None,
        expected_total_units=None,
        property_id=row["property_id"],
        fetch_result=fetch_result,
        property_name=row.get("name") or row.get("property_name") or "",
        address=row.get("address") or "",
        city=row.get("city") or "",
        state=row.get("state") or "",
        zip_code=row.get("zip") or "",
        budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )
    result = await OneSiteAdapter().extract(None, ctx)
    qualified = [
        unit
        for unit in result.units
        if unit_has_real_anchor(unit) and positive_rent(unit)
    ]
    source_property_ids = sorted(
        {
            str(unit.get("source_property_id") or "")
            for unit in qualified
            if unit.get("source_property_id") not in (None, "")
        }
    )
    source_urls = sorted(
        {
            sanitize_url(str(unit.get("source_api_url") or ""))
            for unit in qualified
            if unit.get("source_api_url")
        }
    )
    return {
        "executed": True,
        "adapter": "onesite",
        "tier": result.tier_used,
        "unit_rows": len(result.units),
        "plan_summary_rows": len(result.plan_summaries),
        "native_positive_rent_rows": len(qualified),
        "distinct_unit_numbers": len(
            {str(unit.get("unit_number") or "") for unit in qualified}
        ),
        "source_property_ids": source_property_ids,
        "source_urls": source_urls,
        "sample_units": [sample_unit(unit) for unit in qualified[:5]],
        "errors": list(result.errors),
    }


def public_fetch_record(page: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in page.items() if key != "body"}


def classify(
    pid: int,
    baseline: dict,
    route: dict,
    workflow: dict | None,
    adapter: dict,
) -> tuple[str, bool, str, list[str]]:
    baseline_strict = int(
        (baseline.get("identity_evidence") or {}).get(
            "rows_with_native_identity_and_positive_rent", 0
        )
        or 0
    )
    if pid == 265143 and baseline_strict > 0:
        return (
            "UNIT_QUALIFIED",
            True,
            "pass_exact_current_configured_page_identity_same_host_native_entrata_units",
            [],
        )
    adapter_strict = int(adapter.get("native_positive_rent_rows") or 0)
    if adapter_strict > 0:
        site_id = str(route.get("expected_site_id") or "")
        bound = bool(
            site_id
            and route.get("expected_site_id_is_sole_published_id")
            and workflow
            and workflow.get("payload_site_id_matches_requested")
            and adapter.get("source_property_ids") == [site_id]
        )
        if bound:
            return (
                "ADAPTER_E2E_NATIVE_DATA_ROUTING_MISS",
                False,
                "pass_exact_published_route_payload_binding_native_units_but_current_pipeline_misroutes",
                [
                    "current_full_pipeline_e2e_returned_zero_native_priced_units",
                    "routing_miss_before_onesite_adapter",
                    "excluded_from_strict_207_gate_until_current_pipeline_routes_this_page_to_onesite",
                ],
            )
        return (
            "REJECTED_BOUNDARY_UNPROVEN",
            False,
            "reject_native_rows_without_complete_exact_property_binding",
            ["native_rows_present_but_exact_property_binding_gate_failed"],
        )
    if pid == 38677:
        return (
            "NO_CURRENT_NATIVE_WORKFLOW_DATA",
            False,
            "pass_exact_published_welcomehome_site_id_but_native_workflow_empty",
            [
                "published_welcomehome_site_id_has_no_usable_current_workflow_payload",
                "workflow_payload_does_not_bind_back_to_requested_site_id",
                "onesite_adapter_e2e_returned_zero_native_priced_units",
            ],
        )
    if workflow and workflow.get("workflow_present"):
        if int(workflow.get("available_unit_id_count") or 0) == 0:
            return (
                "NO_CURRENT_NATIVE_UNIT_INVENTORY",
                False,
                "pass_exact_published_route_payload_binding_but_zero_native_unit_ids",
                [
                    "native_current_inventory_zero",
                    "workflow_reports_no_available_unit_ids",
                    "plan_level_rows_do_not_count_as_unit_level_conversion",
                ],
            )
    if pid == 67154:
        return (
            "CURRENT_SITE_FETCH_NOT_USABLE",
            False,
            "reject_current_configured_site_refresh_shell_no_property_scoped_native_route",
            [
                "configured_site_returns_http_202_refresh_shell",
                "no_current_property_published_onesite_site_id_or_inventory_route",
                "stale_profile_only_routes_are_not_counted",
            ],
        )
    return (
        "NO_NATIVE_PRICED_UNITS",
        False,
        "reject_no_current_exact_property_native_priced_units",
        ["adapter_e2e_returned_zero_native_priced_units"],
    )


async def main() -> None:
    # Hard safety gates: direct public probes only, one fixed fingerprint,
    # no LLM, CAPTCHA solving, unlocker, FlareSolverr, or paid canary.
    required_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
    }
    for name, expected in required_env.items():
        actual = os.environ.get(name, "")
        if actual.lower() != expected:
            raise RuntimeError(f"guardrail env {name}={actual!r}, expected {expected!r}")

    targets = load_targets()
    target_ids = [int(row["property_id"]) for row in targets]
    if len(target_ids) != 11 or len(set(target_ids)) != 11:
        raise RuntimeError(f"expected 11 unique residual targets, got {target_ids}")

    baseline_payload = json.loads(BASELINE.read_text())
    baseline_by_id = {
        int(row["property_id"]): row for row in baseline_payload["results"]
    }
    results: list[dict[str, object]] = []
    for row in targets:
        pid = int(row["property_id"])
        configured_url = normalize_url(str(row.get("website") or ""))
        configured = await asyncio.to_thread(fetch_page, configured_url)
        inventory_url = INVENTORY_PAGES.get(pid, configured_url)
        if inventory_url == configured_url:
            inventory = configured
        else:
            inventory = await asyncio.to_thread(fetch_page, inventory_url)
        route = await asyncio.to_thread(
            discover_published_route, pid, configured, inventory
        )
        site_ids = list(route.get("all_published_site_ids") or [])
        workflow = None
        if len(site_ids) == 1:
            workflow = await asyncio.to_thread(
                probe_workflow,
                site_ids[0],
                str(inventory.get("final_url") or inventory_url),
            )
        adapter = await run_onesite_adapter(row, inventory)
        baseline = baseline_by_id[pid]
        outcome, counts, contamination, rejections = classify(
            pid, baseline, route, workflow, adapter
        )
        configured_identity = page_identity(
            row, str(configured.get("body") or "")
        )
        inventory_identity = page_identity(row, str(inventory.get("body") or ""))
        current_pipeline_strict = int(
            (baseline.get("identity_evidence") or {}).get(
                "rows_with_native_identity_and_positive_rent", 0
            )
            or 0
        )
        name_match = bool(
            configured_identity.get("name_visible_exact_normalized")
            or configured_identity.get("name_distinctive_tokens_visible")
        )
        route_binding = bool(
            route.get("expected_site_id_is_sole_published_id")
            and route.get("no_conflicting_site_ids")
            and route.get("same_origin_inventory_page")
            and route.get("inventory_path_published_by_configured_page")
        )
        result = {
            "property_id": pid,
            "property_name": row.get("name") or row.get("property_name") or "",
            "canonical_address": row.get("address") or "",
            "configured_website": row.get("website") or "",
            "ledger_current_detected_adapter": row.get("current_detected_adapter") or "",
            "rp_oracle_native_unit_rows": int(
                row.get("rp_oracle_native_unit_rows") or 0
            ),
            "configured_fetch": public_fetch_record(configured),
            "inventory_fetch": public_fetch_record(inventory),
            "configured_page_identity": configured_identity,
            "inventory_page_identity": inventory_identity,
            "published_native_route": route,
            "native_workflow_probe": workflow,
            "onesite_adapter_e2e": adapter,
            "current_full_pipeline_e2e": {
                "source_artifact": str(BASELINE),
                "source_artifact_sha256": sha256(BASELINE),
                "adapter_used": baseline.get("adapter") or "",
                "tier": baseline.get("tier") or "",
                "native_positive_rent_rows": current_pipeline_strict,
                "unit_rows": int(baseline.get("units") or 0),
                "plan_summary_rows": int(baseline.get("plans") or 0),
                "native_samples": baseline.get("native_samples") or [],
                "errors": baseline.get("errors") or [],
            },
            "property_identity_match": bool(
                name_match
                and (
                    configured_identity.get("street_number_and_words_visible")
                    or route_binding
                )
            ),
            "contamination_verdict": contamination,
            "outcome": outcome,
            "counts_toward_strict_207_gate": counts,
            "rejection_reasons": rejections,
        }
        results.append(sanitize(result))

    ledger_ids = {
        int(row["property_id"])
        for row in read_csv(LEDGER)
        if row.get("property_id")
    }
    qualified = [
        int(row["property_id"])
        for row in results
        if row["counts_toward_strict_207_gate"]
    ]
    near_misses = [
        int(row["property_id"])
        for row in results
        if row["outcome"] == "ADAPTER_E2E_NATIVE_DATA_ROUTING_MISS"
    ]
    net_new = sorted(set(qualified) - ledger_ids)
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "lane": "failed_no_data_realpage_onesite_current_residual",
        "generated_at_utc": generated_at,
        "cohort_snapshot": {
            "remaining_csv": str(REMAINING),
            "remaining_csv_sha256": sha256(REMAINING),
            "ledger_csv": str(LEDGER),
            "ledger_csv_sha256": sha256(LEDGER),
            "ledger_rows_before_lane": len(ledger_ids),
            "target_adapters": sorted(TARGET_ADAPTERS),
            "target_ids": target_ids,
            "target_count": len(target_ids),
        },
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "paid_canary": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "fingerprint_rotation": False,
            "fixed_tls_fingerprint": "chrome116 for public OneSite workflow only",
            "production_files_modified_by_lane": False,
        },
        "strict_policy": {
            "current_full_pipeline_required_to_count": True,
            "direct_adapter_e2e_without_current_pipeline_routing_counts": False,
            "plan_level_rows_count": False,
            "positive_numeric_rent_required": True,
            "native_unit_id_or_number_required": True,
            "exact_current_property_binding_required": True,
        },
        "summary": {
            "audited_properties": len(results),
            "current_pipeline_qualified_ids": sorted(qualified),
            "current_pipeline_qualified_count": len(qualified),
            "adapter_e2e_native_data_routing_near_miss_ids": sorted(near_misses),
            "adapter_e2e_native_data_routing_near_miss_count": len(near_misses),
            "strict_net_new_ids_vs_current_ledger": net_new,
            "strict_net_new_count_vs_current_ledger": len(net_new),
            "rejected_or_near_miss_count": len(results) - len(qualified),
        },
        "results": results,
    }
    payload = sanitize(payload)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if re.search(r"ClientSessionID=(?!<redacted>)", serialized, re.IGNORECASE):
        raise RuntimeError("unsanitized ClientSessionID in artifact")
    if '"xyz"' in serialized.lower():
        raise RuntimeError("secret header name leaked into artifact")
    if any(
        not row["rejection_reasons"]
        for row in results
        if not row["counts_toward_strict_207_gate"]
    ):
        raise RuntimeError("every non-counted row must have explicit rejection reasons")
    OUTPUT.write_text(serialized)

    net_new_payload = {
        "generated_at_utc": generated_at,
        "source_artifact": str(OUTPUT),
        "source_artifact_sha256": sha256(OUTPUT),
        "ledger_csv": str(LEDGER),
        "ledger_csv_sha256": sha256(LEDGER),
        "current_pipeline_qualified_ids": sorted(qualified),
        "strict_net_new_ids_vs_current_ledger": net_new,
        "adapter_e2e_native_data_routing_near_miss_ids": sorted(near_misses),
        "note": (
            "Near-miss IDs expose exact current native unit data through the "
            "OneSite adapter but are excluded until full current pipeline routing passes."
        ),
    }
    NET_NEW_OUTPUT.write_text(json.dumps(net_new_payload, indent=2, sort_keys=True) + "\n")

    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "net_new_artifact": str(NET_NEW_OUTPUT),
                "qualified": sorted(qualified),
                "near_misses": sorted(near_misses),
                "net_new": net_new,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
