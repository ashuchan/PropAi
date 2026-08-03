from __future__ import annotations

import asyncio
import csv
import gzip
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_ID = "268888"
URL = "https://arthaus.mov/building-community.php?slug=arthaus-jack-london"
CAPTURE = ROOT / "hb_arthaus_provider_capture" / "capture.json"
HTML = ROOT / "hb_unknown_high_value5_probe" / "268888.html.gz"
OUTPUT = ROOT / "evidence_arthaus_yardi_proxy_current_e2e.json"


def _configured_row() -> dict[str, str]:
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if str(row.get("apartmentid") or "") == PROPERTY_ID:
                return row
    raise RuntimeError(f"missing configured property {PROPERTY_ID}")


def _normal(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
    )


def _decoded_response(network_log: list[dict], needle: str) -> tuple[str, dict]:
    matches = [entry for entry in network_log if needle in str(entry.get("url") or "")]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {needle!r} response, got {len(matches)}")
    entry = matches[0]
    body = entry.get("body")
    if isinstance(body, str):
        body = json.loads(body)
    if not isinstance(body, dict):
        # The property endpoint returns a one-element array.
        if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
            body = body[0]
        else:
            raise RuntimeError(f"unexpected response shape for {needle}")
    return str(entry.get("url") or ""), body


