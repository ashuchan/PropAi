from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._entrata_hb_recovery import strict_conventional_url
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.scraper import scrape_jugnu

ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "entrata_residual_lane/evidence_30101_current_full_scraper_hb.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = "30101"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _positive_rent(unit: dict[str, object]) -> bool:
    return any(
        isinstance(unit.get(key), (int, float))
        and not isinstance(unit.get(key), bool)
        and float(unit[key]) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "rent",
        )
    )


def _same_origin(left: str, right: str) -> bool:
    try:
        a = urlsplit(left)
        b = urlsplit(right)
    except (TypeError, ValueError):
        return False
    return (
        a.scheme.casefold() == b.scheme.casefold()
        and (a.hostname or "").casefold() == (b.hostname or "").casefold()
        and a.port == b.port
    )


def _identity_visible(row: dict[str, str], body: str) -> dict[str, bool]:
    text = _normalized(BeautifulSoup(body, "lxml").get_text(" ", strip=True))
    tokens = set(text.split())
    name = _normalized(row.get("name") or "")
    address_tokens = _normalized(row.get("address") or "").split()
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token
        for token in address_tokens[1:]
        if token not in {"n", "s", "e", "w", "st", "street", "rd", "road"}
    ]
    zip_code = str(row.get("zip") or "").strip()
    return {
        "name_visible": bool(name and name in text),
        "street_visible": bool(
            street_number
            and street_number in tokens
            and street_words
            and all(token in tokens for token in street_words)
        ),
        "zip_visible": bool(zip_code and zip_code in tokens),
    }


def _read_property() -> dict[str, str]:
    with PROPERTIES.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if row.get("apartmentid") == PROPERTY_ID:
                return row
    raise RuntimeError(f"missing configured property {PROPERTY_ID}")


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "hyperbrowser",
        "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
    }
    for name, expected in expected_env.items():
        if os.environ.get(name, "").casefold() != expected:
            raise RuntimeError(f"guardrail {name} must equal {expected!r}")

    row = _read_property()
    configured_url = str(row.get("website") or "").strip()
    response = await asyncio.to_thread(
        probe_get,
        configured_url,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    body = str(response.text or "")
    final_url = str(response.url or configured_url)
    fetch_result = FetchResult(
        url=configured_url,
        outcome=FetchOutcome.OK,
        status=int(response.status_code or 0),
        body=body.encode(),
        headers=dict(response.headers or {}),
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )
    task = CrawlTask(
        url=configured_url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    result = await scrape_jugnu(
        task,
        fetch_result,
        page=None,
        profile=None,
        csv_row=row,
    )
    output_units = [item for item in (result.get("units") or []) if isinstance(item, dict)]
    strict_units = [
        unit
        for unit in output_units
        if unit_has_real_anchor(unit)
        and _positive_rent(unit)
        and str(unit.get("unit_number") or "").strip()
        and str((unit.get("source_ids") or {}).get("entrata_uid") or "").strip()
    ]
    matched_url = strict_conventional_url(
        body,
        final_url,
        str(row.get("name") or ""),
    )
    identity = _identity_visible(row, body)
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in strict_units
            if str(unit.get("source_api_url") or "").strip()
        }
    )
    native_ids = [
        str((unit.get("source_ids") or {}).get("entrata_uid") or "").strip()
        for unit in strict_units
    ]
    strict_gates = {
        "configured_route_http_200": int(response.status_code or 0) == 200,
        "configured_route_identity_visible": all(identity.values()),
        "one_exact_published_property_conventional_url": bool(matched_url),
        "current_full_scraper_selected_entrata": result.get("_adapter_used") == "entrata",
        "current_full_scraper_selected_unit_tier": str(
            result.get("extraction_tier_used") or ""
        )
        in {
            "TIER_1_DOM_ENTRATA_PP_HYPERBROWSER_UNIT_LEVEL",
            "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL",
        },
        "all_output_rows_native_and_positive_rent": bool(
            strict_units and len(strict_units) == len(output_units)
        ),
        "all_native_ids_unique": bool(
            native_ids and len(native_ids) == len(set(native_ids))
        ),
        "all_sources_same_origin_as_exact_property_grid": bool(
            source_urls
            and matched_url
            and all(_same_origin(url, matched_url) for url in source_urls)
        ),
        "all_sources_match_property_slug": bool(
            source_urls
            and all("apartments-at-bel-air" in url.casefold() for url in source_urls)
        ),
    }
    passed = all(strict_gates.values())
    result_row = {
        "property_id": int(PROPERTY_ID),
        "property_name": row.get("name") or "",
        "website": configured_url,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "property_identity_match": passed,
        "contamination_verdict": (
            "pass_exact_configured_property_published_entrata_grid_same_origin_native_ids"
            if passed
            else "reject_current_full_scraper_entrata_property_binding_incomplete"
        ),
        "units": len(strict_units),
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "configured_final_url": final_url,
        "matched_conventional_url": matched_url,
        "configured_identity": identity,
        "strict_gates": strict_gates,
        "identity_evidence": {
            "rows_with_native_identity": len(strict_units),
            "rows_with_native_identity_and_positive_rent": len(strict_units),
            "source_urls": source_urls,
            "distinct_entrata_uids": len(set(native_ids)),
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": str(unit.get("unit_number") or ""),
                    "entrata_uid": str(
                        (unit.get("source_ids") or {}).get("entrata_uid") or ""
                    ),
                },
                "floor_plan_name": unit.get("floor_plan_name") or "",
                "availability_date": unit.get("availability_date")
                or unit.get("available_date")
                or "",
                "positive_rent_evidence": {
                    key: unit.get(key)
                    for key in ("market_rent_low", "market_rent_high")
                    if unit.get(key) not in (None, "", 0, 0.0)
                },
                "source_api_url": unit.get("source_api_url") or "",
            }
            for unit in strict_units[:8]
        ],
        "errors": result.get("errors") or [],
    }
    payload = {
        "lane": "entrata_30101_current_full_scraper_hb",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser_sessions_max": 1,
            "paid_canary": False,
        },
        "results": [result_row],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": _sha256(OUTPUT),
                "outcome": result_row["outcome"],
                "adapter": result_row["adapter"],
                "tier": result_row["tier"],
                "units": result_row["units"],
                "strict_gates": strict_gates,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not passed:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
