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
PROPERTIES = Path("ma_poc/config/properties.csv")
RP_PATH = Path("/Users/ankur/Downloads/rp_unit_detail_0731.csv")
PROPERTY_ID = os.environ["PROBE_PROPERTY_ID"]
CONFIGURED_URL = os.environ["PROBE_CONFIGURED_URL"]
APPLICATION_URL = os.environ["PROBE_APPLICATION_URL"]
OLL_URL = os.environ["PROBE_OLL_URL"].rstrip("/") + "/"
EXPECTED_SITE_ID = os.environ["PROBE_EXPECTED_SITE_ID"]
OUTPUT = ROOT / f"{PROPERTY_ID}_onesite_direct_strict.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="", errors="replace") as handle:
        return list(csv.DictReader(handle))


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def identity(text: str, url: str, metadata: dict[str, str]) -> dict:
    soup = BeautifulSoup(text, "html.parser")
    visible_raw = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    visible = normalized(visible_raw)
    tokens = set(visible.split())
    name_tokens = [
        token
        for token in normalized(metadata.get("name") or "").split()
        if token not in {"apartments", "apartment", "homes", "home", "the", "of"}
    ]
    address_tokens = normalized(metadata.get("address") or "").split()
    street_number = address_tokens[0] if address_tokens else ""
    ignored_street = {
        "n", "s", "e", "w", "ne", "nw", "se", "sw", "street", "st",
        "avenue", "ave", "road", "rd", "drive", "dr", "parkway", "pkwy",
        "boulevard", "blvd", "lane", "ln", "court", "ct", "highway", "hwy",
    }
    street_words = [
        token for token in address_tokens[1:] if token not in ignored_street
    ]
    city_tokens = normalized(metadata.get("city") or "").split()
    state = normalized(metadata.get("state") or "")
    zip_code = normalized(metadata.get("zip") or "")
    checks = {
        "name_core_tokens_visible": bool(
            name_tokens and all(token in tokens for token in name_tokens)
        ),
        "street_number_and_words_visible": bool(
            street_number
            and street_number in tokens
            and street_words
            and all(token in tokens for token in street_words)
        ),
        "city_visible": bool(city_tokens and all(token in tokens for token in city_tokens)),
        "state_visible": bool(state and state in tokens),
        "zip_visible": bool(zip_code and zip_code in tokens),
    }
    return {
        "url": url,
        "host": urlparse(url).hostname,
        "status_identity_checks": checks,
        "exact_identity": all(checks.values()),
        "sha256": sha_text(text),
        "bytes": len(text.encode("utf-8", "replace")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "text_prefix": visible_raw[:1200],
    }


def positive_rent(row: dict) -> bool:
    for key in ("rent_low", "market_rent_low", "rent_high", "market_rent_high"):
        try:
            if float(row.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(re.search(r"\$\s*[1-9]", str(row.get("rent_range") or "")))


async def main() -> None:
    if os.getenv("COMPLIANCE_MODE", "").casefold() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("COMPLIANCE_MODE must be enabled")
    if os.getenv("WEB_UNLOCKER_KEY", "").strip():
        raise RuntimeError("WEB_UNLOCKER_KEY must be blank")

    metadata = next(
        row for row in read_csv(PROPERTIES) if row["apartmentid"] == PROPERTY_ID
    )
    rp = [
        row for row in read_csv(RP_PATH) if row.get("apartmentid") == PROPERTY_ID
    ]
    reset_web_unlocker_call_count()

    configured_response, application_response, oll_response = await asyncio.gather(
        asyncio.to_thread(
            probe_get, CONFIGURED_URL, timeout=30, unlocker=False, retries=1, proxies={}
        ),
        asyncio.to_thread(
            probe_get, APPLICATION_URL, timeout=30, unlocker=False, retries=1, proxies={}
        ),
        asyncio.to_thread(
            probe_get, OLL_URL, timeout=30, unlocker=False, retries=1, proxies={}
        ),
    )
    configured_html = configured_response.text or ""
    application_html = application_response.text or ""
    oll_html = oll_response.text or ""
    if any(
        int(response.status_code or 0) != 200
        for response in (configured_response, application_response, oll_response)
    ):
        raise RuntimeError("identity-chain page was not HTTP 200")
    if OLL_URL.rstrip("/") not in application_html:
        raise RuntimeError("official application page does not publish exact OLL URL")
    if f"siteId={EXPECTED_SITE_ID}" not in oll_html:
        raise RuntimeError("OLL shell does not publish expected siteId")

    fetched = SimpleNamespace(
        body=application_html + "\n" + oll_html,
        final_url=APPLICATION_URL,
        status_code=200,
    )
    ctx = AdapterContext(
        base_url=APPLICATION_URL,
        detected=DetectedPMS(
            pms="onesite",
            confidence=1.0,
            evidence=["exact official application page publishes exact OLL portal"],
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id=PROPERTY_ID,
        fetch_result=fetched,
        property_name=metadata.get("name") or "",
        address=metadata.get("address") or "",
        city=metadata.get("city") or "",
        state=metadata.get("state") or "",
        zip_code=metadata.get("zip") or "",
    )
    raw_rows = await _probe_onesite_workflowstartup(ctx)
    processed = post_process(raw_rows, property_id=PROPERTY_ID)
    strict = [
        row
        for row in processed.units
        if str(row.get("unit_number") or row.get("unit_id") or "").strip()
        and positive_rent(row)
    ]
    native_ids = [
        str(row.get("unit_number") or row.get("unit_id") or "").strip()
        for row in strict
    ]
    source_site_ids = sorted(
        {
            str(row.get("source_property_id") or "")
            for row in strict
            if row.get("source_property_id") not in (None, "")
        }
    )
    source_api_urls = sorted(
        {
            str(row.get("source_api_url") or "")
            for row in strict
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
    configured_identity = identity(configured_html, CONFIGURED_URL, metadata)
    application_identity = identity(application_html, APPLICATION_URL, metadata)
    oll_identity = identity(oll_html, OLL_URL, metadata)
    strict_checks = {
        "configured_page_exact_identity": configured_identity["exact_identity"],
        "official_application_page_exact_identity": application_identity[
            "exact_identity"
        ],
        "official_application_page_publishes_exact_oll": OLL_URL.rstrip("/")
        in application_html,
        "oll_shell_exact_identity": oll_identity["exact_identity"],
        "oll_shell_publishes_expected_site_id": f"siteId={EXPECTED_SITE_ID}"
        in oll_html,
        "current_native_positive_rent_rows": bool(strict),
        "native_ids_nonblank_and_unique": bool(
            strict and all(native_ids) and len(native_ids) == len(set(native_ids))
        ),
        "all_rows_exact_source_property_id": source_site_ids == [EXPECTED_SITE_ID],
        "all_source_api_urls_exact_site_id": bool(
            source_api_urls
            and source_api_site_ids == [EXPECTED_SITE_ID]
            and all(
                f"/workflowstartup/v1/{EXPECTED_SITE_ID}/English" in url
                for url in source_api_urls
            )
        ),
        "no_web_unlocker_calls": web_unlocker_call_count() == 0,
    }
    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "property_id": int(PROPERTY_ID),
        "property_name": metadata.get("name") or "",
        "canonical_identity": metadata,
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
            "published_oll_url": OLL_URL,
            "oll_shell": oll_identity,
            "published_site_id": EXPECTED_SITE_ID,
        },
        "current_result": {
            "raw_rows": len(raw_rows),
            "post_process_units": len(processed.units),
            "post_process_plan_summaries": len(processed.plan_summaries),
            "post_process_rejected": len(processed.rejected),
            "strict_native_positive_rent_rows": len(strict),
            "distinct_native_ids": len(set(native_ids)),
            "duplicate_native_ids": len(native_ids) - len(set(native_ids)),
            "source_site_ids": source_site_ids,
            "source_api_urls": source_api_urls,
            "source_api_site_ids": source_api_site_ids,
            "samples": strict[:12],
        },
        "rp_oracle_context_only": {
            "rows": len(rp),
            "distinct_nonblank_unit_ids": len(
                {
                    row.get("unitid")
                    for row in rp
                    if row.get("unitid") and row.get("unitid") != "~"
                }
            ),
            "note": "RP is context only; strict admission uses current provider output.",
        },
        "strict_checks": strict_checks,
        "strict_candidate": all(strict_checks.values()),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "strict_candidate": payload["strict_candidate"],
                "strict_checks": strict_checks,
                "current_result": {
                    key: payload["current_result"][key]
                    for key in (
                        "post_process_units",
                        "post_process_plan_summaries",
                        "strict_native_positive_rent_rows",
                        "distinct_native_ids",
                        "duplicate_native_ids",
                        "source_site_ids",
                        "source_api_site_ids",
                    )
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
