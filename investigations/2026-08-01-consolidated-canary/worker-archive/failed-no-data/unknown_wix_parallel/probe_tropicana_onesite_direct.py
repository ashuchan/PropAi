from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ma_poc.extraction.post_process import post_process
from ma_poc.pms.adapters._probe import (
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.onesite import _probe_onesite_workflowstartup
from ma_poc.pms.detector import DetectedPMS


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/unknown_wix_parallel")
PROPERTY_ID = "22964"
CONFIGURED_URL = "https://www.tropicanavillageapartments.com/"
APPLICATION_URL = (
    "https://www.tropicanavillageapartments.com/apply-now/application-process"
)
EXPECTED_OLL_URL = "https://8452181.onlineleasing.realpage.com"
EXPECTED_SITE_ID = "3858548"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _page_identity(text: str, url: str) -> dict:
    soup = BeautifulSoup(text, "html.parser")
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return {
        "url": url,
        "host": urlparse(url).hostname,
        "sha256": _sha(text),
        "bytes": len(text.encode("utf-8", "replace")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "visible_identity_tokens": {
            "tropicana_village": "tropicana village" in visible.casefold(),
            "street": "4995" in visible and "maryland" in visible.casefold(),
            "city_state_zip": all(
                token in visible.casefold() for token in ("las vegas", "nv", "89119")
            ),
        },
        "text_prefix": visible[:1500],
    }


def _rp_rows() -> list[dict[str, str]]:
    path = Path("/Users/ankur/Downloads/rp_unit_detail_0731.csv")
    with path.open(newline="", errors="replace") as handle:
        return [
            row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "") == PROPERTY_ID
        ]


async def main() -> None:
    assert os.getenv("COMPLIANCE_MODE", "").casefold() in {"1", "true", "yes", "on"}
    assert not os.getenv("WEB_UNLOCKER_KEY", "").strip()
    reset_web_unlocker_call_count()

    root_response = await asyncio.to_thread(
        probe_get,
        CONFIGURED_URL,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    application_response = await asyncio.to_thread(
        probe_get,
        APPLICATION_URL,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    root_html = root_response.text or ""
    application_html = application_response.text or ""
    assert root_response.status_code == 200
    assert application_response.status_code == 200
    assert EXPECTED_OLL_URL in application_html

    oll_response = await asyncio.to_thread(
        probe_get,
        EXPECTED_OLL_URL + "/",
        timeout=30,
        unlocker=False,
        retries=1,
    )
    oll_html = oll_response.text or ""
    assert oll_response.status_code == 200
    assert f"siteId={EXPECTED_SITE_ID}" in oll_html

    combined = application_html + "\n" + oll_html
    fetch_result = SimpleNamespace(
        body=combined,
        final_url=APPLICATION_URL,
        status_code=200,
    )
    detected = DetectedPMS(
        pms="onesite",
        confidence=1.0,
        evidence=["official application page publishes exact RealPage OLL portal"],
        recommended_strategy="api_first",
    )
    ctx = AdapterContext(
        base_url=APPLICATION_URL,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=PROPERTY_ID,
        fetch_result=fetch_result,
        property_name="Tropicana Village Apartments",
        address="4995 S Maryland Pkwy",
        city="Las Vegas",
        state="NV",
        zip_code="89119",
    )
    raw_rows = await _probe_onesite_workflowstartup(ctx)
    processed = post_process(raw_rows, property_id=PROPERTY_ID)
    admitted = processed.units

    def positive_rent(row: dict) -> bool:
        for key in ("rent_low", "market_rent_low", "rent_high", "market_rent_high"):
            try:
                if float(row.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        return bool(re.search(r"\$\s*[1-9]", str(row.get("rent_range") or "")))

    native_priced = [
        row
        for row in admitted
        if str(row.get("unit_number") or row.get("unit_id") or "").strip()
        and positive_rent(row)
    ]
    native_ids = [
        str(row.get("unit_number") or row.get("unit_id") or "").strip()
        for row in native_priced
    ]
    source_site_ids = sorted(
        {
            str(row.get("source_property_id") or "")
            for row in native_priced
            if row.get("source_property_id")
        }
    )
    source_portals = sorted(
        {
            str(row.get("source_portal_url") or "")
            for row in native_priced
            if row.get("source_portal_url")
        }
    )
    source_api_urls = sorted(
        {
            str(row.get("source_api_url") or "")
            for row in native_priced
            if row.get("source_api_url")
        }
    )
    source_api_site_ids = sorted(
        {
            match.group(1)
            for url in source_api_urls
            if (
                match := re.search(
                    r"/workflowstartup/v1/(\d+)/English(?:[/?#]|$)", url
                )
            )
        }
    )
    configured_identity = _page_identity(root_html, CONFIGURED_URL)
    application_identity = _page_identity(application_html, APPLICATION_URL)
    oll_identity = _page_identity(oll_html, EXPECTED_OLL_URL + "/")
    configured_identity_exact = all(
        configured_identity["visible_identity_tokens"].values()
    )
    application_identity_exact = all(
        application_identity["visible_identity_tokens"].values()
    )
    oll_identity_exact = all(oll_identity["visible_identity_tokens"].values())
    official_page_publishes_exact_oll = EXPECTED_OLL_URL in application_html
    oll_shell_has_exact_site_id = f"siteId={EXPECTED_SITE_ID}" in oll_html
    all_api_urls_exact_site_id = (
        bool(source_api_urls)
        and source_api_site_ids == [EXPECTED_SITE_ID]
        and all(
            f"/workflowstartup/v1/{EXPECTED_SITE_ID}/English" in url
            for url in source_api_urls
        )
    )
    rp = _rp_rows()

    strict_checks = {
        "current_native_priced_rows_positive": len(native_priced) > 0,
        "current_native_ids_unique": len(native_ids) == len(set(native_ids)),
        "all_rows_exact_source_property_id": source_site_ids == [EXPECTED_SITE_ID],
        "all_source_api_urls_exact_site_id": all_api_urls_exact_site_id,
        "configured_page_exact_identity": configured_identity_exact,
        "official_application_page_exact_identity": application_identity_exact,
        "official_page_publishes_exact_oll": official_page_publishes_exact_oll,
        "oll_shell_exact_identity": oll_identity_exact,
        "oll_shell_has_exact_site_id": oll_shell_has_exact_site_id,
        "no_web_unlocker_calls": web_unlocker_call_count() == 0,
    }

    artifact = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "property_id": int(PROPERTY_ID),
        "property_name": "Tropicana Village Apartments",
        "guardrails": {
            "compliance_mode": True,
            "fingerprint_rotation": False,
            "stable_tls_profile_attempts_per_site_id": 1,
            "web_unlocker": False,
            "web_unlocker_calls": web_unlocker_call_count(),
            "hyperbrowser": False,
            "captcha_solving": False,
            "flaresolverr": False,
            "llm": False,
            "paid_canary": False,
        },
        "identity_chain": {
            "configured_page": configured_identity,
            "official_application_page": application_identity,
            "published_oll_url": EXPECTED_OLL_URL,
            "oll_shell": oll_identity,
            "published_site_id": EXPECTED_SITE_ID,
            "oll_shell_has_exact_site_id": oll_shell_has_exact_site_id,
        },
        "current_result": {
            "raw_rows": len(raw_rows),
            "post_process_units": len(admitted),
            "post_process_plan_summaries": len(processed.plan_summaries),
            "post_process_rejected": len(processed.rejected),
            "native_priced_rows": len(native_priced),
            "distinct_native_ids": len(set(native_ids)),
            "duplicate_native_ids": len(native_ids) - len(set(native_ids)),
            "source_site_ids": source_site_ids,
            "source_portals": source_portals,
            "source_api_urls": source_api_urls,
            "source_api_site_ids": source_api_site_ids,
            "all_rows_exact_site_id": source_site_ids == [EXPECTED_SITE_ID],
            "all_source_api_urls_exact_site_id": all_api_urls_exact_site_id,
            "source_provenance_note": (
                "source_portal_url is empty because the exact official application "
                "page and its exact OLL shell were supplied together; the adapter "
                "therefore records marketing_page_site_id provenance. Every current "
                "row is independently bounded by source_property_id and source_api_url."
            ),
            "samples": native_priced[:10],
        },
        "rp_oracle_context_only": {
            "rows": len(rp),
            "distinct_nonblank_unit_ids": len(
                {
                    row["unitid"]
                    for row in rp
                    if row.get("unitid") and row.get("unitid") != "~"
                }
            ),
            "samples": rp[:5],
            "note": "RP is context only; strict admission uses current provider output above.",
        },
        "strict_checks": strict_checks,
        "strict_candidate": all(strict_checks.values()),
    }
    output = ROOT / "22964_tropicana_onesite_direct.json"
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps(artifact, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
