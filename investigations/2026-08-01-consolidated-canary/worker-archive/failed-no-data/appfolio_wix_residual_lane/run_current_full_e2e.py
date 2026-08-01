from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

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


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "appfolio_wix_residual_lane"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = LANE / "current_configured_route_full_e2e.json"

EXPECTED_REMAINING_SHA256 = (
    "c1971ef86edae3f10d72ed161a3f25b398de56cc63cf2aa79a099c85602643ac"
)
EXPECTED_LEDGER_SHA256 = (
    "04718e2e24b3710bb6d9b9714ffe8e1a183a510b0f87d72496086fd96b9d2a8a"
)
TARGET_ADAPTERS = {"appfolio", "wix_nopms"}

PROVIDER_MARKERS = (
    "appfolio.com",
    "entrata.com",
    "securecafe.com",
    "securecafeapplicant.com",
    "rentcafe.com",
    "realpage.com",
    "myresman.com",
    "doorloop.com",
    "rently.com",
    "leaseleads.co",
    "sightmap.com",
    "knockrentals.com",
    "knockcrm.com",
    "filesusr.com/html/",
    "spherexx.com",
)
INVENTORY_PATH_RE = re.compile(
    r"(?:^|/)(?:availability|available-units?|availableunits|floor-?plans?|"
    r"properties-for-rent|available-rentals|apartments|rentals|listings|"
    r"conventional)(?:/|$|[.?])",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
HREF_RE = re.compile(
    r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize_url(value: str) -> str:
    value = value.strip()
    return value if "://" in value else f"https://{value}"


def normalize_host(value: str) -> str:
    host = (urlparse(value).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return value
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, "")
    )


def positive_rent(unit: dict) -> bool:
    return any(
        isinstance(unit.get(field), (int, float))
        and not isinstance(unit.get(field), bool)
        and unit.get(field) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def source_snapshot() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    tracked = {}
    for path in (
        Path("ma_poc/pms/scraper.py"),
        Path("ma_poc/pms/detector.py"),
        Path("ma_poc/pms/adapters/appfolio.py"),
        Path("ma_poc/pms/adapters/wix_nopms.py"),
        Path("ma_poc/pms/adapters/_universal_recovery.py"),
        Path("ma_poc/pms/adapters/_wix_iframe_walker.py"),
        Path("ma_poc/pms/adapters/entrata.py"),
    ):
        tracked[str(path)] = sha256(path)
    return {
        "git_head": head,
        "dirty": bool(status),
        "git_status_short": status,
        "critical_file_sha256": tracked,
    }


def body_diagnostics(body: str, final_url: str) -> dict:
    published: list[str] = []
    external_provider: list[str] = []
    all_candidates: list[str] = []
    base_host = normalize_host(final_url)

    for raw in [*HREF_RE.findall(body), *URL_RE.findall(body)]:
        raw = raw.replace("&amp;", "&").replace("\\/", "/").strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        candidate = canonical_url(urljoin(final_url, raw))
        try:
            parsed = urlparse(candidate)
        except Exception:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        lower = candidate.casefold()
        if any(marker in lower for marker in PROVIDER_MARKERS):
            if candidate not in external_provider:
                external_provider.append(candidate)
        if normalize_host(candidate) == base_host and INVENTORY_PATH_RE.search(
            parsed.path or ""
        ):
            if candidate not in published:
                published.append(candidate)
        if candidate not in all_candidates:
            all_candidates.append(candidate)

    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL
    )
    title = (
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_match.group(1))).strip()
        if title_match
        else ""
    )
    return {
        "title": title[:300],
        "published_inventory_urls": published[:30],
        "provider_urls_in_body": external_provider[:50],
        "provider_markers": [
            marker for marker in PROVIDER_MARKERS if marker in body.casefold()
        ],
        "filesusr_html_urls": [
            value
            for value in external_provider
            if "filesusr.com/html/" in value.casefold()
        ][:10],
        "candidate_url_count": len(all_candidates),
    }


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
        text = response.text or ""
        body = text.encode()
        if 200 <= status < 300 and body:
            outcome = FetchOutcome.OK
        elif status in {404, 410, 451}:
            outcome = FetchOutcome.DEAD_URL
        else:
            outcome = FetchOutcome.HARD_FAIL
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


def make_task(url: str, property_id: str) -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


