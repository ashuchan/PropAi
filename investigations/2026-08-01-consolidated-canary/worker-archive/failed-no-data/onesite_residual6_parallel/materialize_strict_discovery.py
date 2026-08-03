from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests


OUT = Path("/private/tmp/propai-fnd-vBkmT9/onesite_residual6_parallel")
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
sys.path.insert(0, str(REPO))

from ma_poc.pms.adapters.onesite import (  # noqa: E402
    _generate_xyz_token,
    parse_onesite_workflowstartup,
)


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_meta(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256(path.read_bytes()),
        "bytes": path.stat().st_size,
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, str, bytes, dict[str, str]]:
    merged = {
        "User-Agent": UA,
        "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        merged.update(headers)
    response = requests.get(
        url,
        headers=merged,
        timeout=30,
        allow_redirects=True,
        impersonate="chrome116",
    )
    return response.status_code, str(response.url), bytes(response.content or b""), dict(response.headers)


def store_gzip(name: str, body: bytes) -> dict[str, Any]:
    path = OUT / name
    path.write_bytes(gzip.compress(body, compresslevel=9))
    return {
        **artifact_meta(path),
        "content_sha256": sha256(body),
        "content_bytes": len(body),
    }


def workflow_repeat_39995() -> dict[str, Any]:
    site_id = "5272798"
    origin = "https://www.southpointehanahan.com"
    attempts = []
    for attempt in range(1, 4):
        session_id = str(uuid.uuid4())
        url = (
            "https://leasing.realpage.com/RP.Leasing.AppService.WebHost/"
            f"workflowstartup/v1/{site_id}/English?BpmId=OLL.WorkflowStartUp"
            f"&BpmSequence=0&LogSequence=3&ClientSessionID={session_id}"
        )
        status, final_url, body, _ = fetch(
            url,
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": origin,
                "Referer": origin + "/",
                "XYZ": _generate_xyz_token(site_id),
                "X-AuthToken": "",
                "X-Phased": "",
            },
        )
        raw = store_gzip(f"39995_workflow_repeat_{attempt}.json.gz", body)
        payload = json.loads(body)
        rows = parse_onesite_workflowstartup(payload, final_url)
        native = []
        for row in rows:
            rent = max(
                int(row.get("market_rent_low") or 0),
                int(row.get("market_rent_high") or 0),
            )
            if str(row.get("unit_number") or "").strip() and rent > 0:
                native.append({
                    "unit_number": str(row["unit_number"]),
                    "rent": rent,
                    "floor_plan_name": row.get("floor_plan_name"),
                    "bedrooms": row.get("bedrooms"),
                    "bathrooms": row.get("bathrooms"),
                    "sqft": row.get("sqft"),
                    "availability_status": row.get("availability_status"),
                    "source_property_id": row.get("source_property_id"),
                    "source_api_url": re.sub(
                        r"ClientSessionID=[^&]+",
                        "ClientSessionID=<redacted>",
                        str(row.get("source_api_url") or ""),
                    ),
                })
        attempts.append({
            "attempt": attempt,
            "status": status,
            "requested_site_id": site_id,
            "payload_site_id": str(payload.get("Workflow", {}).get("SiteId") or ""),
            "payload_site_id_matches": str(payload.get("Workflow", {}).get("SiteId") or "") == site_id,
            "native_positive_rows": native,
            "raw": raw,
        })
    fingerprints = {
        tuple((r["unit_number"], r["rent"]) for r in attempt["native_positive_rows"])
        for attempt in attempts
    }
    return {
        "route": (
            "configured South Pointe page -> published 9067331.onlineleasing.realpage.com "
            "-> widget siteId 5272798 -> workflowstartup/v1/5272798/English"
        ),
        "attempts": attempts,
        "repeat_count": 3,
        "stable_across_repeats": len(fingerprints) == 1,
        "strict_native_rows": attempts[-1]["native_positive_rows"],
    }


