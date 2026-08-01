from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup

import ma_poc.fetch as fetch_module
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_module
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.mri_prospectconnect import (
    extract_mri_property_route,
    mri_property_identity_matches,
)
from ma_poc.pms.detector import DetectedPMS


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
PROPERTY_ID = "75314"
CONFIG = Path("ma_poc/config/properties.csv")
CONFIGURED_URL = "https://pettinaro.com/village-park-paladin/"
CAPTURE = ROOT / "hb_unknown_high_value5_probe/75314.html.gz"
OUTPUT = (
    ROOT
    / "entrata_residual_lane/evidence_village_park_mri_current_full_strict.json"
)
EXPECTED_PORTAL = "https://residebpg.mriprospectconnect.com/475PV"
EXPECTED_COMMUNITY = "475PV"
EXPECTED_SEARCH = "https://residebpg.mriprospectconnect.com/Search/Search"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
        )
    )


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "requests",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for key, expected in expected_env.items():
        actual = os.environ.get(key, "").casefold()
        if actual != expected:
            raise RuntimeError(
                f"guardrail {key}={actual!r}; expected {expected!r}"
            )

    with CONFIG.open(newline="", encoding="utf-8-sig") as handle:
        row = next(
            item
            for item in csv.DictReader(handle)
            if item["apartmentid"] == PROPERTY_ID
        )

    root_body = gzip.open(CAPTURE, "rb").read()
    root_html = root_body.decode("utf-8", "replace")
    soup = BeautifulSoup(root_html, "lxml")
    root_text = normalize(soup.get_text(" ", strip=True))
    published_mri_urls = sorted(
        {
            str(anchor.get("href") or "").strip()
            for anchor in soup.select("a[href]")
            if "mriprospectconnect.com" in str(anchor.get("href") or "").casefold()
        }
    )
    canonical_routes = {
        extract_mri_property_route(url) for url in published_mri_urls
    }
    canonical_routes.discard(("", ""))

    fetched_by_url: dict[str, FetchResult] = {}
    fetch_calls: list[str] = []

    async def direct_fetch(task: CrawlTask, profile: object = None) -> FetchResult:
        del profile
        fetch_calls.append(task.url)
        started = time.monotonic()
        try:
            response = await asyncio.to_thread(
                probe_get,
                task.url,
                timeout=30,
                unlocker=False,
                retries=1,
                proxies={},
            )
            status = int(response.status_code or 0)
            body = str(response.text or "").encode()
            fetched = FetchResult(
                url=task.url,
                outcome=(
                    FetchOutcome.OK
                    if status == 200 and body
                    else FetchOutcome.HARD_FAIL
                ),
                status=status,
                body=body,
                headers=dict(response.headers or {}),
                render_mode=task.render_mode,
                final_url=str(response.url or task.url),
                attempts=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - evidence records failure
            fetched = FetchResult(
                url=task.url,
                outcome=FetchOutcome.TRANSIENT,
                status=None,
                body=None,
                headers={},
                render_mode=task.render_mode,
                final_url=task.url,
                attempts=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_signature=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        fetched_by_url[task.url] = fetched
        return fetched

    fetch_module.fetch = direct_fetch
    root_fetch = FetchResult(
        url=CONFIGURED_URL,
        outcome=FetchOutcome.OK,
        status=200,
        body=root_body,
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url=CONFIGURED_URL,
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=CONFIGURED_URL,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    started = time.monotonic()
    result = await asyncio.wait_for(
        scraper_module.scrape_jugnu(
            task,
            root_fetch,
            page=None,
            profile=None,
            csv_row=row,
        ),
        timeout=180,
    )

    emitted = [
        item for item in (result.get("units") or []) if isinstance(item, dict)
    ]
    strict = []
    for item in emitted:
        source_ids = item.get("source_ids") or {}
        native_id = (
            str(source_ids.get("mri_unit_id") or "").strip()
            if isinstance(source_ids, dict)
            else ""
        )
        if (
            unit_has_real_anchor(item)
            and positive_rent(item)
            and str(item.get("unit_number") or "").strip()
            and str(item.get("building") or "").strip()
            and native_id
            and native_id
            == str(item.get("provider_native_unit_id") or "").strip()
        ):
            strict.append(item)

    native_ids = [
        str((item.get("source_ids") or {}).get("mri_unit_id") or "").strip()
        for item in strict
    ]
    source_property_ids = {
        str(item.get("source_property_id") or "").strip() for item in strict
    }
    source_urls = {
        str(item.get("source_api_url") or "").strip() for item in strict
    }
    provider_fetch = fetched_by_url.get(EXPECTED_PORTAL)
    provider_body = (
        (provider_fetch.body or b"").decode("utf-8", "replace")
        if provider_fetch is not None
        else ""
    )
    provider_ctx = AdapterContext(
        base_url=EXPECTED_PORTAL,
        detected=DetectedPMS(
            pms="mri_prospectconnect",
            confidence=0.95,
            evidence=["exact page-published property route"],
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id=PROPERTY_ID,
        fetch_result=provider_fetch,
        property_name=row["name"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip"],
    )
    gates = {
        "current_hb_capture_is_substantive": len(root_body) > 200_000,
        "configured_root_name_visible": all(
            token in set(root_text.split())
            for token in normalize(row["name"]).split()
        ),
        "configured_root_zip_visible": row["zip"] in root_text.split(),
        "sole_exact_page_published_mri_route": canonical_routes
        == {
            (
                "https://residebpg.mriprospectconnect.com/Search/Index/475PV",
                EXPECTED_COMMUNITY,
            )
        },
        "current_full_pipeline_fetched_exact_published_route_first": fetch_calls
        == [EXPECTED_PORTAL],
        "provider_index_http_200": provider_fetch is not None
        and provider_fetch.status == 200,
        "provider_full_property_identity_matches": bool(provider_body)
        and mri_property_identity_matches(
            provider_body, provider_ctx, EXPECTED_COMMUNITY
        ),
        "current_full_pipeline_selected_mri": result.get("_adapter_used")
        == "mri_prospectconnect",
        "current_full_pipeline_selected_native_unit_tier": result.get(
            "extraction_tier_used"
        )
        == "TIER_1_API_MRI_PROSPECTCONNECT",
        "current_full_pipeline_link_hop_from_configured_root": str(
            result.get("_link_hop_from") or ""
        ).rstrip("/")
        == CONFIGURED_URL.rstrip("/"),
        "current_full_pipeline_provider_link_priority": str(
            result.get("_link_hop_anchor") or ""
        ).startswith("embedded-portal:mri_published_link:"),
        "all_emitted_rows_native_and_positive_rent": bool(strict)
        and len(strict) == len(emitted),
        "unique_provider_native_unit_ids": bool(native_ids)
        and len(native_ids) == len(set(native_ids)),
        "sole_exact_provider_community": source_property_ids
        == {EXPECTED_COMMUNITY},
        "sole_exact_provider_search_source": source_urls == {EXPECTED_SEARCH},
    }
    passed = all(gates.values())
    evidence_row = {
        "property_id": int(PROPERTY_ID),
        "property_name": row["name"],
        "website": CONFIGURED_URL,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "property_identity_match": passed,
        "contamination_verdict": (
            "pass_configured_page_sole_mri_community_full_provider_identity_native_units"
            if passed
            else "reject_mri_full_pipeline_or_property_boundary_gate_failed"
        ),
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(strict),
        "strict_gates": gates,
        "identity_evidence": {
            "rows_with_native_identity": len(strict),
            "rows_with_native_identity_and_positive_rent": len(strict),
            "distinct_provider_native_ids": len(set(native_ids)),
            "source_property_ids": sorted(source_property_ids),
            "source_urls": sorted(source_urls),
            "published_provider_urls": published_mri_urls,
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": item.get("unit_number") or "",
                    "building": item.get("building") or "",
                    "mri_unit_id": (item.get("source_ids") or {}).get(
                        "mri_unit_id"
                    )
                    or "",
                },
                "positive_rent_evidence": {
                    "market_rent_low": item.get("market_rent_low")
                },
                "floor_plan_name": item.get("floor_plan_name") or "",
                "availability_date": item.get("availability_date") or "",
                "source_property_id": item.get("source_property_id") or "",
                "source_api_url": item.get("source_api_url") or "",
            }
            for item in strict[:5]
        ],
        "configured_final_url": CONFIGURED_URL,
        "published_provider_route": EXPECTED_PORTAL,
        "winning_page_url": result.get("_winning_page_url") or "",
        "link_hop_from": result.get("_link_hop_from") or "",
        "link_hop_anchor": result.get("_link_hop_anchor") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "errors": result.get("errors") or [],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    payload = {
        "lane": "village_park_mri_current_full_configured_pipeline",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "hyperbrowser_sessions_for_source_capture": 1,
            "new_hyperbrowser_sessions_for_validation": 0,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
        },
        "source_capture": str(CAPTURE),
        "source_capture_sha256": sha256(CAPTURE),
        "results": [evidence_row],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence_row, indent=2, sort_keys=True))
    print(json.dumps({"artifact": str(OUTPUT), "sha256": sha256(OUTPUT)}))


if __name__ == "__main__":
    asyncio.run(main())
