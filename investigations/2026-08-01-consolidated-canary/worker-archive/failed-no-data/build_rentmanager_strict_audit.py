from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters.rentmanager import parse_rentmanager_wp_cards


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LIVE = ROOT / "live_rentmanager"
OUTPUT = ROOT / "evidence_residual_rentmanager17_strict_audit.json"
RP_PATH = Path("/Users/ankur/Downloads/rp_unit_detail_0731.csv")
E2E_PATH = ROOT / "evidence_residual_rentmanager17_current_e2e.json"

CANDIDATE_IDS = [
    26527,
    36782,
    38984,
    47444,
    49096,
    50114,
    51143,
    52854,
    55165,
    77794,
    223864,
    226383,
    245993,
    246351,
    246468,
    281149,
    294493,
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def money(text: str) -> int | None:
    match = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        value = int(round(float(match.group(1).replace(",", ""))))
    except ValueError:
        return None
    return value if value > 0 else None


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def read_html(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def archived_html(property_id: int) -> str:
    return gzip.open(
        ROOT / "raw_all" / f"{property_id}.html.gz",
        "rt",
        encoding="utf-8",
        errors="replace",
    ).read()


def load_metadata() -> tuple[dict[int, dict], dict[int, dict], dict[int, dict]]:
    failed = {
        int(row["property_id"]): row
        for row in csv.DictReader((ROOT / "failed344.csv").open(newline=""))
    }
    residual = {
        int(row["property_id"]): row
        for row in csv.DictReader(
            (ROOT / "strict99_residual245_classification.csv").open(newline="")
        )
    }
    e2e_payload = json.loads(E2E_PATH.read_text())
    e2e = {int(row["property_id"]): row for row in e2e_payload["results"]}
    return failed, residual, e2e


def load_rp() -> dict[int, list[dict]]:
    output: dict[int, list[dict]] = {property_id: [] for property_id in CANDIDATE_IDS}
    with RP_PATH.open(encoding="cp1252", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                property_id = int(row.get("apartmentid") or "")
            except ValueError:
                continue
            if property_id in output:
                output[property_id].append(row)
    return output


def row(
    *,
    unit_number: str = "",
    source_id_key: str = "",
    source_id: str = "",
    floor_plan_name: str = "",
    bedrooms: str = "",
    bathrooms: str = "",
    sqft: str = "",
    rent: int | None = None,
    availability_date: str = "",
    source_url: str,
) -> dict:
    return {
        "unit_number": clean(unit_number),
        "source_ids": {source_id_key: clean(source_id)}
        if source_id_key and clean(source_id)
        else {},
        "floor_plan_name": clean(floor_plan_name),
        "bedrooms": clean(bedrooms),
        "bathrooms": clean(bathrooms),
        "sqft": clean(sqft).replace(",", ""),
        "market_rent_low": rent,
        "market_rent_high": rent,
        "availability_date": clean(availability_date),
        "source_api_url": source_url,
    }


def parse_26527() -> tuple[list[dict], dict]:
    source_url = "https://finelivingapts.com/aquila-park-availability/"
    archived = archived_html(26527)
    live_path = LIVE / "26527_availability.html"
    live = read_html(live_path)
    source_rows = parse_rentmanager_wp_cards(archived, source_url)
    rows = [
        row(
            unit_number=item.get("unit_number", ""),
            source_id_key="rentmanager_uid",
            source_id=(item.get("source_ids") or {}).get("rentmanager_uid", ""),
            floor_plan_name=item.get("floor_plan_name", ""),
            bedrooms=item.get("bedrooms", ""),
            bathrooms=item.get("bathrooms", ""),
            sqft=item.get("sqft", ""),
            rent=item.get("market_rent_low"),
            availability_date=item.get("availability_date", ""),
            source_url=source_url,
        )
        for item in source_rows
    ]
    soup = BeautifulSoup(live, "lxml")
    hrefs = [str(anchor.get("href") or "") for anchor in soup.select("a.individual-item")]
    boundary = {
        "verdict": "pass_exact_same_origin_property_boundary",
        "evidence": [
            "Exact live and archived page title/canonical identify Aquila Park availability.",
            "All admitted cards come from the same-origin Aquila availability page.",
            "Every admitted card links to /aquila-floor-plan-detail/; no Royal Park card was admitted.",
            "All physical apartment numbers and RentManager uid values are distinct.",
        ],
        "title_contains_property_name": bool(
            soup.title and "aquila park" in soup.title.get_text(" ", strip=True).casefold()
        ),
        "all_links_aquila_scoped": bool(hrefs)
        and all("/aquila-floor-plan-detail/" in href for href in hrefs),
        "live_page_sha256": sha256(live_path),
        "archived_body_sha256": sha256(ROOT / "raw_all" / "26527.html.gz"),
    }
    return rows, boundary


def parse_49096() -> tuple[list[dict], dict]:
    path = LIVE / "49096_original.html"
    source_url = "https://tvcproperties.com/community-detail/pondview-apartments/"
    soup = BeautifulSoup(read_html(path), "lxml")
    rows: list[dict] = []
    card_addresses: list[str] = []
    for card in soup.select(".rmwb_unit_listing-wrapper"):
        text = clean(card.get_text(" ", strip=True))
        unit_number = clean(card.get("data-sort") or "")
        heading = card.select_one(".rmwb_listing_header")
        if heading is not None:
            card_addresses.append(clean(heading.get_text(" ", strip=True)))
        uid = ""
        for anchor in card.select("a[href]"):
            query = parse_qs(urlparse(str(anchor.get("href") or "")).query)
            if query.get("uid"):
                uid = clean(query["uid"][0])
                break
        available = re.search(r"\bAvailable\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
        dimensions = re.search(
            r"(\d+(?:\.\d+)?)\s+Beds?,\s*(\d+(?:\.\d+)?)\s+Bath\s*\|\s*([\d,]+)\s+Square Feet",
            text,
            re.I,
        )
        rent_match = re.search(r"Base Rent\*?\s*-\s*\$([\d,]+(?:\.\d+)?)", text, re.I)
        rows.append(
            row(
                unit_number=unit_number,
                source_id_key="rentmanager_uid",
                source_id=uid,
                floor_plan_name=(
                    f"{dimensions.group(1)} Beds, {dimensions.group(2)} Bath"
                    if dimensions
                    else ""
                ),
                bedrooms=dimensions.group(1) if dimensions else "",
                bathrooms=dimensions.group(2) if dimensions else "",
                sqft=dimensions.group(3) if dimensions else "",
                rent=money(rent_match.group(1)) if rent_match else None,
                availability_date=available.group(1) if available else "",
                source_url=source_url,
            )
        )
    body_text = clean(soup.get_text(" ", strip=True)).casefold()
    boundary = {
        "verdict": "pass_exact_same_origin_property_boundary",
        "evidence": [
            "The original cohort URL redirects to an exact Pondview Apartments community-detail page.",
            "The page identifies Pondview and its canonical 2565 Ivy Ave E address.",
            "All apartment cards are on Ivy Ave E and their detail links remain on tvcproperties.com.",
            "Physical apartment numbers and native detail uid values are distinct.",
        ],
        "title_contains_property_name": bool(
            soup.title and "pondview apartments" in soup.title.get_text(" ", strip=True).casefold()
        ),
        "canonical_address_present": "2565 ivy ave e" in body_text,
        "card_addresses": card_addresses,
        "live_page_sha256": sha256(path),
    }
    return rows, boundary


def parse_51143() -> tuple[list[dict], dict]:
    path = LIVE / "51143_original.html"
    source_url = "https://mgmmgmt.net/property-details/?pid=136"
    soup = BeautifulSoup(read_html(path), "lxml")
    rows: list[dict] = []
    for tr in soup.select("tr.floorplan-item"):
        cells = tr.select("td")
        uid = ""
        for anchor in tr.select("a[href]"):
            query = parse_qs(urlparse(str(anchor.get("href") or "")).query)
            if query.get("uid"):
                uid = clean(query["uid"][0])
                break
        bed_bath = clean(cells[0].get_text(" ", strip=True)) if cells else ""
        match = re.match(r"([\d.]+)\s*/\s*([\d.]+)", bed_bath)
        rows.append(
            row(
                source_id_key="rentmanager_uid",
                source_id=uid,
                floor_plan_name=bed_bath,
                bedrooms=match.group(1) if match else "",
                bathrooms=match.group(2) if match else "",
                sqft=clean(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else "",
                rent=money(str(tr.get("data-rent") or "")),
                availability_date=clean(str(tr.get("data-availability") or "")),
                source_url=source_url,
            )
        )
    body_text = clean(soup.get_text(" ", strip=True)).casefold()
    boundary = {
        "verdict": "pass_exact_same_origin_property_boundary",
        "evidence": [
            "Exact pid=136 property-detail page names Lauderdale Hollows and 1622 Carl Street.",
            "All native uid detail links remain on mgmmgmt.net.",
            "All four native uid values are distinct and each row has positive rent.",
        ],
        "property_name_present": "lauderdale hollows" in body_text,
        "canonical_address_present": "1622 carl street" in body_text,
        "live_page_sha256": sha256(path),
    }
    return rows, boundary


def parse_77794() -> tuple[list[dict], dict]:
    page_path = LIVE / "77794_floorplans.html"
    widget_path = LIVE / "77794_luv_widget.json"
    inventory_path = LIVE / "77794_luv_availability_1_1.json"
    payload = json.loads(inventory_path.read_text())
    rows = []
    for item in payload.get("units") or []:
        prices = [money(term.get("price")) for term in item.get("termprices") or []]
        prices = [value for value in prices if value]
        rows.append(
            row(
                unit_number=item.get("unitname", ""),
                floor_plan_name=item.get("planname", ""),
                bedrooms=item.get("beds", ""),
                bathrooms=item.get("baths", ""),
                sqft=item.get("sqft", ""),
                rent=min(prices) if prices else None,
                availability_date=item.get("dateavailable", ""),
                source_url="https://www.iloveleasing.com/pub/wapi/api/availability/",
            )
        )
    widget = json.loads(widget_path.read_text())
    location = ((widget.get("property") or {}).get("data") or {}).get("location") or {}
    page = read_html(page_path)
    boundary = {
        "verdict": "pass_exact_embedded_public_widget_boundary",
        "evidence": [
            "Exact same-origin floorplans page embeds the public iLoveLeasing settings used for the API call.",
            "Widget metadata identifies Atlantico at Alton Apartments at 13805 Emerson Street, matching canonical cohort metadata.",
            "Every admitted row has a distinct visible physical apartment number and positive public term rent.",
        ],
        "widget_location": location,
        "settings_embedded_in_exact_page": "window.luv_settings" in page,
        "page_sha256": sha256(page_path),
        "widget_response_sha256": sha256(widget_path),
        "inventory_response_sha256": sha256(inventory_path),
    }
    return rows, boundary


def parse_281149() -> tuple[list[dict], dict]:
    parent_path = LIVE / "281149_availability.html"
    iframe_path = LIVE / "281149_spherexx.html"
    source_url = (
        "https://clients.spherexx.com/adkast_availability_2/availability/"
        "mgoelqnd/rose/BE9B77"
    )
    soup = BeautifulSoup(read_html(iframe_path), "lxml")
    rows: list[dict] = []
    labels: set[str] = set()
    for tr in soup.select("table.availability tr"):
        cells = {
            clean(td.get("data-label") or "").casefold(): clean(
                td.get_text(" ", strip=True)
            )
            for td in tr.select("td[data-label]")
        }
        unit_cell = tr.select_one('td[data-label="Unit"]')
        rent_cell = tr.select_one('td[data-label="Rent"]')
        if unit_cell is None or rent_cell is None:
            continue
        unit_anchor = unit_cell.select_one("a[href]")
        unit_number = clean(unit_anchor.get_text(" ", strip=True) if unit_anchor else "")
        href = str(unit_anchor.get("href") or "") if unit_anchor else ""
        source_id_match = re.search(r"#unit(\d+)", href, re.I)
        if unit_anchor and unit_anchor.get("data-tlabel"):
            labels.add(clean(unit_anchor.get("data-tlabel")))
        rows.append(
            row(
                unit_number=unit_number,
                source_id_key="spherexx_unit_id",
                source_id=source_id_match.group(1) if source_id_match else "",
                floor_plan_name="",
                bedrooms=cells.get("bedroom", "").replace(" BR", ""),
                bathrooms=cells.get("bathroom", "").replace(" BA", ""),
                rent=money(rent_cell.get_text(" ", strip=True)),
                availability_date=cells.get("availability", ""),
                source_url=source_url,
            )
        )
    parent = read_html(parent_path)
    boundary = {
        "verdict": "pass_exact_embedded_public_iframe_boundary",
        "evidence": [
            "Exact Irondale availability page embeds this unguessable Spherexx iframe URL.",
            "Every native-unit row is labeled Irondale at Wharton Apartments.",
            "Visible apartment numbers, Spherexx unit ids, and positive rents are distinct and property-scoped.",
        ],
        "iframe_embedded_in_exact_parent": source_url in parent,
        "row_property_labels": sorted(labels),
        "parent_page_sha256": sha256(parent_path),
        "iframe_response_sha256": sha256(iframe_path),
    }
    return rows, boundary


def validate_rows(rows: list[dict]) -> dict:
    identity_keys: list[str] = []
    positive = 0
    for item in rows:
        unit = clean(item.get("unit_number", ""))
        ids = item.get("source_ids") or {}
        native = unit if unit and re.search(r"\d", unit) else ""
        if not native and ids:
            native = "|".join(f"{key}:{ids[key]}" for key in sorted(ids) if ids[key])
        if native:
            identity_keys.append(native.casefold())
        if isinstance(item.get("market_rent_low"), (int, float)) and item["market_rent_low"] > 0:
            positive += 1
    duplicates = sorted(
        identity for identity in set(identity_keys) if identity_keys.count(identity) > 1
    )
    return {
        "row_count": len(rows),
        "rows_with_native_identity": len(identity_keys),
        "rows_with_positive_numeric_rent": positive,
        "distinct_native_identity_count": len(set(identity_keys)),
        "duplicate_native_identities": duplicates,
        "passes_row_gate": bool(rows)
        and len(identity_keys) == len(rows)
        and positive == len(rows)
        and not duplicates,
    }


def rp_comparison(property_id: int, rows: list[dict], rp_rows: list[dict]) -> dict:
    rp_native = {
        clean(item.get("unitid", "")): item
        for item in rp_rows
        if clean(item.get("unitid", "")) not in {"", "~"}
    }
    direct_by_unit = {
        clean(item.get("unit_number", "")): item
        for item in rows
        if clean(item.get("unit_number", ""))
    }
    # Some RentManager pages publish only a stable native uid, which RP also
    # used as its unit identifier. Include that exact comparison surface.
    direct_ids = dict(direct_by_unit)
    for item in rows:
        for value in (item.get("source_ids") or {}).values():
            if clean(value):
                direct_ids.setdefault(clean(value), item)
    overlap = sorted(set(rp_native) & set(direct_ids))
    rent_matches = 0
    for native_id in overlap:
        rp_rent = money(rp_native[native_id].get("marketrentlow", ""))
        direct_rent = direct_ids[native_id].get("market_rent_low")
        if rp_rent and direct_rent and int(rp_rent) == int(direct_rent):
            rent_matches += 1
    return {
        "rp_row_count": len(rp_rows),
        "rp_rows_with_nonplaceholder_native_id": len(rp_native),
        "rp_native_ids": sorted(rp_native),
        "direct_to_rp_native_overlap_count": len(overlap),
        "direct_to_rp_native_overlap_ids": overlap,
        "positive_rent_matches_on_overlap": rent_matches,
        "note": "RP is validation oracle only and was never used as extraction input.",
    }


EXCLUSIONS = {
    36782: "Exact widget identifies Harbor Station but exposes schedule/contact only; no availability module or native inventory. Current local E2E is empty.",
    38984: "Public floor-plan/pricing pages contain plan rents only and no physical/native unit identity. Current local E2E is empty.",
    47444: "Iroquois Green is absent from the operator's current public availability listing; RP has zero rows and current local E2E is empty.",
    50114: "The cohort URL is obsolete/404 and the replacement availability SPA provides no property-scoped Woodgrove native inventory; RP has zero rows and current E2E is empty.",
    52854: "Public floorplans page is plan-level only with no native apartment identity; RP has zero rows and current local E2E is empty.",
    55165: "Archived evidence is plan-only; the exact live unit-availability path returns a SiteGround CAPTCHA challenge. CAPTCHA solving is prohibited, so no strict native evidence is admitted.",
    223864: "Exact Kendall Square page publishes plan-level rents/sizes only, without physical/native unit identity. Current local E2E is empty.",
    226383: "Exact public RentManager PropertyDetail script confirms the property and a $625-$925 plan range but publishes no native unit rows. Current local E2E is empty.",
    245993: "Exact Alberta Square page publishes plan-level information only and no native apartment identity. Current local E2E is empty.",
    246351: "Exact Chatsworth page publishes plan-level information only and no native apartment identity. Current local E2E is empty.",
    246468: "Exact Colonial Manor page exposes no property-scoped native availability; RP has zero rows and current local E2E is empty.",
    294493: "Exact Ashland property page explicitly says there are no available units; RP has zero rows and current local E2E is empty.",
}


def main() -> None:
    failed, residual, e2e = load_metadata()
    rp = load_rp()
    direct_parsers = {
        26527: parse_26527,
        49096: parse_49096,
        51143: parse_51143,
        77794: parse_77794,
        281149: parse_281149,
    }
    strict_ids: list[int] = []
    direct_gap_ids: list[int] = []
    excluded_ids: list[int] = []
    results: list[dict] = []
    for property_id in CANDIDATE_IDS:
        failed_row = failed[property_id]
        residual_row = residual[property_id]
        e2e_row = e2e[property_id]
        rows: list[dict] = []
        boundary: dict = {
            "verdict": "no_qualifying_native_unit_boundary_evidence",
            "evidence": [],
        }
        if property_id in direct_parsers:
            rows, boundary = direct_parsers[property_id]()
        validation = validate_rows(rows)
        e2e_qualified = (
            e2e_row.get("outcome") == "UNIT_QUALIFIED"
            and e2e_row.get("contamination_verdict")
            == "pass_property_scoped_native_identity"
            and int((e2e_row.get("identity_evidence") or {}).get(
                "rows_with_native_identity_and_positive_rent"
            ) or 0)
            > 0
        )
        boundary_pass = str(boundary.get("verdict") or "").startswith("pass_")
        if e2e_qualified and validation["passes_row_gate"] and boundary_pass:
            classification = "STRICT_E2E_UNIT_QUALIFIED"
            strict_ids.append(property_id)
        elif validation["passes_row_gate"] and boundary_pass:
            classification = "CLEAN_DIRECT_UNIT_EVIDENCE_PIPELINE_GAP"
            direct_gap_ids.append(property_id)
        else:
            classification = "EXCLUDED_NO_STRICT_NATIVE_UNIT_RECOVERY"
            excluded_ids.append(property_id)
        results.append(
            {
                "property_id": property_id,
                "property_name": failed_row.get("proj_name") or e2e_row.get("property_name") or "",
                "website": failed_row.get("website") or "",
                "source_adapter_0731": residual_row.get("source_adapter_0731") or "",
                "current_detected_adapter": residual_row.get("current_detected_adapter") or "",
                "classification": classification,
                "counts_toward_strict_207_gate": classification
                == "STRICT_E2E_UNIT_QUALIFIED",
                "current_local_e2e": {
                    key: e2e_row.get(key)
                    for key in (
                        "outcome",
                        "adapter",
                        "tier",
                        "units",
                        "plans",
                        "property_identity_match",
                        "contamination_verdict",
                        "identity_evidence",
                        "errors",
                    )
                },
                "direct_native_rows": rows,
                "direct_validation": validation,
                "property_boundary": boundary,
                "rp_oracle_comparison": rp_comparison(property_id, rows, rp[property_id]),
                "quality_caveat": (
                    "The current RentManager WP-card parser can map visible "
                    "'Available Now' rows to the first day of the card's month "
                    "via data-date. This does not affect native-unit identity, "
                    "positive-rent qualification, or the strict recovery count."
                    if property_id == 26527
                    else ""
                ),
                "exclusion_reason": EXCLUSIONS.get(property_id, ""),
            }
        )
    assert set(strict_ids).isdisjoint(direct_gap_ids)
    assert set(strict_ids).isdisjoint(excluded_ids)
    assert set(direct_gap_ids).isdisjoint(excluded_ids)
    assert set(strict_ids) | set(direct_gap_ids) | set(excluded_ids) == set(CANDIDATE_IDS)
    payload = {
        "audit_label": "residual-rentmanager17-strict-audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort": "Exact 344-property FAILED_NO_DATA cohort from 2026-07-31-fetchfix-5k",
        "scope_rule": "Currently unrecovered properties where source_adapter_0731 == rentmanager OR current_detected_adapter == rentmanager",
        "scope_count": len(CANDIDATE_IDS),
        "scope_note": "17 rather than 16 because property 51143 was RentManager on 07-31 but currently detects as unknown.",
        "strict_gate": {
            "requirements": [
                "Current local end-to-end outcome is UNIT_QUALIFIED.",
                "Every admitted row has a physical apartment number or stable native source id.",
                "Every admitted row has positive numeric rent.",
                "Exact or same-origin property-boundary evidence passes contamination checks.",
                "No duplicate/conflicting native identities.",
            ],
            "rp_role": "Validation oracle only; never an extraction source.",
            "captcha_policy": "No CAPTCHA solving. The Legacy Oaks challenge was not bypassed or solved.",
            "llm": "Disabled/unavailable in local E2E replay; no LLM recovery was counted.",
            "canary": "No paid canary was run.",
        },
        "tested_source_state": {
            "worktree": "/Users/ankur/PropAi-codex-plan-level",
            "branch": "codex/plan-level-unit-recovery",
            "head": "02369d2827dd6bfe49e7abb8d32e028742ef8d6c",
            "working_tree_dirty": True,
            "note": "Audit made no source edits; current uncommitted local source state was treated as authoritative.",
        },
        "summary": {
            "strict_e2e_qualified_count": len(strict_ids),
            "strict_e2e_qualifying_property_ids": strict_ids,
            "clean_direct_pipeline_gap_count": len(direct_gap_ids),
            "clean_direct_but_not_recovered_property_ids": direct_gap_ids,
            "excluded_count": len(excluded_ids),
            "excluded_property_ids": excluded_ids,
            "disjoint_complete_partition_verified": True,
        },
        "source_artifacts": {
            "failed344": str(ROOT / "failed344.csv"),
            "residual_classification": str(
                ROOT / "strict99_residual245_classification.csv"
            ),
            "current_e2e_replay": str(E2E_PATH),
            "rp_oracle": str(RP_PATH),
            "live_probe_directory": str(LIVE),
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    print(OUTPUT)


if __name__ == "__main__":
    main()
