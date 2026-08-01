from __future__ import annotations

import csv
import hashlib
import json
import re
import urllib.request
from copy import deepcopy
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._apts247 import parse_apts247_floorplans
from ma_poc.pms.adapters._encoreskyline_units import parse_jonah_resource_json
from ma_poc.pms.adapters._parsing import make_unit_dict


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane")
CAPTURE_DATE = "2026-08-01"
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
LEDGER = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_ledger_current.csv")
REMAINING = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_remaining_current.csv")

GENERIC_ARTIFACT = ROOT / "evidence_appfolio_generic_generic_current_strict.json"
CONSOLIDATED_ARTIFACT = (
    ROOT / "evidence_appfolio_generic_current_strict_consolidated.json"
)
NET_NEW_CSV = ROOT / "strict_appfolio_generic_net_new_ledger_rows.csv"
NET_NEW_JSON = ROOT / "strict_appfolio_generic_net_new_ids.json"

APTS247_URL = (
    "https://www.onyxuptownphx.com/api/v3/floorplans/all/"
    "?api_key=815a9f44797f4889a7669070c4f50a7a762cb7bb"
)
JULINGTON_INDEX = "https://thejulington.com/floorplans/"

PROPERTY_META = {
    15014: {
        "name": "Wildwood Park",
        "configured": "http://www.rentdittmar.com/apartment-communities/wildwood-park",
        "current": "https://www.rentwwp.com/arlington-vaapartments/wildwood-park/conventional/",
    },
    55709: {
        "name": "Vista Pointe Apartments",
        "configured": "https://ridgepointeblueridgeapts.com/en/",
        "current": "https://livevistapointeapts.com/en/",
    },
    60145: {
        "name": "Woodmont Mews",
        "configured": "https://www.woodmontmewsapartments.com/",
        "current": "https://www.woodmontmewsapartments.com/",
    },
    70255: {
        "name": "Coventry Square",
        "configured": "https://coventrysquareapartments.com/",
        "current": "https://coventrysquareapartments.com/",
    },
    78597: {
        "name": "Sentral Union Station",
        "configured": "https://www.sentral.com/denver/union-station",
        "current": "https://sentral.com/denver/union-station",
    },
    258661: {
        "name": "Banyan on Washington",
        "configured": "https://banyanonwashington.com/",
        "current": "https://banyanonwashington.com/",
    },
    260505: {
        "name": "Onyx Uptown PHX",
        "configured": "https://broadstoneuptownphx.com/",
        "current": "https://www.onyxuptownphx.com/",
    },
    263498: {
        "name": "The Julington",
        "configured": "https://risejulington.com/",
        "current": "https://thejulington.com/",
    },
}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def money_bounds(value: object) -> tuple[int | None, int | None]:
    values = [int(x.replace(",", "")) for x in re.findall(r"\d[\d,]*", str(value))]
    values = [x for x in values if x > 0]
    if not values:
        return None, None
    return min(values), max(values)


def positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


def evidence_row(
    *,
    native_unit_id: object,
    unit_number: object,
    floor_plan_id: object = "",
    floor_plan_name: object = "",
    bedrooms: object = "",
    bathrooms: object = "",
    sqft: object = "",
    building: object = "",
    floor: object = "",
    rent_low: int | None,
    rent_high: int | None = None,
    availability_date: object = "",
    source_url: str,
    source_ids: dict | None = None,
    extra: dict | None = None,
) -> dict:
    native = str(native_unit_id or "").strip()
    number = str(unit_number or "").strip()
    source_ids = {str(k): str(v) for k, v in (source_ids or {}).items() if v not in (None, "")}
    standard = make_unit_dict(
        floor_plan_name=str(floor_plan_name or "").strip(),
        bedrooms=str(bedrooms or "").strip(),
        bathrooms=str(bathrooms or "").strip(),
        sqft=str(sqft or "").strip(),
        unit_number=number,
        building=str(building or "").strip(),
        floor=str(floor or "").strip(),
        rent_low=rent_low,
        rent_high=rent_high or rent_low,
        availability_status="AVAILABLE",
        availability_date=str(availability_date or "").strip(),
        source_api_url=source_url,
        extraction_tier="STRICT_CURRENT_NATIVE_EVIDENCE",
        source_ids=source_ids,
    )
    # Every admitted source explicitly publishes this native per-unit key.
    # Carry it as unit_id for the local identity classifier without inventing
    # a hash or deriving identity from a plan label.
    standard["unit_id"] = native
    result = {
        "native_unit_id": native,
        "unit_number": number,
        "floor_plan_id": str(floor_plan_id or "").strip(),
        "floor_plan_name": str(floor_plan_name or "").strip(),
        "bedrooms": str(bedrooms or "").strip(),
        "bathrooms": str(bathrooms or "").strip(),
        "sqft": str(sqft or "").strip(),
        "building": str(building or "").strip(),
        "floor": str(floor or "").strip(),
        "rent_low": rent_low,
        "rent_high": rent_high or rent_low,
        "availability_date": str(availability_date or "").strip(),
        "source_ids": source_ids,
        "source_url": source_url,
        "_standard": standard,
    }
    if extra:
        result.update(extra)
    return result


