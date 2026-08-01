from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests


OUT = Path("/private/tmp/propai-fnd-vBkmT9/onesite_residual6_parallel")
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
sys.path.insert(0, str(REPO))

from ma_poc.pms.adapters.onesite import (  # noqa: E402
    _generate_xyz_token,
    parse_onesite_workflowstartup,
)


TARGETS: dict[str, dict[str, Any]] = {
    "14295": {
        "name": "Timber Ridge Apartment Homes",
        "address": "1025 Adams Cir, Boulder, CO 80303",
        "root": "https://www.myboulderapartment.com/",
        "paths": ["floorplans/", "floor-plans/", "availability/", "apartments/"],
        "site_ids": ["1101338"],
        "portal_urls": ["https://1736093.onlineleasing.realpage.com/"],
    },
    "38677": {
        "name": "Tor View Village",
        "address": "1 Kensington Cir, Garnerville, NY 10923",
        "root": "https://www.torviewvillageapts.com/",
        "paths": ["availability/", "floorplans/", "floor-plans/", "apartments/", "apply-now/"],
        "site_ids": ["1321537"],
        "portal_urls": ["https://property.onesite.realpage.com/welcomehome?siteId=1321537"],
    },
    "39995": {
        "name": "South Pointe",
        "address": "6220 N Murray Dr, Hanahan, SC 29410",
        "root": "https://www.southpointehanahan.com/",
        "paths": ["floor-plans", "floorplans/", "availability/", "apartments/"],
        "site_ids": ["5272798"],
        "portal_urls": ["https://9067331.onlineleasing.realpage.com/"],
    },
    "43520": {
        "name": "Park at Blanding",
        "address": "222 Blairmore Blvd E, Orange Park, FL 32073",
        "root": "https://theparkatblanding.com/",
        "configured_root": "http://www.parkatblanding.com/",
        "paths": ["Floor-Plans.aspx", "floor-plans/", "floorplans/", "availability/"],
        "site_ids": ["5586626"],
        "portal_urls": ["https://9259508.onlineleasing.realpage.com/"],
    },
    "67154": {
        "name": "Southern Pine Apartments",
        "address": "2520 Allie Nicole Cir, Virginia Beach, VA 23456",
        "root": "https://www.southernpineapts.com/",
        "alternate_root": "https://southernpineapts.com/",
        "paths": ["floor-plans/", "floorplans/", "availability/", "apartments/", "apply-now/"],
        "site_ids": ["5440661"],
        "portal_urls": ["https://9165349.onlineleasing.realpage.com/"],
    },
    "291774": {
        "name": "Gallatin Village",
        "address": "20190 Murphy Rd, Bend, OR 97702",
        "root": "https://www.123taylor.com/gallatin-village",
        "paths": ["availability", "floorplans", "single-family-homes", "gallatin-village/availability", "gallatin-village/floorplans"],
        "site_ids": ["4645221"],
        "portal_urls": ["https://8169363.onlineleasing.realpage.com/"],
    },
}

PROVIDER_PATTERNS = {
    "onesite_realpage": r"onesite|onlineleasing\.realpage|leasing\.realpage|c-leasestar-api\.realpage|cs-cdn\.realpage|realpage\.com/ollr",
    "rentcafe_yardi": r"rentcafe|securecafe|yardi|myresman",
    "entrata": r"entrata|propertysolutions|prospectportal",
    "knock": r"knockrentals|knockcrm|knock\.app",
    "funnel_nestio": r"funnelleasing|funnel\.leasing|nestio",
    "resman": r"resman|myresman",
    "appfolio": r"appfolio|appfoliowebsites",
    "rentmanager": r"rentmanager|rmxcdn|iloveleasing",
    "on_site": r"on-site\.com|on_site|online_app3",
    "sightmap": r"sightmap|engrain",
}

