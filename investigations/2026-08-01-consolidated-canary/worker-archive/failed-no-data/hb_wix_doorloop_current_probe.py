from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_wix_doorloop_current_probe")
TARGETS = {
    "19538": "https://www.stadiumapartmentshuntsville.com/availability",
    "118965": "https://www.16bennett.com/",
    "254556": (
        "https://parkplace.app.doorloop.com/tenant-portal/"
        "rental-applications/listing?"
        "companyId=65f8a0d0390bcd90dba4e93f&source=CompanyLink"
    ),
    "263732": "https://www.hoyttowernewark.com/",
    "271721": "https://www.millenniumnw.com/properties-for-rent",
}


async def capture(property_id: str, url: str) -> dict[str, object]:
    task = CrawlTask(
        url=url,
        property_id=property_id,
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    result = await HyperbrowserProvider(mode="render").fetch(task, None)
    body = result.body or b""
    if body:
        with gzip.open(ROOT / f"{property_id}.html.gz", "wb") as handle:
            handle.write(body)
    (ROOT / f"{property_id}.network.json").write_text(
        json.dumps(result.network_log, indent=2) + "\n"
    )
    return {
        "property_id": int(property_id),
        "url": url,
        "final_url": result.final_url,
        "outcome": str(result.outcome),
        "status": result.status,
        "body_bytes": len(body),
        "network_responses": len(result.network_log),
        "network_urls": [item.get("url") for item in result.network_log],
        "captcha_detected": bool(result.captcha_detected),
        "solve_captchas": False,
        "llm_enabled": False,
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for property_id, url in TARGETS.items():
        result = await capture(property_id, url)
        results.append(result)
        print(json.dumps(result), flush=True)
    (ROOT / "summary.json").write_text(
        json.dumps(
            {
                "targets": len(TARGETS),
                "hyperbrowser_sessions": len(TARGETS),
                "solve_captchas": False,
                "llm_enabled": False,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
