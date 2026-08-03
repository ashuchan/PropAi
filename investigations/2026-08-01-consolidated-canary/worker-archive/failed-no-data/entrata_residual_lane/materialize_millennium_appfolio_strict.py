from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "entrata_residual_lane"
CONFIG = Path("ma_poc/config/properties.csv")
MARKETING_CAPTURE = ROOT / "hb_wix_doorloop_current_probe/271721.html.gz"
SOURCE_OUTPUT = LANE / "millennium_appfolio_current_full_pipeline.json"
STRICT_OUTPUT = LANE / "evidence_millennium_appfolio_current_strict.json"
PROPERTY_ID = "271721"
INVENTORY_PAGE = "https://www.millenniumnw.com/properties-for-rent"
EMBED_URL = (
    "https://www-millenniumnw-com.filesusr.com/html/"
    "790584_8774c5f3287cc8cbd5b49adeb9ba3765.html"
)
LISTINGS_URL = (
    "https://newmpm.appfolio.com/listings?"
    "theme_color=%23676767&filters%5Border_by%5D=rent_asc"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def positive_rent(unit: dict[str, object]) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and float(unit[field]) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


def read_config() -> dict[str, str]:
    with CONFIG.open(encoding="utf-8-sig", newline="") as handle:
        return next(
            row for row in csv.DictReader(handle) if row["apartmentid"] == PROPERTY_ID
        )


async def run_current_pipeline(
    row: dict[str, str], provider_html: bytes, final_url: str
) -> dict[str, object]:
    fetch = FetchResult(
        url=LISTINGS_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=provider_html,
        headers={},
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=LISTINGS_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    return await scrape_jugnu(
        task,
        fetch,
        page=None,
        profile=None,
        csv_row=row,
    )


async def main() -> None:
    row = read_config()
    marketing_html = gzip.open(
        MARKETING_CAPTURE, "rt", encoding="utf-8", errors="ignore"
    ).read()
    exact_embed_matches = sorted(
        set(
            re.findall(
                r'https://www-millenniumnw-com\.filesusr\.com/html/'
                r'[A-Za-z0-9_\-]+\.html',
                marketing_html,
            )
        )
    )
    if exact_embed_matches != [EMBED_URL]:
        raise RuntimeError(f"unexpected published embed routes: {exact_embed_matches}")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60,
        headers={"Accept": "text/html,application/xhtml+xml"},
    ) as client:
        embed = await client.get(EMBED_URL)
        provider = await client.get(LISTINGS_URL)
    embed.raise_for_status()
    provider.raise_for_status()
    embed_text = embed.text
    if "hostUrl: 'newmpm.appfolio.com'" not in embed_text:
        raise RuntimeError("published embed does not bind to newmpm AppFolio")
    if "propertyGroup: 'My Group Name'" not in embed_text:
        raise RuntimeError("expected unscoped placeholder propertyGroup not found")

    result = await run_current_pipeline(
        row, provider.content, str(provider.url)
    )
    units = [
        unit for unit in (result.get("units") or []) if isinstance(unit, dict)
    ]
    strict_units = [
        unit for unit in units if unit_has_real_anchor(unit) and positive_rent(unit)
    ]
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in strict_units
            if str(unit.get("source_api_url") or "").strip()
        }
    )
    address_filter_errors = [
        str(error)
        for error in (result.get("errors") or [])
        if str(error).startswith("appfolio-ssr-address-filter:")
    ]
    gates = {
        "exact_configured_inventory_page_capture_http_200": bool(
            MARKETING_CAPTURE.exists() and len(marketing_html) > 100_000
        ),
        "sole_published_filesusr_embed_route": exact_embed_matches == [EMBED_URL],
        "published_embed_binds_newmpm_appfolio": (
            "hostUrl: 'newmpm.appfolio.com'" in embed_text
        ),
        "placeholder_property_group_ignored": (
            "//propertyGroup: 'My Group Name'" in embed_text
        ),
        "current_full_pipeline_selected_appfolio_ssr": (
            result.get("_adapter_used") == "appfolio"
            and result.get("extraction_tier_used") == "TIER_1_DOM_APPFOLIO_SSR"
        ),
        "exact_address_zip_filter_kept_4_dropped_7": bool(
            len(address_filter_errors) == 1
            and "address_filter_applied kept=4 dropped=7" in address_filter_errors[0]
            and "ctx_addr='2002 N Monroe St'" in address_filter_errors[0]
            and "ctx_zip='99205'" in address_filter_errors[0]
        ),
        "all_emitted_rows_native_positive_rent": bool(
            len(units) == len(strict_units) == 4
        ),
        "all_rows_from_exact_appfolio_index": bool(
            source_urls
            and all(
                (urlsplit(url).hostname or "").casefold()
                == "newmpm.appfolio.com"
                and urlsplit(url).path == "/listings"
                for url in source_urls
            )
        ),
        "native_appfolio_listing_ids_present": all(
            str((unit.get("source_ids") or {}).get("appfolio_listing_id") or "")
            for unit in strict_units
        ),
        "configured_property_metadata_exact": (
            row.get("name") == "The Millennium on Monroe"
            and row.get("address") == "2002 N Monroe St"
            and row.get("city") == "Spokane"
            and row.get("state") == "WA"
            and row.get("zip") == "99205"
        ),
    }
    if not all(gates.values()):
        raise RuntimeError(
            f"strict gates failed: {[key for key, passed in gates.items() if not passed]}"
        )

    samples = []
    for unit in strict_units:
        source_ids = unit.get("source_ids") or {}
        samples.append(
            {
                "identity": {
                    "unit_number": str(unit.get("unit_number") or ""),
                    "appfolio_listing_id": str(
                        source_ids.get("appfolio_listing_id") or ""
                    ),
                },
                "floor_plan_name": str(unit.get("floor_plan_name") or ""),
                "bedrooms": unit.get("bedrooms"),
                "bathrooms": unit.get("bathrooms"),
                "sqft": unit.get("sqft"),
                "availability_date": unit.get("availability_date"),
                "positive_rent_evidence": {
                    "market_rent_low": unit.get("market_rent_low"),
                    "market_rent_high": unit.get("market_rent_high"),
                },
                "source_api_url": unit.get("source_api_url") or "",
            }
        )

    source_payload = {
        "lane": "millennium_published_appfolio_current_full_pipeline",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser_sessions_for_current_pipeline": 0,
            "paid_canary": False,
        },
        "property_id": int(PROPERTY_ID),
        "configured_property": row,
        "published_route_chain": {
            "inventory_page": INVENTORY_PAGE,
            "marketing_capture": str(MARKETING_CAPTURE),
            "marketing_capture_sha256": sha256(MARKETING_CAPTURE),
            "marketing_capture_decompressed_sha256": sha256_bytes(
                marketing_html.encode()
            ),
            "embed_url": EMBED_URL,
            "embed_status": embed.status_code,
            "embed_body_sha256": sha256_bytes(embed.content),
            "appfolio_index_url": LISTINGS_URL,
            "appfolio_status": provider.status_code,
            "appfolio_body_sha256": sha256_bytes(provider.content),
        },
        "current_full_pipeline": {
            "adapter": result.get("_adapter_used") or "",
            "tier": result.get("extraction_tier_used") or "",
            "emitted_units": len(units),
            "strict_native_positive_rent_rows": len(strict_units),
            "errors": result.get("errors") or [],
            "source_urls": source_urls,
            "native_samples": samples,
        },
        "strict_gates": gates,
    }
    SOURCE_OUTPUT.write_text(
        json.dumps(source_payload, indent=2, sort_keys=True) + "\n"
    )

    strict_payload = {
        "lane": "millennium_published_appfolio_current_strict",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_artifact": str(SOURCE_OUTPUT),
        "source_artifact_sha256": sha256(SOURCE_OUTPUT),
        "guardrails": source_payload["guardrails"],
        "results": [
            {
                "property_id": int(PROPERTY_ID),
                "property_name": row["name"],
                "website": row["website"],
                "outcome": "UNIT_QUALIFIED",
                "property_identity_match": True,
                "contamination_verdict": (
                    "pass_published_appfolio_index_exact_address_zip_filter_"
                    "native_priced_units"
                ),
                "units": len(strict_units),
                "adapter": result.get("_adapter_used") or "",
                "tier": result.get("extraction_tier_used") or "",
                "strict_gates": gates,
                "identity_evidence": {
                    "rows_with_native_identity": len(strict_units),
                    "rows_with_native_identity_and_positive_rent": len(strict_units),
                    "source_urls": source_urls,
                    "configured_address": row["address"],
                    "configured_zip": row["zip"],
                    "address_filter_errors": address_filter_errors,
                    "published_route_chain": [INVENTORY_PAGE, EMBED_URL, LISTINGS_URL],
                },
                "native_samples": samples,
            }
        ],
    }
    STRICT_OUTPUT.write_text(
        json.dumps(strict_payload, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "source_artifact": str(SOURCE_OUTPUT),
                "source_artifact_sha256": sha256(SOURCE_OUTPUT),
                "strict_artifact": str(STRICT_OUTPUT),
                "strict_artifact_sha256": sha256(STRICT_OUTPUT),
                "net_new_ids": [int(PROPERTY_ID)],
                "unit_counts": {PROPERTY_ID: len(strict_units)},
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