def property_result(
    *,
    property_id: int,
    adapter: str,
    tier: str,
    expected_units: int,
    rows: list[dict],
    property_boundary: str,
    source_urls: list[str],
    local_artifacts: list[Path],
    property_identity_match: bool,
    contamination_verdict: str = "pass_exact_property_native_rows_only",
    validation_notes: dict | None = None,
) -> dict:
    native_ids = [row["native_unit_id"] for row in rows]
    standards = [row["_standard"] for row in rows]
    anchors = [unit_has_real_anchor(row) for row in standards]
    rents = [positive_rent(row) for row in standards]
    source_urls = list(dict.fromkeys(source_urls))
    local_artifacts = list(dict.fromkeys(local_artifacts))
    all_passed = bool(
        property_identity_match
        and len(rows) == expected_units
        and all(native_ids)
        and len(set(native_ids)) == len(native_ids)
        and all(anchors)
        and all(rents)
    )
    serial_rows = []
    for row in rows:
        clean = dict(row)
        clean.pop("_standard", None)
        serial_rows.append(clean)
    meta = PROPERTY_META[property_id]
    return {
        "property_id": property_id,
        "property_name": meta["name"],
        "website": meta["configured"],
        "current_exact_website": meta["current"],
        "adapter": adapter,
        "tier": tier,
        "outcome": "UNIT_QUALIFIED" if all_passed else "UNIT_UNVERIFIED",
        "raw_extractor_outcome": "UNITS" if rows else "EMPTY",
        "units": len(rows),
        "plans": len({row["floor_plan_id"] for row in rows if row["floor_plan_id"]}),
        "property_identity_match": property_identity_match,
        "contamination_verdict": contamination_verdict,
        "counts_toward_strict_207_gate": all_passed,
        "identity_evidence": {
            "rows_with_native_identity": sum(bool(x) for x in native_ids),
            "rows_with_native_identity_and_positive_rent": sum(rents),
            "distinct_native_unit_ids": len(set(native_ids)),
            "distinct_visible_unit_numbers": len(
                {row["unit_number"] for row in rows if row["unit_number"]}
            ),
            "property_boundary": property_boundary,
            "source_urls": source_urls,
            "local_artifacts": [str(path) for path in local_artifacts],
            "local_artifact_sha256": {
                str(path): sha256_file(path) for path in local_artifacts if path.exists()
            },
        },
        "native_units": serial_rows,
        "identity_samples": serial_rows[:3],
        "local_validation": {
            "expected_native_unit_count": expected_units,
            "observed_native_unit_count": len(rows),
            "unit_has_real_anchor_gate": all(anchors),
            "positive_rent_gate": all(rents),
            "unique_native_identity_gate": len(set(native_ids)) == len(native_ids),
            "property_boundary_gate": property_identity_match,
            "all_passed": all_passed,
            **(validation_notes or {}),
        },
        "errors": [],
    }


def parse_coventry() -> dict:
    source_url = "https://clients.spherexx.com/kamson_availability/availability.asp?id=noegpbnd"
    raw_path = ROOT / "live/70255_7bef6f324eba.html"
    soup = BeautifulSoup(raw_path.read_text(errors="replace"), "html.parser")
    rows: list[dict] = []
    for tr in soup.select("tr[data-unitid][data-floorplanid]"):
        cells = {
            str(td.get("data-label") or "").strip(): td.get_text(" ", strip=True)
            for td in tr.select("td[data-label]")
        }
        rent_low, rent_high = money_bounds(cells.get("Rent"))
        visible_date = cells.get("Availability", "")
        rows.append(
            evidence_row(
                native_unit_id=tr.get("data-unitid"),
                unit_number=cells.get("Unit"),
                floor_plan_id=tr.get("data-floorplanid"),
                bedrooms=cells.get("Bedroom", "").replace(" BR", ""),
                bathrooms=cells.get("Bathroom", "").replace(" BA", ""),
                building=cells.get("Building"),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_date=CAPTURE_DATE if visible_date == "Immediate" else visible_date,
                source_url=source_url,
                source_ids={
                    "spherexx_unit_id": tr.get("data-unitid"),
                    "spherexx_floorplan_id": tr.get("data-floorplanid"),
                    "property_id": tr.select_one("td[data-id]").get("data-id"),
                },
                extra={"visible_availability": visible_date},
            )
        )
    property_ids = {
        row["source_ids"].get("property_id") for row in rows if row["source_ids"].get("property_id")
    }
    return property_result(
        property_id=70255,
        adapter="spherexx_html_roster",
        tier="TIER_1_PUBLIC_SPHEREXX_AVAILABILITY",
        expected_units=8,
        rows=rows,
        property_boundary=(
            "Coventry Square's exact current marketing floor-plan page publishes the opaque "
            "Spherexx availability id=noegpbnd; every admitted roster row carries the same "
            "property data-id 6566 and a native data-unitid."
        ),
        source_urls=[source_url, "https://coventrysquareapartments.com/floor-plans/"],
        local_artifacts=[raw_path],
        property_identity_match=property_ids == {"6566"},
        validation_notes={"single_source_property_id_6566": property_ids == {"6566"}},
    )


