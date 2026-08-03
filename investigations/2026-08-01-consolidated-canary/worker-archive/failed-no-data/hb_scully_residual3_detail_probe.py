from __future__ import annotations

import asyncio
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ma_poc.fetch.hyperbrowser_backend import (
    _HbSession,
    _install_block,
    _session_options,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/hb_scully_residual3_detail_probe")
TARGETS = {
    "43995": {
        "name": "Hamilton Hall",
        "host": "hamiltonhall.scullycompany.com",
        "entrata_property_id": "100003046",
        "url": (
            "https://hamiltonhall.scullycompany.com/norristown/hamilton-hall/"
            "?is_responsive_snippet=1&snippet_type=website&occupancy_type=1"
            "&move_in_date=08/01/2026&is_collapsed=0"
            "&include_paragraph_content=1&host_domain=www.scullycompany.com"
        ),
    },
    "60141": {
        "name": "Bridgeview",
        "host": "bridgeview.scullycompany.com",
        "entrata_property_id": "100002842",
        "url": (
            "https://bridgeview.scullycompany.com/allentown/bridgeview/"
            "?is_responsive_snippet=1&snippet_type=website&occupancy_type=1"
            "&move_in_date=08/01/2026&is_collapsed=0"
            "&include_paragraph_content=1&host_domain=www.scullycompany.com"
        ),
    },
    "63191": {
        "name": "Avenir",
        "host": "avenir.scullycompany.com",
        "entrata_property_id": "100002834",
        "url": (
            "https://avenir.scullycompany.com/philadelphia/avenir/"
            "?is_responsive_snippet=1&snippet_type=website&occupancy_type=1"
            "&move_in_date=08/01/2026&is_collapsed=0"
            "&include_paragraph_content=1&host_domain=www.scullycompany.com"
        ),
    },
}


def summarize(html: str, requested_url: str, final_url: str, host: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    source = html.replace("&nbsp;", " ")
    unit_tokens = sorted(
        set(
            re.findall(
                r"(?:unit(?:_space)?(?:%5B|\[)?id(?:%5D|\])?|"
                r"unit(?:\s|&nbsp;)*(?:#|number))"
                r"[^A-Za-z0-9]{0,24}([A-Za-z0-9-]{2,32})",
                source,
                re.IGNORECASE,
            )
        )
    )
    dollar_values = sorted(set(re.findall(r"\$\s*([1-9][0-9,]{2,6})", text)))
    detail_links = []
    for anchor in soup.select("a[href]"):
        href = urljoin(final_url, anchor.get("href") or "")
        if "/Apartments/module/property_floorplans/" in href and href not in detail_links:
            detail_links.append(href)
    return {
        "requested_url": requested_url,
        "final_url": final_url,
        "same_property_host": (urlsplit(final_url).hostname or "").casefold()
        == host.casefold(),
        "bytes": len(html.encode("utf-8")),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "cloudflare_challenge": "Just a moment" in text or "cf-chl-" in html,
        "detail_links": detail_links,
        "unit_tokens": unit_tokens[:100],
        "positive_rents": dollar_values[:100],
        "text_prefix": text[:1000],
    }


async def probe(pid: str, target: dict, options: dict) -> dict:
    session = _HbSession("render")
    page = None
    result = {
        "property_id": int(pid),
        "property_name": target["name"],
        "expected_host": target["host"],
        "entrata_property_id": target["entrata_property_id"],
        "hyperbrowser_sessions": 1,
        "session_options": options,
        "root": {},
        "details": [],
        "error": "",
    }
    try:
        page = await session.open()
        _install_block(page)
        response = await page.goto(target["url"], wait_until="domcontentloaded", timeout=40_000)
        await asyncio.sleep(3)
        html = await page.content()
        root_summary = summarize(html, target["url"], page.url, target["host"])
        root_summary["status"] = getattr(response, "status", None)
        result["root"] = root_summary
        links = root_summary["detail_links"][:12]
        for index, link in enumerate(links):
            response = await page.goto(link, wait_until="domcontentloaded", timeout=40_000)
            await asyncio.sleep(3)
            detail_html = await page.content()
            detail = summarize(detail_html, link, page.url, target["host"])
            detail["status"] = getattr(response, "status", None)
            match = re.search(r"property_floorplan(?:%5B|\[)id(?:%5D|\])?/([0-9]+)", link)
            detail["plan_id"] = match.group(1) if match else str(index)
            raw = ROOT / f"{pid}_plan_{detail['plan_id']}.html.gz"
            raw.write_bytes(gzip.compress(detail_html.encode("utf-8")))
            detail["raw_path"] = str(raw)
            result["details"].append(detail)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
    finally:
        await session.close()
    return result


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    options = _session_options("render")
    assert options["solveCaptchas"] is False
    assert options["useStealth"] is False
    results = await asyncio.gather(
        *(probe(pid, target, options) for pid, target in TARGETS.items())
    )
    artifact = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "lane": "scully_entrata_residual_three_property_detail_probe",
        "guardrails": {
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "llm": False,
            "paid_canary": False,
        },
        "results": results,
    }
    output = ROOT / "summary.json"
    output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps(artifact, indent=2, sort_keys=True))


asyncio.run(main())
