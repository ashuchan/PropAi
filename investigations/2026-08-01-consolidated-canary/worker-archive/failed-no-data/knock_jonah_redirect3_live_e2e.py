from __future__ import annotations

import asyncio
import csv
import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "evidence_knock_jonah_redirect3_strict.json"
TARGETS = {34303, 221995, 252891}


def _metadata() -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            try:
                property_id = int(row.get("apartmentid") or "")
            except ValueError:
                continue
            if property_id in TARGETS:
                rows[property_id] = row
    return rows


def _plain_text(body: str) -> str:
    without_scripts = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _identity_evidence(row: dict[str, str], body: str) -> dict[str, object]:
    text_key = _key(_plain_text(body))
    name_key = _key(row.get("name") or "")
    address = row.get("address") or ""
    address_tokens = _key(address).split()
    street_number = address_tokens[0] if address_tokens else ""
    ignored = {
        "n", "s", "e", "w", "north", "south", "east", "west", "st",
        "street", "rd", "road", "ave", "avenue", "blvd", "boulevard",
        "pkwy", "parkway", "dr", "drive", "ln", "lane", "ct", "court",
    }
    street_words = [
        token for token in address_tokens[1:] if token not in ignored and not token.isdigit()
    ]
    name_match = bool(name_key and name_key in text_key)
    address_match = bool(
        street_number
        and street_number in text_key.split()
        and street_words
        and all(token in text_key.split() for token in street_words)
    )
    return {
        "canonical_name": row.get("name") or "",
        "canonical_address": row.get("address") or "",
        "name_visible_exact_normalized": name_match,
        "street_number_and_words_visible": address_match,
        "street_words_checked": street_words,
        "property_identity_match": name_match and address_match,
    }


def _positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(key), (int, float))
        and not isinstance(unit.get(key), bool)
        and unit.get(key) > 0
        for key in (
            "market_rent_low", "market_rent_high", "rent_low", "rent_high",
            "asking_rent", "rent",
        )
    )


def _sample(unit: dict) -> dict[str, object]:
    return {
        "identity": {
            key: str(unit.get(key))
            for key in ("unit_number", "unit_id", "native_unit_id", "source_unit_id")
            if unit.get(key) not in (None, "")
        },
        "source_ids": unit.get("source_ids")
        if isinstance(unit.get("source_ids"), dict)
        else {},
        "source_property_id": str(unit.get("source_property_id") or ""),
        "source_api_url": str(unit.get("source_api_url") or ""),
        "floor_plan_name": str(unit.get("floor_plan_name") or ""),
        "positive_rent_evidence": {
            key: unit.get(key)
            for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
            if isinstance(unit.get(key), (int, float)) and unit.get(key) > 0
        },
    }


async def _fetch(url: str) -> tuple[int, str, str]:
    response = await asyncio.to_thread(
        probe_get, url, timeout=30, unlocker=False, retries=1
    )
    return (
        int(getattr(response, "status_code", 0) or 0),
        str(getattr(response, "text", "") or ""),
        str(getattr(response, "url", "") or url),
    )


async def _one(pid: int, row: dict[str, str]) -> dict[str, object]:
    configured_url = row["website"]
    scrape_url = configured_url
    if pid in {34303, 221995}:
        archived_path = ROOT / "raw_all" / f"{pid}.html.gz"
        import gzip

        archived = gzip.open(archived_path, "rb").read().decode("utf-8", "replace")
        detected = detect_pms(configured_url, csv_row=row, page_html=archived)
        scrape_url = await scraper_mod._rediscover_stale_gsc_property_url(
            configured_url, detected, str(pid), row
        ) or ""
    if not scrape_url:
        return {
            "property_id": pid,
            "property_name": row.get("name") or "",
            "website": configured_url,
            "outcome": "EMPTY",
            "units": 0,
            "property_identity_match": False,
            "contamination_verdict": "no_exact_property_url",
            "identity_evidence": {
                "rows_with_native_identity": 0,
                "rows_with_native_identity_and_positive_rent": 0,
            },
            "native_samples": [],
        }

    status, body, final_url = await _fetch(scrape_url)
    page_identity = _identity_evidence(row, body)
    if status != 200 or not body:
        result: dict = {}
    else:
        fetch_result = FetchResult(
            url=scrape_url,
            outcome=FetchOutcome.OK,
            status=status,
            body=body.encode(),
            headers={},
            render_mode=RenderMode.GET,
            final_url=final_url,
            attempts=1,
            elapsed_ms=0,
        )
        result = await scraper_mod.scrape(
            scrape_url,
            page=None,
            fetch_result=fetch_result,
            csv_row=row,
            property_id=str(pid),
            shared_budget={
                "llm_api_calls": 0,
                "llm_dom_calls": 0,
                "llm_monolithic": 0,
                "link_hop": 0,
                "_cost_cap_usd": 0,
            },
        )

    units = list(result.get("units") or [])
    native = [unit for unit in units if unit_has_real_anchor(unit)]
    qualified = [unit for unit in native if _positive_rent(unit)]
    unit_ids = [
        str(unit.get("unit_id") or unit.get("unit_number") or "")
        for unit in qualified
    ]
    source_property_ids = {
        str(unit.get("source_property_id") or "")
        for unit in qualified
        if unit.get("source_property_id") not in (None, "")
    }
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in qualified
            if unit.get("source_api_url")
        }
    )
    exact_identity = bool(page_identity["property_identity_match"])
    all_native_priced = bool(units and len(native) == len(units) == len(qualified))
    no_collisions = bool(unit_ids and len(unit_ids) == len(set(unit_ids)))
    # Knock rows all carry one exact provider property id. Jonah rows instead
    # stay bounded by same-property URLs and may not expose a provider id.
    provider_boundary = len(source_property_ids) <= 1
    passed = exact_identity and all_native_priced and no_collisions and provider_boundary
    return {
        "property_id": pid,
        "property_name": row.get("name") or "",
        "website": configured_url,
        "scrape_url": scrape_url,
        "final_url": final_url,
        "status": status,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED" if units else "EMPTY",
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "units": len(units),
        "plans": len(result.get("plan_summaries") or []),
        "property_identity_match": exact_identity,
        "contamination_verdict": (
            "pass_exact_visible_name_address_native_positive_rent_unique_identity"
            if passed
            else "failed_exact_property_or_native_unit_gate"
        ),
        "identity_evidence": {
            **page_identity,
            "rows_with_native_identity": len(native),
            "rows_with_native_identity_and_positive_rent": len(qualified),
            "distinct_final_unit_ids": len(set(unit_ids)),
            "source_property_ids": sorted(source_property_ids),
            "source_urls": source_urls,
            "provider_boundary_single_id": provider_boundary,
            "llm_calls": 0,
            "paid_unlocker_calls": 0,
        },
        "native_samples": [_sample(unit) for unit in qualified[:5]],
        "errors": list(result.get("errors") or [])[-5:],
    }


async def main() -> None:
    metadata = _metadata()
    missing = TARGETS - metadata.keys()
    if missing:
        raise SystemExit(f"Missing canonical metadata: {sorted(missing)}")
    results = await asyncio.gather(*(_one(pid, metadata[pid]) for pid in sorted(TARGETS)))
    payload = {"results": results}
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload))


if __name__ == "__main__":
    asyncio.run(main())
