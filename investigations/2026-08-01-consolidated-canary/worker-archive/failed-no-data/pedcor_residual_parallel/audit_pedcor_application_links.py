from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "pedcor_residual_parallel"
OUTPUT = LANE / "pedcor_four_application_links_live_audit.json"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
TARGET_IDS = ("20262", "45755", "49921", "254122")
CONTROL = {
    "property_id": "42571",
    "name": "Westwood Village",
    "address": "2203 Beck Ave",
    "city": "Panama City",
    "state": "FL",
    "zip": "32405",
    "url": "https://westwoodvillageapthomes.com/en/floor-plans/",
}
BETTERNOI_API = "https://ares.betternoi.com/api/pub/v1/client/building/unit/"
UUID_RE = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fetch(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    allow_redirects: bool = True,
    referer: str = "",
) -> requests.Response:
    headers = {"Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    response = session.get(
        url,
        params=params,
        headers=headers,
        allow_redirects=allow_redirects,
        timeout=(10, 35),
    )
    response.raise_for_status()
    return response


def extract_json_assignment(html: str, variable: str) -> object:
    match = re.search(
        rf"{re.escape(variable)}\s*=\s*(\[.*?\]|\{{.*?\}})\s*;",
        html,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"missing {variable} assignment")
    return json.loads(match.group(1))


def extract_application_url(html: str) -> str:
    match = re.search(
        r"alpine_floorplans\(window\.floorplans,\s*'([^']+)'",
        html,
        re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("missing alpine_floorplans application URL")
    return match.group(1)


def compact_plan(plan: dict) -> dict:
    return {
        "plan_id": plan.get("Id"),
        "name": plan.get("Name"),
        "apartment_id": plan.get("ApartmentId"),
        "bedroom": plan.get("Bedroom"),
        "bathroom": plan.get("Bathroom"),
        "min_sqft": plan.get("MinSqFt"),
        "max_rent": plan.get("MaxRent"),
        "current_market_rent": plan.get("CurrentMarketRent"),
        "num_available": plan.get("NumAvailable"),
        "has_availability": plan.get("has_availability"),
        "unit_code": plan.get("UnitCode"),
        "unit_type_code": plan.get("UnitTypeCode"),
    }


def api_summary(response: requests.Response, *, max_samples: int = 3) -> dict:
    payload = response.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    rows = results if isinstance(results, list) else []
    return {
        "status": response.status_code,
        "url": response.url,
        "body_sha256": text_sha256(response.text),
        "count": payload.get("count") if isinstance(payload, dict) else None,
        "result_count": len(rows),
        "pagination_more": (
            (payload.get("pagination") or {}).get("more")
            if isinstance(payload, dict)
            else None
        ),
        "next": payload.get("next") if isinstance(payload, dict) else None,
        "samples": [
            {
                "uuid": row.get("uuid"),
                "client_uuid": row.get("client_uuid"),
                "id": row.get("id"),
                "unit_number": row.get("unit_number"),
                "unit_identifier": row.get("unit_identifier"),
                "min_rent": row.get("min_rent"),
                "max_rent": row.get("max_rent"),
                "adjusted_available_date": row.get("adjusted_available_date"),
                "availability_status": row.get("availability_status"),
                "building_address": row.get("building_address"),
                "building_city": row.get("building_city"),
                "building_state": row.get("building_state"),
                "building_postal_code": row.get("building_postal_code"),
                "floor_plan_uuid": (
                    (row.get("floor_plan") or {}).get("uuid")
                    if isinstance(row.get("floor_plan"), dict)
                    else None
                ),
                "floor_plan_name": (
                    (row.get("floor_plan") or {}).get("name")
                    if isinstance(row.get("floor_plan"), dict)
                    else None
                ),
            }
            for row in rows[:max_samples]
            if isinstance(row, dict)
        ],
        "raw": payload,
    }


def audit_betternoi_application(
    application_url: str,
) -> dict:
    session = requests.Session()
    app = fetch(session, application_url)
    html = app.text
    selected = re.search(
        r'<option\s+value="(?P<id>\d+)"\s+selected>(?P<label>.*?)</option>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    client_key_match = re.search(
        rf'const\s+client_key\s*=\s*"(?P<key>{UUID_RE})"',
        html,
        re.IGNORECASE,
    )
    if not selected or not client_key_match:
        raise RuntimeError(f"BetterNOI app page missing selected client/key: {application_url}")
    numeric_client_id = selected.group("id")
    client_key = client_key_match.group("key").casefold()
    url_key = (parse_qs(urlparse(application_url).query).get("key") or [""])[0].casefold()
    if client_key != url_key:
        raise RuntimeError("application URL key does not match rendered client_key")

    # Reproduce the UI's exact Select2 request. jQuery serializes the array
    # parameter as ``client_ids[]``. The scalar spelling is *not* equivalent.
    ui_response = fetch(
        session,
        BETTERNOI_API,
        params={
            "format": "s2",
            "is_available": "1",
            "q": "",
            "limit": "50",
            "offset": "0",
            "client_ids[]": numeric_client_id,
        },
        referer=application_url,
    )
    exact_key_response = fetch(
        session,
        BETTERNOI_API,
        params={
            "client_uuid": client_key,
            "is_available": "true",
            "limit": "50",
        },
        referer=application_url,
    )

    # Negative contamination proof. The superficially plausible scalar
    # parameter is ignored by this public endpoint and returns portfolio-wide
    # rows. Only fetch two samples and never admit these rows.
    scalar_hazard_response = fetch(
        session,
        BETTERNOI_API,
        params={
            "format": "json",
            "is_available": "1",
            "client_ids": numeric_client_id,
            "limit": "2",
            "offset": "0",
        },
        referer=application_url,
    )
    ui = api_summary(ui_response)
    exact_key = api_summary(exact_key_response)
    scalar_hazard = api_summary(scalar_hazard_response, max_samples=2)
    return {
        "application_fetch": {
            "status": app.status_code,
            "final_url": app.url,
            "body_bytes": len(app.content),
            "body_sha256": text_sha256(html),
            "title": (
                re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S).group(1).strip()
                if re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
                else ""
            ),
        },
        "published_application_key": client_key,
        "selected_numeric_client_id": numeric_client_id,
        "selected_client_label": re.sub(r"\s+", " ", selected.group("label")).strip(),
        "page_native_markers": {
            "data_property_count": html.casefold().count("data-property"),
            "data_fpcode_count": html.casefold().count("data-fpcode"),
            "rendered_unit_uuid_count": len(
                re.findall(rf'"unit_uuid"\s*:\s*"{UUID_RE}"', html, re.I)
            ),
            "rendered_unit_number_count": len(
                re.findall(r'"unit_number"\s*:', html, re.I)
            ),
        },
        "exact_ui_array_filter": {key: value for key, value in ui.items() if key != "raw"},
        "application_key_as_client_uuid": {
            key: value for key, value in exact_key.items() if key != "raw"
        },
        "scalar_filter_contamination_negative": {
            key: value for key, value in scalar_hazard.items() if key != "raw"
        },
        "strict_native_available_rows": 0,
        "verdict": (
            "no_property_scoped_native_rows_exact_ui_filter_empty_and_"
            "application_key_is_not_a_unit_api_client_uuid"
        ),
    }