def parse_banyan() -> dict:
    source_url = (
        "https://api.union.build/api/v1/chat/settings/"
        "?property_id=400"
    )
    raw_path = ROOT / "probes/258661_union_chat_settings.json"
    payload = json.loads(raw_path.read_text())
    rows: list[dict] = []
    exact_address = True
    for floor_plan in payload.get("floor_plans") or []:
        for unit in floor_plan.get("units") or []:
            if unit.get("status") != "AVAILABLE" or not unit.get("active"):
                continue
            rent_low = int(float(unit.get("market_rent") or 0)) or None
            exact_address = exact_address and (
                unit.get("property") == 400
                and unit.get("street") == "5353 East Washington Street"
                and unit.get("city") == "Phoenix"
                and unit.get("state") == "AZ"
                and str(unit.get("zip")) == "85034"
            )
            rows.append(
                evidence_row(
                    native_unit_id=unit.get("id"),
                    unit_number=unit.get("unit"),
                    floor_plan_id=floor_plan.get("id"),
                    floor_plan_name=floor_plan.get("display_name") or floor_plan.get("plan"),
                    bedrooms=floor_plan.get("bedrooms"),
                    bathrooms=floor_plan.get("bathrooms"),
                    sqft=floor_plan.get("square_footage"),
                    building=unit.get("building_name"),
                    floor=unit.get("floor_level"),
                    rent_low=rent_low,
                    availability_date=unit.get("available_date"),
                    source_url=source_url,
                    source_ids={
                        "union_resman_unit_id": unit.get("id"),
                        "union_resman_external_unit_id": unit.get("external_id"),
                        "property_id": unit.get("property"),
                        "floor_plan_id": floor_plan.get("id"),
                    },
                    extra={"pms_sync_date": unit.get("pms_sync_date")},
                )
            )
    property_identity = bool(
        payload.get("is_valid_property")
        and payload.get("customer_name") == "Mark Taylor"
        and exact_address
        and all(fp.get("property") == "Banyan on Washington" for fp in payload.get("floor_plans") or [])
    )
    return property_result(
        property_id=258661,
        adapter="union_resman_chat_bootstrap",
        tier="TIER_1_PUBLIC_UNION_RESMAN_BOOTSTRAP",
        expected_units=7,
        rows=rows,
        property_boundary=(
            "Public bootstrap validates property 400, names Banyan on Washington, and every "
            "admitted AVAILABLE row carries the exact 5353 East Washington Street address."
        ),
        source_urls=[source_url, "https://banyanonwashington.com/"],
        local_artifacts=[raw_path, ROOT / "probes/258661_union_chat_settings.headers"],
        property_identity_match=property_identity,
        validation_notes={"all_available_rows_exact_address": exact_address},
    )


def parse_vista() -> dict:
    client_uuid = "54523ff9-0329-43dd-83b4-3066820f136e"
    source_url = (
        "https://ares.betternoi.com/api/pub/v1/client/building/unit"
        f"?client_uuid={client_uuid}&is_available=true"
    )
    raw_path = ROOT / "probes3/55709_betternoi_all_b590dedd1e2b.body"
    payload = json.loads(raw_path.read_text())
    rows: list[dict] = []
    exact_address = True
    for unit in payload.get("results") or []:
        rent_low = int(float(unit.get("min_rent") or 0)) or None
        rent_high = int(float(unit.get("max_rent") or 0)) or rent_low
        exact_address = exact_address and (
            unit.get("client_uuid") == client_uuid
            and unit.get("building_address") == "2981 Ridge Avenue"
            and unit.get("building_city") == "Macon"
            and unit.get("building_state") == "GA"
            and str(unit.get("building_postal_code")) == "31210"
        )
        floor_plan = unit.get("floor_plan") or {}
        rows.append(
            evidence_row(
                native_unit_id=unit.get("uuid"),
                unit_number=unit.get("unit_number"),
                floor_plan_id=floor_plan.get("uuid"),
                floor_plan_name=floor_plan.get("name"),
                bedrooms=unit.get("bedroom_count"),
                bathrooms=unit.get("bathroom_count"),
                sqft=unit.get("min_square_feet"),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_date=unit.get("adjusted_available_date"),
                source_url=source_url,
                source_ids={
                    "betternoi_unit_uuid": unit.get("uuid"),
                    "betternoi_unit_id": unit.get("id"),
                    "property_id": unit.get("client_uuid"),
                    "floor_plan_id": floor_plan.get("uuid"),
                },
                extra={"availability_status": unit.get("availability_status")},
            )
        )
    return property_result(
        property_id=55709,
        adapter="betternoi_public_units",
        tier="TIER_1_PUBLIC_BETTERNOI_API",
        expected_units=7,
        rows=rows,
        property_boundary=(
            "The exact current Vista Pointe floor-plan page embeds client UUID "
            f"{client_uuid}; every API row carries that UUID and 2981 Ridge Avenue, Macon, GA 31210."
        ),
        source_urls=[source_url, "https://livevistapointeapts.com/en/floor-plans"],
        local_artifacts=[
            raw_path,
            ROOT / "probes3/55709_betternoi_all_b590dedd1e2b.headers",
            ROOT / "probes2/55709_exact_floorplans_1353f17f02f7.body",
        ],
        property_identity_match=exact_address and payload.get("count") == 7,
        validation_notes={"all_rows_exact_client_uuid_and_address": exact_address},
    )