URL_RE = re.compile(r"https?://[^\"'<>\\\s]+", re.I)
SITE_ID_RE = re.compile(r"(?:siteId=|siteid%3[dD]|workflowstartup/v1/)(\d+)", re.I)
MONEY_RE = re.compile(r"\$\s?([1-9]\d{2,4})(?:\.\d{2})?")


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)[:130]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)
    try:
        response = requests.get(
            url,
            headers=request_headers,
            timeout=30,
            allow_redirects=True,
            impersonate="chrome116",
        )
        body = bytes(response.content or b"")
        return {
            "requested_url": url,
            "final_url": str(response.url),
            "status": response.status_code,
            "headers": {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower() in {"content-type", "location", "refresh", "server", "x-powered-by"}
            },
            "body": body,
            "error": None,
        }
    except Exception as exc:
        return {
            "requested_url": url,
            "final_url": "",
            "status": None,
            "headers": {},
            "body": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def inspect_html(pid: str, label: str, result: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    body = result.pop("body")
    artifact_name = f"{pid}_{slug(label)}_{slug(result['requested_url'])}.html.gz"
    artifact = OUT / artifact_name
    artifact.write_bytes(gzip.compress(body, compresslevel=9))
    text = body.decode("utf-8", "replace")
    soup = BeautifulSoup(text, "html.parser")
    visible = " ".join(soup.stripped_strings)
    extracted: set[str] = set(URL_RE.findall(text))
    for tag in soup.find_all(True):
        for attr in ("href", "src", "action", "data-url", "data-src", "data-href"):
            value = tag.get(attr)
            if isinstance(value, str) and value.strip():
                extracted.add(urljoin(result.get("final_url") or result["requested_url"], value.strip()))
    provider_urls = sorted(
        url for url in extracted
        if any(re.search(pattern, url, re.I) for pattern in PROVIDER_PATTERNS.values())
    )
    inventory_links = sorted(
        url for url in extracted
        if re.search(r"floor.?plan|availab|apartments?|units?|apply|lease|rent", url, re.I)
    )
    name_tokens = [tok.lower() for tok in re.findall(r"[A-Za-z0-9]+", target["name"]) if len(tok) >= 4]
    address_tokens = [tok.lower() for tok in re.findall(r"[A-Za-z0-9]+", target["address"]) if len(tok) >= 4]
    visible_lower = visible.lower()
    return {
        **result,
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "name_tokens_visible": sorted(tok for tok in name_tokens if tok in visible_lower),
        "address_tokens_visible": sorted(tok for tok in address_tokens if tok in visible_lower),
        "provider_markers": {
            provider: bool(re.search(pattern, text, re.I))
            for provider, pattern in PROVIDER_PATTERNS.items()
        },
        "published_site_ids": sorted(set(SITE_ID_RE.findall(text))),
        "provider_urls": provider_urls[:100],
        "inventory_links": inventory_links[:100],
        "positive_money_samples": sorted(set(int(v) for v in MONEY_RE.findall(visible)))[:50],
    }


def workflow_probe(pid: str, site_id: str, origin: str) -> dict[str, Any]:
    url = (
        "https://leasing.realpage.com/RP.Leasing.AppService.WebHost/"
        f"workflowstartup/v1/{site_id}/English?BpmId=OLL.WorkflowStartUp"
        f"&BpmSequence=0&LogSequence=3&ClientSessionID={uuid.uuid4()}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": origin.rstrip("/"),
        "Referer": origin.rstrip("/") + "/",
        "XYZ": _generate_xyz_token(site_id),
        "X-AuthToken": "",
        "X-Phased": "",
    }
    result = fetch(url, headers=headers)
    body = result.pop("body")
    artifact = OUT / f"{pid}_workflow_{site_id}.json.gz"
    artifact.write_bytes(gzip.compress(body, compresslevel=9))
    parsed: Any = None
    parse_error = None
    try:
        parsed = json.loads(body)
    except Exception as exc:
        parse_error = f"{type(exc).__name__}: {exc}"
    rows = parse_onesite_workflowstartup(parsed, url) if isinstance(parsed, dict) else []

    floorplans: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        stack: list[Any] = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "Floorplans" and isinstance(value, list):
                        floorplans.extend(v for v in value if isinstance(v, dict))
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)

    fp_summary = []
    for fp in floorplans:
        unit_ids = [str(v) for v in (fp.get("UnitIds") or []) if str(v).strip()]
        lo = fp.get("MinPriceRange") or fp.get("MinimumMarketRent") or 0
        hi = fp.get("MaxPriceRange") or fp.get("MaximumMarketRent") or 0
        fp_summary.append({
            "name": fp.get("Name") or fp.get("name") or "",
            "available_units": fp.get("AvailableUnits") or 0,
            "unit_ids": unit_ids,
            "min_rent": lo,
            "max_rent": hi,
        })

    native_rows = [
        row for row in rows
        if str(row.get("unit_number") or "").strip()
        and any(
            int(v or 0) > 0
            for v in (
                row.get("market_rent_low"),
                row.get("market_rent_high"),
                row.get("rent_min"),
                row.get("rent_max"),
            )
        )
    ]
    return {
        **result,
        "requested_url": re.sub(r"ClientSessionID=[^&]+", "ClientSessionID=<redacted>", result["requested_url"]),
        "body_bytes": len(body),
        "body_sha256": sha256(body),
        "artifact": str(artifact),
        "artifact_sha256": sha256(artifact.read_bytes()),
        "parse_error": parse_error,
        "floorplan_count": len(fp_summary),
        "floorplans": fp_summary,
        "adapter_row_count": len(rows),
        "native_positive_rent_row_count": len(native_rows),
        "native_positive_rent_rows": native_rows[:50],
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs: dict[Any, tuple[str, str, str]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for pid, target in TARGETS.items():
            roots = [target["root"]]
            for key in ("configured_root", "alternate_root"):
                if target.get(key):
                    roots.append(target[key])
            seen: set[str] = set()
            for root in roots:
                candidates = [(f"root_{len(seen)}", root)]
                candidates.extend((f"path_{path}", urljoin(root.rstrip("/") + "/", path)) for path in target["paths"])
                for label, url in candidates:
                    if url in seen:
                        continue
                    seen.add(url)
                    jobs[pool.submit(fetch, url)] = (pid, label, url)
            for i, url in enumerate(target["portal_urls"]):
                if url not in seen:
                    seen.add(url)
                    jobs[pool.submit(fetch, url)] = (pid, f"portal_{i}", url)

        page_results: dict[str, list[dict[str, Any]]] = {pid: [] for pid in TARGETS}
        for future in as_completed(jobs):
            pid, label, _ = jobs[future]
            page_results[pid].append(inspect_html(pid, label, future.result(), TARGETS[pid]))

    workflow_results: dict[str, list[dict[str, Any]]] = {}
    for pid, target in TARGETS.items():
        workflow_results[pid] = [
            workflow_probe(pid, site_id, target["root"])
            for site_id in target["site_ids"]
        ]

    output = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "guardrails": {
            "transport": "direct curl_cffi, one fixed chrome116 fingerprint",
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "paid_calls": 0,
        },
        "targets": {
            pid: {
                "metadata": target,
                "pages": sorted(page_results[pid], key=lambda row: row["requested_url"]),
                "workflows": workflow_results[pid],
            }
            for pid, target in TARGETS.items()
        },
    }
    output_path = OUT / "direct_route_probe.json"
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(output_path),
        "sha256": sha256(output_path.read_bytes()),
        "page_count": sum(len(v) for v in page_results.values()),
        "workflow_count": sum(len(v) for v in workflow_results.values()),
    }, indent=2))


if __name__ == "__main__":
    main()