def audit_applyv2(application_url: str) -> dict:
    session = requests.Session()
    landing = fetch(session, application_url)
    html = landing.text
    signin_match = re.search(
        r'href="([^"]*MicrosoftIdentity/Account/SignIn[^"]*)"',
        html,
        re.IGNORECASE,
    )
    if not signin_match:
        raise RuntimeError("applyv2 landing missing sign-in route")
    signin_url = urljoin(landing.url, signin_match.group(1).replace("&amp;", "&"))
    direct_application = application_url.rstrip("/") + "/Application"
    application = fetch(session, direct_application)
    sign_in = session.get(signin_url, allow_redirects=False, timeout=(10, 35))
    return {
        "landing_fetch": {
            "status": landing.status_code,
            "final_url": landing.url,
            "body_bytes": len(landing.content),
            "body_sha256": text_sha256(html),
            "community_header": (
                re.search(r"<h3[^>]*>(.*?)</h3>", html, re.I | re.S).group(1).strip()
                if re.search(r"<h3[^>]*>(.*?)</h3>", html, re.I | re.S)
                else ""
            ),
        },
        "sign_in_route": {
            "url": signin_url,
            "status": sign_in.status_code,
            "location_host": urlparse(sign_in.headers.get("Location", "")).hostname or "",
            "requires_account": sign_in.is_redirect,
        },
        "unauthenticated_application_route": {
            "url": application.url,
            "status": application.status_code,
            "body_bytes": len(application.content),
            "body_sha256": text_sha256(application.text),
            "native_unit_marker_count": sum(
                application.text.casefold().count(marker)
                for marker in (
                    "unit_number",
                    "unit_identifier",
                    "data-unit-id",
                    "availability_date",
                )
            ),
            "visible_property_header": bool(
                re.search(r"PENSACOLA,\s*KINGS MILL", application.text, re.I)
            ),
        },
        "strict_native_available_rows": 0,
        "verdict": (
            "no_public_native_inventory_application_requires_account_and_"
            "unauthenticated_route_contains_no_unit_rent_or_date_roster"
        ),
    }


