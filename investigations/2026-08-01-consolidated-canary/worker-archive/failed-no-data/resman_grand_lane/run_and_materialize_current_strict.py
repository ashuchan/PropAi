from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.apts247 import Apts247Adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.resman import (
    _applicant_redirect_availability,
    _extract_unittypes,
    find_resman_applicant_url,
    parse_resman_unittypes,
)
from ma_poc.pms.detector import detect_pms, detect_pms_candidates


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "resman_grand_lane"
HARNESS = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
EVIDENCE = OUT / "evidence_grand_westchase_current_strict.json"
PROPERTY_ID = "12989"
PROPERTY_NAME = "Grand at Westchase"
WEBSITE = "https://www.thegrandatwestchase.com/"
ADDRESS = "10881 Richmond Ave."
CITY = "Houston"
ZIP = "77042"
EXPECTED_ACCOUNT = "1054"
EXPECTED_TENANT = "richmark.myresman.com"

EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_DC_PROXY_TIER": "false",
    "ENABLE_RESIDENTIAL_TIER": "false",
    "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "FETCH_BACKEND": "brightdata",
    "RENDER_BACKEND": "local",
    "PROBE_PROXY_URL": "",
    "PROXY_POOL_URLS": "",
    "ENABLE_RENDER_ON_EMPTY": "false",
    "ENABLE_PLAN_UNIT_RENDER": "false",
    "ENABLE_ENTRATA_PLAN_RENDER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and math.isfinite(float(row[field]))
        and float(row[field]) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def load_harness():
    spec = importlib.util.spec_from_file_location("grand_e2e_harness", HARNESS)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load configured-pipeline harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def apts247_control(label: str, property_id: str, url: str) -> dict:
    response = probe_get(
        url,
        timeout=30,
        unlocker=False,
        retries=1,
        proxies={},
    )
    html = response.text or ""
    detected = detect_pms(str(response.url), page_html=html)
    candidates = detect_pms_candidates(
        str(response.url),
        page_html=html,
        max_candidates=5,
    )
    ctx = AdapterContext(
        base_url=url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(
            body=response.content,
            final_url=str(response.url),
        ),
    )
    result = await Apts247Adapter().extract(None, ctx)
    native = [
        row
        for row in result.units
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    assert response.status_code == 200
    assert detected.pms == "apts247"
    assert candidates and candidates[0].pms == "apts247"
    assert result.tier_used == "TIER_1_API_APTS247"
    assert len(native) > 0
    assert len(native) == len(result.units)
    return {
        "label": label,
        "property_id": property_id,
        "url": url,
        "final_url": str(response.url),
        "detector_candidates": [
            {"pms": item.pms, "confidence": item.confidence}
            for item in candidates
        ],
        "adapter": "apts247",
        "tier": result.tier_used,
        "native_positive_rent_rows": len(native),
        "winning_url": result.winning_url,
        "verdict": "primary_apts247_unit_roster_preserved_no_resman_retry_needed",
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "")
        if actual.casefold() != expected.casefold():
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    harness = load_harness()
    remaining = next(
        row
        for row in harness.read_csv(harness.REMAINING)
        if row.get("property_id") == PROPERTY_ID
    )
    metadata = next(
        row
        for row in harness.read_csv(harness.PROPERTIES)
        if row.get("apartmentid") == PROPERTY_ID
    )
    assert metadata["name"] == PROPERTY_NAME
    assert metadata["address"] == ADDRESS
    assert metadata["city"] == CITY
    assert metadata["zip"] == ZIP
    assert metadata["website"] == WEBSITE

    parent_response = probe_get(
        WEBSITE,
        timeout=30,
        unlocker=False,
        retries=1,
        proxies={},
    )
    assert parent_response.status_code == 200
    parent_html = parent_response.text or ""
    parent_text = BeautifulSoup(parent_html, "lxml").get_text(" ", strip=True).casefold()
    assert "grand at westchase" in parent_text
    assert "10881 richmond" in parent_text
    assert "houston" in parent_text and ZIP in parent_text
    applicant_url = find_resman_applicant_url(parent_html)
    assert applicant_url is not None
    assert urlsplit(applicant_url).hostname == EXPECTED_TENANT
    assert urlsplit(applicant_url).query == f"a={EXPECTED_ACCOUNT}"

    provider_response = probe_get(
        applicant_url,
        timeout=30,
        unlocker=False,
        retries=1,
        proxies={},
    )
    assert provider_response.status_code == 200
    availability_url = _applicant_redirect_availability(
        applicant_url,
        str(provider_response.url),
    )
    assert availability_url == str(provider_response.url)
    provider_html = provider_response.text or ""
    provider_text = BeautifulSoup(provider_html, "lxml").get_text(" ", strip=True)
    assert PROPERTY_NAME.casefold() in provider_text.casefold()
    data = _extract_unittypes(provider_html)
    assert data
    raw_rows = parse_resman_unittypes(data, availability_url)
    raw_native = [
        row
        for row in raw_rows
        if unit_has_real_anchor(row) and positive_rent(row)
    ]
    raw_unit_numbers = [str(row.get("unit_number") or "") for row in raw_native]
    assert raw_native and len(raw_unit_numbers) == len(set(raw_unit_numbers))

    harness.fetch_mod.fetch = harness.direct_fetch
    harness.reset_web_unlocker_call_count()
    harness.reset_hyperbrowser_property_counts()
    configured = await harness.one(remaining, metadata)
    assert configured["current_detected_pms"] == "resman"
    assert configured["adapter"] == "resman"
    assert configured["tier"] == "TIER_1_API_RESMAN"
    assert configured["fallback_chain"] == ["apts247", "retry:resman"]
    assert configured["configured_fetch"]["status"] == 200
    assert configured["configured_fetch"]["outcome"] == "OK"
    assert configured["winning_page_url"] == availability_url
    assert configured["source_urls"] == [availability_url]
    assert not configured["errors"] and not configured["exception"]
    assert not configured["llm_interactions"]
    assert configured["emitted_unit_rows"] == len(raw_native)
    assert configured["native_identity_rows"] == len(raw_native)
    assert configured["strict_native_positive_rent_rows"] == len(raw_native)
    final_rows = configured["strict_shape_rows"]
    assert len(final_rows) == len(raw_native)
    final_unit_numbers = [str(row.get("unit_number") or "") for row in final_rows]
    assert set(final_unit_numbers) == set(raw_unit_numbers)
    assert len(final_unit_numbers) == len(set(final_unit_numbers))
    assert all(positive_rent(row) for row in final_rows)
    raw_by_unit = {str(row["unit_number"]): row for row in raw_native}
    final_by_unit = {str(row["unit_number"]): row for row in final_rows}
    assert all(
        raw_by_unit[unit]["market_rent_low"]
        == final_by_unit[unit]["market_rent_low"]
        for unit in final_by_unit
    )

    parent_raw = OUT / "12989_current_parent.html.gz"
    provider_raw = OUT / "12989_current_resman_availability.html.gz"
    with gzip.open(parent_raw, "wb") as handle:
        handle.write(parent_response.content)
    with gzip.open(provider_raw, "wb") as handle:
        handle.write(provider_response.content)

    controls = [
        await apts247_control(
            "Encore on Mustang",
            "9186",
            "https://www.encoreonmustangapts.com/",
        ),
        await apts247_control(
            "Crossings at Berkley Square",
            "live_control",
            "https://www.crossingsatberkleysquare.com/",
        ),
    ]

    critical_source = {
        str(path): sha256(Path(path))
        for path in (
            "ma_poc/pms/detector.py",
            "ma_poc/pms/adapters/apts247.py",
            "ma_poc/pms/adapters/resman.py",
            "ma_poc/pms/scraper.py",
        )
    }
    result = {
        "property_id": int(PROPERTY_ID),
        "property_name": PROPERTY_NAME,
        "website": WEBSITE,
        "outcome": "UNIT_QUALIFIED",
        "units": len(final_rows),
        "property_identity_match": True,
        "contamination_verdict": (
            "pass_exact_configured_property_published_single_resman_applicant_"
            "same_tenant_account_property_guid_full_pipeline_native_priced_units"
        ),
        "identity_evidence": {
            "canonical_name": PROPERTY_NAME,
            "canonical_address": ADDRESS,
            "current_parent_name_address_city_zip_match": True,
            "single_published_property_scoped_applicant_url": applicant_url,
            "same_tenant_same_account_availability_redirect": True,
            "provider_page_exact_property_name_match": True,
            "provider_property_guid": urlsplit(availability_url).query.split("p=", 1)[1],
            "configured_pipeline_apts247_plan_only_then_resman_unit_retry": True,
            "rows_with_native_identity": len(final_rows),
            "rows_with_native_identity_and_positive_rent": len(final_rows),
            "distinct_unit_numbers": len(final_rows),
            "source_urls": [availability_url],
        },
        "native_samples": [
            {
                "identity": {"unit_number": row["unit_number"]},
                "positive_rent_evidence": {
                    "market_rent_low": row["market_rent_low"],
                    "market_rent_high": row["market_rent_high"],
                },
                "source_api_url": row["source_api_url"],
            }
            for row in final_rows
        ],
        "native_rows": final_rows,
        "configured_pipeline": configured,
        "current_parent_capture": {
            "status": parent_response.status_code,
            "final_url": str(parent_response.url),
            "raw_artifact": str(parent_raw),
            "raw_artifact_sha256": sha256(parent_raw),
            "body_sha256": hashlib.sha256(parent_response.content).hexdigest(),
        },
        "current_provider_capture": {
            "status": provider_response.status_code,
            "final_url": availability_url,
            "raw_artifact": str(provider_raw),
            "raw_artifact_sha256": sha256(provider_raw),
            "body_sha256": hashlib.sha256(provider_response.content).hexdigest(),
            "raw_native_positive_rent_rows": len(raw_native),
            "exact_match_to_configured_pipeline": True,
        },
        "live_cluster_controls": controls,
    }
    payload = {
        "summary": {
            "result_type": "strict_current_grand_westchase_apts247_resman_retry",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": 1,
            "strict_unit_qualified_property_ids": [int(PROPERTY_ID)],
            "native_positive_rent_rows": len(final_rows),
            "live_cluster_properties_probed": 3,
            "live_positive_apts247_primary_controls": 2,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "web_unlocker": False,
            "web_unlocker_call_count": harness.web_unlocker_call_count(),
            "llm_used": False,
            "paid_canary_run": False,
            "critical_source_sha256": critical_source,
            "environment": EXPECTED_ENV,
        },
        "results": [result],
    }
    assert payload["summary"]["web_unlocker_call_count"] == 0
    assert harness.hyperbrowser_property_call_count(PROPERTY_ID) == 0
    EVIDENCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256(EVIDENCE),
                "property_id": int(PROPERTY_ID),
                "native_positive_rent_rows": len(final_rows),
                "live_cluster_properties_probed": 3,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