def tor_view_native_rows(direct: dict[str, Any]) -> dict[str, Any]:
    page = next(
        row for row in direct["targets"]["38677"]["pages"]
        if row["requested_url"] == "https://www.torviewvillageapts.com/"
    )
    with gzip.open(page["artifact"], "rt", errors="replace") as handle:
        html = handle.read()
    soup = BeautifulSoup(html, "html.parser")
    canonical_visible = " ".join(soup.stripped_strings)
    matches = []
    pattern = re.compile(
        r"(?P<unit>\d{1,3}[A-Z])\s+(?P<street>Hasbrouck\s+Drive|Kensington\s+Circle)\s*[- ]*\$\s*(?P<rent>[1-9]\d{2,4})(?:\.00)?",
        re.I,
    )
    for text_node in soup.find_all(string=re.compile(r"\$\s*\d")):
        match = pattern.search(str(text_node))
        if not match:
            continue
        card = text_node.find_parent(class_=lambda value: value and "team-list" in value)
        heading = card.find("h3") if card else None
        matches.append({
            "unit_number": match.group("unit").upper(),
            "unit_address_label": f"{match.group('unit').upper()} {match.group('street')}",
            "rent": int(match.group("rent")),
            "floor_plan_name": heading.get_text(" ", strip=True) if heading else "",
            "published_listing_url": text_node.find_parent("a").get("href") if text_node.find_parent("a") else "",
        })
    rows = sorted(matches, key=lambda row: row["unit_number"])
    return {
        "route": "current configured first-party page https://www.torviewvillageapts.com/",
        "configured_page": {
            "status": page["status"],
            "final_url": page["final_url"],
            "body_sha256": page["body_sha256"],
            "artifact": artifact_meta(Path(page["artifact"])),
        },
        "identity_binding": {
            "name_visible": "tor view village" in canonical_visible.lower(),
            "canonical_address_visible": "1 kensington circle" in canonical_visible.lower(),
            "canonical_city_zip_visible": "garnerville" in canonical_visible.lower() and "10923" in canonical_visible,
            "all_rows_use_property_published_street_names": all(
                row["unit_address_label"].endswith("Hasbrouck Drive")
                or row["unit_address_label"].endswith("Kensington Circle")
                for row in rows
            ),
        },
        "strict_native_rows": rows,
        "distinct_unit_count": len({row["unit_number"] for row in rows}),
        "all_positive_rent": all(row["rent"] > 0 for row in rows),
    }


