"""Bounded, compliance-safe Hyperbrowser check for three RentCafe residuals.

This is an evidence runner only.  It does not mutate production code or the
shared FAILED_NO_DATA ledger.  Every session is capped to one call per
property, CAPTCHA solving is hard-disabled by the backend, and this runner
also forces basic stealth off.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
from pathlib import Path
from typing import Any


CANDIDATE_REPO = Path(
    "/Users/ankur/PropAi-main/.claude/worktrees/wf_94b9d351-073-7"
)
sys.path.insert(0, str(CANDIDATE_REPO))

# These are policy invariants for this evidence run, not user-tunable knobs.
os.environ["HB_USE_STEALTH"] = "0"
os.environ["HYPERBROWSER_MAX_CALLS_PER_PROPERTY"] = "1"

from ma_poc.discovery.contracts import CrawlTask, TaskReason  # noqa: E402
from ma_poc.extraction.post_process import post_process  # noqa: E402
from ma_poc.fetch.contracts import RenderMode  # noqa: E402
from ma_poc.fetch.hyperbrowser_backend import (  # noqa: E402
    HyperbrowserProvider,
    _session_options,
    hyperbrowser_property_call_count,
)
from ma_poc.pms.adapters._rentcafe_hosted_table import (  # noqa: E402
    parse_rentcafe_hosted_table,
)
from ma_poc.pms.adapters._rentcafe_nestin import parse_nestin_detail_page  # noqa: E402
from ma_poc.pms.adapters._securecafe_applicant import (  # noqa: E402
    parse_securecafe_applicant_floorplans,
)
from ma_poc.pms.adapters.rentcafe import (  # noqa: E402
    _is_rentcafe_response,
    _unwrap_rentcafe_list,
    parse_rentcafe_floorplans,
    parse_rentcafe_ysi_unitslist,
    parse_securecafe_availableunits,
)
from ma_poc.pms.adapters.rentcafe_layout_tab import (  # noqa: E402
    parse_rentcafe_lt_applyga,
)


HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "hb_priority3_raw"
TARGETS = {
    "219752": {
        "property_name": "Lamphouse",
        "url": "https://lamphouseapts.com/",
        "rp_oracle_native_unit_rows": 11,
    },
    "220907": {
        "property_name": "Reserve At Bradbury Place",
        "url": "https://www.keystonemanagement.com/apartments/nc/reserve-at-bradbury-place",
        "rp_oracle_native_unit_rows": 21,
    },
    "58546": {
        "property_name": "Lake Park Estates",
        "url": "https://wright-weber.com/property/LakePark",
        "rp_oracle_native_unit_rows": 1,
    },
}


def _positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row.get(key) > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def _real_native_unit(row: dict[str, Any]) -> bool:
    unit = str(row.get("unit_number") or "").strip()
    return bool(unit) and not unit.upper().startswith("WAIT")


def _strict_rows(rows: list[dict[str, Any]], property_id: str) -> list[dict[str, Any]]:
    candidates = [row for row in rows if _real_native_unit(row) and _positive_rent(row)]
    if not candidates:
        return []
    processed = post_process(candidates, property_id=property_id)
    return [
        row
        for row in processed.admitted
        if _real_native_unit(row) and _positive_rent(row)
    ]


def _parse_html(body: str, url: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "securecafe_availableunits": parse_securecafe_availableunits(body, url),
        "rentcafe_ysi_unitslist": parse_rentcafe_ysi_unitslist(body, url),
        "rentcafe_hosted_table": parse_rentcafe_hosted_table(body, url),
        "rentcafe_nestin_detail": parse_nestin_detail_page(body, url),
        "rentcafe_layout_tab_applyga": parse_rentcafe_lt_applyga(body, url),
    }


def _parse_network(entry: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    url = str(entry.get("url") or "")
    raw = entry.get("body")
    if isinstance(raw, str):
        try:
            payload: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    else:
        payload = raw
    out: dict[str, list[dict[str, Any]]] = {}
    applicant = parse_securecafe_applicant_floorplans(payload, url)
    if applicant:
        out["securecafe_applicant"] = applicant
    if _is_rentcafe_response(payload):
        items = _unwrap_rentcafe_list(payload)
        rentcafe = parse_rentcafe_floorplans(items, url)
        if rentcafe:
            out["rentcafe_floorplans"] = rentcafe
    return out


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "unit_number",
            "floor_plan_name",
            "bedrooms",
            "bathrooms",
            "sqft",
            "market_rent_low",
            "market_rent_high",
            "availability_date",
            "source_api_url",
            "source_ids",
        )
    }


async def _run_one(property_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    provider = HyperbrowserProvider(mode="render")
    task = CrawlTask(
        url=str(meta["url"]),
        property_id=property_id,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
        expected_pms="rentcafe",
    )
    result = await provider.fetch(task, None)
    body = (result.body or b"").decode("utf-8", "replace")
    if body:
        with gzip.open(RAW_DIR / f"{property_id}.html.gz", "wb") as handle:
            handle.write(body.encode("utf-8", "replace"))
    (RAW_DIR / f"{property_id}.network.json").write_text(
        json.dumps(result.network_log, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    parser_rows: dict[str, list[dict[str, Any]]] = _parse_html(
        body, result.final_url or str(meta["url"])
    )
    for index, entry in enumerate(result.network_log):
        for parser, rows in _parse_network(entry).items():
            parser_rows[f"network_{index}:{parser}"] = rows

    strict_by_parser = {
        parser: _strict_rows(rows, property_id)
        for parser, rows in parser_rows.items()
    }
    strongest_parser, strongest = max(
        strict_by_parser.items(), key=lambda item: len(item[1]), default=("", [])
    )
    return {
        "property_id": int(property_id),
        **meta,
        "outcome": result.outcome.value,
        "status": result.status,
        "final_url": result.final_url,
        "body_bytes": len(result.body or b""),
        "network_log_count": len(result.network_log),
        "network_urls": [str(entry.get("url") or "") for entry in result.network_log],
        "session_calls": hyperbrowser_property_call_count(property_id),
        "parser_row_counts": {key: len(value) for key, value in parser_rows.items()},
        "strict_row_counts": {key: len(value) for key, value in strict_by_parser.items()},
        "strongest_parser": strongest_parser,
        "strict_native_positive_rent_rows": len(strongest),
        "strict_samples": [_sample(row) for row in strongest[:4]],
        "error_signature": result.error_signature,
        "captcha_detected": result.captcha_detected,
    }


async def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    options = _session_options("render")
    if options.get("solveCaptchas") or options.get("useStealth"):
        raise RuntimeError(f"unsafe Hyperbrowser options: {options}")
    results = []
    # Sequential on purpose: bounded sessions, predictable cost and load.
    for property_id, meta in TARGETS.items():
        row = await _run_one(property_id, meta)
        results.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
    artifact = {
        "run_kind": "bounded_hyperbrowser_exact_url_probe",
        "llm_enabled": False,
        "canary": False,
        "session_options": options,
        "targets": len(TARGETS),
        "results": results,
    }
    (HERE / "hb_priority3_summary.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
