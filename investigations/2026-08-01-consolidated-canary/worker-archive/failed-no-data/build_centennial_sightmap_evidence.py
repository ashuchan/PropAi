from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.extraction.post_process import post_process
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.sightmap import (
    extract_sightmap_api_url,
    find_sightmap_embed_codes,
    parse_sightmap_payload,
)


PROPERTY_ID = "238508"
PROPERTY_CONFIG = Path("ma_poc/config/properties.csv")
OUTPUT = Path(
    "/private/tmp/propai-fnd-vBkmT9/evidence_centennial_sightmap_strict.json"
)
SUBPAGE = "https://www.liveatcentennialapartments.com/apartments/"


def _key(value: object) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    return "".join(
        token
        for token in tokens
        if token not in {"apartment", "apartments", "community", "the"}
    )


def main() -> None:
    with PROPERTY_CONFIG.open(encoding="utf-8-sig", newline="") as handle:
        canonical = next(
            row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") == PROPERTY_ID
        )

    property_host = (urlparse(canonical["website"]).hostname or "").removeprefix(
        "www."
    )
    subpage_host = (urlparse(SUBPAGE).hostname or "").removeprefix("www.")
    subpage_response = probe_get(
        SUBPAGE,
        timeout=20,
        unlocker=False,
        proxies={},
        verify=True,
        retries=1,
    )
    subpage_body = str(subpage_response.text or "")
    embed_codes = find_sightmap_embed_codes(subpage_body)
    if len(embed_codes) != 1:
        raise SystemExit(f"Expected one exact SightMap embed, got {embed_codes}")
    embed_url = f"https://sightmap.com/embed/{embed_codes[0]}"
    embed_response = probe_get(
        embed_url,
        timeout=20,
        unlocker=False,
        proxies={},
        verify=True,
        retries=1,
    )
    api_url = extract_sightmap_api_url(str(embed_response.text or ""))
    if not api_url:
        raise SystemExit("SightMap embed did not publish a canonical API URL")
    api_response = probe_get(
        api_url,
        timeout=20,
        unlocker=False,
        proxies={},
        verify=True,
        retries=1,
    )
    payload = json.loads(str(api_response.text or ""))
    raw_units, dropped = parse_sightmap_payload(payload, api_url)
    processed = post_process(raw_units, property_id=PROPERTY_ID)
    qualified = [
        row
        for row in processed.admitted
        if unit_has_real_anchor(row)
        and isinstance(row.get("market_rent_low"), (int, float))
        and not isinstance(row.get("market_rent_low"), bool)
        and row["market_rent_low"] > 0
    ]

    data = payload.get("data") if isinstance(payload, dict) else {}
    asset = data.get("asset") if isinstance(data, dict) else {}
    asset_name = str(asset.get("name") or "") if isinstance(asset, dict) else ""
    body_lower = subpage_body.casefold()
    address_proof = all(
        str(canonical.get(field) or "").strip().casefold() in body_lower
        for field in ("address", "city", "zip")
    )
    proofs = {
        "same_origin_property_subpage": property_host == subpage_host,
        "subpage_http_200": int(subpage_response.status_code or 0) == 200,
        "canonical_address_city_zip_on_subpage": address_proof,
        "single_exact_embed": len(embed_codes) == 1,
        "embed_http_200": int(embed_response.status_code or 0) == 200,
        "embed_publishes_api_url": bool(api_url),
        "api_http_200": int(api_response.status_code or 0) == 200,
        "asset_name_matches_canonical": _key(asset_name) == _key(canonical["name"]),
        "zero_parser_join_drops": dropped == 0,
        "native_positive_rent_rows": len(qualified),
    }
    strict_pass = all(
        bool(value)
        for key, value in proofs.items()
        if key != "native_positive_rent_rows"
    ) and len(qualified) > 0

    recovery = {
        "property_id": int(PROPERTY_ID),
        "property_name": canonical["name"],
        "website": canonical["website"],
        "strict_verdict": (
            "pass_exact_same_origin_sightmap_chain"
            if strict_pass
            else "fail_sightmap_property_boundary"
        ),
        "native_identity_rows": len(qualified),
        "native_positive_rent_rows": len(qualified),
        "source_urls": [SUBPAGE, embed_url, api_url],
        "property_boundary_evidence": {
            "canonical_address": canonical["address"],
            "canonical_city": canonical["city"],
            "canonical_zip": canonical["zip"],
            "sightmap_asset_name": asset_name,
            "embed_code": embed_codes[0],
            "proofs": proofs,
        },
        "units": [
            {
                "unit_number": str(row.get("unit_number") or ""),
                "floor_plan_name": str(row.get("floor_plan_name") or ""),
                "rent": row.get("market_rent_low"),
                "market_rent_high": row.get("market_rent_high"),
                "availability_date": str(row.get("availability_date") or ""),
                "source_url": api_url,
                "source_ids": row.get("source_ids") or {},
            }
            for row in qualified
        ],
    }
    OUTPUT.write_text(
        json.dumps(
            {
                "batch_label": "centennial-sightmap-strict",
                "recoveries": [recovery],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "strict_verdict": recovery["strict_verdict"],
                "native_positive_rent_rows": len(qualified),
                "proofs": proofs,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