def park_at_blanding_cws() -> dict[str, Any]:
    direct = load_json(OUT / "direct_route_probe.json")
    official_page = next(
        row for row in direct["targets"]["43520"]["pages"]
        if row["requested_url"] == "https://theparkatblanding.com/Floor-Plans.aspx"
    )
    with gzip.open(official_page["artifact"], "rt", errors="replace") as handle:
        official_html = handle.read()
    property_id = re.search(r"\bpropertyId\s*=\s*['\"](\d+)['\"]", official_html).group(1)
    property_key = re.search(r"\bpropertyKey\s*=\s*['\"]([^'\"]+)['\"]", official_html).group(1)
    api_key = re.search(r"\bapiKey\s*:\s*['\"]([^'\"]+)['\"]", official_html).group(1)
    partner_id = re.search(r"PartnerPropertyId['\"]?\s*:\s*['\"](\d+)['\"]", official_html).group(1)
    headers = {"x-ws-authkey": api_key, "Origin": "https://theparkatblanding.com"}
    base = f"https://api.ws.realpage.com/v2/property/{property_id}"
    results: dict[str, Any] = {}
    bodies: dict[str, Any] = {}
    routes = {
        "property_details": f"{base}/PropertyDetails",
        "floorplans": f"{base}/floorplans",
        "units_unfiltered": f"{base}/units",
        "units_widget_filtered": (
            f"{base}/units?available=true&honordisplayorder=true&siteid={property_id}"
            "&bestprice=true&leaseterm=6,7,8,9,10,11,12,13,14&dateneeded=2026-08-01"
        ),
    }
    for label, url in routes.items():
        status, final_url, body, _ = fetch(url, headers)
        bodies[label] = json.loads(body)
        results[label] = {
            "status": status,
            "final_url": final_url,
            "raw": store_gzip(f"43520_cws_{label}.json.gz", body),
        }

    details = bodies["property_details"].get("response") or {}
    floorplans = bodies["floorplans"].get("response", {}).get("floorplans") or []
    floorplan_map = {str(row.get("id")): row for row in floorplans}
    units = bodies["units_unfiltered"].get("response", {}).get("units") or []
    strict_rows = []
    for unit in units:
        floorplan = floorplan_map.get(str(unit.get("floorplanId")))
        if not floorplan:
            continue
        if unit.get("active") is not True or unit.get("leaseStatus") != "AVAILABLE_READY":
            continue
        rent = int(unit.get("rent") or 0)
        unit_number = str(unit.get("unitNumber") or unit.get("name") or "").strip()
        native_id = str(unit.get("id") or "").strip()
        if not unit_number or not native_id or rent <= 0:
            continue
        strict_rows.append({
            "native_unit_id": native_id,
            "unit_number": unit_number,
            "rent": rent,
            "availability_status": unit.get("leaseStatus"),
            "active": unit.get("active"),
            "floorplan_id": str(unit.get("floorplanId")),
            "floor_plan_name": floorplan.get("name"),
            "bedrooms": floorplan.get("bedRooms"),
            "bathrooms": floorplan.get("bathRooms"),
            "sqft": unit.get("squareFeet"),
        })
    strict_rows.sort(key=lambda row: row["unit_number"])
    address = details.get("address") or {}
    widget_filtered = bodies["units_widget_filtered"]
    return {
        "route": (
            "configured http://www.parkatblanding.com/ -> current official "
            "https://theparkatblanding.com/Floor-Plans.aspx -> embedded RPFP_config "
            f"propertyId {property_id} -> api.ws.realpage.com/v2/property/{property_id}/units"
        ),
        "published_config": {
            "property_id": property_id,
            "property_key": property_key,
            "partner_property_id": partner_id,
            "api_key_sha256": sha256(api_key.encode()),
            "api_key_redacted": True,
            "official_page": {
                "final_url": official_page["final_url"],
                "status": official_page["status"],
                "body_sha256": official_page["body_sha256"],
                "artifact": artifact_meta(Path(official_page["artifact"])),
            },
        },
        "property_details_identity": {
            "id": str(details.get("id") or ""),
            "name": details.get("name"),
            "address": address,
            "id_matches_published_property_id": str(details.get("id") or "") == property_id,
            "exact_name_matches": details.get("name") == "The Park at Blanding",
            "street_city_state_zip_match": (
                address.get("address1") == "222 Blairmore Boulevard East"
                and address.get("cityName") == "Orange Park"
                and address.get("stateCode") == "FL"
                and address.get("postalCode") == "32073"
            ),
        },
        "endpoint_results": results,
        "widget_filtered_endpoint_observation": {
            "status": widget_filtered.get("status"),
            "message": widget_filtered.get("message"),
            "response_is_null": widget_filtered.get("response") is None,
        },
        "strict_filter": "active is true AND leaseStatus == AVAILABLE_READY AND native id/number present AND rent > 0 AND floorplanId belongs to exact property floorplan payload",
        "strict_native_rows": strict_rows,
        "distinct_native_unit_ids": len({row["native_unit_id"] for row in strict_rows}),
        "distinct_unit_numbers": len({row["unit_number"] for row in strict_rows}),
        "all_rows_join_exact_property_floorplans": all(row["floorplan_id"] in floorplan_map for row in strict_rows),
        "all_positive_rent": all(row["rent"] > 0 for row in strict_rows),
    }