def sample_row(row: dict) -> dict:
    return {
        "unit_number": str(row.get("unit_number") or ""),
        "native_unit_id": str(row.get("native_unit_id") or ""),
        "unit_id": str(row.get("unit_id") or ""),
        "unit_name": str(row.get("unit_name") or ""),
        "address": str(row.get("address") or row.get("street_address") or ""),
        "floor_plan_name": str(row.get("floor_plan_name") or ""),
        "sqft": str(row.get("sqft") or ""),
        "market_rent_low": row.get("market_rent_low"),
        "market_rent_high": row.get("market_rent_high"),
        "source_property_id": str(row.get("source_property_id") or ""),
        "source_property_name": str(row.get("source_property_name") or ""),
        "source_api_url": str(row.get("source_api_url") or ""),
        "source_ids": row.get("source_ids") if isinstance(row.get("source_ids"), dict) else {},
    }


async def one(residual: dict[str, str], metadata: dict[str, str]) -> dict:
    pid = residual["property_id"]
    configured_url = normalize_url(metadata.get("website") or residual["website"])
    task = make_task(configured_url, pid)
    fetched = await direct_fetch(task)
    html = (
        fetched.body.decode("utf-8", "replace")
        if isinstance(fetched.body, bytes)
        else str(fetched.body or "")
    )
    diagnostics = body_diagnostics(html, fetched.final_url or configured_url)
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
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "property_id": int(pid),
            "property_name": metadata.get("name") or residual.get("property_name") or "",
            "configured_url": configured_url,
            "remaining_adapter": residual.get("current_detected_adapter") or "",
            "configured_fetch": {
                "status": fetched.status,
                "outcome": fetched.outcome.value,
                "final_url": fetched.final_url,
                "body_bytes": len(fetched.body or b""),
            },
            "body_diagnostics": diagnostics,
            "exception": f"{type(exc).__name__}: {str(exc)[:500]}",
            "strict_native_positive_rent_rows": 0,
            "strict_accept": False,
            "strict_rejection": "full_pipeline_exception",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    units = [item for item in result.get("units") or [] if isinstance(item, dict)]
    plans = [item for item in result.get("plan_summaries") or [] if isinstance(item, dict)]
    native = [item for item in units if unit_has_real_anchor(item)]
    strict = [item for item in native if positive_rent(item)]
    if not units:
        rejection = "plan_only_no_provider_native_unit_identity" if plans else "no_unit_rows"
    elif not native:
        rejection = "emitted_rows_lack_provider_native_unit_identity"
    elif not strict:
        rejection = "native_unit_rows_lack_positive_rent"
    else:
        # Any candidate rows are held for the consolidated property-boundary
        # audit. Presence of strict shape alone is never enough to accept.
        rejection = "candidate_requires_exact_property_boundary_audit"

    return {
        "property_id": int(pid),
        "property_name": metadata.get("name") or residual.get("property_name") or "",
        "canonical_address": metadata.get("address") or "",
        "canonical_city": metadata.get("city") or "",
        "canonical_state": metadata.get("state") or "",
        "canonical_zip": metadata.get("zip") or "",
        "configured_url": configured_url,
        "cohort_url": residual.get("website") or "",
        "remaining_adapter": residual.get("current_detected_adapter") or "",
        "source_adapter_0731": residual.get("source_adapter_0731") or "",
        "rp_oracle_native_unit_rows": int(residual.get("rp_oracle_native_unit_rows") or 0),
        "rp_oracle_distinct_floorplans": int(residual.get("rp_oracle_distinct_floorplans") or 0),
        "prior_disposition": residual.get("prior_disposition") or "",
        "configured_fetch": {
            "status": fetched.status,
            "outcome": fetched.outcome.value,
            "final_url": fetched.final_url,
            "body_bytes": len(fetched.body or b""),
            "attempts": fetched.attempts,
        },
        "body_diagnostics": diagnostics,
        "current_detected_pms": (result.get("_detected_pms") or {}).get("pms") or "",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "emitted_unit_rows": len(units),
        "plan_rows": len(plans),
        "native_identity_rows": len(native),
        "strict_native_positive_rent_rows": len(strict),
        "strict_shape_samples": [sample_row(item) for item in strict[:5]],
        "strict_shape_rows": [sample_row(item) for item in strict[:50]],
        "emitted_samples": [sample_row(item) for item in units[:5]],
        "plan_samples": [
            {
                "floor_plan_name": str(item.get("floor_plan_name") or ""),
                "unit_number": str(item.get("unit_number") or ""),
                "market_rent_low": item.get("market_rent_low"),
                "market_rent_high": item.get("market_rent_high"),
                "source_api_url": str(item.get("source_api_url") or ""),
            }
            for item in plans[:8]
        ],
        "source_property_ids": sorted(
            {
                str(item.get("source_property_id") or "")
                for item in strict
                if item.get("source_property_id") not in (None, "")
            }
        ),
        "source_urls": sorted(
            {
                str(item.get("source_api_url") or "")
                for item in strict
                if item.get("source_api_url")
            }
        )[:20],
        "winning_page_url": result.get("_winning_page_url") or "",
        "link_hop_success": bool(result.get("_link_hop_success")),
        "link_hop_from": result.get("_link_hop_from") or "",
        "fallback_chain": result.get("_fallback_chain") or [],
        "tier_attempts": result.get("_tier_attempts") or [],
        "llm_interactions": result.get("_llm_interactions") or [],
        "errors": result.get("errors") or [],
        "exception": "",
        "strict_accept": False,
        "strict_rejection": rejection,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def main() -> None:
    expected_env = {
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
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    if sha256(REMAINING) != EXPECTED_REMAINING_SHA256:
        raise RuntimeError("remaining CSV changed")
    if sha256(LEDGER) != EXPECTED_LEDGER_SHA256:
        raise RuntimeError("strict ledger changed")

    all_remaining = read_csv(REMAINING)
    residuals = [
        row for row in all_remaining if row.get("current_detected_adapter") in TARGET_ADAPTERS
    ]
    counts = {
        adapter: sum(row.get("current_detected_adapter") == adapter for row in residuals)
        for adapter in sorted(TARGET_ADAPTERS)
    }
    if len(residuals) != 19 or counts != {"appfolio": 10, "wix_nopms": 9}:
        raise RuntimeError(f"unexpected target cohort: rows={len(residuals)} counts={counts}")

    metadata_by_id = {
        row["apartmentid"]: row
        for row in read_csv(PROPERTIES)
        if row.get("apartmentid")
    }
    if not all(row["property_id"] in metadata_by_id for row in residuals):
        raise RuntimeError("missing canonical metadata")

    # Every link-hop call is forced through the same ordinary direct GET.
    # This makes paid/cloud/browser backends unreachable from this harness.
    fetch_mod.fetch = direct_fetch
    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()

    semaphore = asyncio.Semaphore(3)

    async def bounded(row: dict[str, str]) -> dict:
        async with semaphore:
            output = await one(row, metadata_by_id[row["property_id"]])
            print(
                json.dumps(
                    {
                        "property_id": output["property_id"],
                        "adapter": output.get("adapter") or "",
                        "tier": output.get("tier") or "",
                        "units": output.get("emitted_unit_rows", 0),
                        "plans": output.get("plan_rows", 0),
                        "strict": output.get("strict_native_positive_rent_rows", 0),
                    }
                ),
                flush=True,
            )
            return output

    results = await asyncio.gather(*(bounded(row) for row in residuals))
    results.sort(key=lambda item: int(item["property_id"]))
    hb_counts = {
        str(row["property_id"]): hyperbrowser_property_call_count(str(row["property_id"]))
        for row in residuals
    }
    strict_candidates = [
        row for row in results if row.get("strict_native_positive_rent_rows", 0) > 0
    ]
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "appfolio_wix_residual_current_configured_route_full_scrape_jugnu_e2e",
        "source_snapshot": source_snapshot(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "proxy": False,
            "paid_canary": False,
            "hyperbrowser_policy": "not needed; zero sessions used",
            "web_unlocker_call_count": web_unlocker_call_count(),
            "hyperbrowser_property_call_counts": hb_counts,
            "environment": expected_env,
        },
        "cohort": {
            "remaining_csv": str(REMAINING),
            "remaining_csv_sha256": sha256(REMAINING),
            "ledger": str(LEDGER),
            "ledger_sha256": sha256(LEDGER),
            "ledger_rows": len(read_csv(LEDGER)),
            "remaining_rows": len(all_remaining),
            "target_rows": len(residuals),
            "target_adapter_counts": counts,
            "target_property_ids": [int(row["property_id"]) for row in residuals],
        },
        "summary": {
            "full_pipeline_rows": len(results),
            "configured_fetch_ok": sum(
                row.get("configured_fetch", {}).get("outcome") == "OK" for row in results
            ),
            "emitted_unit_properties": sum(
                int(row.get("emitted_unit_rows") or 0) > 0 for row in results
            ),
            "plan_only_properties": sum(
                int(row.get("emitted_unit_rows") or 0) == 0
                and int(row.get("plan_rows") or 0) > 0
                for row in results
            ),
            "strict_shape_candidate_properties": len(strict_candidates),
            "strict_shape_candidate_ids": [row["property_id"] for row in strict_candidates],
            "strict_accept_properties": 0,
            "strict_accept_ids": [],
        },
        "results": results,
    }
    if payload["guardrails"]["web_unlocker_call_count"] != 0 or any(hb_counts.values()):
        raise RuntimeError("forbidden backend call observed")
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(OUTPUT), **payload["summary"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
