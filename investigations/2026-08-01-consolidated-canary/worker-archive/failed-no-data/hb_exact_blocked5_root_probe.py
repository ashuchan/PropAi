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
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
OUT = Path("/private/tmp/propai-fnd-vBkmT9/hb_exact_blocked5_root_probe")
sys.path.insert(0, str(REPO))

# A stable residential browser session only. The backend hard-disables CAPTCHA
# solving; these flags make the probe's compliance posture explicit and auditable.
os.environ["HB_USE_STEALTH"] = "false"
os.environ["HB_USE_PROXY"] = "true"
os.environ["HB_ADBLOCK"] = "true"

from ma_poc.fetch.hyperbrowser_backend import (  # noqa: E402
    _HbSession,
    _install_block,
    _install_capture,
    _session_options,
)


TARGETS = [
    {
        "property_id": 1617,
        "name": "Crossing at Riverlake",
        "address": "1500 River Lake Dr, Sacramento, CA 95831",
        "url": "https://www.crossingatriverlake.com/",
    },
    {
        "property_id": 16509,
        "name": "Brooklawn Gardens",
        "address": "301 N White Horse Pike, Lindenwold, NJ 08021",
        "url": "http://brooklawngardensapts.com/property/brooklawn/",
    },
    {
        "property_id": 40733,
        "name": "Country Club",
        "address": "1900 Country Club Rd, Lake Charles, LA 70605",
        "url": "https://www.smdproperty.com/resident-properties/country-club-apartments/",
    },
    {
        "property_id": 235473,
        "name": "Rooftop 252",
        "address": "252 N Front St, Wilmington, NC 28401",
        "url": "http://www.rooftop252.com/",
    },
    {
        "property_id": 24982,
        "name": "Fisher Building",
        "address": "343 S Dearborn St, Chicago, IL 60604",
        "url": "https://www.cityclubapartments.com/usa/chicago-il/downtown/fisher-building/",
    },
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def summarize(target: dict[str, object], final_url: str, title: str, html: str) -> dict[str, object]:
    property_id = int(target["property_id"])
    body = html.encode("utf-8", "replace")
    artifact = OUT / f"{property_id}_root.html.gz"
    artifact.write_bytes(gzip.compress(body, compresslevel=9))
    soup = BeautifulSoup(html, "lxml")
    visible = " ".join(soup.stripped_strings)
    normalized = normalize(visible)
    requested_url = str(target["url"])
    links: set[str] = set()
    for tag in soup.find_all(True):
        for attr in ("href", "src", "action", "data-url", "data-src"):
            value = tag.get(attr)
            if not isinstance(value, str) or not value.strip():
                continue
            absolute = urljoin(final_url or requested_url, value.strip())
            if absolute.startswith(("http://", "https://")):
                links.add(absolute)
    candidate_links = sorted(
        link
        for link in links
        if any(
            token in link.casefold()
            for token in (
                "avail",
                "unit",
                "floorplan",
                "floor-plan",
                "apply",
                "lease",
                "rentcafe",
                "securecafe",
                "entrata",
                "onesite",
                "realpage",
                "sightmap",
            )
        )
    )
    provider_markers = sorted(
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
            "nestio",
        )
        if marker in html.casefold()
    )
    unit_tokens = {
        token: len(re.findall(token, html, re.IGNORECASE))
        for token in (
            r"unit-number",
            r"unit_number",
            r"data-unit-id",
            r"ApartmentId",
            r"available now",
            r"availability",
        )
    }
    challenge = bool(
        re.search(
            r"just a moment|verify you are human|checking your browser|cf-chl-|captcha",
            html,
            re.IGNORECASE,
        )
    )
    name_tokens = [token for token in normalize(str(target["name"])).split() if len(token) > 3]
    address_tokens = [token for token in normalize(str(target["address"])).split() if len(token) > 3]
    return {
        "property_id": property_id,
        "configured_name": target["name"],
        "configured_address": target["address"],
        "requested_url": requested_url,
        "final_url": final_url,
        "final_host": urlsplit(final_url).hostname or "",
        "title": title,
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "challenge_or_captcha_marker": challenge,
        "identity_token_hits": {
            "name": {token: token in normalized for token in name_tokens},
            "address": {token: token in normalized for token in address_tokens},
        },
        "provider_markers": provider_markers,
        "unit_tokens": unit_tokens,
        "candidate_links": candidate_links[:120],
        "visible_text_prefix": visible[:1800],
    }


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
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
        for target in TARGETS:
            network_start = len(network_log)
            try:
                await page.goto(str(target["url"]), wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(6)
                row = summarize(target, page.url, await page.title(), await page.content())
                row["network_responses"] = len(network_log) - network_start
                row["network_urls"] = [
                    item.get("url")
                    for item in network_log[network_start:]
                    if isinstance(item, dict)
                    and any(
                        token in str(item.get("url") or "").casefold()
                        for token in ("avail", "unit", "floor", "lease", "api", "portal")
                    )
                ][:120]
                pages.append(row)
            except Exception as exc:  # noqa: BLE001
                pages.append(
                    {
                        "property_id": target["property_id"],
                        "configured_name": target["name"],
                        "requested_url": target["url"],
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        await session.close()

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "hyperbrowser_sessions": 1 if session.session_id else 0,
        "session_options": options,
        "guardrails": {
            "captcha_solving": False,
            "basic_stealth": False,
            "fingerprint_rotation": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "llm": False,
            "paid_canary": False,
        },
        "error": error,
        "pages": pages,
    }
    summary = OUT / "summary.json"
    summary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
