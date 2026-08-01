from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.yotta import yotta_property_identity_matches
from ma_poc.pms.scraper import scrape_jugnu

ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "unknown_residual_lane/evidence_pepper_tree_yotta_current_e2e.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = "34785"
DBA_ID = "55"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_property() -> dict[str, str]:
    with PROPERTIES.open(newline="", encoding="utf-8-sig") as handle:
        return next(
            row for row in csv.DictReader(handle) if row["apartmentid"] == PROPERTY_ID
        )


def _positive_rent(unit: dict[str, object]) -> bool:
    return any(
        isinstance(unit.get(key), (int, float))
        and not isinstance(unit.get(key), bool)
        and float(unit[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


async def main() -> None:
    for name, expected in {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "brightdata",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
    }.items():
        if os.environ.get(name, "").casefold() != expected:
            raise RuntimeError(f"guardrail {name} must equal {expected!r}")

    row = _read_property()
    configured_url = str(row["website"])
    selected_url = re.sub(
        r"^http://", "https://", configured_url, flags=re.IGNORECASE
    )
    response = await asyncio.to_thread(
        probe_get,
        selected_url,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    body = str(response.text or "")
    final_url = str(response.url or selected_url)
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
        budget_ms=60_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    result = await asyncio.wait_for(
        scrape_jugnu(
            task,
            fetch_result,
            page=None,
            profile=None,
            csv_row=row,
        ),
        timeout=90,
    )

    details_url = (
        "https://residentapis.yottareal.com/api/DBA/GetDBADetails/55"
    )
    details_response = await asyncio.to_thread(
        probe_get,
        details_url,
        timeout=25,
        unlocker=False,
        retries=1,
        headers={"Accept": "application/json"},
    )
    details = details_response.json() if details_response.status_code == 200 else {}
    units = [item for item in (result.get("units") or []) if isinstance(item, dict)]
    native_ids = [
        str((unit.get("source_ids") or {}).get("yotta_unit_id") or "").strip()
        for unit in units
    ]
    source_property_ids = sorted(
        {
            str(unit.get("source_property_id") or "")
            for unit in units
            if str(unit.get("source_property_id") or "").strip()
        }
    )
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in units
            if str(unit.get("source_api_url") or "").strip()
        }
    )
    strict_gates = {
        "configured_scheme_only_normalization": selected_url
        == configured_url.replace("http://", "https://", 1),
        "selected_route_http_200": int(response.status_code or 0) == 200,
        "provider_details_http_200": int(details_response.status_code or 0) == 200,
        "provider_identity_matches_exact_configured_property": (
            isinstance(details, dict)
            and yotta_property_identity_matches(details, _adapter_context(row), DBA_ID)
        ),
        "current_full_scraper_selected_yotta": result.get("_adapter_used") == "yotta",
        "current_full_scraper_selected_yotta_unit_tier": result.get(
            "extraction_tier_used"
        )
        == "TIER_1_API_YOTTA",
        "all_rows_native_and_positive_rent": bool(
            units
            and all(
                unit_has_real_anchor(unit)
                and _positive_rent(unit)
                and str(unit.get("unit_number") or "").strip()
                and str((unit.get("source_ids") or {}).get("yotta_unit_id") or "").strip()
                for unit in units
            )
        ),
        "native_ids_unique": bool(native_ids and len(native_ids) == len(set(native_ids))),
        "sole_source_property_id_matches_dba": source_property_ids == [DBA_ID],
        "sole_source_url_matches_exact_dba": source_urls
        == ["https://residentapis.yottareal.com/api/DBA/GetFloorPlans/55/1"],
    }
    passed = all(strict_gates.values())
    result_row = {
        "property_id": int(PROPERTY_ID),
        "property_name": row["name"],
        "website": configured_url,
        "outcome": "UNIT_QUALIFIED" if passed else "UNIT_UNVERIFIED",
        "property_identity_match": passed,
        "contamination_verdict": (
            "pass_exact_configured_property_yotta_details_dba_and_native_unit_ids"
            if passed
            else "reject_yotta_property_boundary_or_native_unit_gate_incomplete"
        ),
        "units": len(units),
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "configured_final_url": final_url,
        "provider_dba_id": DBA_ID,
        "provider_details": {
            "name": details.get("dbaName") if isinstance(details, dict) else "",
            "address": (
                f"{details.get('address1') or ''} {details.get('address2') or ''}".strip()
                if isinstance(details, dict)
                else ""
            ),
            "city": details.get("city") if isinstance(details, dict) else "",
            "state": details.get("stateCode") if isinstance(details, dict) else "",
            "zip": details.get("zip") if isinstance(details, dict) else "",
        },
        "strict_gates": strict_gates,
        "identity_evidence": {
            "rows_with_native_identity": len(units),
            "rows_with_native_identity_and_positive_rent": sum(
                _positive_rent(unit) for unit in units
            ),
            "source_urls": source_urls,
            "source_property_ids": source_property_ids,
            "distinct_yotta_unit_ids": len(set(native_ids)),
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": str(unit.get("unit_number") or ""),
                    "yotta_unit_id": str(
                        (unit.get("source_ids") or {}).get("yotta_unit_id") or ""
                    ),
                },
                "positive_rent_evidence": {
                    "market_rent_low": unit.get("market_rent_low"),
                    "market_rent_high": unit.get("market_rent_high"),
                },
                "availability_date": unit.get("availability_date") or "",
                "source_property_id": unit.get("source_property_id") or "",
                "source_api_url": unit.get("source_api_url") or "",
            }
            for unit in units[:8]
        ],
        "errors": result.get("errors") or [],
    }
    payload = {
        "lane": "pepper_tree_yotta_current_source_full_pipeline",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "hyperbrowser": False,
            "fingerprint_rotation": False,
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
                "units": result_row["units"],
                "strict_gates": strict_gates,
            },
            indent=2,
            sort_keys=True,
        )
    )
    if not passed:
        raise SystemExit(2)


def _adapter_context(row: dict[str, str]):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.detector import DetectedPMS

    return AdapterContext(
        base_url=row["website"],
        detected=DetectedPMS(
            pms="yotta",
            confidence=0.95,
            evidence=["strict validator"],
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id=PROPERTY_ID,
        fetch_result=SimpleNamespace(body=b"", final_url=row["website"]),
        property_name=row["name"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip"],
    )


if __name__ == "__main__":
    asyncio.run(main())