def parse_browser_rows() -> list[dict]:
    raw_path = ROOT / "browser_rows_current_strict.json"
    payload = json.loads(raw_path.read_text())
    results: list[dict] = []

    wood_rows: list[dict] = []
    wood_property_ids: set[str] = set()
    wood_urls: list[str] = []
    for group in payload["woodmont"]["results"]:
        wood_urls.append(group["url"])
        plan_name = group["heading"].split(" - ", 1)[0]
        for raw in group["rows"]:
            rent_low, rent_high = money_bounds(raw["rent_text"])
            wood_property_ids.add(str(raw["property_id"]))
            wood_rows.append(
                evidence_row(
                    native_unit_id=raw["native_unit_id"],
                    unit_number=raw["unit_number"],
                    floor_plan_id=raw["floorplan_id"],
                    floor_plan_name=plan_name,
                    sqft=raw["sqft"],
                    rent_low=rent_low,
                    rent_high=rent_high,
                    availability_date=raw["available_date"],
                    source_url=group["url"],
                    source_ids={
                        "securecafe_apartment_id": raw["native_unit_id"],
                        "securecafe_floorplan_id": raw["floorplan_id"],
                        "property_id": raw["property_id"],
                    },
                )
            )
    results.append(
        property_result(
            property_id=60145,
            adapter="securecafe_rendered_roster",
            tier="TIER_1_CURRENT_CHROME_SECURECAFE_ROSTER",
            expected_units=10,
            rows=wood_rows,
            property_boundary=(
                "The exact Woodmont Mews marketing site publishes four SecureCafe plan links; "
                "all rendered roster rows carry property id 144461 and the page footer confirms "
                "1345 Martin Court, Bethlehem, PA 18018."
            ),
            source_urls=[payload["woodmont"]["marketing_url"], *wood_urls],
            local_artifacts=[raw_path],
            property_identity_match=wood_property_ids == {"144461"},
            validation_notes={
                "single_securecafe_property_id_144461": wood_property_ids == {"144461"},
                "captcha_used": payload.get("captcha_used"),
            },
        )
    )

    wild = payload["wildwood"]["result"]
    wild_rows: list[dict] = []
    for raw in wild["units"]:
        sqft_match = re.search(r"12mo lease\s*\n(\d+)", raw.get("row_text") or "")
        wild_rows.append(
            evidence_row(
                native_unit_id=raw["native_unit_id"],
                unit_number=raw["unit_number"],
                floor_plan_id=raw["floorplan_id"],
                floor_plan_name="1x1 Modernized",
                bedrooms=raw["beds"],
                bathrooms=raw["baths"],
                sqft=sqft_match.group(1) if sqft_match else "",
                rent_low=int(float(raw["rent_min"])),
                rent_high=int(float(raw["rent_max"])),
                availability_date=raw["available_date"],
                source_url=wild["url"],
                source_ids={
                    "entrata_uid": raw["native_unit_id"],
                    "entrata_fpid": raw["floorplan_id"],
                    "property_id": "211184",
                },
            )
        )
    results.append(
        property_result(
            property_id=15014,
            adapter="entrata_prospect_portal_unit_cards",
            tier="TIER_1_CURRENT_CHROME_ENTRATA_UNIT_CARDS",
            expected_units=11,
            rows=wild_rows,
            property_boundary=(
                "Exact Wildwood Park plan page title/address identifies 5550 Columbia Pike, "
                "Arlington, VA 22204; its calculator link carries property id 211184 and every "
                "modal row publishes native data-unit/data-floorplan attributes."
            ),
            source_urls=[payload["wildwood"]["marketing_url"], wild["url"]],
            local_artifacts=[raw_path],
            property_identity_match=(
                wild.get("address") is True
                and "property[id]=211184" in wild.get("propertyId", "")
                and {row["source_ids"]["property_id"] for row in wild_rows} == {"211184"}
            ),
            validation_notes={"captcha_used": payload.get("captcha_used")},
        )
    )

    sentral_rows: list[dict] = []
    sentral_property_ids: set[str] = set()
    for raw in payload["sentral"]["rows"]:
        rent_low, rent_high = money_bounds(raw["rent_text"])
        sentral_property_ids.add(str(raw["property_id"]))
        sentral_rows.append(
            evidence_row(
                native_unit_id=raw["native_unit_id"],
                unit_number=raw["unit_number"],
                floor_plan_id=raw["floorplan_id"],
                floor_plan_name=raw["floor_plan_name"],
                bedrooms=raw["beds"],
                bathrooms=raw["baths"],
                sqft=raw["sqft"],
                rent_low=rent_low,
                rent_high=rent_high,
                availability_date=raw["available_date"],
                source_url=payload["sentral"]["source_url"],
                source_ids={
                    "securecafe_apartment_id": raw["native_unit_id"],
                    "securecafe_floorplan_id": raw["floorplan_id"],
                    "property_id": raw["property_id"],
                },
                extra={"visible_availability": raw["visible_available"]},
            )
        )
    results.append(
        property_result(
            property_id=78597,
            adapter="securecafe_rendered_roster",
            tier="TIER_1_CURRENT_CHROME_SECURECAFE_ROSTER",
            expected_units=51,
            rows=sentral_rows,
            property_boundary=(
                "Sentral's current exact Contentful property entry names Sentral Union Station, "
                "1777 Wewatta St, Denver, CO 80202 and publishes SecureCafe property id 1707719; "
                "all 51 admitted roster rows carry that same id."
            ),
            source_urls=[
                payload["sentral"]["marketing_url"],
                payload["sentral"]["source_url"],
                (
                    "https://cdn.contentful.com/spaces/ech69gzmnnzr/environments/master/entries/"
                    "entrataPropertyId-731641"
                ),
            ],
            local_artifacts=[raw_path, ROOT / "sentral_union_page.html"],
            property_identity_match=sentral_property_ids == {"1707719"},
            validation_notes={
                "single_securecafe_property_id_1707719": sentral_property_ids == {"1707719"},
                "captcha_used": payload.get("captcha_used"),
            },
        )
    )
    return results


