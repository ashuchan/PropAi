from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import (
    HyperbrowserProvider,
    hyperbrowser_property_call_count,
)
from ma_poc.pms.scraper import scrape_jugnu


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "entrata_residual_lane"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = Path(
    os.environ.get(
        "OUTPUT_PATH",
        str(LANE / "hb_unknown_rentcafe_high_value3_current_full.json"),
    )
)
TARGETS = tuple(
    value.strip()
    for value in os.environ.get("PROPERTY_IDS", "48389,219752,22964").split(",")
    if value.strip()
)
TIMEOUT_SECONDS = 180


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalize(value: object) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def name_tokens(value: object) -> set[str]:
    ignored = {"apartment", "apartments", "community", "the", "at", "on"}
    return {token for token in normalize(value).split() if token not in ignored}


def street_match(canonical: object, visible: str) -> bool:
    ignored = {
        "n",
        "s",
        "e",
        "w",
        "north",
        "south",
        "east",
        "west",
        "st",
        "street",
        "rd",
        "road",
        "ave",
        "avenue",
        "dr",
        "drive",
        "blvd",
        "boulevard",
        "ln",
        "lane",
    }
    tokens = normalize(canonical).split()
    observed = set(visible.split())
    if not tokens:
        return False
    number = tokens[0]
    core = {token for token in tokens[1:] if token not in ignored and len(token) > 1}
    return bool(number in observed and core and core <= observed)


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
            "asking_rent",
            "rent",
        )
    )


def sample(unit: dict[str, object]) -> dict[str, object]:
    source_ids = unit.get("source_ids")
    return {
        "unit_number": str(unit.get("unit_number") or ""),
        "floor_plan_name": str(unit.get("floor_plan_name") or ""),
        "availability_date": str(
            unit.get("availability_date") or unit.get("available_date") or ""
        ),
        "market_rent_low": unit.get("market_rent_low"),
        "market_rent_high": unit.get("market_rent_high"),
        "source_api_url": str(unit.get("source_api_url") or ""),
        "source_property_id": str(unit.get("source_property_id") or ""),
        "source_property_name": str(unit.get("source_property_name") or ""),
        "source_ids": dict(source_ids) if isinstance(source_ids, dict) else {},
    }


def task_for(row: dict[str, str]) -> CrawlTask:
    website = str(row.get("website") or "").strip()
    if "://" not in website:
        website = f"https://{website}"
    return CrawlTask(
        url=website,
        property_id=row["apartmentid"],
        priority=0,
        budget_ms=150_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )


async def run_one(row: dict[str, str]) -> dict[str, object]:
    task = task_for(row)
    started = time.monotonic()
    try:
        fetched = await asyncio.wait_for(
            HyperbrowserProvider(mode="render").fetch(task, None),
            timeout=TIMEOUT_SECONDS,
        )
        result = await asyncio.wait_for(
            scrape_jugnu(task, fetched, page=None, profile=None, csv_row=row),
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "property_id": int(row["apartmentid"]),
            "property_name": row.get("name") or "",
            "website": task.url,
            "outcome": "ERROR",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "session_calls": hyperbrowser_property_call_count(row["apartmentid"]),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    body = (fetched.body or b"").decode("utf-8", "replace")
    visible = normalize(BeautifulSoup(body, "lxml").get_text(" ", strip=True))
    wanted = name_tokens(row.get("name") or "")
    units = [item for item in (result.get("units") or []) if isinstance(item, dict)]
    plans = [
        item for item in (result.get("plan_summaries") or []) if isinstance(item, dict)
    ]
    strict = [
        unit
        for unit in units
        if unit_has_real_anchor(unit) and positive_rent(unit)
    ]
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "")
            for unit in strict
            if str(unit.get("source_api_url") or "").strip()
        }
    )
    source_hosts = sorted(
        {(urlsplit(url).hostname or "").casefold() for url in source_urls}
    )
    identity = {
        "name_visible": bool(wanted and wanted <= set(visible.split())),
        "street_visible": street_match(row.get("address") or "", visible),
        "zip_visible": bool(
            str(row.get("zip") or "").strip()
            and str(row.get("zip") or "").strip() in set(visible.split())
        ),
    }
    candidate = bool(strict and len(strict) == len(units) and all(identity.values()))
    return {
        "property_id": int(row["apartmentid"]),
        "property_name": row.get("name") or "",
        "website": task.url,
        "configured_final_url": fetched.final_url,
        "fetch_outcome": fetched.outcome.value,
        "fetch_status": fetched.status,
        "rendered_body_bytes": len(fetched.body or b""),
        "captured_network_responses": len(fetched.network_log or []),
        "outcome": "UNIT_CANDIDATE" if candidate else "UNIT_UNVERIFIED" if strict else "PLAN_ONLY" if plans else "EMPTY",
        "adapter": result.get("_adapter_used") or "",
        "tier": result.get("extraction_tier_used") or "",
        "emitted_units": len(units),
        "strict_native_positive_rent_rows": len(strict),
        "plans": len(plans),
        "configured_identity": identity,
        "all_emitted_rows_strict": bool(strict and len(strict) == len(units)),
        "source_urls": source_urls,
        "source_hosts": source_hosts,
        "native_samples": [sample(unit) for unit in strict[:6]],
        "errors": result.get("errors") or [],
        "session_calls": hyperbrowser_property_call_count(row["apartmentid"]),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


async def main() -> None:
    expected_env = {
        "COMPLIANCE_MODE": "1",
        "FETCH_BACKEND": "hyperbrowser",
        "ENABLE_HYPERBROWSER": "true",
        "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "HB_USE_PROXY": "true",
    }
    for name, expected in expected_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {name}={actual!r}; expected {expected!r}")

    metadata = {row["apartmentid"]: row for row in read_csv(PROPERTIES)}
    rows = [metadata[property_id] for property_id in TARGETS]
    results: list[dict[str, object]] = []
    for row in rows:
        result = await run_one(row)
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    payload = {
        "lane": "hb_unknown_rentcafe_high_value3_current_full_pipeline",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser_sessions_max_per_property": 1,
            "paid_canary": False,
            "authoritative_recoveries": False,
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "unit_candidate_ids": [
                    row["property_id"]
                    for row in results
                    if row.get("outcome") == "UNIT_CANDIDATE"
                ],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
