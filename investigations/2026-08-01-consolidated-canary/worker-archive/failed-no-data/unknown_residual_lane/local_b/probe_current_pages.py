#!/usr/bin/env python3
"""Read-only current-page evidence for the local half of unknown residuals."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "unknown_residual_lane/local_b/current_page_probe.json"
TARGET_IDS = {
    "1765", "4756", "19245", "22964", "27349", "33993", "34708",
    "37071", "40733", "42977", "48389", "53567", "64068", "72732",
    "74523", "232583", "246962", "274886",
}
PROVIDER_TOKENS = (
    "avail", "unit", "floor", "lease", "apply", "portal", "knock",
    "doorway", "realpage", "onlineleasing", "rentcafe", "securecafe",
    "entrata", "prospectportal", "mriprospectconnect", "sightmap",
    "engrain", "appfolio", "rentmanager", "resman", "yottareal",
)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(value or "").lower()).strip()


def _read_targets() -> list[dict[str, str]]:
    with (ROOT / "strict_recovery_remaining_current.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        residual = {
            row["property_id"]: row
            for row in csv.DictReader(handle)
            if row.get("current_detected_adapter") == "unknown"
            and row.get("property_id") in TARGET_IDS
        }
    metadata: dict[str, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row.get("apartmentid") in TARGET_IDS:
                metadata[row["apartmentid"]] = row
    assert set(residual) == TARGET_IDS
    return [
        {
            **residual[pid],
            "canonical_name": metadata.get(pid, {}).get("name", "")
            or residual[pid].get("property_name", ""),
            "canonical_address": metadata.get(pid, {}).get("address", ""),
            "canonical_city": metadata.get(pid, {}).get("city", ""),
            "canonical_state": metadata.get(pid, {}).get("state", ""),
            "canonical_zip": metadata.get(pid, {}).get("zip", ""),
        }
        for pid in sorted(TARGET_IDS, key=int)
    ]


def _one(row: dict[str, str]) -> dict[str, object]:
    configured = (row.get("website") or "").strip()
    request_url = configured if "://" in configured else f"https://{configured}"
    base: dict[str, object] = {
        "property_id": int(row["property_id"]),
        "property_name": row.get("canonical_name") or "",
        "canonical_address": row.get("canonical_address") or "",
        "configured_url": configured,
        "request_url": request_url,
    }
    try:
        response = probe_get(request_url, timeout=40, unlocker=False, retries=1)
    except Exception as exc:
        return {
            **base,
            "status": None,
            "final_url": "",
            "body_bytes": 0,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "llm_calls": 0,
            "web_unlocker_calls": 0,
            "captcha_interactions": 0,
        }
    body = str(getattr(response, "text", "") or "")
    final_url = str(getattr(response, "url", "") or request_url)
    soup = BeautifulSoup(body, "html.parser")
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
    for tag in soup(["script", "style", "svg", "noscript"]):
        tag.decompose()
    visible = " ".join(soup.get_text(" ", strip=True).split())
    visible_key = _norm(visible)
    name_key = _norm(row.get("canonical_name") or "")
    name_core = [
        token for token in name_key.split()
        if token not in {"the", "at", "of", "apartments", "apartment", "homes", "home"}
    ]
    address_key = _norm(row.get("canonical_address") or "")
    address_tokens = address_key.split()
    street_no = address_tokens[0] if address_tokens else ""
    street_words = [
        token for token in address_tokens[1:]
        if token not in {
            "n", "s", "e", "w", "north", "south", "east", "west",
            "st", "street", "rd", "road", "ave", "avenue", "blvd",
            "boulevard", "dr", "drive", "ln", "lane", "ct", "court",
            "pkwy", "parkway", "pl", "place", "way", "se", "sw", "ne", "nw",
        }
    ]
    links: list[str] = []
    for match in re.finditer(
        r"(?:href|src|action)\s*=\s*[\"']([^\"']+)", body, re.IGNORECASE
    ):
        raw = html.unescape(match.group(1).strip())
        absolute = urljoin(final_url, raw)
        if any(token in absolute.lower() for token in PROVIDER_TOKENS):
            links.append(absolute)
    links = list(dict.fromkeys(links))[:80]
    marker_counts = {
        "knock_doorway_init": len(re.findall(r"knockDoorway\.init\s*\(", body, re.I)),
        "native_data_unitid": len(re.findall(r"data-unitid\s*=", body, re.I)),
        "native_unit_number_tokens": len(re.findall(r"(?:unit(?:number|_number|\s+#?)|apartment\s+#?)\s*[:=\"']", body, re.I)),
        "positive_currency_tokens": len(re.findall(r"\$\s*[1-9][0-9,]{2,}", visible)),
        "not_available_tokens": len(re.findall(r"\b(?:not available|no availability|unavailable)\b", visible, re.I)),
    }
    detector = detect_pms(final_url, page_html=body)
    final_host = (urlparse(final_url).hostname or "").lower()
    request_host = (urlparse(request_url).hostname or "").lower()
    return {
        **base,
        "status": int(getattr(response, "status_code", 0) or 0),
        "final_url": final_url,
        "request_host": request_host,
        "final_host": final_host,
        "same_or_subdomain_host": bool(
            final_host == request_host
            or final_host.endswith("." + request_host.removeprefix("www."))
            or request_host.endswith("." + final_host.removeprefix("www."))
        ),
        "body_bytes": len(body.encode()),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "title": title,
        "detected_adapter": detector.pms,
        "detector_confidence": detector.confidence,
        "name_visible": bool(
            name_key and (name_key in visible_key or (name_core and all(t in visible_key.split() for t in name_core)))
        ),
        "address_visible": bool(
            street_no and street_no in visible_key.split()
            and street_words and all(t in visible_key.split() for t in street_words)
        ),
        "city_state_zip_visible": all(
            _norm(row.get(key) or "") in visible_key
            for key in ("canonical_city", "canonical_state", "canonical_zip")
            if _norm(row.get(key) or "")
        ),
        "identity_snippets": [
            visible[max(0, match.start() - 120): match.start() + 320]
            for needle in filter(None, [row.get("canonical_name"), row.get("canonical_address")])
            for match in list(re.finditer(re.escape(str(needle)), visible, re.I))[:1]
        ],
        "provider_links": links,
        "marker_counts": marker_counts,
        "llm_calls": 0,
        "web_unlocker_calls": 0,
        "captcha_interactions": 0,
        "error": "",
    }


def main() -> None:
    rows = _read_targets()
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_one, row): row for row in rows}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps({
                key: result.get(key)
                for key in ("property_id", "status", "final_url", "detected_adapter", "name_visible", "address_visible")
            }), flush=True)
    results.sort(key=lambda item: int(item["property_id"]))
    payload = {
        "audit": "unknown residual current page probe local half",
        "targets": len(results),
        "policy": {
            "llm_calls": 0,
            "web_unlocker_calls": 0,
            "captcha_interactions": 0,
            "paid_canary": False,
        },
        "results": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"targets": len(results), "output": str(OUT)}))


if __name__ == "__main__":
    main()
