from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ma_poc.fetch.hyperbrowser_backend import (
    hyperbrowser_property_call_count,
    reset_hyperbrowser_property_counts,
)
from ma_poc.pms.adapters._probe import (
    reset_web_unlocker_call_count,
    web_unlocker_call_count,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
LANE = ROOT / "entrata_residual_lane"
OUTPUT = LANE / "evidence_tribeca_leaseleads_current_full_strict.json"
FULL_HELPER = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
PROPERTY_ID = "57195"
LEASELEADS_UUID = "9e4f1b70-ec67-4725-81da-7dd6b6c04d8c"
SOURCE_URL = (
    "https://api.leaseleads.co/api/v2/property/"
    f"{LEASELEADS_UUID}/floor-plans"
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("fnd_full_tribeca", FULL_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load current full-pipeline helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        "WEB_UNLOCKER_KEY": "",
    }
    for key, expected in expected_env.items():
        actual = os.environ.get(key, "").casefold()
        if actual != expected:
            raise RuntimeError(f"guardrail {key}={actual!r}; expected {expected!r}")

    remaining = {row["property_id"]: row for row in read_csv(REMAINING)}
    metadata = {row["apartmentid"]: row for row in read_csv(PROPERTIES)}
    if PROPERTY_ID not in remaining or PROPERTY_ID not in metadata:
        raise RuntimeError("Tribeca is not in the authoritative residual partition")
    canonical = metadata[PROPERTY_ID]
    if (
        canonical.get("name") != "Tribeca"
        or canonical.get("address") != "720 W 3rd Ave"
        or canonical.get("city") != "Columbus"
        or canonical.get("state") != "OH"
        or canonical.get("zip") != "43212"
    ):
        raise RuntimeError("canonical property identity changed")

    reset_web_unlocker_call_count()
    reset_hyperbrowser_property_counts()
    helper = load_helper()
    result = await helper.one(remaining[PROPERTY_ID], canonical)

    units = [
        row
        for row in (result.get("strict_shape_rows") or [])
        if isinstance(row, dict)
    ]
    native_ids = [
        str((row.get("source_ids") or {}).get("leaseleads_unit_id") or "")
        for row in units
    ]
    unit_numbers = [str(row.get("unit_number") or "").strip() for row in units]
    configured = result.get("configured_fetch") or {}
    final_host = (urlparse(str(configured.get("final_url") or "")).hostname or "")
    source_urls = list(result.get("source_urls") or [])
    strict = bool(
        configured.get("status") == 200
        and configured.get("outcome") == "OK"
        and final_host.casefold().removeprefix("www.") == "tribecacolumbus.com"
        and result.get("current_detected_pms") == "squarespace_nopms"
        and result.get("adapter") == "squarespace_nopms"
        and result.get("tier") == "TIER_1_API_LEASELEADS"
        and int(result.get("emitted_unit_rows") or 0) == len(units) > 0
        and int(result.get("plan_rows") or 0) == 0
        and int(result.get("native_identity_rows") or 0) == len(units)
        and int(result.get("strict_native_positive_rent_rows") or 0) == len(units)
        and len(native_ids) == len(set(native_ids)) == len(units)
        and all(native_ids)
        and len(unit_numbers) == len(set(unit_numbers)) == len(units)
        and all(unit_numbers)
        and all(positive_rent(row) for row in units)
        and result.get("source_property_ids") == [LEASELEADS_UUID]
        and source_urls == [SOURCE_URL]
        and not result.get("errors")
        and not result.get("exception")
        and not result.get("llm_interactions")
        and web_unlocker_call_count() == 0
        and hyperbrowser_property_call_count(PROPERTY_ID) == 0
    )
    if not strict:
        raise RuntimeError("Tribeca current full-pipeline strict gates failed")

    critical_paths = [
        Path("ma_poc/pms/adapters/_leaseleads_embed.py"),
        Path("ma_poc/pms/adapters/_universal_recovery.py"),
        Path("ma_poc/pms/adapters/squarespace_nopms.py"),
        Path("ma_poc/core/source_ids.py"),
        Path("ma_poc/pms/scraper.py"),
        Path("ma_poc/pms/detector.py"),
    ]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    payload = {
        "lane": "tribeca_leaseleads_current_full_configured_pipeline_strict",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": {
            "git_head": head,
            "critical_file_sha256": {
                str(path): sha256(path) for path in critical_paths
            },
        },
        "guardrails": {
            "paid_canary": False,
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser_sessions": 0,
            "ordinary_direct_get_only": True,
        },
        "results": [
            {
                "property_id": int(PROPERTY_ID),
                "property_name": "Tribeca",
                "website": canonical["website"],
                "outcome": "UNIT_QUALIFIED",
                "property_identity_match": True,
                "contamination_verdict": (
                    "pass_exact_published_leaseleads_uuid_domain_name_street_"
                    "city_zip_native_unit_ids_full_configured_pipeline"
                ),
                "units": len(units),
                "identity_evidence": {
                    "rows_with_native_identity": len(units),
                    "rows_with_native_identity_and_positive_rent": len(units),
                    "distinct_native_ids": len(set(native_ids)),
                    "distinct_unit_numbers": len(set(unit_numbers)),
                    "source_urls": source_urls,
                    "source_property_ids": [LEASELEADS_UUID],
                },
                "configured_fetch": configured,
                "adapter": result.get("adapter"),
                "tier": result.get("tier"),
                "fallback_chain": result.get("fallback_chain") or [],
                "native_samples": [
                    {
                        "identity": {
                            "unit_number": row["unit_number"],
                            "leaseleads_unit_id": row["source_ids"][
                                "leaseleads_unit_id"
                            ],
                        },
                        "positive_rent_evidence": {
                            "market_rent_low": row.get("market_rent_low"),
                            "market_rent_high": row.get("market_rent_high"),
                        },
                        "source_api_url": row.get("source_api_url"),
                    }
                    for row in units[:8]
                ],
                "unit_rows": units,
                "rp_oracle_native_unit_rows": int(
                    remaining[PROPERTY_ID]["rp_oracle_native_unit_rows"]
                ),
                "rp_oracle_distinct_floorplans": int(
                    remaining[PROPERTY_ID]["rp_oracle_distinct_floorplans"]
                ),
            }
        ],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "strict_properties": 1,
                "strict_native_rows": len(units),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
