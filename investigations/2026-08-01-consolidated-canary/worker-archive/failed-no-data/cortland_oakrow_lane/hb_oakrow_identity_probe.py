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
OUT = Path("/private/tmp/propai-fnd-vBkmT9/cortland_oakrow_lane")
sys.path.insert(0, str(REPO))

# Clean residential rendering only. CAPTCHA solving is hard-disabled in the
# backend; disabling basic stealth here also makes the evidence self-auditing.
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
    "https://cortland.com/apartments/cortland-north-dallas/",
    "https://www.oakrowdallas.com/",
    "https://www.oakrowdallas.com/floorplans/",
    "https://www.oakrowdallas.com/floorplans/dallas-TX/oak-row-north-dallas/a2-1157677-1/",
    "https://www.oakrowdallas.com/floorplans/dallas-TX/oak-row-north-dallas/a15-1157682-1/",
    "https://www.oakrowdallas.com/floorplans/dallas-TX/oak-row-north-dallas/b3-1157673-1/",
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def summarize(requested_url: str, final_url: str, title: str, html: str, index: int) -> dict[str, object]:
    body = html.encode("utf-8", "replace")
    artifact = OUT / f"25489_hb_page_{index}.html.gz"
    artifact.write_bytes(gzip.compress(body, compresslevel=9))
    soup = BeautifulSoup(html, "lxml")
    visible = " ".join(soup.stripped_strings)
    normalized = normalize(visible)
    links = sorted(
        {
            str(tag.get(attr)).strip()
            for tag in soup.find_all(True)
            for attr in ("href", "src", "action", "data-url", "data-src")
            if isinstance(tag.get(attr), str) and str(tag.get(attr)).strip()
        }
    )
    exact_plan_links = [
        value
        for value in links
        if re.search(
            r"/floorplans/dallas-TX/oak-row-north-dallas/[a-z0-9-]+-\d{2,9}-\d+/?(?:[?#].*)?$",
            value,
            re.IGNORECASE,
        )
    ]
    foreign_plan_links = [
        value
        for value in links
        if "/floorplans/" in value.casefold()
        and re.search(r"/floorplans/[^/]+/[^/]+/", value, re.IGNORECASE)
        and "/floorplans/dallas-tx/oak-row-north-dallas/" not in value.casefold()
    ]
    unit_cards = soup.select(".unit-card")
    unit_numbers = []
    unit_uids = []
    for card in unit_cards:
        node = card.select_one(".unit-number")
        number = node.get_text(" ", strip=True) if node else ""
        if number:
            unit_numbers.append(number)
        uid = str(card.get("data-unit-id") or "").strip()
        if not uid:
            tagged = card.select_one("[data-unit-id], [data-uid], [data-uspid]")
            if tagged:
                uid = str(
                    tagged.get("data-unit-id")
                    or tagged.get("data-uid")
                    or tagged.get("data-uspid")
                    or ""
                ).strip()
        if uid:
            unit_uids.append(uid)
    challenge = bool(
        re.search(
            r"just a moment|verify you are human|cf-chl-|challenge-platform|captcha",
            html,
            re.IGNORECASE,
        )
    )
    return {
        "requested_url": requested_url,
        "final_url": final_url,
        "final_host": urlsplit(final_url).hostname or "",
        "title": title,
        "status_semantics": "challenge_or_access_barrier" if challenge else "rendered_page",
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "identity": {
            "oak_row_north_dallas_visible": "oak row north dallas" in normalized,
            "legacy_cortland_north_dallas_visible": "cortland north dallas" in normalized,
            "street_17811_vail_visible": "17811 vail" in normalized,
            "dallas_tx_visible": "dallas tx" in normalized,
            "zip_75287_visible": "75287" in normalized,
        },
        "exact_plan_link_count": len(exact_plan_links),
        "exact_plan_links": exact_plan_links[:40],
        "foreign_property_plan_links": foreign_plan_links[:40],
        "unit_card_count": len(unit_cards),
        "distinct_visible_unit_numbers": sorted(set(unit_numbers)),
        "distinct_native_unit_uids": sorted(set(unit_uids)),
        "text_prefix": visible[:1200],
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
    pages: list[dict[str, object]] = []
    network_log: list[dict[str, object]] = []
    error = None
    try:
        page = await session.open()
        _install_block(page)
        _install_capture(page, network_log)
        for index, url in enumerate(URLS):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                await asyncio.sleep(6)
                pages.append(
                    summarize(url, page.url, await page.title(), await page.content(), index)
                )
            except Exception as exc:  # noqa: BLE001
                pages.append(
                    {
                        "requested_url": url,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        await session.close()

    safe_network = [
        {k: row.get(k) for k in ("url", "status", "content_type", "method")}
        for row in network_log
        if isinstance(row, dict)
    ]
    evidence = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "property_id": 25489,
        "configured_property_name": "Cortland North Dallas",
        "configured_address": "17811 Vail St, Dallas, TX 75287",
        "current_property_name_expected": "Oak Row North Dallas",
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
    artifact = OUT / "hb_oakrow_identity_probe.json"
    artifact.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(artifact),
                "artifact_sha256": sha256(artifact.read_bytes()),
                "sessions": evidence["hyperbrowser_sessions"],
                "error": error,
                "pages": pages,
                "network_count": len(safe_network),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
