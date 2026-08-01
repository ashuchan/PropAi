from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import ma_poc.fetch as fetch_mod
from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "livebh_portfolio_redirect_guard"
OUTPUT = LANE / "evidence_livebh_city_landing_fail_closed.json"
PROPERTIES = Path("ma_poc/config/properties.csv")
SCRAPER = Path("ma_poc/pms/scraper.py")
TEST = Path("ma_poc/tests/pms/test_livebh_portfolio_redirect_guard.py")
NEGATIVE_IDS = ("47182", "8789")
CONTROL_IDS = ("2190", "8740", "9480")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_rows() -> dict[str, dict[str, str]]:
    wanted = {*NEGATIVE_IDS, *CONTROL_IDS}
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["apartmentid"]: row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") in wanted
        }


def task_for(row: dict[str, str]) -> CrawlTask:
    return CrawlTask(
        url=row["website"],
        property_id=row["apartmentid"],
        priority=0,
        budget_ms=45_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.GET,
    )


async def direct_fetch(task: CrawlTask) -> FetchResult:
    started = time.monotonic()
    response = await asyncio.to_thread(
        probe_get,
        task.url,
        timeout=30,
        unlocker=False,
        retries=1,
    )
    status = int(response.status_code or 0)
    body = (response.text or "").encode()
    return FetchResult(
        url=task.url,
        outcome=(
            FetchOutcome.OK
            if 200 <= status < 300 and body
            else FetchOutcome.HARD_FAIL
        ),
        status=status,
        body=body,
        headers={},
        render_mode=task.render_mode,
        final_url=str(response.url or task.url),
        attempts=1,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def forbidden_pipeline_fetch(*_args, **_kwargs):
    raise AssertionError(
        "identity-rejected city landing attempted an adapter/link-hop fetch"
    )


async def main() -> None:
    required_env = {
        "COMPLIANCE_MODE": "1",
        "ENABLE_TIER4_LLM": "false",
        "ENABLE_TIER_ESCALATION": "false",
        "ENABLE_UNLOCKER_TIER": "false",
        "ENABLE_FLARESOLVERR_TIER": "false",
        "ENABLE_HYPERBROWSER": "false",
        "ENABLE_BODY_RESOLVER": "false",
        "ENABLE_CRAWL_GET_GATE": "false",
    }
    for name, expected in required_env.items():
        actual = os.environ.get(name, "").casefold()
        if actual != expected:
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    rows = read_rows()
    if set(rows) != {*NEGATIVE_IDS, *CONTROL_IDS}:
        raise RuntimeError(f"canonical rows missing: {sorted(rows)}")

    source_sha_before = sha256(SCRAPER)
    test_sha_before = sha256(TEST)
    fetch_mod.fetch = forbidden_pipeline_fetch

    negative_runs: list[dict[str, object]] = []
    for run_index in range(1, 4):
        for property_id in NEGATIVE_IDS:
            row = rows[property_id]
            task = task_for(row)
            fetched = await direct_fetch(task)
            partial_state: dict[str, object] = {}
            result = await scraper_mod.scrape_jugnu(
                task,
                fetched,
                page=None,
                profile=None,
                csv_row=row,
                partial_state=partial_state,
            )
            guard = result.get("_portfolio_redirect_identity_rejected") or {}
            record = {
                "run_index": run_index,
                "property_id": int(property_id),
                "property_name": row["name"],
                "canonical_address": row["address"],
                "configured_url": row["website"],
                "configured_status": fetched.status,
                "configured_final_url": fetched.final_url,
                "configured_body_bytes": len(fetched.body or b""),
                "configured_body_sha256": sha256_bytes(fetched.body or b""),
                "output_units": len(result.get("units") or []),
                "output_plans": len(result.get("plan_summaries") or []),
                "tier": result.get("extraction_tier_used"),
                "guard": guard,
                "partial_state": partial_state,
                "errors": result.get("errors") or [],
                "extract_result_records": len(result["_extract_result"].records),
            }
            record["strict_pass"] = bool(
                fetched.status == 200
                and "/apartments-in/" in fetched.final_url
                and record["output_units"] == 0
                and record["output_plans"] == 0
                and record["extract_result_records"] == 0
                and record["tier"]
                == "generic:portfolio_redirect_identity_rejected"
                and guard.get("adapters_skipped") is True
                and guard.get("link_hop_skipped") is True
                and partial_state == {}
            )
            negative_runs.append(record)

    controls: list[dict[str, object]] = []
    for property_id in CONTROL_IDS:
        row = rows[property_id]
        task = task_for(row)
        fetched = await direct_fetch(task)
        rejected = scraper_mod._livebh_retired_property_redirect_identity_rejected(
            task.url,
            fetched,
            row,
        )
        controls.append(
            {
                "property_id": int(property_id),
                "property_name": row["name"],
                "canonical_address": row["address"],
                "configured_status": fetched.status,
                "configured_final_url": fetched.final_url,
                "configured_body_sha256": sha256_bytes(fetched.body or b""),
                "identity_guard_rejected": rejected,
                "strict_pass": bool(
                    fetched.status == 200
                    and "/apartments/" in fetched.final_url
                    and "/apartments-in/" not in fetched.final_url
                    and not rejected
                ),
            }
        )

    source_sha_after = sha256(SCRAPER)
    test_sha_after = sha256(TEST)
    if source_sha_after != source_sha_before or test_sha_after != test_sha_before:
        raise RuntimeError("source changed during live evidence capture")

    payload = {
        "capture_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Fail-closed regression evidence for retired LiveBH property URLs "
            "that redirect to cross-property city portfolio landings"
        ),
        "source_sha256": source_sha_after,
        "test_sha256": test_sha_after,
        "environment": required_env,
        "cohort_probe_observation": {
            "configured_livebh_rows_probed": 32,
            "property_scoped_controls": 30,
            "generic_city_redirects_without_configured_identity": 2,
            "generic_city_redirect_property_ids": [47182, 8789],
        },
        "negative_runs": negative_runs,
        "active_property_controls": controls,
        "strict_pass": bool(
            len(negative_runs) == 6
            and all(run["strict_pass"] for run in negative_runs)
            and len(controls) == 3
            and all(control["strict_pass"] for control in controls)
        ),
        "admission": {
            "strict_ledger_count_delta": 0,
            "reason": (
                "identity negatives; prevents sibling inventory contamination "
                "but does not recover the retired configured properties"
            ),
        },
    }
    if not payload["strict_pass"]:
        raise RuntimeError("live fail-closed evidence did not pass all gates")
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "strict_pass": True}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
