from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ma_poc.pms.detector import detect_pms


COHORT = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_remaining_current.csv")
ROOT = Path("/private/tmp/propai-fnd-vBkmT9/remaining113_direct_scan")
MAX_BYTES = 2_000_000
CONCURRENCY = 12
PROVIDER_RE = re.compile(
    r"(?:securecafe|rentcafe|entrata|prospectportal|appfolio|implicity|resman|"
    r"rentmanager|iloveleasing|showmojo|nestiolistings|on-site\.com|"
    r"rentfunnel|funnelleasing|knockrentals|knockcrm|sightmap|g5|betternoi|"
    r"doorloop|rently|mriprospectconnect|yottareal|leaseleads|marketapts|"
    r"realpage|onesite|aspen(?:square)?|yardi)",
    re.IGNORECASE,
)
PATH_RE = re.compile(
    r"(?:availab|floor[-_ ]?plans?|apartments?|units?|apply|leasing|listings?)",
    re.IGNORECASE,
)
NATIVE_MARKERS = (
    "data-unit-id",
    "data-unit-number",
    "unitrow_",
    '"unitnumber"',
    '"unit_number"',
    '"apartmentname"',
    '"apartmentid"',
    "data-aptid",
    "data-apartment-id",
)


def target_url(row: dict[str, str]) -> str:
    choices = (row.get("website", ""), row.get("property_name", ""))
    for raw in choices:
        raw = str(raw or "").strip()
        if raw.startswith(("http://", "https://")):
            return raw
        if "." in raw and " " not in raw and raw.lower() not in {"unknown", "none"}:
            return "https://" + raw
    return ""


async def read_limited(response: httpx.Response) -> bytes:
    out = bytearray()
    async for chunk in response.aiter_bytes():
        if len(out) + len(chunk) > MAX_BYTES:
            out.extend(chunk[: MAX_BYTES - len(out)])
            break
        out.extend(chunk)
    return bytes(out)


async def fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    row: dict[str, str],
) -> dict[str, object]:
    pid = str(row["property_id"])
    url = target_url(row)
    base: dict[str, object] = {
        "property_id": int(pid),
        "property_name": row.get("property_name", ""),
        "configured_url": url,
        "prior_disposition": row.get("prior_disposition", ""),
        "guardrails": {
            "direct_only": True,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "paid_canary": False,
        },
    }
    if not url:
        return base | {"status": 0, "error": "no_url"}
    try:
        async with sem, client.stream("GET", url) as response:
            body = await read_limited(response)
            final_url = str(response.url)
            status = response.status_code
    except Exception as exc:
        return base | {"status": 0, "error": f"{type(exc).__name__}:{str(exc)[:160]}"}

    path = ROOT / f"{pid}.html.gz"
    if body:
        with gzip.open(path, "wb") as handle:
            handle.write(body)
    text = body.decode("utf-8", "replace")
    low = text.casefold()
    links: list[str] = []
    try:
        soup = BeautifulSoup(text, "lxml")
        for node in soup.select("a[href], iframe[src], script[src], form[action]"):
            raw = str(
                node.get("href")
                or node.get("src")
                or node.get("action")
                or ""
            ).strip()
            if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute = urljoin(final_url, raw)
            if absolute not in links and (PROVIDER_RE.search(absolute) or PATH_RE.search(absolute)):
                links.append(absolute)
            if len(links) >= 60:
                break
    except Exception:
        pass
    try:
        detected = detect_pms(final_url, page_html=text)
        detected_pms = detected.pms
        detected_confidence = detected.confidence
    except Exception as exc:
        detected_pms = ""
        detected_confidence = 0.0
        base["detect_error"] = f"{type(exc).__name__}:{str(exc)[:120]}"
    return base | {
        "status": status,
        "final_url": final_url,
        "body_bytes": len(body),
        "truncated": len(body) >= MAX_BYTES,
        "sha256": hashlib.sha256(body).hexdigest(),
        "detected_pms": detected_pms,
        "detected_confidence": detected_confidence,
        "provider_marker": bool(PROVIDER_RE.search(text)),
        "native_marker_counts": {m: low.count(m) for m in NATIVE_MARKERS},
        "residence_header": bool(
            re.search(r"\bresidence\b", text, re.IGNORECASE)
            and re.search(r"\b(?:price|rent)\b", text, re.IGNORECASE)
        ),
        "candidate_urls": links,
        "raw_path": str(path) if body else "",
    }


async def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with COHORT.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    timeout = httpx.Timeout(25.0, connect=12.0)
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=headers,
        timeout=timeout,
        verify=True,
    ) as client:
        results = await asyncio.gather(*(fetch_one(client, sem, row) for row in rows))
    manifest = {
        "cohort": str(COHORT),
        "cohort_rows": len(rows),
        "guardrails": {
            "direct_only": True,
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "hyperbrowser": False,
            "llm": False,
            "paid_canary": False,
        },
        "results": results,
    }
    out = ROOT / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "rows": len(results),
                "http_200": sum(r.get("status") == 200 for r in results),
                "native_marker_pages": sum(
                    any((r.get("native_marker_counts") or {}).values()) for r in results
                ),
                "provider_marker_pages": sum(bool(r.get("provider_marker")) for r in results),
                "residence_header_pages": sum(bool(r.get("residence_header")) for r in results),
                "manifest": str(out),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
