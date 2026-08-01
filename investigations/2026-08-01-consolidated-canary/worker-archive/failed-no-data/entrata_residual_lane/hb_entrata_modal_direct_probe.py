#!/usr/bin/env python3
"""Open an exact listing and its published Entrata availability endpoint in one HB session."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from ma_poc.fetch.hyperbrowser_backend import _HbSession, _session_options


OUT = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
TARGETS = {
    "9297": {
        "warmup": "https://www.parkcreekmanor.com/",
        "listing": (
            "https://www.parkcreekmanor.com/dallas/"
            "park-creek-manor-apartments/conventional/"
        ),
        "floorplan_id": "897486",
    },
    "234772": {
        "warmup": "https://www.liveattp.com/",
        "listing": (
            "https://www.liveattp.com/millersville/"
            "the-pointe-at-harpers-mill/conventional/"
        ),
        "floorplan_id": "",
    },
    "239274": {
        "warmup": "https://www.strataoncalifornia.com/",
        "listing": (
            "https://www.strataoncalifornia.com/seattle/"
            "strata-on-california/conventional/"
        ),
        "floorplan_id": "856131",
    },
}


def challenge(html: str, title: str) -> bool:
    text = f"{title}\n{html[:12000]}".casefold()
    return any(
        marker in text
        for marker in (
            "just a moment",
            "verify you are human",
            "checking your browser",
            "cf-chl-",
        )
    )


async def main() -> None:
    property_id = os.environ.get("PROPERTY_ID", "9297")
    configured = TARGETS.get(property_id, {})
    target = {
        "warmup": os.environ.get("WARMUP_URL") or configured.get("warmup", ""),
        "listing": os.environ.get("LISTING_URL") or configured.get("listing", ""),
        "floorplan_id": (
            os.environ.get("FLOORPLAN_ID")
            if "FLOORPLAN_ID" in os.environ
            else configured.get("floorplan_id", "")
        ),
    }
    if not target["warmup"] or not target["listing"]:
        raise SystemExit(
            "Unknown PROPERTY_ID requires WARMUP_URL and LISTING_URL"
        )
    session = _HbSession(mode="render")
    try:
        page = await session.open()
        await page.goto(target["warmup"], wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(8)
        warmup_html = await page.content()
        warmup_title = str(await page.title() or "")
        await page.goto(target["listing"], wait_until="domcontentloaded", timeout=90_000)
        await asyncio.sleep(10)
        listing_html = await page.content()
        listing_title = str(await page.title() or "")
        selector = "button[data-url*='action=view_unit_spaces']"
        if target["floorplan_id"]:
            selector += f"[data-url*='property_floorplan[id]={target['floorplan_id']}']"
        buttons = await page.locator(selector).evaluate_all(
            """els => els.map(e => ({
                text: (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim(),
                data_url: e.getAttribute('data-url') || '',
                aria: e.getAttribute('aria-label') || ''
            }))"""
        )
        buttons = [
            row
            for row in buttons
            if "is_availability_alert=true" not in str(row.get("data_url") or "")
        ]
        if not buttons:
            payload = {
                "capture_timestamp_utc": datetime.now(UTC).isoformat(),
                "property_id": int(property_id),
                "outcome": "NO_CURRENT_PUBLISHED_AVAILABILITY_ENDPOINT",
                "warmup_title": warmup_title,
                "listing_title": listing_title,
                "warmup_challenge": challenge(warmup_html, warmup_title),
                "listing_challenge": challenge(listing_html, listing_title),
                "listing_url": str(page.url or ""),
                "session_options": _session_options("render"),
                "captcha_solving": False,
                "listing_html_sha256": hashlib.sha256(
                    listing_html.encode("utf-8", "replace")
                ).hexdigest(),
            }
        else:
            endpoint = str(buttons[0]["data_url"])
            probe_mode = os.environ.get("PROBE_MODE", "navigate").strip().casefold()
            response = None
            response_body = ""
            click_error = ""
            if probe_mode == "click":
                try:
                    async with page.expect_response(
                        lambda item: "action=view_unit_spaces" in item.url,
                        timeout=30_000,
                    ) as response_info:
                        await page.locator(selector).first.evaluate("element => element.click()")
                    response = await response_info.value
                    response_body = await response.text()
                except Exception as exc:
                    click_error = type(exc).__name__
                await asyncio.sleep(8)
                page_html = await page.content()
                endpoint_html = response_body or page_html
            else:
                response = await page.goto(
                    endpoint, wait_until="domcontentloaded", timeout=90_000
                )
                await asyncio.sleep(8)
                page_html = await page.content()
                endpoint_html = page_html
            endpoint_title = str(await page.title() or "")
            endpoint_status = int(response.status or 0) if response else 0
            payload = {
                "capture_timestamp_utc": datetime.now(UTC).isoformat(),
                "property_id": int(property_id),
                "outcome": (
                    "CURRENT_PUBLISHED_MODAL_ENDPOINT_CAPTURED"
                    if response is not None
                    and endpoint_status == 200
                    and not challenge(endpoint_html, endpoint_title)
                    else "PUBLISHED_ENDPOINT_BLOCKED"
                ),
                "warmup_title": warmup_title,
                "listing_title": listing_title,
                "warmup_challenge": challenge(warmup_html, warmup_title),
                "listing_challenge": challenge(listing_html, listing_title),
                "listing_url": target["listing"],
                "published_button": buttons[0],
                "published_endpoint": endpoint,
                "probe_mode": probe_mode,
                "click_error": click_error,
                "endpoint_status": endpoint_status,
                "endpoint_final_url": str(page.url or ""),
                "endpoint_title": endpoint_title,
                "endpoint_challenge": challenge(endpoint_html, endpoint_title),
                "session_options": _session_options("render"),
                "captcha_solving": False,
                "listing_html_sha256": hashlib.sha256(
                    listing_html.encode("utf-8", "replace")
                ).hexdigest(),
                "endpoint_html_sha256": hashlib.sha256(
                    endpoint_html.encode("utf-8", "replace")
                ).hexdigest(),
                "endpoint_html": endpoint_html,
                "page_html_sha256": hashlib.sha256(
                    page_html.encode("utf-8", "replace")
                ).hexdigest(),
                "page_html": page_html,
                "marker_counts": {
                    marker: len(re.findall(marker, endpoint_html, re.I))
                    for marker in ("unit", "rent", "available", "unit_space")
                },
            }
        output = OUT / f"hb_modal_direct_{property_id}_current.json"
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "artifact": str(output),
                    "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "property_id": int(property_id),
                    "outcome": payload["outcome"],
                    "endpoint_status": payload.get("endpoint_status"),
                    "marker_counts": payload.get("marker_counts"),
                }
            )
        )
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