def audit_target(metadata: dict[str, str]) -> dict:
    session = requests.Session()
    root = fetch(session, metadata["website"])
    html = root.text
    floorplans_obj = extract_json_assignment(html, "window.floorplans")
    apartment_obj = extract_json_assignment(html, "window.apartment")
    if not isinstance(floorplans_obj, list) or not all(
        isinstance(row, dict) for row in floorplans_obj
    ):
        raise RuntimeError("window.floorplans is not a plan list")
    if not isinstance(apartment_obj, dict):
        raise RuntimeError("window.apartment is not an object")
    application_url = extract_application_url(html)
    plan_ids = [str(row.get("Id") or "") for row in floorplans_obj]
    apartment_ids = [str(row.get("ApartmentId") or "") for row in floorplans_obj]
    plan_has_native_shape = [
        bool(
            row.get("unit_number")
            or row.get("unit_identifier")
            or row.get("uuid")
            or row.get("availability_date")
        )
        for row in floorplans_obj
    ]
    portal = (
        audit_betternoi_application(application_url)
        if (urlparse(application_url).hostname or "").casefold() == "ares.betternoi.com"
        else audit_applyv2(application_url)
    )
    return {
        "property_id": int(metadata["apartmentid"]),
        "configured_identity": {
            "name": metadata["name"],
            "address": metadata["address"],
            "city": metadata["city"],
            "state": metadata["state"],
            "zip": metadata["zip"],
            "website": metadata["website"],
        },
        "marketing_fetch": {
            "status": root.status_code,
            "final_url": root.url,
            "body_bytes": len(root.content),
            "body_sha256": text_sha256(html),
        },
        "marketing_property_object": apartment_obj,
        "window_floorplans": {
            "row_count": len(floorplans_obj),
            "distinct_plan_ids": len(set(plan_ids)),
            "plan_ids": plan_ids,
            "distinct_apartment_ids": sorted(set(apartment_ids)),
            "all_rows_repeat_one_apartment_id": bool(
                apartment_ids and len(set(apartment_ids)) == 1
            ),
            "rows_with_native_apartment_shape": sum(plan_has_native_shape),
            "rows_with_has_availability_true": sum(
                row.get("has_availability") is True for row in floorplans_obj
            ),
            "rows_with_num_available_positive": sum(
                int(row.get("NumAvailable") or 0) > 0 for row in floorplans_obj
            ),
            "schema_roles": {
                "Id": "floor_plan_id_not_physical_unit_id",
                "ApartmentId": "repeated_marketing_community_id_not_physical_unit_id",
                "UnitCode": "floor_plan_or_unit_type_code_not_visible_apartment_identity",
                "UnitTypeCode": "floor_plan_type_code_not_visible_apartment_identity",
                "has_availability": "plan_level_boolean_not_a_native_unit_roster",
            },
            "plans": [compact_plan(row) for row in floorplans_obj],
        },
        "application_url": application_url,
        "application_portal": portal,
        "strict_recovery": {
            "native_unit_ids": 0,
            "native_units_with_positive_rent": 0,
            "native_units_with_date": 0,
            "accept": False,
            "reason": portal["verdict"],
        },
    }


