from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_arthaus_provider_capture")
URL = "https://arthaus.mov/building-community.php?slug=arthaus-jack-london"


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    task = CrawlTask(
        url=URL,
        property_id="268888",
        priority=0,
        budget_ms=90_000,
        reason=TaskReason.MANUAL,
        render_mode=RenderMode.RENDER,
    )
    result = await HyperbrowserProvider(mode="render").fetch(task, None)
    payload = {
        "url": result.url,
        "final_url": result.final_url,
        "outcome": str(result.outcome),
        "status": result.status,
        "body_bytes": len(result.body or b""),
        "network_log": result.network_log,
        "solve_captchas": False,
    }
    (ROOT / "capture.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(
        json.dumps(
            {
                "outcome": payload["outcome"],
                "status": payload["status"],
                "body_bytes": payload["body_bytes"],
                "network_responses": len(payload["network_log"]),
                "network_urls": [
                    item.get("url") for item in payload["network_log"]
                ],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
