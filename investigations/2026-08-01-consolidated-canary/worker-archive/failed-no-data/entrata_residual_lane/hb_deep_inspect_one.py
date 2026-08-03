#!/usr/bin/env python3
"""Read-only deep inspection of one exact Entrata property page via clean HB."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ma_poc.fetch.hyperbrowser_backend import _HbSession, _session_options


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
TARGETS = {
    "35192": (
        "https://www.enclaveongoldentriangle.com/floorplans/fort-worth-TX/"
        "enclave-on-golden-triangle/the-baltic-776780-1/"
    ),
    "20672": (
        "https://www.quietwaterslanding.com/annapolis/"
        "quiet-waters-landing/conventional/"
    ),
    "234772": "https://www.liveattp.com/floorplans",
}


def same_host(left: str, right: str) -> bool:
    return (urlsplit(left).hostname or "").casefold() == (
        urlsplit(right).hostname or ""
    ).casefold()


async def main() -> None:
    property_id = os.environ.get("PROPERTY_ID", "35192")
    target = os.environ.get("TARGET_URL") or TARGETS[property_id]
    session = _HbSession(mode="render")
    responses: list[dict[str, object]] = []
    response_tasks: list[asyncio.Task[None]] = []

    async def record_response(response: object) -> None:
        url = str(getattr(response, "url", "") or "")
        request = getattr(response, "request", None)
        resource_type = str(getattr(request, "resource_type", "") or "")
        headers = await response.all_headers()
        content_type = str(headers.get("content-type") or "")
        interesting = bool(
            same_host(url, target)
            or any(
                marker in url.casefold()
                for marker in (
                    "entrata",
                    "prospectportal",
                    "availability",
                    "floorplan",
                    "unit",
                )
            )
        )
        if not interesting:
            return
        item: dict[str, object] = {
            "url": url,
            "status": int(getattr(response, "status", 0) or 0),
            "resource_type": resource_type,
            "content_type": content_type,
        }
        if (
            resource_type in {"document", "xhr", "fetch"}
            and int(getattr(response, "status", 0) or 0) == 200
        ):
            try:
                body = await response.body()
                item["body_bytes"] = len(body)
                item["body_sha256"] = hashlib.sha256(body).hexdigest()
                if len(body) <= 2_000_000:
                    item["body"] = body.decode("utf-8", "replace")
            except Exception as exc:
                item["body_error"] = type(exc).__name__
        responses.append(item)

    def on_response(response: object) -> None:
        response_tasks.append(asyncio.create_task(record_response(response)))

    try:
        page = await session.open()
        page.on("response", on_response)
        await page.goto(target, wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(12)
        click_selector = os.environ.get("CLICK_SELECTOR", "").strip()
        click_result: dict[str, object] | None = None
        if click_selector:
            locator = page.locator(click_selector).first
            click_result = {
                "selector": click_selector,
                "count": await page.locator(click_selector).count(),
            }
            try:
                await locator.click(timeout=15_000)
                await asyncio.sleep(8)
                click_result["clicked"] = True
            except Exception as exc:
                click_result["clicked"] = False
                click_result["error"] = type(exc).__name__
        html = await page.content()
        title = str(await page.title() or "")
        final_url = str(page.url or "")
        elements = await page.locator("a,button,[role=button]").evaluate_all(
            """els => els.map((e, i) => ({
                i,
                tag: e.tagName,
                text: (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim(),
                href: e.href || e.getAttribute('href') || '',
                id: e.id || '',
                cls: e.className || '',
                aria: e.getAttribute('aria-label') || '',
                data: Object.fromEntries([...e.attributes]
                    .filter(a => a.name.startsWith('data-'))
                    .map(a => [a.name, a.value]))
            })).filter(x => x.text || x.href || x.aria)"""
        )
        await asyncio.sleep(2)
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        result = {
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "property_id": int(property_id),
            "target_url": target,
            "final_url": final_url,
            "title": title,
            "same_host": same_host(final_url, target),
            "session_options": _session_options("render"),
            "captcha_solving": False,
            "html_bytes": len(html.encode("utf-8", "replace")),
            "html_sha256": hashlib.sha256(
                html.encode("utf-8", "replace")
            ).hexdigest(),
            "html": html,
            "clickable_elements": elements,
            "responses": responses,
            "marker_counts": {
                marker: len(re.findall(marker, html, re.I))
                for marker in (
                    "unit",
                    "available",
                    "rent",
                    "property_floorplan",
                    "check_availability",
                    "view_unit_spaces",
                )
            },
            "click_result": click_result,
        }
        output_tag = re.sub(
            r"[^a-zA-Z0-9_-]+", "_", os.environ.get("OUTPUT_TAG", "current")
        ).strip("_") or "current"
        output = OUT / f"hb_deep_inspect_{property_id}_{output_tag}.json"
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "artifact": str(output),
                    "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "property_id": int(property_id),
                    "title": title,
                    "final_url": final_url,
                    "html_bytes": result["html_bytes"],
                    "clickable_elements": len(elements),
                    "interesting_responses": len(responses),
                    "marker_counts": result["marker_counts"],
                }
            )
        )
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
