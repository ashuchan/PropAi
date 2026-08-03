from __future__ import annotations

import asyncio
import gzip
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import _HbSession, _session_options


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/unknown_wix_parallel")
PROPERTY_ID = "22964"
URL = "https://8452181.onlineleasing.realpage.com/"


def _summarize_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    controls: list[dict[str, Any]] = []
    for node in soup.select("button, input, select, a[href]"):
        label = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        value = str(node.get("value") or "")
        href = str(node.get("href") or "")
        if any(
            token in (label + " " + value + " " + href).casefold()
            for token in ("continue", "start", "floor", "unit", "apartment", "submit", "apply")
        ):
            controls.append(
                {
                    "tag": node.name,
                    "label": label[:300],
                    "value": value[:300],
                    "href": href[:500],
                    "id": str(node.get("id") or ""),
                    "name": str(node.get("name") or ""),
                    "type": str(node.get("type") or ""),
                }
            )
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "text_prefix": text[:6000],
        "controls": controls[:100],
        "bytes": len(html.encode("utf-8", "replace")),
        "captcha_or_challenge": any(
            token in (html + " " + text).casefold()
            for token in ("cf-chl-", "just a moment", "verify you are human", "captcha")
        ),
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    # Hyperbrowser's basic stealth is one stable browser fingerprint for this
    # single session (not account-level advanced stealth or fingerprint
    # rotation). Keep the actual value in the artifact for auditability.
    session = _HbSession("render")
    captured: list[dict[str, Any]] = []
    capture_tasks: set[asyncio.Task[None]] = set()

    async def capture(response: Any) -> None:
        url = str(response.url)
        lower = url.casefold()
        if not any(
            token in lower
            for token in (
                "realpage.com",
                "onlineleasing",
                "appstate",
                "getunits",
                "floorplan",
                "/units",
            )
        ):
            return
        item: dict[str, Any] = {
            "url": url,
            "status": int(response.status),
            "method": str(response.request.method),
            "content_type": str((await response.all_headers()).get("content-type") or ""),
        }
        try:
            body_text = await response.text()
        except Exception as exc:  # noqa: BLE001
            item["body_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            captured.append(item)
            return
        item["body_bytes"] = len(body_text.encode("utf-8", "replace"))
        if len(body_text) <= 5_000_000:
            try:
                item["body"] = json.loads(body_text)
            except json.JSONDecodeError:
                item["body_text_prefix"] = body_text[:10000]
        captured.append(item)

    def schedule(response: Any) -> None:
        task = asyncio.create_task(capture(response))
        capture_tasks.add(task)
        task.add_done_callback(capture_tasks.discard)

    page = None
    error = ""
    click_attempts: list[dict[str, Any]] = []
    try:
        page = await session.open()
        page.on("response", schedule)
        response = await page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(12)
        html_initial = await page.content()
        (ROOT / "22964_tropicana_oll_initial.html.gz").write_bytes(
            gzip.compress(html_initial.encode("utf-8", "replace"))
        )

        # Only click ordinary application navigation controls. Never interact
        # with challenge/CAPTCHA elements.
        for selector in (
            "button:has-text('Start')",
            "button:has-text('Continue')",
            "a:has-text('Start')",
            "a:has-text('Continue')",
        ):
            try:
                locator = page.locator(selector).first
                count = await page.locator(selector).count()
                click_attempts.append({"selector": selector, "count": count})
                if count:
                    await locator.click(timeout=5000)
                    click_attempts[-1]["clicked"] = True
                    await asyncio.sleep(12)
                    break
            except Exception as exc:  # noqa: BLE001
                click_attempts[-1]["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"

        html_final = await page.content()
        (ROOT / "22964_tropicana_oll_final.html.gz").write_bytes(
            gzip.compress(html_final.encode("utf-8", "replace"))
        )
        await asyncio.sleep(2)
        if capture_tasks:
            await asyncio.gather(*list(capture_tasks), return_exceptions=True)
        artifact = {
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "property_id": int(PROPERTY_ID),
            "configured_site": "https://www.tropicanavillageapartments.com/",
            "official_application_page": (
                "https://www.tropicanavillageapartments.com/apply-now/application-process"
            ),
            "oll_url": URL,
            "final_url": str(page.url),
            "status": getattr(response, "status", None),
            "session_options": options,
            "guardrails": {
                "solve_captchas": False,
                "use_stealth": options["useStealth"],
                "fingerprint_rotation": False,
                "web_unlocker": False,
                "flaresolverr": False,
                "llm": False,
                "paid_canary": False,
            },
            "initial": _summarize_html(html_initial),
            "final": _summarize_html(html_final),
            "click_attempts": click_attempts,
            "captured": captured,
            "error": error,
        }
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:2000]}"
        if capture_tasks:
            await asyncio.gather(*list(capture_tasks), return_exceptions=True)
        artifact = {
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "property_id": int(PROPERTY_ID),
            "oll_url": URL,
            "session_options": options,
            "guardrails": {
                "solve_captchas": False,
                "use_stealth": options["useStealth"],
                "fingerprint_rotation": False,
            },
            "captured": captured,
            "click_attempts": click_attempts,
            "error": error,
        }
    finally:
        await session.close()

    out = ROOT / "22964_tropicana_oll_hb.json"
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    summary = {
        "property_id": artifact["property_id"],
        "final_url": artifact.get("final_url"),
        "error": artifact.get("error"),
        "captured_responses": len(captured),
        "workflow_bodies": sum(
            isinstance(item.get("body"), dict) and "Workflow" in item["body"]
            for item in captured
        ),
        "initial": artifact.get("initial"),
        "final": artifact.get("final"),
        "click_attempts": click_attempts,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
