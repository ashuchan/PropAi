#!/usr/bin/env python3
"""Materialize the strict, property-bound vendor-tail audit from captured evidence."""

from __future__ import annotations

import csv
import gzip
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from ma_poc.pms.adapters.onesite import parse_onesite_workflowstartup
from ma_poc.pms.adapters.resman import _extract_unittypes, parse_resman_unittypes


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
TMP = Path("/private/tmp")
OUT = ROOT / "evidence_vendor_tail30_strict.json"
LEDGER_OUT = ROOT / "strict_vendor_tail30_ledger_rows.csv"
NET_NEW_LEDGER_OUT = ROOT / "strict_vendor_tail30_net_new_ledger_rows.csv"

ADAPTERS = {
    37798: "resman", 32097: "resman", 11089: "resman",
    18389: "g5", 68956: "g5", 220109: "g5", 6274: "g5",
    25489: "cortland", 218378: "equity",
    234945: "sightmap", 230598: "sightmap", 63191: "sightmap",
    60141: "sightmap", 43995: "sightmap", 8119: "sightmap", 8740: "sightmap",
    273828: "funnel", 273790: "funnel", 218177: "funnel",
    8789: "funnel", 270672: "funnel", 47182: "funnel",
    8654: "apts247", 72766: "repli360",
    61459: "knock", 68497: "knock", 48946: "knock",
    12586: "essex", 17984: "essex", 2282: "essex",
}

QUALIFYING_IDS = {
    2282, 12586, 17984, 230598, 218177, 273828, 273790, 270672,
    37798, 11089, 72766, 18389, 220109, 6274, 8119,
}


def load_metadata() -> dict[int, dict]:
    out = {}
    with (ROOT / "failed344_input.csv").open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pid = int(row["apartmentid"])
            if pid not in ADAPTERS:
                continue
            out[pid] = {
                "property_id": pid,
                "property_name": row["name"],
                "address": row["address"],
                "city": row["city"],
                "state": row["state"],
                "zip": row["zip"],
                "website": row["website"],
                "cohort_adapter": ADAPTERS[pid],
            }
    assert set(out) == set(ADAPTERS), (set(ADAPTERS) - set(out), set(out) - set(ADAPTERS))
    return out


def clean_row(row: dict) -> dict:
    keys = (
        "unit_number", "unit_name", "floor_plan_name", "bedrooms", "bathrooms", "sqft",
        "market_rent_low", "market_rent_high", "availability_date", "available_date",
        "source_api_url", "extraction_tier", "source_ids",
    )
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def jsonld_units(path: Path) -> tuple[dict, list[dict]]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    payload = json.loads(soup.select_one('script[type="application/ld+json"]').get_text())
    about = payload["about"]
    rows = []
    for unit in about.get("containsPlace", []):
        offer = unit.get("offers") or {}
        rent = offer.get("price")
        number = re.sub(r"^APT\s+", "", str(unit.get("name") or ""), flags=re.I).strip()
        if not number or not isinstance(rent, (int, float)) or rent <= 0:
            continue
        rows.append({
            "unit_number": number,
            "bedrooms": unit.get("numberOfBedrooms"),
            "bathrooms": unit.get("numberOfBathroomsTotal"),
            "sqft": (unit.get("floorSize") or {}).get("value"),
            "market_rent_low": rent,
            "market_rent_high": rent,
            "availability_date": offer.get("availabilityStarts"),
        })
    return {"name": about.get("name"), "address": about.get("address")}, rows


def qualifier(metadata: dict[int, dict], pid: int, lane: str, rows: list[dict], *,
              sources: list[str], artifacts: list[str], boundary: dict,
              contamination: str = "pass_exact_property_boundary_no_sibling_contamination",
              complete_rows: bool = True, extra: dict | None = None) -> dict:
    native = [clean_row(row) for row in rows]
    ids = [str(row.get("unit_number", "")).strip() for row in native if row.get("unit_number")]
    assert native and all((row.get("market_rent_low") or 0) > 0 for row in native)
    assert len(ids) == len(set(ids)) == len(native), (pid, len(ids), len(set(ids)), len(native))
    result = {
        **metadata[pid],
        "outcome": "UNIT_QUALIFIED",
        "evidence_lane": lane,
        "native_priced_unit_count": len(native),
        "distinct_native_unit_count": len(set(ids)),
        "native_unit_ids": ids,
        "native_priced_rows": native,
        "unit_rows_complete": complete_rows,
        "source_urls": sources,
        "artifact_paths": artifacts,
        "property_boundary_evidence": boundary,
        "contamination_verdict": contamination,
    }
    if extra:
        result.update(extra)
    return result