def main() -> None:
    direct_path = OUT / "direct_route_probe.json"
    hb_path = OUT / "hb_southern_pine_clean_probe.json"
    direct = load_json(direct_path)
    hb = load_json(hb_path)

    recovered = {
        "38677": tor_view_native_rows(direct),
        "39995": workflow_repeat_39995(),
        "43520": park_at_blanding_cws(),
    }
    not_recovered = {}
    for pid in ("14295", "67154", "291774"):
        target = direct["targets"][pid]
        workflow = target["workflows"][0]
        reason = "exact published OneSite workflow has zero native UnitIds"
        extra: dict[str, Any] = {}
        if pid == "67154":
            reason = (
                "clean Hyperbrowser exposed exact current portal and identity page, "
                "but public page advertises a waiting list and the bound OneSite workflow has zero native UnitIds"
            )
            extra = {
                "clean_hyperbrowser": {
                    "artifact": artifact_meta(hb_path),
                    "sessions": hb.get("hyperbrowser_sessions"),
                    "session_options": hb.get("session_options"),
                    "identity_page": hb.get("pages", [None])[0],
                }
            }
        not_recovered[pid] = {
            "property_name": target["metadata"]["name"],
            "route": (
                f"{target['metadata']['root']} -> "
                f"{target['metadata']['portal_urls'][0]} -> "
                f"workflowstartup/v1/{target['metadata']['site_ids'][0]}/English"
            ),
            "reason": reason,
            "workflow": {
                "status": workflow["status"],
                "floorplan_count": workflow["floorplan_count"],
                "floorplans": workflow["floorplans"],
                "native_positive_rent_row_count": workflow["native_positive_rent_row_count"],
                "raw": {
                    "path": workflow["artifact"],
                    "sha256": workflow["artifact_sha256"],
                    "content_sha256": workflow["body_sha256"],
                },
            },
            **extra,
        }

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "cohort": [14295, 38677, 39995, 43520, 67154, 291774],
            "recovered_pids": [38677, 39995, 43520],
            "not_recovered_pids": [14295, 67154, 291774],
            "cluster_members_live_probed": 6,
            "cluster_generalization": (
                "No route is generalized solely from one property. The existing OneSite workflow "
                "path was live-probed across all six. The Tor View page-local text pattern and Park "
                "RPFP unfiltered-CWS pattern each occur in only one member, so the implementation "
                "proposal gates them by exact published markers and fails closed elsewhere."
            ),
        },
        "guardrails": {
            "repo_source_edits": False,
            "builder_edits": False,
            "ledger_edits": False,
            "paid_canary": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "direct_transport": "one fixed chrome116 fingerprint",
            "hyperbrowser_sessions": 1,
            "hyperbrowser_solve_captchas": False,
            "hyperbrowser_use_stealth": False,
        },
        "source_artifacts": {
            "direct_route_probe": artifact_meta(direct_path),
            "southern_pine_clean_hyperbrowser": artifact_meta(hb_path),
        },
        "recoverable": recovered,
        "fail_closed": not_recovered,
        "implementation_proposal": {
            "priority_1_rpfp_cws": (
                "When the exact current property page publishes RPFP_config with propertyId, pKey, "
                "apiUrl and apiKey, call PropertyDetails + floorplans + unfiltered units directly. "
                "Require PropertyDetails id/name/address to match canonical property; require every "
                "row active=true, leaseStatus=AVAILABLE_READY, native id and unitNumber, rent>0, and "
                "floorplanId joined to that exact property payload. Treat a null filtered-units "
                "response as non-authoritative only under these exact guards. Otherwise fail closed."
            ),
            "priority_2_page_local_native_text": (
                "After a OneSite workflow returns zero native rows, allow same-origin page-local "
                "native unit text only when canonical name/address are visible and each row carries "
                "an explicit unit number/address plus positive rent inside an Available Units "
                "section. Never accept plan-only cards or external PMC-wide rosters. This pattern "
                "has one positive member in this cohort, so keep it marker-gated and do not apply "
                "cluster-wide without two additional positive probes."
            ),
            "priority_3_existing_workflow": (
                "Retain the current sole-published-siteId OneSite workflow path. PID 39995 now "
                "returns a stable native unit across three repeats and should convert without a "
                "new provider route; preserve the UnitIds + positive-rent + payload SiteId guards."
            ),
        },
        "informational_search_only_not_counted": [
            {
                "url": "https://www.apartments.com/tor-view-village-apartments-garnerville-ny/8t7d3e1/",
                "purpose": "corroborated Tor View native unit labels; strict rows come from first-party page only",
            },
            {
                "url": "https://www.apartments.com/the-park-at-blanding-orange-park-fl/6vm0hht/",
                "purpose": "suggested CWS inventory existed; strict rows come from exact first-party-published CWS route only",
            },
            {
                "url": "https://www.homes.com/property/southern-pine-virginia-beach-va/hs0bp67r48se1/",
                "purpose": "syndicated units were not counted because exact official OneSite workflow exposed zero UnitIds",
            },
        ],
    }
    out_path = OUT / "evidence_onesite_residual6_strict_discovery.json"
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "artifact": str(out_path),
        "sha256": sha256(out_path.read_bytes()),
        "recoverable_pids": result["scope"]["recovered_pids"],
        "recoverable_row_counts": {
            pid: len(data["strict_native_rows"])
            for pid, data in recovered.items()
        },
    }, indent=2))


if __name__ == "__main__":
    main()
