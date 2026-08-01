from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
from pathlib import Path

from ma_poc.discovery.contracts import CrawlTask, TaskReason
from ma_poc.fetch.contracts import RenderMode
from ma_poc.fetch.hyperbrowser_backend import HyperbrowserProvider


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_blocked_unknown3_current_probe")
TARGETS = {
    "4756": "https://www.gables.com/community/2430886",
    "34362": "https://www.thetamarronapts.com/",
    "39198": "http://www.vistadepalmasapartments.com/",
    "48075": "https://www.edgefieldaptsva.com/",
    "53932": "http://www.landmarkrealty.org/misty-hollow/",
}


def challenge_markers(body: bytes) -> list[str]:
    text = body[:50_000].decode("utf-8", "ignore").casefold()
    markers = (
        "just a moment",
        "verify you are human",
        "checking your browser",
        "cf-chl-",
        "sgcaptcha",
    )
    return [marker for marker in markers if marker in text]


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
    body_path = ROOT / f"{property_id}.html.gz"
    if body:
        with gzip.open(body_path, "wb") as handle:
            handle.write(body)
    network_path = ROOT / f"{property_id}.network.json"
    network_path.write_text(json.dumps(result.network_log, indent=2) + "\n")
    text = body.decode("utf-8", "ignore")
    provider_markers = sorted(
        {
            marker
            for marker in (
                "appfolio",
                "entrata",
                "rentcafe",
                "securecafe",
                "rentmanager",
                "onesite",
                "realpage",
                "knock",
                "funnelleasing",
                "sightmap",
                "resman",
                "doorloop",
                "apts247",
                "bettercms",
                "spherexx",
                "yardi",
            )
            if marker in text.casefold()
        }
    )
    candidate_urls = sorted(
        {
            match.rstrip("\\'\\\"),.;")
            for match in re.findall(r"https?://[^\\s<>\\\"']+", text, re.I)
            if any(
                token in match.casefold()
                for token in (
                    "avail",
                    "unit",
                    "floorplan",
                    "floor-plan",
                    "apply",
                    "lease",
                    "resident",
                    "portal",
                    "api",
                )
            )
        }
    )
    return {
        "property_id": int(property_id),
        "url": url,
        "final_url": result.final_url,
        "outcome": str(result.outcome),
        "status": result.status,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest() if body else "",
        "network_responses": len(result.network_log),
        "network_urls": [item.get("url") for item in result.network_log],
        "challenge_markers": challenge_markers(body),
        "captcha_detected": bool(result.captcha_detected),
        "provider_markers": provider_markers,
        "candidate_urls": candidate_urls,
        "solve_captchas": False,
        "llm_enabled": False,
        "stealth_enabled": False,
        "unlocker_enabled": False,
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    selected_id = os.environ.get("PROPERTY_ID", "").strip()
    selected_targets = (
        {selected_id: TARGETS[selected_id]} if selected_id else TARGETS
    )
    for property_id, url in selected_targets.items():
        result = await capture(property_id, url)
        results.append(result)
        print(json.dumps(result), flush=True)
    summary = ROOT / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "targets": len(selected_targets),
                "hyperbrowser_sessions": len(selected_targets),
                "solve_captchas": False,
                "stealth_enabled": False,
                "unlocker_enabled": False,
                "llm_enabled": False,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"summary": str(summary)}))


if __name__ == "__main__":
    asyncio.run(main())
