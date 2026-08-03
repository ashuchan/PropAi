from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
OUT = Path("/private/tmp/propai-fnd-vBkmT9/onesite_residual6_parallel")
sys.path.insert(0, str(REPO))

os.environ["HB_USE_STEALTH"] = "false"
os.environ["HB_USE_PROXY"] = "true"
os.environ["HB_ADBLOCK"] = "true"

from ma_poc.fetch.hyperbrowser_backend import (  # noqa: E402
    _HbSession,
    _install_block,
    _install_capture,
    _session_options,
)


URLS = [
    "https://www.southernpineapts.com/",
    "https://www.southernpineapts.com/floor-plans/",
    "https://southernpineapts.com/floor-plans/",
]
OUTCOME_PATTERNS = {
    "captcha": r"sgcaptcha|captcha|verify you are human|challenge-platform",
    "onesite": r"onesite|onlineleasing\.realpage|leasing\.realpage",
    "rentcafe": r"rentcafe|securecafe|yardi",
    "entrata": r"entrata|propertysolutions|prospectportal",
    "resman": r"resman|myresman",
    "knock": r"knockrentals|knockcrm",
    "funnel": r"funnelleasing|nestio",
    "appfolio": r"appfolio",
    "rentmanager": r"rentmanager|iloveleasing",
    "native_unit_number": r"(?:unit|apartment|apt)\s*(?:#|number|no\.?|id)?\s*([A-Za-z0-9-]{1,12})",
    "positive_rent": r"\$\s?([1-9][0-9,]{2,5})(?:\.\d{2})?",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def summarize(url: str, final_url: str, title: str, html: str, index: int) -> dict[str, object]:
    body = html.encode("utf-8", "replace")
    artifact = OUT / f"67154_hb_page_{index}.html.gz"
    artifact.write_bytes(gzip.compress(body, compresslevel=9))
    soup = BeautifulSoup(html, "html.parser")
    visible = " ".join(soup.stripped_strings)
    links = sorted({
        str(tag.get(attr))
        for tag in soup.find_all(True)
        for attr in ("href", "src", "action", "data-url", "data-src")
        if isinstance(tag.get(attr), str) and str(tag.get(attr)).strip()
    })
    provider_urls = [
        value for value in links
        if re.search(r"realpage|onesite|rentcafe|securecafe|yardi|entrata|resman|knock|funnel|nestio|appfolio|rentmanager", value, re.I)
    ]
    return {
        "requested_url": url,
        "final_url": final_url,
        "final_host": urlsplit(final_url).hostname or "",
        "title": title,
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "name_visible": "southern pine" in visible.lower(),
        "address_visible": "2520" in visible and "allie nicole" in visible.lower(),
        "markers": {
            key: bool(re.search(pattern, html, re.I))
            for key, pattern in OUTCOME_PATTERNS.items()
            if key not in {"native_unit_number", "positive_rent"}
        },
        "unit_number_candidates": sorted(set(re.findall(OUTCOME_PATTERNS["native_unit_number"], visible, re.I)))[:100],
        "positive_rents": sorted(set(re.findall(OUTCOME_PATTERNS["positive_rent"], visible, re.I)))[:100],
        "provider_urls": provider_urls[:100],
        "text_prefix": visible[:1000],
    }


async def main() -> None:
    options = _session_options("render")
    assert options == {
        "solveCaptchas": False,
        "useStealth": False,
        "useProxy": True,
        "adblock": True,
    }
    session = _HbSession("render")
    page = None
    network_log: list[dict[str, object]] = []
    pages: list[dict[str, object]] = []
    error = None
    try:
        page = await session.open()
        _install_block(page)
        _install_capture(page, network_log)
        for index, url in enumerate(URLS):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                await asyncio.sleep(5)
                html = await page.content()
                title = await page.title()
                pages.append(summarize(url, page.url, title, html, index))
            except Exception as exc:
                pages.append({
                    "requested_url": url,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await session.close()

    safe_network = []
    for row in network_log:
        safe_network.append({
            key: value for key, value in row.items()
            if key in {"url", "status", "content_type", "body", "method"}
        })
    evidence = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "property_id": 67154,
        "property_name": "Southern Pine Apartments",
        "canonical_address": "2520 Allie Nicole Cir, Virginia Beach, VA 23456",
        "hyperbrowser_sessions": 1 if session.session_id else 0,
        "session_options": options,
        "guardrails": {
            "captcha_solving": False,
            "basic_stealth": False,
            "fingerprint_rotation": False,
            "web_unlocker": False,
            "flaresolverr": False,
        },
        "error": error,
        "pages": pages,
        "network_log": safe_network,
    }
    artifact = OUT / "hb_southern_pine_clean_probe.json"
    artifact.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "sessions": evidence["hyperbrowser_sessions"],
        "error": error,
        "pages": pages,
        "network_count": len(safe_network),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