def exclusion(metadata: dict[int, dict], pid: int, reason: str, details: str,
              artifacts: list[str], sources: list[str] | None = None,
              boundary: dict | None = None, contamination: str = "no_qualifying_contamination") -> dict:
    return {
        **metadata[pid],
        "outcome": "EXCLUDED",
        "strict_exclusion_reason": reason,
        "details": details,
        "native_priced_unit_count": 0,
        "source_urls": sources or [],
        "artifact_paths": artifacts,
        "property_boundary_evidence": boundary or {},
        "contamination_verdict": contamination,
    }


def ledger_ids(path: Path) -> set[int]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {int(row["property_id"]) for row in csv.DictReader(f)}


def main() -> None:
    metadata = load_metadata()
    qualifiers = []

    # Exact SightMap JSON-LD embedded from each target property page.
    sightmaps = {
        230598: (TMP / "sightmap_230598_embed.html", "https://sightmap.com/app/api/v1/rxwjql4zv1e/sightmaps/100250", "https://sightmap.com/embed/rxwjq74zv1e"),
        273828: (TMP / "vendor_tail_273828_sightmap.html", "https://sightmap.com/embed/yjp2de1evxl", "https://www.pointegrandsouthlake.com/"),
        273790: (TMP / "vendor_tail_273790_sightmap.html", "https://sightmap.com/embed/n9w636emw71", "https://www.pointegrandaugusta.com/"),
        270672: (TMP / "vendor_tail_270672_sightmap.html", "https://sightmap.com/embed/9zw4zo6lv87", "https://www.pointegrandwarnerrobins.com/"),
    }
    for pid, (path, provider_url, parent_url) in sightmaps.items():
        identity, rows = jsonld_units(path)
        qualifiers.append(qualifier(
            metadata, pid, "exact_property_embedded_sightmap_jsonld", rows,
            sources=[parent_url, provider_url], artifacts=[str(path)],
            boundary={
                "exact_embed_from_target_page": True,
                "provider_property_name": identity["name"],
                "provider_postal_address": identity["address"],
                "single_property_unit_roster": True,
                "note": "The Ridge provider street number is 17920 vs cohort 17940; exact embed, name, city, state, and ZIP match. Apartment labels are unit identities, not conflicting addresses." if pid == 230598 else "Exact named property embed and single property roster.",
            },
        ))

    # Static RentPress/Funnel payload is explicitly bound to Mill & Main/property code 8671.
    mill_path = ROOT / "raw_all/218177.html.gz"
    mill_html = gzip.open(mill_path, "rt", encoding="utf-8", errors="replace").read()
    mill_data = json.loads(BeautifulSoup(mill_html, "html.parser").select_one("#rentpress-app")["data-floorplans"])
    mill_rows = []
    for fp in mill_data:
        if fp.get("floorplan_parent_property_code") != "8671" or fp.get("floorplan_parent_property_name") != "Mill & Main":
            continue
        for unit in fp.get("units") or []:
            rent = float(unit.get("unit_rent_best") or unit.get("unit_rent_base") or 0)
            if unit.get("unit_name") and rent > 0:
                mill_rows.append({
                    "unit_number": unit["unit_name"], "floor_plan_name": fp.get("floorplan_name"),
                    "bedrooms": unit.get("unit_bedrooms"), "bathrooms": unit.get("unit_bathrooms"),
                    "sqft": unit.get("unit_sqft"), "market_rent_low": rent,
                    "market_rent_high": rent, "availability_date": unit.get("unit_available_on"),
                    "source_api_url": unit.get("unit_availability_url"),
                })
    qualifiers.append(qualifier(
        metadata, 218177, "archived_exact_rentpress_payload", mill_rows,
        sources=["https://millandmain.com/apartments/mill-main/"], artifacts=[str(mill_path)],
        boundary={"parent_property_name": "Mill & Main", "parent_property_code": "8671", "exact_parent_link": "https://millandmain.com/apartments/mill-main/", "all_rows_same_property": True},
    ))

    # ResMan availability portals discovered from exact property registration/floor-plan pages.
    resman = {
        37798: (TMP / "vendor_tail_37798_portal.html", "https://stewart.myresman.com/Portal/Applicants/Availability?a=1041&p=68f5dd84-230b-4dad-9a2e-9d18dfaccdc2", {"portal_property_guid": "68f5dd84-230b-4dad-9a2e-9d18dfaccdc2", "account_id": "1041", "exact_property_floor_page": str(TMP / "vendor_tail_37798_floor.html")}),
        11089: (TMP / "vendor_tail_11089_portal.html", "https://livenjoy.myresman.com/Portal/Applicants/Availability?a=1588&p=844aaf4f-ea66-4db0-9289-f5297ce3b3de", {"portal_property_guid": "844aaf4f-ea66-4db0-9289-f5297ce3b3de", "account_id": "1588", "registration_name_address_match": "Village Green of Bear Creek, 1800 Fuller Wiser Rd, Euless TX 76039"}),
    }
    for pid, (path, url, boundary) in resman.items():
        payload = _extract_unittypes(path.read_text(encoding="utf-8", errors="replace"))
        rows = [row for row in parse_resman_unittypes(payload, url) if row.get("unit_number") and (row.get("market_rent_low") or 0) > 0]
        artifacts = [str(path), str(TMP / f"vendor_tail_{pid}_floor.html")]
        if pid == 11089:
            artifacts.append(str(TMP / "vendor_tail_11089_new.html"))
        qualifiers.append(qualifier(metadata, pid, "exact_resman_availability_portal", rows, sources=[url], artifacts=artifacts, boundary=boundary))

    # The marketing pages are G5, but their exact property OLL embeds expose native units.
    g5_evidence_path = ROOT / "evidence_g5_browser3.json"
    g5_evidence = json.loads(g5_evidence_path.read_text())
    for pid, site_id in ((18389, "1046799"), (220109, "1054659")):
        target = next(item for item in g5_evidence["targets"] if item["property_id"] == pid)
        network = next(item for item in target["network"] if "/workflowstartup/" in item["url"])
        rows = [row for row in parse_onesite_workflowstartup(json.loads(network["body_prefix"]), network["url"])
                if row.get("unit_number") and (row.get("market_rent_low") or 0) > 0]
        qualifiers.append(qualifier(
            metadata, pid, "exact_g5_page_embedded_realpage_oll", rows,
            sources=[target["requested_url"], network["url"]], artifacts=[str(g5_evidence_path), target["rendered_html_path"]],
            boundary={"marketing_page_name_address_exact": True, "page_config_realpage_id": site_id, "workflow_site_id": site_id, "site_id_match": True},
        ))

    onesite_6274_path = ROOT / "evidence_onesite_6274_direct.json"
    onesite_6274 = json.loads(onesite_6274_path.read_text())
    rows_6274 = [row for row in onesite_6274["native_priced_rows"] if row.get("unit_number") and (row.get("market_rent_low") or 0) > 0]
    qualifiers.append(qualifier(
        metadata, 6274, "exact_g5_page_embedded_realpage_oll", rows_6274,
        sources=[onesite_6274["marketing_page"], onesite_6274["request_url"]],
        artifacts=[str(onesite_6274_path), onesite_6274["raw_body_path"]],
        boundary={**onesite_6274["property_boundary"], "marketing_page_name_address_exact": True, "site_id_match": True},
    ))

    # Repli360 template points to a two-map SightMap asset. Select only the Fox Ridge submap.
    fox_path = TMP / "vendor_tail_72766_fox_api.dec.json"
    lake_path = TMP / "vendor_tail_72766_lakeview_api.dec.json"
    fox_data = json.loads(fox_path.read_text())["data"]
    lake_data = json.loads(lake_path.read_text())["data"]
    fps = {str(fp["id"]): fp for fp in fox_data["floor_plans"]}
    fox_rows = []
    for unit in fox_data["units"]:
        rent = unit.get("price")
        if not unit.get("unit_number") or not isinstance(rent, (int, float)) or rent <= 0:
            continue
        fp = fps.get(str(unit.get("floor_plan_id")), {})
        fox_rows.append({
            "unit_number": unit["unit_number"], "floor_plan_name": fp.get("name"),
            "bedrooms": fp.get("bedroom_count"), "bathrooms": fp.get("bathroom_count"),
            "sqft": unit.get("area"), "market_rent_low": rent, "market_rent_high": rent,
            "availability_date": unit.get("available_on"),
            "source_api_url": "https://sightmap.com/app/api/v1/yjp248qrvxl/sightmaps/108287",
        })
    qualifiers.append(qualifier(
        metadata, 72766, "repli360_exact_property_to_sightmap_submap", fox_rows,
        sources=["https://www.liveatfoxridge.com/", "https://sightmap.com/embed/8epm5y46w6d", "https://sightmap.com/app/api/v1/yjp248qrvxl/sightmaps/108287"],
        artifacts=[str(TMP / "vendor_tail_72766_repli.js"), str(TMP / "vendor_tail_72766_template.html"), str(TMP / "vendor_tail_72766_sightmap.html"), str(fox_path), str(lake_path)],
        boundary={
            "selected_submap_name": fox_data["share_landing_page"]["name"],
            "selected_submap_id": "108287",
            "all_selected_floorplans_prefixed": "FR / Fox Ridge",
            "excluded_sibling_submap_name": lake_data["share_landing_page"]["name"],
            "excluded_sibling_submap_id": "108288",
            "excluded_sibling_unit_count": len(lake_data["units"]),
        },
        contamination="pass_after_exact_submap_selection_sibling_lakeview_explicitly_excluded",
    ))

    # Essex rows were already materialized by the current adapter replay. The exact same-host
    # property API is strict; preserve its count and available native samples after a later 429.
    replay_path = ROOT / "evidence_vendor_tail30_current_adapter.json"
    replay = json.loads(replay_path.read_text())
    replay_by_id = {item["property_id"]: item for item in replay["results"]}
    for pid in (2282, 12586, 17984):
        item = replay_by_id[pid]
        samples = []
        for sample in item["identity_samples"]:
            samples.append({
                "unit_number": sample["identity"]["unit_number"],
                "floor_plan_name": sample.get("floor_plan_name"),
                "market_rent_low": sample["positive_rent_evidence"]["market_rent_low"],
                "market_rent_high": sample["positive_rent_evidence"]["market_rent_high"],
                "availability_date": sample.get("availability_date"),
                "source_api_url": sample.get("source_api_url"),
            })
        # Keep strict count separate from the two persisted samples; do not fabricate missing IDs.
        result = {
            **metadata[pid], "outcome": "UNIT_QUALIFIED", "evidence_lane": "exact_same_host_essex_property_api",
            "native_priced_unit_count": item["units"], "distinct_native_unit_count": item["identity_evidence"]["distinct_unit_numbers"],
            "native_unit_ids": [sample["unit_number"] for sample in samples], "native_priced_rows": samples,
            "unit_rows_complete": False, "unit_row_persistence_note": "Adapter replay persisted aggregate count and two native samples; exact API re-fetch later returned HTTP 429. No count is inferred from the samples.",
            "source_urls": item["identity_evidence"]["source_urls"], "artifact_paths": [str(replay_path)],
            "property_boundary_evidence": {"exact_property_api_id_in_url": True, "same_host_as_target": True, "native_identity_rows": item["identity_evidence"]["rows_with_native_identity"], "native_positive_rent_rows": item["identity_evidence"]["rows_with_native_identity_and_positive_rent"]},
            "contamination_verdict": "pass_exact_same_host_property_api_no_sibling_roster",
        }
        qualifiers.append(result)

    # One compliant Hyperbrowser session navigated three exact Entrata detail pages.
    # Count only the seven native unit_space IDs actually observed (the two unvisited plans
    # remain uncounted); no CAPTCHA was solved or interacted with.
    st_andrews_rows = []
    plan_names = {"1521": "Ardennes", "1522": "Bordeaux", "1520": "Cannes"}
    for plan_id in ("1521", "1522", "1520"):
        path = ROOT / f"hb_scully_8119_plan_{plan_id}.html"
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for button in soup.select("button.js-show-details[data-unit][data-date]"):
            row_node = button.find_parent("div", class_="option-row")
            text = " ".join(row_node.get_text(" ", strip=True).split()) if row_node else ""
            unit_number_match = re.search(r"\bUnit\s+(\S+)", text)
            rent_match = re.search(r"Rent\s+Starting from\s+\$([\d,]+)", text, re.I)
            sqft_match = re.search(r"Sq\.ft\.\s+([\d,]+)", text, re.I)
            unit_id = str(button["data-unit"])
            rent = int(rent_match.group(1).replace(",", "")) if rent_match else 0
            assert button.get("data-floorplan") == plan_id and rent > 0
            st_andrews_rows.append({
                "unit_number": unit_id,
                "unit_name": unit_number_match.group(1) if unit_number_match else "",
                "floor_plan_name": plan_names[plan_id],
                "sqft": int(sqft_match.group(1).replace(",", "")) if sqft_match else None,
                "market_rent_low": rent,
                "market_rent_high": rent,
                "availability_date": button.get("data-unitavailabilitydate") or button.get("data-date"),
                "source_api_url": str(path),
                "source_ids": {"entrata_property_id": "100002888", "entrata_floorplan_id": plan_id, "entrata_unit_space_id": unit_id},
            })
    st_andrews_summary = ROOT / "hb_scully_8119_detail3_one_session.json"
    qualifiers.append(qualifier(
        metadata, 8119, "exact_entrata_detail_pages_one_hyperbrowser_session", st_andrews_rows,
        sources=["https://standrews.scullycompany.com/Apartments/module/property_floorplans/property%5Bid%5D/100002888/"],
        artifacts=[str(st_andrews_summary)] + [str(ROOT / f"hb_scully_8119_plan_{plan_id}.html") for plan_id in ("1521", "1522", "1520")],
        boundary={
            "exact_host_title": True,
            "entrata_property_id": "100002888",
            "all_rows_repeat_exact_property_id": True,
            "exact_floorplan_ids_probed": ["1521", "1522", "1520"],
            "unprobed_floorplans_not_counted": ["1519", "1518"],
            "hyperbrowser_sessions": 1,
            "solve_captchas": False,
            "use_stealth": False,
        },
        contamination="pass_exact_property_id_on_every_native_unit_row_no_sibling_inventory",
    ))

    qualifiers.sort(key=lambda item: item["property_id"])
    assert {item["property_id"] for item in qualifiers} == QUALIFYING_IDS
    assert sum(item["native_priced_unit_count"] for item in qualifiers) == 256

    exclusions = [
        exclusion(metadata, 32097, "plan_only_no_native_unit", "Exact ResMan portal yielded one priced 2x1.5 floor-plan placeholder but no unit number/ID.", [str(TMP / "vendor_tail_32097_portal.html"), str(TMP / "vendor_tail_32097_floor.html")], ["https://ginkgo.myresman.com/Portal/Applicants/Availability"], {"exact_property_portal": True}),
        exclusion(metadata, 63191, "plan_only_no_native_unit", "One Hyperbrowser call (solveCaptchas=false) reached exact Avenir page and showed seven priced plan cards; zero native unit IDs/numbers.", [str(ROOT / "hb_scully_63191.html"), str(ROOT / "hb_scully_63191_43995_summary.json")], boundary={"exact_host_title_address": True, "entrata_property_id": "100002834"}),
        exclusion(metadata, 43995, "plan_only_no_native_unit", "One Hyperbrowser call (solveCaptchas=false) reached exact Hamilton Hall page and showed three priced plan cards; zero native unit IDs/numbers.", [str(ROOT / "hb_scully_43995.html"), str(ROOT / "hb_scully_63191_43995_summary.json")], boundary={"exact_host_title_address": True, "entrata_property_id": "100003046"}),
        exclusion(metadata, 8740, "native_roster_no_positive_rent", "Exact Parc Plaza SightMap exposes 201 native apartments, but every price is null/zero; strict rule requires positive numeric rent.", [str(TMP / "sightmap_8740_embed.html"), str(TMP / "sightmap_8740_api.json")], ["https://sightmap.com/app/api/v1/8epmlyokv6d/sightmaps/69637"], {"exact_property_asset": True, "native_roster_count": 201}),
        exclusion(metadata, 234945, "exact_provider_empty", "Exact IMT Belasera SightMap configuration returned zero units.", [str(TMP / "vendor_tail_234945_sightmap.html"), str(TMP / "vendor_tail_234945_sightmap_api.dec.json")], ["https://sightmap.com/app/api/v1/l8epm8e4w6d/sightmaps/12540"], {"exact_property_asset": True, "provider_unit_count": 0}),
        exclusion(metadata, 60141, "removed_or_redirected_property_surface", "Legacy Bridgeview link resolves to generic Scully corporate apartment search, not a Bridgeview property roster.", [str(ROOT / "raw_all/60141.html.gz")], contamination="excluded_corporate_sibling_surface"),
        exclusion(metadata, 8789, "removed_or_redirected_property_surface", "Target URL redirects to a LiveBH Arlington city collection; target property is absent and listed inventory is sibling/corporate contamination.", [str(TMP / "vendor_tail_8789_live.html")], contamination="excluded_city_collection_sibling_inventory"),
        exclusion(metadata, 47182, "removed_or_redirected_property_surface", "Target URL redirects to a LiveBH Richmond city collection; target property is absent and listed inventory is sibling/corporate contamination.", [str(TMP / "vendor_tail_47182_live.html")], contamination="excluded_city_collection_sibling_inventory"),
        exclusion(metadata, 8654, "inactive_dead_property_surface", "Exact Riverfalls URL returns the Apts247 'Page Not Found / website no longer active' surface; no property API key or roster.", [str(ROOT / "raw_all/8654.html.gz")], contamination="no_roster_dead_surface"),
        exclusion(metadata, 68956, "exact_current_adapter_no_native_inventory", "Exact current G5 GraphQL apartmentComplex.apartments response was empty.", [str(replay_path)]),
        exclusion(metadata, 25489, "exact_current_adapter_no_native_inventory", "Exact Cortland page exposed no preload floorplans/availprice unit data.", [str(replay_path)]),
        exclusion(metadata, 218378, "exact_current_adapter_no_native_inventory", "Exact Equity property page exposed no ea5 unit blocks.", [str(replay_path)]),
        exclusion(metadata, 61459, "exact_current_adapter_no_native_inventory", "Exact Knock community ID resolved, but Doorway API returned no units.", [str(replay_path)]),
        exclusion(metadata, 68497, "exact_current_adapter_no_native_inventory", "Exact Maya property page contains no Knock Doorway initialization and no qualifying native roster.", [str(replay_path)]),
        exclusion(metadata, 48946, "exact_current_adapter_no_native_inventory", "Exact Knock community ID resolved, but Doorway API returned no units.", [str(replay_path)]),
    ]
    exclusions.sort(key=lambda item: item["property_id"])

    all_ids = [item["property_id"] for item in qualifiers + exclusions]
    assert len(all_ids) == len(set(all_ids)) == 30
    assert set(all_ids) == set(ADAPTERS)

    frozen_ids = ledger_ids(ROOT / "strict99_authoritative_ledger.csv")
    current_ids = ledger_ids(ROOT / "strict_recovery_ledger_current.csv")
    qids = set(QUALIFYING_IDS)
    overlap_frozen = sorted(qids & frozen_ids)
    overlap_current = sorted(qids & current_ids)
    net_new = sorted(qids - current_ids)
    net_new_units = sum(item["native_priced_unit_count"] for item in qualifiers if item["property_id"] in net_new)
    assert overlap_frozen == []

    output = {
        "audit": "FAILED_NO_DATA deterministic residual vendor-tail strict unit audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": {"source": str(ROOT / "failed344_input.csv"), "exact_target_count": 30, "target_property_ids": sorted(ADAPTERS)},
        "strict_rule": "Count only property-scoped native unit identities with positive numeric rent and exact property-boundary evidence. Plans, synthetic IDs, sibling/corporate inventory, CAPTCHA-derived evidence, and non-positive rents are excluded.",
        "guardrails": {"source_edits": False, "paid_canary": False, "captcha_solving": False, "hyperbrowser_calls": {"63191": 1, "43995": 1, "8119": 2}, "hyperbrowser_solve_captchas": False},
        "summary": {
            "qualifying_properties": len(qualifiers), "excluded_properties": len(exclusions),
            "strict_native_priced_units": sum(item["native_priced_unit_count"] for item in qualifiers),
            "qualifier_rate_within_lane": len(qualifiers) / 30,
            "exclusion_reason_counts": dict(sorted(Counter(item["strict_exclusion_reason"] for item in exclusions).items())),
        },
        "deduplication": {
            "frozen_strict99_qualifier_overlap": overlap_frozen,
            "current_live_ledger_qualifier_overlap": overlap_current,
            "current_live_ledger_overlap_units": sum(item["native_priced_unit_count"] for item in qualifiers if item["property_id"] in overlap_current),
            "net_new_vs_current_live_ledger_property_ids": net_new,
            "net_new_vs_current_live_ledger_properties": len(net_new),
            "net_new_vs_current_live_ledger_native_priced_units": net_new_units,
            "note": "Computed against strict_recovery_ledger_current.csv at materialization time; merge only the net-new CSV to avoid double counting.",
        },
        "qualifying_results": qualifiers,
        "excluded_results": exclusions,
        "integrity_checks": {
            "qualifiers_plus_exclusions_equals_30": len(qualifiers) + len(exclusions) == 30,
            "all_30_property_ids_unique": len(set(all_ids)) == 30,
            "all_qualifier_counts_positive": all(item["native_priced_unit_count"] > 0 for item in qualifiers),
            "frozen_strict99_overlap_zero": not overlap_frozen,
            "qualifier_total_recomputed": sum(item["native_priced_unit_count"] for item in qualifiers),
        },
    }
    OUT.write_text(json.dumps(output, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    ledger_fields = [
        "property_id", "property_name", "website", "evidence_lane", "artifact", "units",
        "property_identity_match", "contamination_verdict", "native_identity_rows",
        "native_positive_rent_rows", "source_urls", "sample_native_unit_ids", "local_validation",
    ]
    ledger_rows = []
    for item in qualifiers:
        ledger_rows.append({
            "property_id": item["property_id"],
            "property_name": item["property_name"],
            "website": item["website"],
            "evidence_lane": item["evidence_lane"],
            "artifact": str(OUT),
            "units": item["native_priced_unit_count"],
            "property_identity_match": True,
            "contamination_verdict": item["contamination_verdict"],
            "native_identity_rows": item["distinct_native_unit_count"],
            "native_positive_rent_rows": item["native_priced_unit_count"],
            "source_urls": " | ".join(item["source_urls"]),
            "sample_native_unit_ids": " | ".join(item["native_unit_ids"][:5]),
            "local_validation": "artifact_backed_no_paid_canary",
        })
    for path, rows in (
        (LEDGER_OUT, ledger_rows),
        (NET_NEW_LEDGER_OUT, [row for row in ledger_rows if row["property_id"] in net_new]),
    ):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=ledger_fields)
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({
        "output": str(OUT),
        "ledger_compatible_all_qualifiers": str(LEDGER_OUT),
        "ledger_compatible_net_new_only": str(NET_NEW_LEDGER_OUT),
        "summary": output["summary"],
        "deduplication": output["deduplication"],
        "integrity_checks": output["integrity_checks"],
    }, indent=2))


if __name__ == "__main__":
    main()
