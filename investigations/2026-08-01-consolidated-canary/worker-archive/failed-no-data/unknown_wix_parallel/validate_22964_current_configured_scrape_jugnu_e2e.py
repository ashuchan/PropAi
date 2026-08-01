from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import ma_poc.fetch as fetch_mod
from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import (
    probe_get,
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/unknown_wix_parallel")
REMAINING = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_remaining_current.csv")
LEDGER = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_ledger_current.csv")
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = os.environ.get("PROBE_PROPERTY_ID", "22964")
CONFIGURED_URL = os.environ.get(
    "PROBE_CONFIGURED_URL", "https://www.tropicanavillageapartments.com/"
)
APPLICATION_URL = os.environ.get(
    "PROBE_APPLICATION_URL",
    "https://www.tropicanavillageapartments.com/apply-now/application-process",
)
EXPECTED_OLL_HOST = os.environ.get(
    "PROBE_EXPECTED_OLL_HOST", "8452181.onlineleasing.realpage.com"
)
EXPECTED_SITE_ID = os.environ.get("PROBE_EXPECTED_SITE_ID", "3858548")
EXPECTED_APPLICATION_PATH = urlparse(APPLICATION_URL).path
OUTPUT = ROOT / f"{PROPERTY_ID}_current_configured_scrape_jugnu_e2e.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(unit: dict) -> bool:
    for key in (
        "market_rent_low",
        "market_rent_high",
        "rent_low",
        "rent_high",
        "asking_rent",
        "rent",
    ):
        try:
            if float(unit.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(re.search(r"\$\s*[1-9]", str(unit.get("rent_range") or "")))


async def direct_fetch(task: CrawlTask, profile=None) -> FetchResult:
    del profile
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
        body = (response.text or "").encode()
        outcome = (
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.DEAD_URL
            if status in {404, 410, 451}
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


def make_task(url: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=PROPERTY_ID,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def sample(unit: dict) -> dict:
    return {
        "unit_number": str(unit.get("unit_number") or ""),
        "floor_plan_name": str(unit.get("floor_plan_name") or ""),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "source_property_id": str(unit.get("source_property_id") or ""),
        "source_api_url": str(unit.get("source_api_url") or ""),
        "source_property_provenance": str(
            unit.get("source_property_provenance") or ""
        ),
        "source_portal_url": str(unit.get("source_portal_url") or ""),
    }


async def run_route(url: str, metadata: dict[str, str]) -> dict:
    task = make_task(url)
    fetched = await direct_fetch(task)
    html = (fetched.body or b"").decode("utf-8", "replace")
    ranked = scraper_mod._rank_internal_links(  # noqa: SLF001
        html, fetched.final_url or url, limit=20
    )
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            scraper_mod.scrape_jugnu(
                task,
                fetched,
                page=None,
                profile=None,
                csv_row=metadata,
            ),
            timeout=150,
        )
        exception = ""
    except Exception as exc:  # noqa: BLE001
        result = {}
        exception = f"{type(exc).__name__}: {str(exc)[:500]}"

    units = [row for row in result.get("units") or [] if isinstance(row, dict)]
    strict = [
        row for row in units if unit_has_real_anchor(row) and positive_rent(row)
    ]
    native_ids = [str(row.get("unit_number") or "").strip() for row in strict]
    source_property_ids = sorted(
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
            for api_url in source_api_urls
            if (
                match := re.search(
                    r"/workflowstartup/v1/(\d+)/English(?:[/?#]|$)", api_url
                )
            )
        }
    )
    strict_checks = {
        "has_native_positive_rent_rows": bool(strict),
        "all_emitted_rows_are_native_positive_rent": bool(
            strict and len(strict) == len(units)
        ),
        "native_unit_numbers_nonblank_and_unique": bool(
            strict
            and all(native_ids)
            and len(native_ids) == len(set(native_ids))
        ),
        "all_rows_exact_source_property_id": source_property_ids
        == [EXPECTED_SITE_ID],
        "all_source_api_urls_exact_site_id": bool(
            source_api_urls
            and source_api_site_ids == [EXPECTED_SITE_ID]
            and all(
                f"/workflowstartup/v1/{EXPECTED_SITE_ID}/English" in api_url
                for api_url in source_api_urls
            )
        ),
    }
    return {
        "input_url": url,
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "body_sha256": hashlib.sha256(fetched.body or b"").hexdigest(),
            "contains_application_path": EXPECTED_APPLICATION_PATH in html,
            "contains_exact_oll_host": EXPECTED_OLL_HOST in html,
        },
        "ranked_links": [
            {"url": item[0], "score": item[1], "anchor": item[2]}
            for item in ranked
        ],
        "current_detected_pms": (result.get("_detected_pms") or {}).get("pms")
        or "",
        "detection_evidence": (result.get("_detected_pms") or {}).get("evidence")
        or [],
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "units": len(units),
        "strict_native_positive_rent_rows": len(strict),
        "plan_summaries": len(result.get("plan_summaries") or []),
        "native_ids": native_ids,
        "source_property_ids": source_property_ids,
        "source_api_urls": source_api_urls,
        "source_api_site_ids": source_api_site_ids,
        "strict_checks": strict_checks,
        "strict_accept": all(strict_checks.values()),
        "samples": [sample(row) for row in strict[:10]],
        "winning_page_url": result.get("_winning_page_url") or "",
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": result.get("_link_hop_from") or "",
        "official_application_onesite_success": bool(
            result.get("_official_application_onesite_success")
        ),
        "official_application_onesite": result.get(
            "_official_application_onesite"
        ) or {},
        "official_application_onesite_attempts": result.get(
            "_official_application_onesite_attempts"
        ) or [],
        "fallback_chain": result.get("_fallback_chain") or [],
        "tier_attempts": result.get("_tier_attempts") or [],
        "llm_interactions": result.get("_llm_interactions") or [],
        "errors": result.get("errors") or [],
        "exception": exception,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")
    if os.environ.get("WEB_UNLOCKER_KEY", "").strip():
        raise RuntimeError("WEB_UNLOCKER_KEY must be blank")

    remaining_rows = read_csv(REMAINING)
    ledger_rows = read_csv(LEDGER)
    residual = next(row for row in remaining_rows if row["property_id"] == PROPERTY_ID)
    if residual.get("current_detected_adapter") not in {"unknown", "wix_nopms"}:
        raise RuntimeError(f"out-of-lane adapter: {residual}")
    if any(row["property_id"] == PROPERTY_ID for row in ledger_rows):
        raise RuntimeError("property already exists in strict ledger")
    metadata = next(
        row for row in read_csv(PROPERTIES) if row["apartmentid"] == PROPERTY_ID
    )

    fetch_mod.fetch = direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()

    configured_route = await run_route(CONFIGURED_URL, metadata)
    published_application_route = await run_route(APPLICATION_URL, metadata)
    hb_calls = hyperbrowser_property_call_count(PROPERTY_ID)

    status = subprocess.run(
        ["git", "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "unknown_wix_residual_current_configured_scrape_jugnu_e2e",
        "property_id": int(PROPERTY_ID),
        "property_name": metadata["name"],
        "canonical_identity": metadata,
        "residual_snapshot": residual,
        "source_snapshot": {
            "git_head": head,
            "dirty": bool(status),
            "git_status_short": status,
            "critical_file_sha256": {
                path: sha256(Path(path))
                for path in (
                    "ma_poc/pms/scraper.py",
                    "ma_poc/pms/detector.py",
                    "ma_poc/pms/adapters/onesite.py",
                )
            },
        },
        "cohort_guard": {
            "remaining_csv": str(REMAINING),
            "remaining_csv_sha256": sha256(REMAINING),
            "ledger": str(LEDGER),
            "ledger_sha256": sha256(LEDGER),
            "not_already_in_strict_ledger": True,
            "assigned_adapter": residual.get("current_detected_adapter"),
        },
        "guardrails": {
            "environment": expected_env,
            "compliance_mode": True,
            "llm": False,
            "web_unlocker": False,
            "web_unlocker_calls": web_unlocker_call_count(),
            "hyperbrowser": False,
            "hyperbrowser_calls": hb_calls,
            "captcha_solving": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "all_link_hops_forced_to_direct_get": True,
        },
        "configured_route": configured_route,
        "published_application_route_control": published_application_route,
        "conclusion": {
            "configured_route_strict_accept": configured_route["strict_accept"],
            "published_application_route_strict_accept": published_application_route[
                "strict_accept"
            ],
            "navigation_gap": bool(
                not configured_route["strict_accept"]
                and published_application_route["strict_accept"]
                and configured_route["configured_fetch"]["contains_application_path"]
            ),
        },
    }
    if web_unlocker_call_count() != 0 or hb_calls != 0:
        raise RuntimeError("forbidden backend call observed")
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "configured": {
                    "strict": configured_route["strict_accept"],
                    "rows": configured_route["strict_native_positive_rent_rows"],
                    "detected": configured_route["current_detected_pms"],
                    "links": configured_route["ranked_links"],
                },
                "application_control": {
                    "strict": published_application_route["strict_accept"],
                    "rows": published_application_route[
                        "strict_native_positive_rent_rows"
                    ],
                    "detected": published_application_route["current_detected_pms"],
                    "links": published_application_route["ranked_links"],
                },
                "conclusion": payload["conclusion"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