def parse_onyx() -> dict:
    raw_path = ROOT / "onyx_floorplans_api.json"
    raw_floorplans = json.loads(raw_path.read_text())
    parsed = parse_apts247_floorplans({"objects": raw_floorplans}, APTS247_URL)
    parsed = [
        row for row in parsed if (row.get("source_ids") or {}).get("apts247_unit_id")
    ]
    source_lookup: dict[str, dict] = {}
    exact_identity = True
    for floor_plan in raw_floorplans:
        community = floor_plan.get("community") or {}
        exact_identity = exact_identity and (
            community.get("name") == "Onyx Uptown PHX"
            and community.get("address") == "500 W Camelback Road"
            and community.get("city") == "Phoenix"
            and community.get("state") == "AZ"
            and str(community.get("zip_code")) == "85013"
        )
        for unit in floor_plan.get("units") or []:
            source_lookup[str(unit.get("id"))] = unit
    rows: list[dict] = []
    listable_uids: set[str] = set()
    for row in parsed:
        source_ids = row.get("source_ids") or {}
        native = str(source_ids.get("apts247_unit_id") or "")
        raw = source_lookup[native]
        match = re.search(r"[?&]listable_uid=([0-9a-f-]+)", raw.get("availability_link") or "")
        listable_uid = match.group(1) if match else ""
        if listable_uid:
            listable_uids.add(listable_uid)
        rows.append(
            evidence_row(
                native_unit_id=native,
                unit_number=row.get("unit_number"),
                floor_plan_id=source_ids.get("apts247_floor_plan_id"),
                floor_plan_name=row.get("floor_plan_name"),
                bedrooms=row.get("bedrooms"),
                bathrooms=row.get("bathrooms"),
                sqft=row.get("sqft"),
                building=row.get("building"),
                floor=row.get("floor"),
                rent_low=row.get("market_rent_low"),
                rent_high=row.get("market_rent_high"),
                availability_date=row.get("availability_date"),
                source_url=APTS247_URL,
                source_ids={
                    **source_ids,
                    "apts247_unit_uuid": raw.get("uuid"),
                    "appfolio_listable_uid": listable_uid,
                },
            )
        )
    return property_result(
        property_id=260505,
        adapter="apts247",
        tier="TIER_1_API_APTS247_FLOORPLANS",
        expected_units=15,
        rows=rows,
        property_boundary=(
            "The configured Broadstone domain is parked, but the exact current official "
            "Onyx Uptown PHX site and public apts247 response both identify 500 W Camelback "
            "Road, Phoenix, AZ 85013; only its 15 embedded unit rows are admitted."
        ),
        source_urls=[APTS247_URL, "https://www.onyxuptownphx.com/floorplans/"],
        local_artifacts=[
            raw_path,
            ROOT / "onyx_floorplans_api.headers",
            ROOT / "onyx_floorplans.html",
        ],
        property_identity_match=exact_identity and len(listable_uids) == 15,
        validation_notes={
            "production_parser": "parse_apts247_floorplans",
            "distinct_appfolio_listable_uids": len(listable_uids),
            "all_rows_exact_community_address": exact_identity,
        },
    )


