from __future__ import annotations

import csv
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane")
OUT = ROOT / "appfolio_collections"
OUT.mkdir(parents=True, exist_ok=True)

SITES = {
    "3788": ("https://www.citadelvillageco.com", "b79c836c", "Citadel Village"),
    "25443": ("https://www.leesquareapartments.com", "02e9806d", ""),
    "38107": ("https://www.livefountainplaceapts.com", "1b4904d2", ""),
    "44955": ("https://www.kjaxproperty.com", "a764de4b", ""),
    "46576": ("https://www.proresidential.com", "08205cf0", "Campus Pointe"),
    "47845": ("https://www.equilibriumprops.com", "1160e52d", ""),
    "56567": ("https://www.apartmentsstatecollege.com", "24e5665e", ""),
    "241145": ("https://www.midtownwestdetroit.com", "bc220411", ""),
    "282381": ("https://www.missionmanagementlindale.com", "0854c71d", ""),
}


def url(origin: str, site_id: str, page: int) -> str:
    return (
        f"{origin}/rts/collections/public/{site_id}/runtime/collection/"
        f"appfolio-listings/query-data?pageSize=100&pageNumber={page}"
        "&query=()&language=ENGLISH"
    )


def curl_json(target_url: str) -> dict:
    proc = subprocess.run(
        [
            "curl",
            "-L",
            "--compressed",
            "--max-time",
            "40",
            "--connect-timeout",
            "12",
            "--retry",
            "2",
            "-A",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/136 Safari/537.36",
            "-sS",
            target_url,
        ],
        capture_output=True,
        timeout=90,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-500:])
    return json.loads(proc.stdout)


first_pages: dict[str, dict] = {}
for pid, (origin, site_id, _) in SITES.items():
    first_pages[pid] = curl_json(url(origin, site_id, 0))

jobs: list[tuple[str, int, str]] = []
for pid, (origin, site_id, _) in SITES.items():
    page = first_pages[pid].get("page") or {}
    total_pages = int(page.get("totalPages") or 1)
    for number in range(1, total_pages):
        jobs.append((pid, number, url(origin, site_id, number)))

pages: dict[str, dict[int, dict]] = {
    pid: {0: first_pages[pid]} for pid in SITES
}
with ThreadPoolExecutor(max_workers=8) as pool:
    future_map = {
        pool.submit(curl_json, target_url): (pid, number, target_url)
        for pid, number, target_url in jobs
    }
    for future in as_completed(future_map):
        pid, number, target_url = future_map[future]
        pages[pid][number] = future.result()

properties: dict[str, dict[str, str]] = {}
with Path(
    "/Users/ankur/PropAi-codex-failed-no-data/ma_poc/config/properties.csv"
).open(newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        if row.get("apartmentid") in SITES:
            properties[row["apartmentid"]] = row


def norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def street_key(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\b(street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?)\b", "", text)
    return norm(text)


results = []
for pid, (origin, site_id, property_group) in SITES.items():
    values = [
        value
        for number in sorted(pages[pid])
        for value in (pages[pid][number].get("values") or [])
        if isinstance(value, dict) and isinstance(value.get("data"), dict)
    ]
    metadata = properties.get(pid, {})
    target_name = norm(metadata.get("name"))
    target_street = street_key(metadata.get("address"))
    target_zip = norm(metadata.get("zip"))
    property_lists = sorted(
        {
            str(entry.get("name") or "").strip()
            for value in values
            for entry in (value["data"].get("property_lists") or [])
            if isinstance(entry, dict) and str(entry.get("name") or "").strip()
        }
    )
    candidate_rows = []
    for value in values:
        data = value["data"]
        names = [
            str(entry.get("name") or "").strip()
            for entry in (data.get("property_lists") or [])
            if isinstance(entry, dict)
        ]
        full_address = str(data.get("full_address") or "")
        group_match = bool(
            property_group
            and any(norm(name) == norm(property_group) for name in names)
        )
        name_match = bool(
            target_name
            and any(
                target_name in norm(name) or norm(name) in target_name
                for name in names
                if norm(name)
            )
        )
        address_match = bool(
            target_street
            and (
                target_street in street_key(full_address)
                or street_key(full_address) in target_street
            )
            and (not target_zip or target_zip in norm(full_address))
        )
        if group_match or name_match or address_match:
            candidate_rows.append(
                {
                    "match_reasons": [
                        reason
                        for reason, matched in (
                            ("property_group", group_match),
                            ("property_list_name", name_match),
                            ("exact_street_zip", address_match),
                        )
                        if matched
                    ],
                    "listable_uid": data.get("listable_uid"),
                    "appfolio_id": data.get("id"),
                    "full_address": full_address,
                    "address_address2": data.get("address_address2"),
                    "property_lists": names,
                    "unit_template_name": data.get("unit_template_name"),
                    "bedrooms": data.get("bedrooms"),
                    "bathrooms": data.get("bathrooms"),
                    "square_feet": data.get("square_feet"),
                    "market_rent": data.get("market_rent"),
                    "rent_range": data.get("rent_range"),
                    "available": data.get("available"),
                    "available_date": data.get("available_date"),
                }
            )
    raw_path = OUT / f"{pid}.json"
    raw_path.write_text(
        json.dumps(
            {
                "property_id": int(pid),
                "origin": origin,
                "site_id": site_id,
                "property_group": property_group,
                "source_pages": [
                    url(origin, site_id, number) for number in sorted(pages[pid])
                ],
                "values": values,
            },
            indent=2,
        )
    )
    results.append(
        {
            "property_id": int(pid),
            "property_name": metadata.get("name", ""),
            "property_address": metadata.get("address", ""),
            "property_zip": metadata.get("zip", ""),
            "property_group": property_group,
            "total_collection_rows": len(values),
            "distinct_property_lists": property_lists,
            "candidate_count": len(candidate_rows),
            "candidate_rows": candidate_rows,
            "raw_artifact": str(raw_path),
        }
    )

(ROOT / "appfolio_collection_audit.json").write_text(
    json.dumps(
        {
            "batch": "appfolio_remaining_live_collection_audit",
            "direct_public_api": True,
            "captcha_used": False,
            "properties": results,
        },
        indent=2,
    )
)
print(
    json.dumps(
        [
            {
                "property_id": row["property_id"],
                "name": row["property_name"],
                "collection_rows": row["total_collection_rows"],
                "property_group": row["property_group"],
                "candidate_count": row["candidate_count"],
                "property_lists": row["distinct_property_lists"][:30],
            }
            for row in results
        ],
        indent=2,
    )
)
