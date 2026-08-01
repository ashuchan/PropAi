from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.adapters.rentvision import RentVisionAdapter


PROPERTY_ID = "56182"
REPO_ROOT = Path("/Users/ankur/PropAi-codex-failed-no-data")
SOURCE_PATHS = (
    "ma_poc/pms/adapters/rentvision.py",
    "ma_poc/pms/scraper.py",
)
OUTPUT = Path(
    "/private/tmp/propai-fnd-vBkmT9/acorn_56182_current_rentvision/"
    "evidence_acorn_56182_full_scrape_jugnu_3x.json"
)
PROPERTIES = Path("ma_poc/config/properties.csv")

GUARDRAIL_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_DC_PROXY_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
    "ENABLE_RESIDENTIAL_TIER": "false",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER5_VISION": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "PROBE_PROXY_URL": "",
    "WEB_UNLOCKER_KEY": "",
}


def _positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "unit_id",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_date",
            "available_date",
            "availability_status",
            "source_api_url",
            "source_portal_url",
            "source_property_id",
            "source_property_name",
            "source_property_address",
            "source_property_provenance",
            "source_ids",
            "extraction_tier",
        )
    }


def _metadata() -> dict[str, str]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("apartmentid") == PROPERTY_ID:
                return row
    raise RuntimeError(f"missing configured property {PROPERTY_ID}")