def fetch_current(url: str) -> tuple[bytes, str, dict[str, str], int]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/138 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return (
            response.read(),
            response.geturl(),
            {str(k): str(v) for k, v in response.headers.items()},
            int(response.status),
        )


def parse_julington() -> dict:
    output_dir = ROOT / "julington_current"
    output_dir.mkdir(parents=True, exist_ok=True)
    fetch_manifest: list[dict] = []

    index_bytes, index_final, index_headers, index_status = fetch_current(JULINGTON_INDEX)
    index_path = output_dir / "263498_floorplans_index.html"
    index_path.write_bytes(index_bytes)
    index_html = index_bytes.decode("utf-8", errors="replace")
    match = re.search(
        r'<script type="application/json" id="jd-fp-data-script-app">(.*?)</script>',
        index_html,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError("Julington current floor-plan app payload is missing")
    app_payload = json.loads(match.group(1))
    app_units = app_payload.get("units") or []
    app_native_ids = {
        str((unit.get("engrain_data") or {}).get("unit_id") or unit.get("id_value") or "")
        for unit in app_units
    }
    app_property_ids = {str(unit.get("property_id") or "") for unit in app_units}
    fetch_manifest.append(
        {
            "requested_url": JULINGTON_INDEX,
            "final_url": index_final,
            "status": index_status,
            "headers": index_headers,
            "local_artifact": str(index_path),
            "sha256": sha256_bytes(index_bytes),
        }
    )

    parsed_rows: list[dict] = []
    source_urls: list[str] = [JULINGTON_INDEX]
    plan_paths: list[Path] = [index_path]
    expected_by_plan = 0
    for plan in app_payload.get("floorplans") or []:
        expected_by_plan += int(plan.get("availability_count") or 0)
        slug = str(plan.get("slug") or "").strip()
        if not slug:
            continue
        plan_url = urljoin(JULINGTON_INDEX, f"{slug}/")
        body, final_url, headers, status = fetch_current(plan_url)
        plan_path = output_dir / f"263498_{slug}.html"
        plan_path.write_bytes(body)
        plan_paths.append(plan_path)
        source_urls.append(final_url)
        fetch_manifest.append(
            {
                "requested_url": plan_url,
                "final_url": final_url,
                "status": status,
                "headers": headers,
                "local_artifact": str(plan_path),
                "sha256": sha256_bytes(body),
            }
        )
        parsed_rows.extend(
            parse_jonah_resource_json(body.decode("utf-8", errors="replace"), final_url)
        )

    fetch_manifest_path = output_dir / "263498_fetch_manifest.json"
    fetch_manifest_path.write_text(json.dumps(fetch_manifest, indent=2))
    plan_paths.append(fetch_manifest_path)

    rows: list[dict] = []
    parser_native_ids: set[str] = set()
    for row in parsed_rows:
        source_ids = row.get("source_ids") or {}
        native = str(source_ids.get("sightmap_unit_id") or row.get("unit_id") or "")
        native = native.removeprefix("sightmap_unit_id-")
        parser_native_ids.add(native)
        rows.append(
            evidence_row(
                native_unit_id=native,
                unit_number=row.get("unit_number"),
                floor_plan_id="",
                floor_plan_name=row.get("floor_plan_name"),
                bedrooms=row.get("bedrooms"),
                bathrooms=row.get("bathrooms"),
                sqft=row.get("sqft"),
                building=row.get("building"),
                floor=row.get("floor"),
                rent_low=row.get("market_rent_low"),
                rent_high=row.get("market_rent_high"),
                availability_date=row.get("availability_date"),
                source_url=row.get("source_api_url") or JULINGTON_INDEX,
                source_ids=source_ids,
            )
        )

    # The current Jonah resource parser correctly returns 24 rows, but three
    # one-unit plans (Iris, Savannah, Birch) publish
    # ``availability_count: 0`` on the unit object while simultaneously
    # rendering "Available <date>", positive lease pricing, a native SightMap
    # id, an application link, and an overview plan count of one. The parser's
    # boolean-style filter therefore drops those three exact physical rows.
    # This evidence-only validator admits them from the same current overview
    # payload and records the discrepancy explicitly; no production source is
    # changed in this lane.
    visible_missing_rows = []
    for raw in app_units:
        engrain = raw.get("engrain_data") or {}
        native = str(engrain.get("unit_id") or raw.get("id_value") or "")
        if not native or native in parser_native_ids:
            continue
        price_entity = raw.get("price_entity") or {}
        adjusted = price_entity.get("adjusted") or {}
        rent_low, rent_high = money_bounds(
            adjusted.get("low_no_fees")
            or raw.get("rent_min")
            or price_entity.get("priceLow")
        )
        visible = str(raw.get("available_display") or "")
        if not visible.startswith("Available") or not rent_low:
            continue
        visible_missing_rows.append(native)
        rows.append(
            evidence_row(
                native_unit_id=native,
                unit_number=raw.get("apartment_number"),
                floor_plan_id=raw.get("floorplan_id"),
                floor_plan_name=raw.get("floorplan_title"),
                bedrooms=raw.get("bedrooms"),
                bathrooms=raw.get("bathrooms"),
                sqft=raw.get("square_feet"),
                building=raw.get("building"),
                floor=engrain.get("floor_name"),
                rent_low=rent_low,
                rent_high=rent_high,
                availability_date=price_entity.get("date"),
                source_url=JULINGTON_INDEX,
                source_ids={
                    "sightmap_unit_id": native,
                    "property_id": raw.get("property_id"),
                    "floor_plan_id": raw.get("floorplan_id"),
                },
                extra={
                    "visible_availability": visible,
                    "source_unit_availability_count": raw.get("availability_count"),
                    "evidence_note": "visible one-unit plan dropped by current Jonah parser filter",
                },
            )
        )

    admitted_native_ids = {row["native_unit_id"] for row in rows}

    exact_identity = bool(
        "The Julington" in index_html
        and "12397 San Jose" in index_html
        and app_property_ids == {"18807"}
        and len(app_units) == 27
        and expected_by_plan == 27
        and parser_native_ids < app_native_ids
        and len(parser_native_ids) == 24
        and len(visible_missing_rows) == 3
        and admitted_native_ids == app_native_ids
    )
    return property_result(
        property_id=263498,
        adapter="encoreskyline_template_jonah_resource",
        tier="TIER_1_API_JONAH_RESOURCE",
        expected_units=27,
        rows=rows,
        property_boundary=(
            "Current exact official site names The Julington at 12397 San Jose Blvd, "
            "Jacksonville, FL 32223; its overview contains exactly 27 property-18807 unit "
            "records. Production parse_jonah_resource_json reproduces 24; three visibly "
            "available one-unit plans carry availability_count=0 and are admitted directly "
            "from the exact overview payload. Together they match all 27 SightMap native ids."
        ),
        source_urls=source_urls,
        local_artifacts=plan_paths,
        property_identity_match=exact_identity,
        validation_notes={
            "production_parser": "parse_jonah_resource_json",
            "production_parser_rows": len(parser_native_ids),
            "visible_one_unit_plan_rows_added_from_overview": len(visible_missing_rows),
            "visible_one_unit_plan_native_ids": sorted(visible_missing_rows),
            "overview_units": len(app_units),
            "overview_property_ids": sorted(app_property_ids),
            "sum_floorplan_availability_count": expected_by_plan,
            "production_parser_native_ids_are_strict_subset": parser_native_ids < app_native_ids,
            "admitted_native_ids_match_overview": admitted_native_ids == app_native_ids,
        },
    )


def result_source_urls(row: dict) -> list[str]:
    return list(row.get("identity_evidence", {}).get("source_urls") or [])


def sample_native_ids(row: dict) -> list[str]:
    native_units = row.get("native_units") or []
    if native_units:
        return [str(x.get("native_unit_id") or "") for x in native_units[:3]]
    samples = row.get("identity_samples") or []
    found: list[str] = []
    for sample in samples:
        ids = sample.get("source_ids") or {}
        candidate = next(
            (
                ids.get(key)
                for key in (
                    "appfolio_listable_uid",
                    "appfolio_id",
                    "apts247_unit_id",
                    "securecafe_apartment_id",
                    "sightmap_unit_id",
                )
                if ids.get(key)
            ),
            None,
        )
        if not candidate:
            candidate = (sample.get("identity") or {}).get("unit_id") or (
                sample.get("identity") or {}
            ).get("unit_number")
        if candidate:
            found.append(str(candidate))
    return found[:3]


def main() -> None:
    generic_results = [
        parse_coventry(),
        parse_banyan(),
        parse_vista(),
        *parse_browser_rows(),
        parse_onyx(),
        parse_julington(),
    ]
    generic_results.sort(key=lambda row: row["property_id"])
    if not all(row["counts_toward_strict_207_gate"] for row in generic_results):
        failed = [row["property_id"] for row in generic_results if not row["counts_toward_strict_207_gate"]]
        raise AssertionError(f"generic strict validation failed for {failed}")
    if len(generic_results) != 8 or sum(row["units"] for row in generic_results) != 136:
        raise AssertionError("expected eight generic properties / 136 native units")

    generic_artifact = {
        "batch_label": "appfolio-generic-remaining-generic-current-strict",
        "capture_date": CAPTURE_DATE,
        "evidence_is_current_live": True,
        "direct_public_sources": True,
        "chrome_rendered_sources": [15014, 60145, 78597],
        "hyperbrowser_used": False,
        "captcha_used": False,
        "paid_canary_run": False,
        "source_edits_performed": False,
        "results": generic_results,
        "strict_qualified_property_ids": [row["property_id"] for row in generic_results],
        "strict_qualified_properties": len(generic_results),
        "strict_native_positive_rent_units": sum(row["units"] for row in generic_results),
    }
    GENERIC_ARTIFACT.write_text(json.dumps(generic_artifact, indent=2))

    appfolio_path = ROOT / "evidence_appfolio_generic_appfolio_current_strict.json"
    appfolio_artifact = json.loads(appfolio_path.read_text())
    appfolio_names = {38107: "Fountain Place", 46576: "Campus Pointe", 282381: "Mission Ranch"}
    appfolio_results = deepcopy(appfolio_artifact["results"])
    for row in appfolio_results:
        if not row.get("property_name"):
            row["property_name"] = appfolio_names[row["property_id"]]
    all_results = sorted(appfolio_results + generic_results, key=lambda row: row["property_id"])
    if not all(row["counts_toward_strict_207_gate"] for row in all_results):
        raise AssertionError("consolidated artifact contains a non-strict row")
    if len(all_results) != 11 or sum(row["units"] for row in all_results) != 167:
        raise AssertionError("expected eleven consolidated properties / 167 native units")

    consolidated = {
        "batch_label": "appfolio-generic-remaining-current-strict-consolidated",
        "capture_date": CAPTURE_DATE,
        "baseline_cohort": str(REMAINING),
        "evidence_is_current_live": True,
        "hyperbrowser_used": False,
        "captcha_used": False,
        "paid_canary_run": False,
        "source_edits_performed": False,
        "component_artifacts": [str(appfolio_path), str(GENERIC_ARTIFACT)],
        "results": all_results,
        "strict_qualified_property_ids": [row["property_id"] for row in all_results],
        "strict_qualified_properties": len(all_results),
        "strict_native_positive_rent_units": sum(row["units"] for row in all_results),
    }
    CONSOLIDATED_ARTIFACT.write_text(json.dumps(consolidated, indent=2))

    with LEDGER.open(newline="", encoding="utf-8-sig") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger_ids = {int(row["property_id"]) for row in ledger_rows}
    with REMAINING.open(newline="", encoding="utf-8-sig") as handle:
        remaining_ids = {int(row["property_id"]) for row in csv.DictReader(handle)}
    qualified_ids = {row["property_id"] for row in all_results}
    if not qualified_ids <= remaining_ids:
        raise AssertionError(
            f"strict winners absent from current remaining cohort: {sorted(qualified_ids - remaining_ids)}"
        )

    net_new_results = [row for row in all_results if row["property_id"] not in ledger_ids]
    fieldnames = [
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
    with NET_NEW_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in net_new_results:
            evidence = row.get("identity_evidence") or {}
            writer.writerow(
                {
                    "property_id": row["property_id"],
                    "property_name": row["property_name"],
                    "website": row["website"],
                    "evidence_lane": "appfolio_generic_current_native_strict",
                    "artifact": str(CONSOLIDATED_ARTIFACT),
                    "units": row["units"],
                    "property_identity_match": row["property_identity_match"],
                    "contamination_verdict": row["contamination_verdict"],
                    "native_identity_rows": evidence.get("rows_with_native_identity", row["units"]),
                    "native_positive_rent_rows": evidence.get(
                        "rows_with_native_identity_and_positive_rent", row["units"]
                    ),
                    "source_urls": " | ".join(result_source_urls(row)),
                    "sample_native_unit_ids": " | ".join(sample_native_ids(row)),
                    "local_validation": "artifact_backed_no_paid_canary",
                }
            )

    net_new_summary = {
        "capture_date": CAPTURE_DATE,
        "baseline_ledger": str(LEDGER),
        "baseline_ledger_rows": len(ledger_rows),
        "baseline_ledger_unique_property_ids": len(ledger_ids),
        "baseline_ledger_sha256": sha256_file(LEDGER),
        "remaining_cohort": str(REMAINING),
        "remaining_cohort_sha256": sha256_file(REMAINING),
        "consolidated_artifact": str(CONSOLIDATED_ARTIFACT),
        "net_new_ledger_rows_csv": str(NET_NEW_CSV),
        "qualified_property_ids": sorted(qualified_ids),
        "overlap_with_baseline_ledger": sorted(qualified_ids & ledger_ids),
        "net_new_strict_property_ids": [row["property_id"] for row in net_new_results],
        "net_new_strict_properties": len(net_new_results),
        "net_new_native_positive_rent_units": sum(row["units"] for row in net_new_results),
        "shared_ledger_modified": False,
    }
    NET_NEW_JSON.write_text(json.dumps(net_new_summary, indent=2))
    print(json.dumps(net_new_summary, indent=2))


if __name__ == "__main__":
    main()
