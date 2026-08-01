from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
BASE = ROOT / "realpage_onesite_residual_lane" / "scan_remaining_current_pipeline.py"
OUTPUT = ROOT / "knock_migration_screen_current.json"
TARGETS = {"42977", "68497", "224888"}


async def main() -> None:
    spec = importlib.util.spec_from_file_location("fnd_current_scan", BASE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {BASE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    residuals = {
        row["property_id"]: row for row in module.read_csv(module.REMAINING)
    }
    metadata = {
        row["apartmentid"]: row for row in module.read_csv(module.PROPERTIES)
    }
    module.fetch_mod.fetch = module.direct_fetch
    results = []
    for property_id in sorted(TARGETS, key=int):
        row = await module.one(residuals[property_id], metadata[property_id])
        results.append(row)
        print(
            json.dumps(
                {
                    "property_id": property_id,
                    "adapter": row.get("adapter") or "",
                    "strict_native_priced_rows": row.get(
                        "strict_native_priced_rows", 0
                    ),
                    "fallback_chain": row.get("fallback_chain") or [],
                    "errors": row.get("errors") or [],
                    "exception": row.get("exception") or "",
                }
            ),
            flush=True,
        )
    OUTPUT.write_text(
        json.dumps(
            {
                "guardrails": {
                    "direct_only": True,
                    "llm": False,
                    "web_unlocker": False,
                    "hyperbrowser": False,
                    "captcha_solving": False,
                    "flaresolverr": False,
                    "fingerprint_rotation": False,
                    "paid_canary": False,
                },
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
