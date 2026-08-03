from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import (
    _HbSession,
    _install_block,
    _install_capture,
    _session_options,
)


PROPERTY_ID = "8119"
ROOT_URL = (
    "https://standrews.scullycompany.com/pompano-beach/st.-andrews/"
    "?is_responsive_snippet=1&snippet_type=website&occupancy_type=1"
    "&move_in_date=08/01/2026&is_collapsed=0&include_paragraph_content=1"
    "&host_domain=www.scullycompany.com"
)
DETAIL_URLS = {
    "1521": (
        "https://standrews.scullycompany.com/Apartments/module/"
        "property_floorplans/property%5Bid%5D/100002888/"
        "property_floorplan[id]/1521/is_premium_view/1/"
        "is_responsive_snippet/1/occupancy_type/conventional/"
        "is_collapsed/0/snippet_type/website/"
    ),
    "1522": (
        "https://standrews.scullycompany.com/Apartments/module/"
        "property_floorplans/property%5Bid%5D/100002888/"
        "property_floorplan[id]/1522/is_premium_view/1/"
        "is_responsive_snippet/1/occupancy_type/conventional/"
        "is_collapsed/0/snippet_type/website/"
    ),
    "1520": (
        "https://standrews.scullycompany.com/Apartments/module/"
        "property_floorplans/property%5Bid%5D/100002888/"
        "property_floorplan[id]/1520/is_premium_view/1/"
        "is_responsive_snippet/1/occupancy_type/conventional/"
        "is_collapsed/0/snippet_type/website/"
    ),
}
OUTDIR = Path("/private/tmp/propai-fnd-vBkmT9")


def _summarize_html(plan_id: str, url: str, final_url: str, html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    unit_tokens = sorted(
        set(
            re.findall(
                r"(?:unit(?:_space)?(?:%5B|\[)?id(?:%5D|\])?|unit(?:\s|&nbsp;)*(?:#|number))"
                r"[^A-Za-z0-9]{0,20}([A-Za-z0-9-]{2,24})",
                html,
                re.IGNORECASE,
            )
        )
    )
    dollar_values = sorted(set(re.findall(r"\$\s*([1-9][0-9,]{2,6})", text)))
    available_dates = sorted(
        set(
            re.findall(
                r"(?:available|move[- ]?in)[^0-9]{0,30}"
                r"((?:0?[1-9]|1[0-2])[/.-](?:0?[1-9]|[12][0-9]|3[01])[/.-](?:20)?\d{2})",
                text,
                re.IGNORECASE,
            )
        )
    )
    return {
        "plan_id": plan_id,
        "requested_url": url,
        "final_url": final_url,
        "same_property_host": urlsplit(final_url).hostname == "standrews.scullycompany.com",
        "bytes": len(html.encode("utf-8")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "unit_tokens": unit_tokens[:40],
        "positive_rents": dollar_values[:40],
        "available_dates": available_dates[:40],
        "text_prefix": text[:500],
    }


async def main() -> None:
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False
    assert options["useProxy"] is True

    session = _HbSession("render")
    page = None
    network_log: list[dict[str, object]] = []
    details: list[dict[str, object]] = []
    try:
        page = await session.open()
        _install_block(page)
        _install_capture(page, network_log)
        await asyncio.sleep(0)

        await page.goto(ROOT_URL, wait_until="domcontentloaded", timeout=40_000)
        await asyncio.sleep(2)
        for plan_id, url in DETAIL_URLS.items():
            before = len(network_log)
            await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
            await asyncio.sleep(4)
            html = await page.content()
            raw_path = OUTDIR / f"hb_scully_8119_plan_{plan_id}.html"
            raw_path.write_text(html, encoding="utf-8")
            summary = _summarize_html(plan_id, url, page.url, html)
            summary["network_responses"] = len(network_log) - before
            summary["network_urls"] = [
                str(entry.get("url") or "") for entry in network_log[before:]
            ]
            summary["raw_path"] = str(raw_path)
            details.append(summary)
    finally:
        await session.close()

    artifact = {
        "property_id": int(PROPERTY_ID),
        "hyperbrowser_sessions": 1,
        "compliance_options": options,
        "details": details,
        "network_log": network_log,
    }
    artifact_path = OUTDIR / "hb_scully_8119_detail3_one_session.json"
    artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps({**artifact, "network_log": []}, indent=2))


asyncio.run(main())