async def main() -> None:
    configured = _configured_row()
    capture = json.loads(CAPTURE.read_text())
    network_log = capture["network_log"]
    body = gzip.open(HTML, "rb").read()
    fetch_result = FetchResult(
        url=URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=URL,
        attempts=1,
        elapsed_ms=0,
        network_log=network_log,
    )
    result = await scraper_mod.scrape(
        URL,
        page=None,
        fetch_result=fetch_result,
        csv_row=configured,
        property_id=PROPERTY_ID,
        shared_budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )

    property_url, property_payload = _decoded_response(
        network_log, "endpoint=property&slug=arthaus-jack-london"
    )
    availability_url, availability_payload = _decoded_response(
        network_log, "yardi_endpoint=/data/PJDUP/availability/p2109661"
    )
    raw_units = availability_payload.get("apartmentAvailabilities") or []
    units = list(result.get("units") or [])

    native = [row for row in units if unit_has_real_anchor(row)]
    native_priced = [row for row in native if _positive_rent(row)]
    apartment_ids = [
        str((row.get("source_ids") or {}).get("securecafe_apartment_id") or "")
        for row in units
    ]
    unit_numbers = [str(row.get("unit_number") or "") for row in units]
    source_property_ids = {
        str(row.get("source_property_id") or "") for row in units
    }
    output_dates = [str(row.get("availability_date") or "") for row in units]
    raw_dates = [str(row.get("availableDate") or "") for row in raw_units]

    api_title = str((property_payload.get("title") or {}).get("rendered") or "")
    api_acf = property_payload.get("acf") or {}
    api_address = str(api_acf.get("address") or "")
    api_slug = str(property_payload.get("slug") or "")
    configured_street = _normal(configured.get("address"))
    exact_url_slug = urlparse(URL).query == "slug=arthaus-jack-london"
    identity_match = bool(
        exact_url_slug
        and api_slug == "arthaus-jack-london"
        and configured_street
        and configured_street in _normal(api_address)
        and _normal(configured.get("city")) in _normal(api_address)
        and _normal(configured.get("state")) in _normal(api_address)
        and _normal(configured.get("zip")) in _normal(api_address)
        # Config has the historical one-letter typo "Artthaus"; the exact
        # slug plus full street/city/state/ZIP boundary is the stronger key.
        and _normal(api_title) == "arthaus jack london"
    )

    raw_property_ids = {str(row.get("propertyId") or "") for row in raw_units}
    apply_property_ids = {
        str(
            (parse_qs(urlparse(str(row.get("applyOnlineURL") or "")).query).get(
                "myOlePropertyid"
            )
            or [""])[0]
        )
        for row in raw_units
    }
    contamination_pass = bool(
        raw_units
        and raw_property_ids == {"2142454"}
        and apply_property_ids == {"2142454"}
        and source_property_ids == {"2142454"}
        and all("233-broadway-oakland-ca-94607" in str(row.get("applyOnlineURL") or "") for row in raw_units)
    )
    strict_pass = bool(
        identity_match
        and contamination_pass
        and result.get("_adapter_used") == "rentcafe"
        and len(units) == len(raw_units) == 11
        and len(native) == len(native_priced) == len(units)
        and all(apartment_ids)
        and len(set(apartment_ids)) == len(apartment_ids)
        and all(unit_numbers)
        and len(set(unit_numbers)) == len(unit_numbers)
        and output_dates == raw_dates
    )

    artifact = {
        "generated_from": {
            "capture": str(CAPTURE),
            "rendered_html": str(HTML),
            "live_cluster_probe": str(
                ROOT / "hb_arthaus_yardi_cluster_probe" / "cluster.json"
            ),
            "llm_enabled": False,
            "paid_canary": False,
            "captcha_solving": False,
        },
        "results": [
            {
                "property_id": int(PROPERTY_ID),
                "property_name": configured.get("name") or "",
                "website": configured.get("website") or URL,
                "outcome": "UNIT_QUALIFIED" if strict_pass else "UNIT_UNVERIFIED",
                "adapter": result.get("_adapter_used"),
                "tier": result.get("extraction_tier_used"),
                "units": len(units),
                "plans": len(result.get("plan_summaries") or []),
                "property_identity_match": identity_match,
                "contamination_verdict": (
                    "pass_exact_slug_address_and_single_yardi_property_id"
                    if contamination_pass
                    else "fail_property_boundary"
                ),
                "identity_evidence": {
                    "configured_name": configured.get("name") or "",
                    "configured_address": ", ".join(
                        str(configured.get(key) or "")
                        for key in ("address", "city", "state", "zip")
                    ),
                    "api_title": api_title,
                    "api_address": api_address,
                    "api_slug": api_slug,
                    "property_endpoint": property_url,
                    "rows_with_native_identity": len(native),
                    "rows_with_native_identity_and_positive_rent": len(native_priced),
                    "distinct_native_apartment_ids": len(set(apartment_ids)),
                    "distinct_native_unit_numbers": len(set(unit_numbers)),
                    "source_property_ids": sorted(source_property_ids),
                    "raw_property_ids": sorted(raw_property_ids),
                    "apply_url_property_ids": sorted(apply_property_ids),
                    "source_urls": [availability_url],
                    "native_dates_preserved_exactly": output_dates == raw_dates,
                },
                "native_samples": [
                    {
                        "identity": {
                            "unit_number": str(row.get("unit_number") or ""),
                            "native_unit_id": str(
                                (row.get("source_ids") or {}).get(
                                    "securecafe_apartment_id"
                                )
                                or ""
                            ),
                        },
                        "source_ids": row.get("source_ids") or {},
                        "source_property_id": str(
                            row.get("source_property_id") or ""
                        ),
                        "source_api_url": str(row.get("source_api_url") or ""),
                        "floor_plan_name": str(row.get("floor_plan_name") or ""),
                        "availability_date": str(
                            row.get("availability_date") or ""
                        ),
                        "positive_rent_evidence": {
                            "market_rent_low": row.get("market_rent_low"),
                            "market_rent_high": row.get("market_rent_high"),
                        },
                    }
                    for row in units
                ],
                "local_validation": {
                    "strict_pass": strict_pass,
                    "all_output_rows_equal_native_roster": len(units) == len(raw_units),
                    "all_output_dates_equal_native_dates": output_dates == raw_dates,
                    "all_output_rows_have_unique_native_ids": (
                        bool(apartment_ids)
                        and all(apartment_ids)
                        and len(set(apartment_ids)) == len(apartment_ids)
                    ),
                    "all_output_rows_have_positive_rent": len(native_priced) == len(units),
                },
            }
        ],
    }
    OUTPUT.write_text(json.dumps(artifact, indent=2) + "\n")
    print(json.dumps(artifact["results"][0], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
