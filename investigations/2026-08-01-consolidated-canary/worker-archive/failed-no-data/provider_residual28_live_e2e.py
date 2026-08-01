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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "evidence_provider_residual28_current_probe.json"
ADAPTERS = {
    "onesite",
    "realpage_oll",
    "sightmap",
    "knock",
    "encoreskyline_template",
}


def _load_targets() -> list[dict[str, str]]:
    with (ROOT / "strict_recovery_remaining_current.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        residual = [
            row
            for row in csv.DictReader(handle)
            if row.get("current_detected_adapter") in ADAPTERS
        ]
    metadata: dict[str, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            metadata[str(row.get("apartmentid") or "")] = row
    rows = []
    for target in residual:
        canonical = metadata.get(target["property_id"], {})
        rows.append({**target, **canonical, "property_id": target["property_id"]})
    return rows


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _page_identity(row: dict[str, str], body: str) -> dict[str, object]:
    text = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text_key = _key(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    text_tokens = set(text_key.split())
    name = row.get("name") or row.get("property_name") or ""
    name_key = _key(name)
    name_tokens = [
        token
        for token in name_key.split()
        if token not in {"the", "at", "of", "apartments", "apartment", "homes", "home"}
    ]
    name_exact = bool(name_key and name_key in text_key)
    name_token_match = bool(name_tokens and all(token in text_tokens for token in name_tokens))
    address = row.get("address") or ""
    address_tokens = _key(address).split()
    ignored = {
        "n", "s", "e", "w", "north", "south", "east", "west", "st",
        "street", "rd", "road", "ave", "avenue", "blvd", "boulevard",
        "pkwy", "parkway", "dr", "drive", "ln", "lane", "ct", "court",
        "hwy", "highway", "way", "pl", "place", "cir", "circle",
    }
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token for token in address_tokens[1:] if token not in ignored and not token.isdigit()
    ]
    address_match = bool(
        street_number
        and street_number in text_tokens
        and street_words
        and all(token in text_tokens for token in street_words)
    )
    return {
        "canonical_name": name,
        "canonical_address": address,
        "name_visible_exact_normalized": name_exact,
        "name_distinctive_tokens_visible": name_token_match,
        "street_number_and_words_visible": address_match,
        "street_words_checked": street_words,
        "visible_identity_match": bool((name_exact or name_token_match) and address_match),
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


def _anchor(unit: dict) -> str:
    if unit.get("unit_id") not in (None, ""):
        return str(unit["unit_id"])
    source_ids = unit.get("source_ids")
    if isinstance(source_ids, dict):
        for key, value in sorted(source_ids.items()):
            if value not in (None, "") and not key.endswith(("floorplan_id", "floor_plan_id")):
                return f"{key}:{value}"
    return str(unit.get("unit_number") or "")


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


async def _one(row: dict[str, str], semaphore: asyncio.Semaphore) -> dict[str, object]:
    async with semaphore:
        pid = row["property_id"]
        configured_url = str(row.get("website") or "")
        url = configured_url if "://" in configured_url else f"https://{configured_url}"
        try:
            response = await asyncio.to_thread(
                probe_get, url, timeout=30, unlocker=False, retries=1
            )
            status = int(getattr(response, "status_code", 0) or 0)
            body = str(getattr(response, "text", "") or "")
            final_url = str(getattr(response, "url", "") or url)
        except Exception as exc:
            return {
                "property_id": int(pid),
                "property_name": row.get("name") or row.get("property_name") or "",
                "website": configured_url,
                "source_adapter": row.get("current_detected_adapter") or "",
                "outcome": "FETCH_ERROR",
                "units": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        page_identity = _page_identity(row, body)
        if status != 200 or not body:
            result: dict = {}
        else:
            fetch_result = FetchResult(
                url=url,
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
                url,
                page=None,
                fetch_result=fetch_result,
                csv_row=row,
                property_id=pid,
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
        anchors = [_anchor(unit) for unit in qualified]
        source_urls = sorted(
            {
                str(unit.get("source_api_url") or "")
                for unit in qualified
                if unit.get("source_api_url")
            }
        )
        source_property_ids = sorted(
            {
                str(unit.get("source_property_id") or "")
                for unit in qualified
                if unit.get("source_property_id") not in (None, "")
            }
        )
        source_id_keys = sorted(
            {
                str(key)
                for unit in qualified
                for key in (
                    unit.get("source_ids", {}).keys()
                    if isinstance(unit.get("source_ids"), dict)
                    else []
                )
            }
        )
        return {
            "property_id": int(pid),
            "property_name": row.get("name") or row.get("property_name") or "",
            "website": configured_url,
            "source_adapter": row.get("current_detected_adapter") or "",
            "status": status,
            "final_url": final_url,
            "final_host": (urlparse(final_url).hostname or "").lower(),
            "outcome": "HAS_NATIVE_PRICED_UNITS" if qualified else "NO_NATIVE_PRICED_UNITS",
            "adapter": result.get("_adapter_used"),
            "tier": result.get("extraction_tier_used"),
            "units": len(units),
            "plans": len(result.get("plan_summaries") or []),
            "identity_evidence": {
                **page_identity,
                "rows_with_native_identity": len(native),
                "rows_with_native_identity_and_positive_rent": len(qualified),
                "distinct_effective_anchors": len(set(anchors)),
                "source_urls": source_urls,
                "source_property_ids": source_property_ids,
                "source_id_keys": source_id_keys,
                "llm_calls": 0,
                "paid_unlocker_calls": 0,
            },
            "native_samples": [_sample(unit) for unit in qualified[:3]],
            "errors": list(result.get("errors") or [])[-5:],
        }


async def main() -> None:
    targets = _load_targets()
    semaphore = asyncio.Semaphore(4)
    results = await asyncio.gather(*(_one(row, semaphore) for row in targets))
    results.sort(key=lambda row: int(row["property_id"]))
    payload = {
        "target_adapters": sorted(ADAPTERS),
        "targets": len(targets),
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "targets": len(results),
        "with_native_priced_units": sum(
            row.get("outcome") == "HAS_NATIVE_PRICED_UNITS" for row in results
        ),
        "candidate_ids": [
            row["property_id"]
            for row in results
            if row.get("outcome") == "HAS_NATIVE_PRICED_UNITS"
        ],
    }))


if __name__ == "__main__":
    asyncio.run(main())