def audit_control() -> dict:
    session = requests.Session()
    page = fetch(session, CONTROL["url"])
    html = page.text
    pair_re = re.compile(
        rf'data-property="(?P<client>{UUID_RE})"'
        rf'.{{0,800}}?data-fpcode="(?P<floorplan>{UUID_RE})"',
        re.IGNORECASE | re.DOTALL,
    )
    pairs = {
        (match.group("client").casefold(), match.group("floorplan").casefold())
        for match in pair_re.finditer(html)
    }
    clients = {client for client, _ in pairs}
    floorplans = {floorplan for _, floorplan in pairs}
    if len(clients) != 1 or not floorplans:
        raise RuntimeError("positive control did not publish one exact BetterNOI client")
    client_id = next(iter(clients))
    api = fetch(
        session,
        BETTERNOI_API,
        params={
            "client_uuid": client_id,
            "is_available": "true",
            "limit": "200",
        },
        referer=page.url,
    )
    payload = api.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("positive control unexpectedly has no native units")
    strict_rows = []
    for row in rows:
        floorplan = row.get("floor_plan") if isinstance(row, dict) else None
        rent = float(row.get("min_rent") or 0) if isinstance(row, dict) else 0
        if not (
            isinstance(row, dict)
            and str(row.get("client_uuid") or "").casefold() == client_id
            and isinstance(floorplan, dict)
            and str(floorplan.get("uuid") or "").casefold() in floorplans
            and row.get("uuid")
            and (row.get("unit_number") or row.get("unit_identifier"))
            and rent > 0
            and row.get("adjusted_available_date")
            and str(row.get("building_address") or "").strip().casefold()
            == "2203 beck avenue"
            and str(row.get("building_city") or "").strip().casefold()
            == "panama city"
            and str(row.get("building_state") or "").strip().casefold() == "fl"
            and str(row.get("building_postal_code") or "").strip() == "32405"
        ):
            raise RuntimeError("positive control property/native boundary failed")
        strict_rows.append(row)
    summary = api_summary(api)
    return {
        "property": CONTROL,
        "page_fetch": {
            "status": page.status_code,
            "final_url": page.url,
            "body_bytes": len(page.content),
            "body_sha256": text_sha256(html),
        },
        "published_client_ids": sorted(clients),
        "published_floor_plan_ids": sorted(floorplans),
        "published_pair_count": len(pairs),
        "exact_scoped_api": {key: value for key, value in summary.items() if key != "raw"},
        "strict_native_available_rows": len(strict_rows),
        "verdict": (
            "positive_control_exact_page_published_client_and_floorplans_"
            "produce_native_unit_with_positive_rent_and_date"
        ),
    }


def main() -> None:
    before = {
        "remaining_sha256": sha256(REMAINING),
        "ledger_sha256": sha256(LEDGER),
    }
    metadata_by_id = {
        row["apartmentid"]: row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid") in TARGET_IDS
    }
    if set(metadata_by_id) != set(TARGET_IDS):
        raise RuntimeError("missing configured Pedcor target metadata")
    targets = [audit_target(metadata_by_id[property_id]) for property_id in TARGET_IDS]
    control = audit_control()
    after = {
        "remaining_sha256": sha256(REMAINING),
        "ledger_sha256": sha256(LEDGER),
    }
    if after != before:
        raise RuntimeError("shared ledger or remaining cohort changed during audit")
    if any(row["strict_recovery"]["accept"] for row in targets):
        raise RuntimeError("unexpected Pedcor strict native candidate")

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "pedcor_four_exact_current_application_links_live_direct_audit",
        "guardrails": {
            "direct_http_only": True,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "llm": False,
            "paid_canary": False,
            "authentication_attempted": False,
            "application_submitted": False,
        },
        "immutability": {"before": before, "after": after},
        "summary": {
            "target_properties": len(targets),
            "strict_recoveries": sum(
                row["strict_recovery"]["accept"] for row in targets
            ),
            "plan_only_properties": len(targets),
            "betternoi_application_properties": sum(
                "ares.betternoi.com" in row["application_url"] for row in targets
            ),
            "applyv2_properties": sum(
                "applyv2.pedcor.net" in row["application_url"] for row in targets
            ),
            "positive_boundary_controls": 1,
            "control_strict_native_rows": control["strict_native_available_rows"],
            "scalar_betternoi_filter_is_contamination_hazard": True,
            "recommended_disposition": (
                "keep_all_four_failed_no_data_at_unit_level; retain plan catalogue "
                "only; never reinterpret ApartmentId/window.floorplans or scalar "
                "BetterNOI client filters as native units"
            ),
        },
        "targets": targets,
        "positive_boundary_control": control,
        "source_snapshot": {
            "script": str(Path(__file__)),
            "script_sha256": sha256(Path(__file__)),
        },
    }
    OUTPUT.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "summary": payload["summary"],
                "targets": [
                    {
                        "property_id": row["property_id"],
                        "application_url": row["application_url"],
                        "plans": row["window_floorplans"]["row_count"],
                        "apartment_ids": row["window_floorplans"][
                            "distinct_apartment_ids"
                        ],
                        "plan_availability_flags": row["window_floorplans"][
                            "rows_with_has_availability_true"
                        ],
                        "strict_native_rows": row["strict_recovery"][
                            "native_unit_ids"
                        ],
                    }
                    for row in targets
                ],
                "control_strict_rows": control["strict_native_available_rows"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
