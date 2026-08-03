from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
HARNESS_PATH = ROOT / "appfolio_wix_residual_lane" / "run_current_full_e2e.py"
OUTPUT = ROOT / "hb_scully_residual3_detail_probe" / "current_configured_scrape_jugnu_e2e.json"
TARGET_IDS = ("43995", "60141", "63191")

EXPECTED_ENV = {
    "COMPLIANCE_MODE": "1",
    "ENABLE_TIER4_LLM": "false",
    "ENABLE_TIER_ESCALATION": "false",
    "ENABLE_DC_PROXY_TIER": "false",
    "ENABLE_RESIDENTIAL_TIER": "false",
    "ENABLE_RESIDENTIAL_RENDER_TIER": "false",
    "ENABLE_UNLOCKER_TIER": "false",
    "ENABLE_FLARESOLVERR_TIER": "false",
    "FETCH_BACKEND": "hyperbrowser",
    "RENDER_BACKEND": "local",
    "PROBE_PROXY_URL": "",
    "PROXY_POOL_URLS": "",
    "ENABLE_RENDER_ON_EMPTY": "false",
    "ENABLE_PLAN_UNIT_RENDER": "false",
    "ENABLE_ENTRATA_PLAN_RENDER": "false",
    "ENABLE_BODY_RESOLVER": "false",
    "ENABLE_CRAWL_GET_GATE": "false",
    "HB_USE_STEALTH": "false",
    "HB_USE_PROXY": "true",
    "HYPERBROWSER_MAX_CALLS_PER_PROPERTY": "1",
}


def load_harness():
    spec = importlib.util.spec_from_file_location("fnd_current_e2e_harness", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load configured-pipeline harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main() -> None:
    for name, expected in EXPECTED_ENV.items():
        actual = os.environ.get(name, "")
        if actual.casefold() != expected.casefold():
            raise RuntimeError(f"{name}={actual!r}; expected {expected!r}")

    harness = load_harness()
    remaining = {
        row["property_id"]: row
        for row in harness.read_csv(harness.REMAINING)
        if row.get("property_id") in TARGET_IDS
    }
    metadata = {
        row["apartmentid"]: row
        for row in harness.read_csv(harness.PROPERTIES)
        if row.get("apartmentid") in TARGET_IDS
    }
    if set(remaining) != set(TARGET_IDS) or set(metadata) != set(TARGET_IDS):
        raise RuntimeError(
            f"target mismatch remaining={sorted(remaining)} metadata={sorted(metadata)}"
        )

    harness.fetch_mod.fetch = harness.direct_fetch
    harness.reset_web_unlocker_call_count()
    harness.reset_hyperbrowser_property_counts()

    results = []
    for property_id in TARGET_IDS:
        result = await harness.one(remaining[property_id], metadata[property_id])
        results.append(result)
        print(
            json.dumps(
                {
                    "property_id": property_id,
                    "detected": result.get("current_detected_pms"),
                    "adapter": result.get("adapter"),
                    "tier": result.get("tier"),
                    "units": result.get("emitted_unit_rows"),
                    "native_priced": result.get("strict_native_positive_rent_rows"),
                    "source_property_ids": result.get("source_property_ids"),
                    "exception": result.get("exception"),
                }
            ),
            flush=True,
        )

    hb_counts = {
        property_id: harness.hyperbrowser_property_call_count(property_id)
        for property_id in TARGET_IDS
    }
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "scully_entrata_three_current_configured_scrape_jugnu_e2e",
        "source_snapshot": harness.source_snapshot(),
        "cohort": {
            "failed_no_data_source": str(harness.REMAINING),
            "failed_no_data_source_sha256": harness.sha256(harness.REMAINING),
            "target_property_ids": [int(value) for value in TARGET_IDS],
        },
        "guardrails": {
            "llm_enabled": False,
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "paid_canary": False,
            "hyperbrowser": True,
            "hyperbrowser_session_options": {
                "solveCaptchas": False,
                "useStealth": False,
                "useProxy": True,
            },
            "hyperbrowser_property_call_counts": hb_counts,
            "web_unlocker_call_count": harness.web_unlocker_call_count(),
            "environment": EXPECTED_ENV,
        },
        "summary": {
            "properties": len(results),
            "configured_fetch_ok": sum(
                row.get("configured_fetch", {}).get("outcome") == "OK"
                for row in results
            ),
            "entrata_detected": sum(
                row.get("current_detected_pms") == "entrata" for row in results
            ),
            "strict_native_priced_properties": sum(
                int(row.get("strict_native_positive_rent_rows") or 0) > 0
                for row in results
            ),
            "strict_native_priced_rows": sum(
                int(row.get("strict_native_positive_rent_rows") or 0)
                for row in results
            ),
        },
        "results": results,
    }
    if payload["guardrails"]["web_unlocker_call_count"] != 0:
        raise RuntimeError("web unlocker call observed")
    if any(count != 1 for count in hb_counts.values()):
        raise RuntimeError(f"unexpected Hyperbrowser session counts: {hb_counts}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"artifact": str(OUTPUT), **payload["summary"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
