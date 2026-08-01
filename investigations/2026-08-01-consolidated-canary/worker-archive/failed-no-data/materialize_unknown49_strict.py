#!/usr/bin/env python3
"""Materialize strict native-unit recoveries from the 49 current-unknown properties.

This is an evidence builder only.  It does not edit PropAi source or call a paid
canary.  Every qualifying row must have an exact-property boundary, a native
unit identifier, a positive rent, and no sibling/portfolio contamination.
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
TARGET_CSV = ROOT / "strict_recovery_remaining_current.csv"
REDETECTION_JSON = ROOT / "evidence_unknown49_current_redetection.json"
CURRENT_LEDGER = ROOT / "strict_recovery_ledger_current.csv"
EVIDENCE_OUT = ROOT / "evidence_unknown49_strict.json"
LEDGER_OUT = ROOT / "strict_unknown49_ledger_rows.csv"
NET_NEW_OUT = ROOT / "strict_unknown49_net_new_ledger_rows.csv"

CAPTURE_DATE = "2026-08-01"
LEDGER_FIELDS = [
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


target = [
    row
    for row in read_csv(TARGET_CSV)
    if row.get("current_detected_adapter") == "unknown"
]
assert len(target) == 49, len(target)
target_by_pid = {row["property_id"]: row for row in target}
redetection = {
    str(row["property_id"]): row
    for row in json.loads(REDETECTION_JSON.read_text(encoding="utf-8"))
}


def numeric_rent(value: str) -> float:
    match = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", value or "")
    return float(match.group(1).replace(",", "")) if match else 0.0


def parse_tgm(
    pid: str,
    property_name: str,
    portal_property_id: str,
    source_url: str,
    expected: int,
) -> dict:
    raw_path = ROOT / "unknown49_current" / f"{pid}_mri.html"
    raw = raw_path.read_text(encoding="utf-8", errors="ignore")
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.I | re.S)
    root_property = re.search(r'data-propertyid="([^"]+)"', raw, re.I)
    assert root_property and root_property.group(1) == portal_property_id
    assert heading and property_name.lower().replace(" apartments", "") in re.sub(
        r"<[^>]+>", " ", heading.group(1)
    ).lower()

    units: list[dict] = []
    seen: set[str] = set()
    for button_match in re.finditer(
        r"<button\b(?=[^>]*\bdata-unitid=)[^>]*>", raw, re.I | re.S
    ):
        button = button_match.group(0)
        attrs = {
            key.lower(): html.unescape(value)
            for key, value in re.findall(r'data-([\w-]+)="([^"]*)"', button, re.I)
        }
        unit = attrs.get("unitid", "").strip()
        building = attrs.get("bldgid", "").strip()
        address = attrs.get("unit-address", "").strip()
        native_id = f"{building}:{unit}" if building else unit
        tail = raw[button_match.end() : button_match.end() + 1300]
        rent_match = re.search(
            r"<option[^>]*>\s*[^<(]*\(\s*([0-9]+(?:\.[0-9]+)?)\s+USD\s*\)",
            tail,
            re.I,
        )
        assert unit and native_id and rent_match, (pid, button[:200], tail[:300])
        rent = float(rent_match.group(1))
        assert rent > 0
        assert native_id not in seen, (pid, native_id)
        seen.add(native_id)
        units.append(
            {
                "unit": unit,
                "provider_unit_id": native_id,
                "building_id": building,
                "unit_address": address,
                "available_date": attrs.get("available-date", ""),
                "available_end_date": attrs.get("available-end-date", ""),
                "lease_term_months": attrs.get("term", ""),
                "rent": rent,
                "source_url": source_url,
            }
        )
    assert len(units) == expected, (pid, len(units), expected)
    return {
        "property_id": pid,
        "property_name": property_name,
        "provider": "MRI ProspectConnect",
        "provider_property_id": portal_property_id,
        "source_urls": [source_url],
        "capture_method": "live exact-property provider HTML",
        "raw_artifacts": [str(raw_path)],
        "property_identity_evidence": {
            "provider_h1": re.sub(r"<[^>]+>", " ", heading.group(1)).strip(),
            "provider_title": re.sub(r"<[^>]+>", " ", title.group(1)).strip()
            if title
            else "",
            "root_data_propertyid": portal_property_id,
        },
        "contamination_evidence": "Single exact-property portal; no portfolio/sibling roster in the extracted unit region.",
        "units": units,
    }


def browser_winner(
    pid: str,
    property_name: str,
    provider: str,
    source_urls: list[str],
    identity: str,
    units: list[dict],
) -> dict:
    assert units
    normalized: list[dict] = []
    seen: set[str] = set()
    for row in units:
        row = dict(row)
        native_id = str(row.get("provider_unit_id") or row.get("unit") or "").strip()
        assert native_id and native_id not in seen, (pid, native_id)
        seen.add(native_id)
        assert numeric_rent(str(row.get("rent", ""))) > 0, (pid, row)
        row["provider_unit_id"] = native_id
        normalized.append(row)
    return {
        "property_id": pid,
        "property_name": property_name,
        "provider": provider,
        "source_urls": source_urls,
        "capture_method": "Chrome DOM snapshot of live exact-property page/provider flow",
        "property_identity_evidence": identity,
        "contamination_evidence": "Exact property page/provider boundary; no sibling or portfolio rows in the extracted unit region.",
        "units": normalized,
    }


winners: list[dict] = [
    parse_tgm(
        "2239",
        "TGM Autumn Woods",
        "288034",
        "https://tgm.mriprospectconnect.com/Search/Index/288034?minbeds=1&maxbeds=2",
        50,
    ),
    parse_tgm(
        "2844",
        "TGM Danada",
        "360002",
        "https://tgm.mriprospectconnect.com/Search/Index/360002?minbeds=1&maxbeds=3",
        40,
    ),
    parse_tgm(
        "32996",
        "TGM Ridge",
        "241007",
        "https://tgm.mriprospectconnect.com/Search/Index/241007?minbeds=1&maxbeds=2",
        34,
    ),
    parse_tgm(
        "75912",
        "TGM Creekside Village",
        "294005",
        "https://tgm.mriprospectconnect.com/Search/Index/294005?minbeds=1&maxbeds=3",
        23,
    ),
]


wyndham_meadow = [
    ("5400", "Starting at $989", "Available Now"),
    ("5467", "Starting at $974", "Available Now"),
    ("5551", "Starting at $999", "Available Now"),
    ("5555", "Starting at $974", "Available Now"),
    ("5556", "Starting at $974", "Available Now"),
    ("5443", "Starting at $999", "Available Now"),
    ("5508", "Starting at $974", "Available Now"),
    ("5325", "Starting at $989", "Available Now"),
    ("5424", "Starting at $974", "Available Now"),
    ("5421", "Starting at $979", "Available Aug 05"),
    ("5465", "Starting at $987", "Available Sep 12"),
    ("5434", "Starting at $972", "Available Sep 24"),
    ("1114", "Starting at $981", "Available Nov 04"),
]
wyndham_orchard = [
    ("5564", "Starting at $954", "Available Aug 05"),
    ("5456", "Starting at $952", "Available Aug 11"),
    ("5562", "Starting at $962", "Available Aug 14"),
    ("5547", "Starting at $937", "Available Aug 20"),
    ("5497", "Starting at $937", "Available Aug 22"),
    ("5455", "Starting at $952", "Available Aug 26"),
    ("5483", "Starting at $932", "Available Sep 02"),
    ("5565", "Starting at $932", "Available Sep 22"),
    ("5527", "Starting at $936", "Available Oct 07"),
    ("5409", "Starting at $939", "Available Oct 28"),
]
winners.append(
    browser_winner(
        "251597",
        "Wyndham Ridge",
        "RealPage Online Leasing embedded availability",
        [
            "https://www.wyndhamridgecolumbus.com/floorplans/meadow/",
            "https://www.wyndhamridgecolumbus.com/floorplans/orchard/",
        ],
        "Wyndham Ridge name/address on exact site; only its Meadow and Orchard plan flows were opened.",
        [
            {"unit": unit, "plan": "Meadow", "rent": rent, "available": available, "sqft": "850"}
            for unit, rent, available in wyndham_meadow
        ]
        + [
            {"unit": unit, "plan": "Orchard", "rent": rent, "available": available, "sqft": "850"}
            for unit, rent, available in wyndham_orchard
        ],
    )
)


invitational = [
    ("001-0212", "Coral", "$1,200.00", "Available Now"),
    ("001-2221", "Coral", "$1,200.00", "Available 8/5"),
    ("001-1124", "Coral", "$1,200.00", "Available 8/20"),
    ("001-1614", "Coral", "$1,200.00", "Available 8/31"),
    ("001-0222", "Coral", "$1,238.00", "Available Now"),
    ("001-0722", "Pearl", "$1,470.00", "Available Now"),
    ("001-0724", "Pearl", "$1,470.00", "Available 8/15"),
    ("001-1011", "Pearl", "$1,470.00", "Available 8/20"),
    ("001-1421", "Pearl", "$1,470.00", "Available 8/22"),
    ("001-2124", "Pearl", "$1,470.00", "Available 9/18"),
]
winners.append(
    browser_winner(
        "4330",
        "Invitational Apartments",
        "Spherexx property availability",
        [
            "https://www.invitationalapartments.com/floorplans/1-bedroom/coral/",
            "https://www.invitationalapartments.com/floorplans/2-bedroom/pearl/",
        ],
        "Invitational property footer/address and property-scoped Spherexx PID 1457.",
        [
            {"unit": unit, "plan": plan, "rent": rent, "available": available}
            for unit, plan, rent, available in invitational
        ],
    )
)


winners.append(
    browser_winner(
        "61285",
        "Allez Apartments",
        "RentCafe",
        [
            "https://www.allezliving.com/floorplans/a-1",
            "https://www.allezliving.com/floorplans/a-7",
            "https://www.allezliving.com/floorplans/s-1",
        ],
        "Allez exact site plus consistent RentCafe myOlePropertyId 626768 on all three unit links.",
        [
            {"unit": "0321", "provider_unit_id": "10904407", "plan": "A-1", "rent": "$2,496.00 to -$2,765.00", "available": "8/1/2026", "rentcafe_property_id": "626768"},
            {"unit": "0110", "provider_unit_id": "10904350", "plan": "A-7", "rent": "$2,581.00 to -$2,859.00", "available": "8/1/2026", "rentcafe_property_id": "626768"},
            {"unit": "0314", "provider_unit_id": "10904400", "plan": "S-1", "rent": "$2,474.00 to -$2,677.00", "available": "8/7/2026", "rentcafe_property_id": "626768"},
        ],
    )
)


boardwalk = [
    ("0409", "Studio A", "$2,970/12mo", "Available Now", "1"),
    ("1109", "Studio A", "$2,970/12mo", "Available Now", "1"),
    ("0407", "Plan 1CR", "$3,731/12mo", "Available Now", "1"),
    ("0705W", "Plan 1CR", "$3,736/12mo", "Available Now", "1"),
    ("0405", "Plan 1CR", "$3,742/12mo", "Available Now", "1"),
    ("0211W", "Plan 1CR", "$3,791/12mo", "Available Now", "1"),
    ("0203W", "Plan 1CR", "$3,802/12mo", "Available Now", "1"),
    ("1405", "Plan 1CR", "$3,802/12mo", "Available Now", "1"),
    ("1102", "Plan 1CR", "$3,901/12mo", "Available Now", "2"),
    ("0702", "Plan 1CR", "$3,928/12mo", "Available Now", "2"),
    ("0408", "Plan 1CR", "$3,891/12mo", "Available Aug 9", "2"),
    ("0307W", "Plan 2AR", "$4,788/12mo", "Available Aug 14", "1"),
    ("0603", "Plan 2AR", "$4,823/12mo", "Available Aug 29", "1"),
    ("0801", "Plan 2AR", "$4,788/12mo", "Available Sep 5", "1"),
]
winners.append(
    browser_winner(
        "24139",
        "Boardwalk",
        "Prometheus custom React availability",
        ["https://prometheusapartments.com/ca/santa-clara-apartments/boardwalk/"],
        "Boardwalk exact property route; every extracted native ID links back to /boardwalk/unit-<id>.",
        [
            {
                "unit": unit,
                "plan": plan,
                "rent": rent,
                "available": available,
                "floor": floor,
                "source_url": f"https://prometheusapartments.com/ca/santa-clara-apartments/boardwalk/unit-{unit}",
            }
            for unit, plan, rent, available, floor in boardwalk
        ],
    )
)


winners.append(
    browser_winner(
        "51143",
        "Lauderdale Hollows",
        "RentManager WordPress availability plugin",
        [
            "https://mgmmgmt.net/property-details/?pid=136",
            "https://mgmmgmt.net/unit-details/?uid=7792",
            "https://mgmmgmt.net/unit-details/?uid=7828",
            "https://mgmmgmt.net/unit-details/?uid=7874",
        ],
        "Exact property page pid=136 links each detail; every detail heading is Lauderdale Hollows / Unit: <id>.",
        [
            {"unit": "1618-09", "provider_unit_id": "7792", "rent": "$1270", "source_url": "https://mgmmgmt.net/unit-details/?uid=7792"},
            {"unit": "1630-03", "provider_unit_id": "7828", "rent": "$1085", "source_url": "https://mgmmgmt.net/unit-details/?uid=7828"},
            {"unit": "1622-203", "provider_unit_id": "7874", "rent": "$1095", "source_url": "https://mgmmgmt.net/unit-details/?uid=7874"},
        ],
    )
)


winners.append(
    browser_winner(
        "269346",
        "Ashford Apartment Homes",
        "RentPro RPA5 availability",
        ["https://rentpro.rpa5.com/availibility/avapage.a5w?cd=AB"],
        "Ashford Brook exact site links cd=AB directly; the provider page applies only to ashfordbrook and exposes no sibling roster.",
        [
            {"unit": "0307", "available": "08/06/2026", "rent": "$1,249", "beds": "2.00", "type": "2X1L", "min_credit": "600"},
            {"unit": "0707", "available": "Immediately", "rent": "$1,249", "beds": "2.00", "type": "2X1S", "min_credit": "600"},
            {"unit": "0801", "available": "Immediately", "rent": "$1,249", "beds": "2.00", "type": "2X1S", "min_credit": "600"},
        ],
    )
)


exclusions = {
    "1617": ("fetch_and_browser_timeout", "Exact property site timed out in both direct and Chrome probes; no source unit evidence."),
    "1765": ("native_listing_explicitly_not_available", "Exact unit D3 and $950 rent are visible, but the page twice states This Property Is Not Available; not current inventory."),
    "4124": ("plan_only_contact_for_availability", "Exact page says contact the community for current availability and exposes floor-plan rents only."),
    "4756": ("redirected_corporate_homepage_sibling_contamination", "Requested Clear Run currently serves the Gables corporate homepage with many sibling communities."),
    "16509": ("fetch_timeout", "Current exact property page timed out; no live native-unit evidence."),
    "19245": ("plan_only_and_blank_onesite", "Exact site publishes plan-level availability only; linked OneSite root identifies Tenzen but exposes no native rows."),
    "22962": ("exact_mri_plan_only_no_native_rows", "Fox Lane MRI portal shows three plan cards, zero data-unitid rows, and no positive rents."),
    "22964": ("exact_site_no_published_availability", "Exact Tropicana site exposes apply/contact navigation but no native availability."),
    "24982": ("provider_hosting_suspended", "Current URL redirects to Spherexx Site Taken Down (Non-Payment)."),
    "27349": ("plan_only_no_native_identity", "Eleven floor-plan rows are visible, but no native unit identity is published."),
    "32978": ("redirected_corporate_homepage", "Retired Camden property route redirects to the Camden corporate homepage; no exact property inventory."),
    "33993": ("provider_hosting_suspended", "Current URL redirects to Spherexx Site Taken Down (Non-Payment)."),
    "34362": ("tls_certificate_invalid", "Both direct and Chrome exact-domain probes fail ERR_CERT_COMMON_NAME_INVALID."),
    "34708": ("redirected_corporate_homepage", "Retired Camden property route redirects to the Camden corporate homepage; no exact property inventory."),
    "34785": ("provider_403_or_blank_root", "Direct Yotta URL is 403; Chrome redirects to a blank provider root with no unit evidence."),
    "37071": ("domain_hijacked", "The current domain serves an unrelated Chinese gambling page, not Summerwood."),
    "39198": ("network_security_interstitial", "Current route resolves to a Spectrum/CUJO security warning, not exact property data."),
    "40733": ("plan_only_no_native_identity", "Units & Prices expands six style/plan rows only; no native unit IDs."),
    "42554": ("fetch_timeout", "Current exact property page timed out; no live native-unit evidence."),
    "42977": ("provider_call_support_page", "Current RealPage route is a call-support/dead-property page with no inventory."),
    "48075": ("security_interstitial_no_source_data", "Direct is 403 and Chrome shows only a connection-security interstitial; no challenge interaction or derived rows."),
    "48389": ("captcha_blocked_no_interaction", "Village Gate redirected to /sgcaptcha/; no CAPTCHA interaction and no CAPTCHA-derived data."),
    "52541": ("exact_mri_plan_only_no_native_rows", "Woodbridge Manor MRI portal has exact identity but zero native rows and zero prices."),
    "53567": ("exact_mri_plan_only_no_native_rows", "Trafalgar Square MRI portal has exact identity but zero native rows and zero prices."),
    "53932": ("static_plan_pricing_no_native_availability", "Exact Misty Hollow page publishes amenity/plan pricing only; no native availability identity."),
    "64068": ("parked_or_placeholder_domain", "Current exact domain returns a 114-byte placeholder/parked response."),
    "71962": ("exact_onesite_blank_no_published_availability", "Towne House OneSite root confirms exact property/address but publishes no unit rows."),
    "72732": ("exact_plan_table_no_native_identity", "Willows exact page has two bedroom-type rent ranges, not native units."),
    "74523": ("static_plan_only_no_native_availability", "Charter Club rental information contains no native current inventory."),
    "75314": ("security_interstitial_no_source_data", "Direct returns 202/190 bytes and Chrome shows only a connection-security interstitial."),
    "78597": ("exact_floorplan_api_no_native_units", "Sentral exposes floorplans, but its authenticated production floorplan normalizer returns an empty units array."),
    "232583": ("exact_mri_plan_only_no_native_rows", "Custer Crossing MRI property 404 shows four plan cards but no native unit rows or prices."),
    "235473": ("static_floorplans_no_native_availability", "Rooftop252 exact floorplan page publishes plan links/amenities only; no rents or native units."),
    "246962": ("parked_or_placeholder_domain", "Current exact domain returns a 114-byte placeholder/parked response."),
    "260505": ("expired_parked_domain", "Current Broadstone Uptown PHX domain is a GoDaddy parked/expired page."),
    "263498": ("expired_parked_domain", "Current Rise Julington domain is a GoDaddy parked/expired page."),
    "268888": ("tls_protocol_error", "Direct and Chrome probes fail TLS/SSL protocol negotiation; no source unit evidence."),
    "272772": ("exact_site_no_published_availability", "Hawkeye Village exact site publishes leasing/application content but no current unit/rent rows."),
    "274886": ("plan_only_no_native_identity", "Aster Village Two publishes eleven floor-plan styles; no native unit identity is exposed."),
}


winner_by_pid = {winner["property_id"]: winner for winner in winners}
assert len(winner_by_pid) == 10, winner_by_pid.keys()
assert set(winner_by_pid).isdisjoint(exclusions)
assert set(target_by_pid) == set(winner_by_pid) | set(exclusions), (
    set(target_by_pid) - set(winner_by_pid) - set(exclusions),
    set(winner_by_pid) | set(exclusions) - set(target_by_pid),
)


for winner in winners:
    pid = winner["property_id"]
    row = target_by_pid[pid]
    winner["website"] = row["website"]
    winner["rp_oracle_native_unit_rows"] = int(row["rp_oracle_native_unit_rows"] or 0)
    winner["rp_oracle_distinct_floorplans"] = int(row["rp_oracle_distinct_floorplans"] or 0)
    winner["native_identity_rows"] = len(winner["units"])
    winner["native_positive_rent_rows"] = sum(
        numeric_rent(str(unit.get("rent", ""))) > 0 for unit in winner["units"]
    )
    assert winner["native_identity_rows"] == winner["native_positive_rent_rows"]
    winner["strict_verdict"] = "pass_exact_property_native_identity_positive_rent_no_contamination"


audited_properties: list[dict] = []
for pid in sorted(target_by_pid, key=int):
    base = target_by_pid[pid]
    current = redetection.get(pid, {})
    if pid in winner_by_pid:
        winner = winner_by_pid[pid]
        audited_properties.append(
            {
                "property_id": pid,
                "property_name": winner["property_name"],
                "strict_qualifies": True,
                "disposition": "strict_recovered",
                "provider": winner["provider"],
                "native_positive_rent_rows": winner["native_positive_rent_rows"],
                "rp_oracle_native_unit_rows": winner["rp_oracle_native_unit_rows"],
                "source_urls": winner["source_urls"],
            }
        )
    else:
        disposition, reason = exclusions[pid]
        audited_properties.append(
            {
                "property_id": pid,
                "property_name": base.get("property_name", ""),
                "website": base.get("website", ""),
                "strict_qualifies": False,
                "disposition": disposition,
                "reason": reason,
                "rp_oracle_native_unit_rows": int(base.get("rp_oracle_native_unit_rows") or 0),
                "current_fetch": {
                    key: current.get(key)
                    for key in ("status", "final_url", "title", "bytes", "error", "redetected_pms")
                    if current.get(key) not in (None, "")
                },
            }
        )


total_units = sum(winner["native_positive_rent_rows"] for winner in winners)
assert total_units == 203, total_units

evidence = {
    "audit": "FAILED_NO_DATA residual current-detected-adapter=unknown strict native-unit recovery",
    "capture_date": CAPTURE_DATE,
    "scope": {
        "input": str(TARGET_CSV),
        "input_rows": len(target),
        "filter": "current_detected_adapter == unknown",
        "rp_oracle_is_prioritization_only": True,
    },
    "strict_gate": [
        "exact property identity",
        "native unit identity",
        "positive base rent",
        "current publicly visible source data",
        "no sibling/portfolio contamination",
        "no plan-only conversion",
        "no CAPTCHA-derived data",
    ],
    "summary": {
        "audited_properties": len(target),
        "strict_recovered_properties": len(winners),
        "strict_recovered_native_positive_rent_units": total_units,
        "strict_excluded_properties": len(exclusions),
        "property_recovery_rate": round(len(winners) / len(target), 6),
        "paid_canary": False,
    },
    "provider_clusters": {
        "MRI ProspectConnect (TGM)": {"properties": 4, "units": 147},
        "RealPage Online Leasing": {"properties": 1, "units": 23},
        "Spherexx": {"properties": 1, "units": 10},
        "RentCafe": {"properties": 1, "units": 3},
        "Prometheus custom React": {"properties": 1, "units": 14},
        "RentManager": {"properties": 1, "units": 3},
        "RentPro RPA5": {"properties": 1, "units": 3},
    },
    "recoveries": sorted(winners, key=lambda row: int(row["property_id"])),
    "audited_properties": audited_properties,
}
EVIDENCE_OUT.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


ledger_rows: list[dict[str, str | int | bool]] = []
for winner in sorted(winners, key=lambda row: int(row["property_id"])):
    unit_ids = [str(unit["provider_unit_id"]) for unit in winner["units"]]
    ledger_rows.append(
        {
            "property_id": winner["property_id"],
            "property_name": winner["property_name"],
            "website": winner["website"],
            "evidence_lane": "unknown49_exact_provider_live",
            "artifact": str(EVIDENCE_OUT),
            "units": winner["native_positive_rent_rows"],
            "property_identity_match": True,
            "contamination_verdict": "pass_exact_property_no_sibling_contamination",
            "native_identity_rows": winner["native_identity_rows"],
            "native_positive_rent_rows": winner["native_positive_rent_rows"],
            "source_urls": " | ".join(winner["source_urls"]),
            "sample_native_unit_ids": " | ".join(unit_ids[:3]),
            "local_validation": "artifact_backed_browser_or_live_provider_no_paid_canary",
        }
    )


def write_ledger(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


write_ledger(LEDGER_OUT, ledger_rows)
existing = {
    row["property_id"]
    for row in read_csv(CURRENT_LEDGER)
} if CURRENT_LEDGER.exists() else set()
net_new = [row for row in ledger_rows if str(row["property_id"]) not in existing]
write_ledger(NET_NEW_OUT, net_new)

print(
    json.dumps(
        {
            "evidence": str(EVIDENCE_OUT),
            "ledger": str(LEDGER_OUT),
            "net_new_ledger": str(NET_NEW_OUT),
            "strict_properties": len(ledger_rows),
            "strict_units": total_units,
            "net_new_properties_vs_current_ledger": len(net_new),
            "net_new_units_vs_current_ledger": sum(int(row["units"]) for row in net_new),
        },
        indent=2,
    )
)