async def _direct_fetch(task: CrawlTask, profile: object | None = None) -> FetchResult:
    del profile
    started = time.monotonic()
    try:
        response = await asyncio.to_thread(
            probe_get,
            task.url,
            timeout=min(30, max(5, int(task.budget_ms / 1000))),
            unlocker=False,
            retries=1,
            proxies={},
            verify=True,
        )
        status = int(response.status_code or 0)
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.HARD_FAIL
        )
        return FetchResult(
            url=task.url,
            outcome=outcome,
            status=status,
            body=body,
            headers=dict(getattr(response, "headers", {}) or {}),
            render_mode=task.render_mode,
            final_url=str(response.url or task.url),
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult(
            url=task.url,
            outcome=FetchOutcome.TRANSIENT,
            status=None,
            body=None,
            headers={},
            render_mode=task.render_mode,
            final_url=task.url,
            attempts=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_signature=f"{type(exc).__name__}: {str(exc)[:300]}",
        )


async def main() -> None:
    for key, value in GUARDRAIL_ENV.items():
        os.environ[key] = value

    metadata = _metadata()
    configured_url = metadata["website"]
    task = CrawlTask(
        url=configured_url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=120_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )
    fetch_mod.fetch = _direct_fetch

    active_trace: dict[str, Any] = {}
    original_floorplans = RentVisionAdapter._fetch_floorplans_html
    original_details = RentVisionAdapter._fetch_detail_pages
    original_extract = RentVisionAdapter.extract

    async def traced_floorplans(page: Any, floorplans_url: str) -> str:
        started = time.monotonic()
        html = await original_floorplans(page, floorplans_url)
        active_trace["floorplans_fetch"] = {
            "url": floorplans_url,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "bytes": len(html.encode()),
            "sha256": hashlib.sha256(html.encode()).hexdigest() if html else "",
        }
        return html

    async def traced_details(
        cls: type[RentVisionAdapter],
        detail_urls: list[str],
        *,
        max_concurrency: int = 8,
    ) -> list[tuple[str, str]]:
        del cls
        started = time.monotonic()
        pages = await original_details(
            detail_urls,
            max_concurrency=max_concurrency,
        )
        active_trace["detail_fetch"] = {
            "requested": len(detail_urls),
            "returned_nonempty": sum(bool(html) for _, html in pages),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "max_concurrency": max_concurrency,
            "urls": detail_urls,
            "body_bytes": [len(html.encode()) for _, html in pages],
        }
        return pages

    async def traced_extract(
        self: RentVisionAdapter,
        page: Any,
        ctx: Any,
    ) -> Any:
        started = time.monotonic()
        result = await original_extract(self, page, ctx)
        active_trace["adapter_extract"] = {
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "tier": result.tier_used,
            "units": len(result.units),
            "plans": len(result.plan_summaries),
            "winning_url": result.winning_url,
            "errors": result.errors,
        }
        return result

    RentVisionAdapter._fetch_floorplans_html = staticmethod(traced_floorplans)
    RentVisionAdapter._fetch_detail_pages = classmethod(traced_details)
    RentVisionAdapter.extract = traced_extract

    repeats: list[dict[str, Any]] = []
    try:
        for repeat_number in range(1, 4):
            active_trace.clear()
            fetched = await _direct_fetch(task)
            if fetched.outcome != FetchOutcome.OK or not fetched.body:
                raise RuntimeError(
                    f"configured fetch failed repeat={repeat_number} "
                    f"outcome={fetched.outcome} status={fetched.status}"
                )
            configured_text = fetched.body.decode("utf-8", errors="replace")
            normalized_text = re.sub(
                r"[^a-z0-9]+", "", configured_text.casefold()
            )
            identity_checks = {
                "exact_property_name_visible": "acornacres" in normalized_text,
                "street_number_and_name_visible": (
                    "3605brandywine" in normalized_text
                ),
                "city_visible": "lafayette" in normalized_text,
                "zip_visible": "47905" in normalized_text,
                "configured_host_preserved": (
                    str(fetched.final_url or "").startswith(
                        "https://www.liveatacornacres.com/"
                    )
                ),
            }
            if not all(identity_checks.values()):
                raise RuntimeError(
                    f"configured identity failed repeat={repeat_number}: "
                    f"{identity_checks!r}"
                )

            started = time.monotonic()
            result = await asyncio.wait_for(
                scraper_mod.scrape_jugnu(
                    task,
                    fetched,
                    page=None,
                    profile=None,
                    csv_row=metadata,
                ),
                timeout=180,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            units = [
                row for row in (result.get("units") or []) if isinstance(row, dict)
            ]
            strict = [
                row for row in units
                if unit_has_real_anchor(row) and _positive_rent(row)
            ]
            signature_payload = [
                {
                    "unit_number": row.get("unit_number"),
                    "floor_plan_name": row.get("floor_plan_name"),
                    "rent_low": row.get("market_rent_low"),
                    "rent_high": row.get("market_rent_high"),
                    "available_date": (
                        row.get("availability_date") or row.get("available_date")
                    ),
                }
                for row in strict
            ]
            repeat = {
                "repeat": repeat_number,
                "configured_fetch": {
                    "status": fetched.status,
                    "final_url": fetched.final_url,
                    "elapsed_ms": fetched.elapsed_ms,
                    "body_bytes": len(fetched.body),
                    "body_sha256": hashlib.sha256(fetched.body).hexdigest(),
                },
                "configured_identity_checks": identity_checks,
                "scrape_jugnu_elapsed_ms": elapsed_ms,
                "trace": dict(active_trace),
                "detected_pms": (result.get("_detected_pms") or {}).get("pms"),
                "adapter": result.get("_adapter_used"),
                "fallback_chain": result.get("_fallback_chain") or [],
                "tier": result.get("extraction_tier_used"),
                "winning_page_url": result.get("winning_page_url"),
                "units": len(units),
                "strict_native_positive_rent_rows": len(strict),
                "plans": len(result.get("plan_summaries") or []),
                "errors": result.get("errors") or [],
                "unit_signature_sha256": hashlib.sha256(
                    json.dumps(
                        signature_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "samples": [_compact(row) for row in strict],
            }
            repeats.append(repeat)
            print(json.dumps({
                "repeat": repeat_number,
                "elapsed_ms": elapsed_ms,
                "adapter": repeat["adapter"],
                "tier": repeat["tier"],
                "strict_rows": len(strict),
                "signature": repeat["unit_signature_sha256"],
                "trace": active_trace,
            }, sort_keys=True))
    finally:
        RentVisionAdapter._fetch_floorplans_html = staticmethod(original_floorplans)
        RentVisionAdapter._fetch_detail_pages = original_details  # type: ignore[method-assign]
        RentVisionAdapter.extract = original_extract

    payload = {
        "lane": "acorn_current_rentvision_full_configured_e2e",
        "cohort": "exact_2026-07-31_FAILED_NO_DATA_344",
        "property_id": int(PROPERTY_ID),
        "property_name": metadata.get("name") or "",
        "configured_url": configured_url,
        "configured_identity": {
            key: metadata.get(key) or ""
            for key in ("address", "city", "state", "zip")
        },
        "route": (
            "fresh configured GET -> scrape_jugnu(page=None) -> "
            "RentVisionAdapter -> /floorplans -> bounded detail-page drill"
        ),
        "source_snapshot": {
            relative: hashlib.sha256(
                (REPO_ROOT / relative).read_bytes()
            ).hexdigest()
            for relative in SOURCE_PATHS
        },
        "materializer": {
            "path": str(Path(__file__).resolve()),
            "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "guardrails": {
            "direct_only": True,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "llm": False,
            "paid_canary": False,
            "environment": GUARDRAIL_ENV,
        },
        "repeats": repeats,
        "stable": (
            len(repeats) == 3
            and all(row["strict_native_positive_rent_rows"] > 0 for row in repeats)
            and all(
                all(row["configured_identity_checks"].values())
                for row in repeats
            )
            and len({row["unit_signature_sha256"] for row in repeats}) == 1
        ),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
