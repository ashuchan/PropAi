#!/usr/bin/env python3
"""One-session Entrata route drill for high-value residual properties.

The seed for each target is either the exact current conventional grid or the
canonical property URL/profile entry.  At most one Hyperbrowser session is
created per property.  Within that session, only page-published exact-property
routes and same-origin plan/VUS URLs are fetched.  CAPTCHA solving is hard off.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.hyperbrowser_backend import (
    _HbSession,
    _NAV_TIMEOUT_MS,
    _hb_try_reserve_property,
    _session_options,
)
from ma_poc.pms.adapters.entrata import (
    _find_pp_conventional_index,
    _extract_vus_urls,
    find_entrata_pp_plan_links,
    parse_entrata_available_units,
    parse_entrata_floorplan_html_jsonld,
    parse_entrata_modern_units_data,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_pp_unit_cards,
    parse_prospectportal_unit_spaces,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
PROPERTIES = Path("ma_poc/config/properties.csv")
OUTPUT = OUT / "evidence_entrata_residual_followup_hb_strict.json"
TARGETS = {
    "19877": "http://www.adveniratmagnolia.com/",
    "22552": "https://www.lavinaapts.com/livermore/la-vina/conventional/",
    "46257": (
        "https://www.advenirliving.com/mayfield/midland/"
        "advenir-at-mayfield/conventional/"
    ),
    "54798": "https://thepointatabington.prospectportal.com/",
}
PARSERS = (
    parse_entrata_pp_unit_cards,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_modern_units_data,
    parse_entrata_floorplan_html_jsonld,
    parse_entrata_available_units,
)
RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)
INVENTORY_MARKERS = (
    "fp-card",
    "fp-group-item",
    "view_unit_spaces",
    "unitsdata",
    "jd-fp-unit-card",
)
MAX_LINKS = 30
FETCH_JS = """async (args) => {
  try {
    const response = await fetch(args.path, {
      credentials: 'include',
      headers: args.xhr
        ? {'Accept': 'text/html, */*; q=0.01', 'X-Requested-With': 'XMLHttpRequest'}
        : {'Accept': 'text/html,application/xhtml+xml'}
    });
    const body = await response.text();
    return {status: response.status, body: body};
  } catch (e) {
    return {status: 0, body: ''};
  }
}"""


def canonical_rows() -> dict[str, dict[str, str]]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row["apartmentid"]: row
            for row in csv.DictReader(handle)
            if row.get("apartmentid") in TARGETS
        }


def normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def same_origin(left_url: str, right_url: str) -> bool:
    left = urlsplit(left_url)
    right = urlsplit(right_url)
    return bool(
        left.scheme.casefold() == right.scheme.casefold()
        and (left.hostname or "").casefold() == (right.hostname or "").casefold()
        and left.port == right.port
        and left.username is None
        and left.password is None
    )


def challenge(html: str, title: str = "") -> bool:
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


def identity(html: str, meta: dict[str, str], url: str) -> dict[str, Any]:
    text = normalized(html)
    words = set(text.split())
    name_tokens = [
        token
        for token in normalized(meta.get("name") or "").split()
        if token not in {"the", "apartments", "apartment", "at", "on", "of"}
    ]
    address = normalized(meta.get("address") or "")
    street_number = (address.split() or [""])[0]
    zip_code = normalized(meta.get("zip") or "")
    name_match = bool(name_tokens) and all(token in words for token in name_tokens)
    address_match = bool(address) and address in text
    street_zip_match = bool(street_number and zip_code) and (
        street_number in words and zip_code in words
    )
    url_words = set(normalized(url).split())
    url_overlap = sum(token in url_words for token in name_tokens)
    return {
        "canonical_name": meta.get("name") or "",
        "canonical_address": meta.get("address") or "",
        "canonical_city_state_zip": " ".join(
            str(meta.get(key) or "") for key in ("city", "state", "zip")
        ).strip(),
        "name_match": name_match,
        "full_address_match": address_match,
        "street_number_and_zip_match": street_zip_match,
        "url_name_token_overlap": url_overlap,
        "pass": bool((address_match or street_zip_match) and (name_match or url_overlap)),
    }


def positive_rent(row: dict[str, Any]) -> bool:
    for key in RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


def native_map(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for anchor in soup.select("a.unit-button"):
        uid_value = anchor.get("data-unit") or anchor.get("rel") or ""
        if isinstance(uid_value, list):
            uid_value = uid_value[0] if uid_value else ""
        uid = str(uid_value).strip()
        parent = anchor.find_parent(class_="unit-row-wrapper") or anchor.find_parent(
            class_="unit-row"
        )
        unit = ""
        if parent is not None:
            node = parent.select_one(".unit-col.unit .unit-col-text")
            unit = node.get_text(strip=True) if node else ""
        if unit and uid:
            out[unit] = uid
    return out


def augment_vus_rows(
    rows: list[dict[str, Any]],
    html: str,
    url: str,
) -> list[dict[str, Any]]:
    ids = native_map(html)
    query = parse_qs(urlsplit(url).query)
    property_id = (query.get("property[id]") or [""])[0]
    floorplan_id = (query.get("property_floorplan[id]") or [""])[0]
    if not property_id or not floorplan_id:
        return []
    out = []
    for row in rows:
        unit = str(row.get("unit_number") or "")
        uid = ids.get(unit, "")
        if not uid:
            continue
        copy = dict(row)
        copy["source_ids"] = {
            "entrata_uid": uid,
            "entrata_fpid": floorplan_id,
            "entrata_property_id": property_id,
        }
        out.append(copy)
    return out


def strict_rows(rows: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    seen_uids: set[str] = set()
    for row in rows:
        unit = str(row.get("unit_number") or "").strip()
        source = str(row.get("source_api_url") or "")
        source_ids = row.get("source_ids")
        uid = str(source_ids.get("entrata_uid") or "") if isinstance(source_ids, dict) else ""
        fpid = str(source_ids.get("entrata_fpid") or "") if isinstance(source_ids, dict) else ""
        if (
            not unit
            or unit in seen_units
            or not uid
            or uid in seen_uids
            or not fpid
            or not unit_has_real_anchor(row)
            or not positive_rent(row)
            or not source
            or not same_origin(source, origin)
        ):
            continue
        seen_units.add(unit)
        seen_uids.add(uid)
        out.append(row)
    return out


async def navigate(page: Any, url: str) -> tuple[str, str, str, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 3):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            await asyncio.sleep(5)
            html = await page.content()
            title = str(await page.title() or "")
            final = str(getattr(page, "url", "") or url)
            is_challenge = challenge(html, title)
            accepted = bool(html and not is_challenge)
            attempts.append(
                {
                    "attempt": attempt,
                    "final_url": final,
                    "title": title,
                    "body_bytes": len(html.encode("utf-8", "replace")),
                    "body_sha256": hashlib.sha256(
                        html.encode("utf-8", "replace")
                    ).hexdigest(),
                    "challenge_detected": is_challenge,
                    "accepted": accepted,
                }
            )
            if accepted:
                return html, final, title, attempts
        except Exception as exc:
            attempts.append(
                {"attempt": attempt, "error": type(exc).__name__, "accepted": False}
            )
    return "", "", "", attempts


async def inpage_fetch(page: Any, url: str, origin: str, xhr: bool) -> dict[str, Any]:
    if not same_origin(url, origin):
        return {"url": url, "status": 0, "body": "", "same_origin": False}
    parsed = urlsplit(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        response = await page.evaluate(FETCH_JS, {"path": path, "xhr": xhr})
    except Exception as exc:
        return {
            "url": url,
            "status": 0,
            "body": "",
            "same_origin": True,
            "error": type(exc).__name__,
        }
    body = str((response or {}).get("body") or "")
    return {
        "url": url,
        "status": int((response or {}).get("status") or 0),
        "body": body,
        "body_bytes": len(body.encode("utf-8", "replace")),
        "body_sha256": hashlib.sha256(body.encode("utf-8", "replace")).hexdigest(),
        "same_origin": True,
    }


async def audit_one(
    property_id: str,
    seed_url: str,
    meta: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        if not _hb_try_reserve_property(property_id):
            return {
                "property_id": int(property_id),
                "property_name": meta["name"],
                "outcome": "SESSION_CAP_EXHAUSTED",
                "session_calls": 0,
            }
        session = _HbSession(mode="render")
        try:
            page = await session.open()
            seed_html, seed_final, seed_title, seed_attempts = await navigate(page, seed_url)
            if not seed_html:
                return {
                    "property_id": int(property_id),
                    "property_name": meta["name"],
                    "seed_url": seed_url,
                    "outcome": "SEED_BLOCKED_OR_EMPTY",
                    "session_calls": 1,
                    "seed_navigation_attempts": seed_attempts,
                }
            seed_identity = identity(seed_html, meta, seed_final)
            if not seed_identity["pass"]:
                return {
                    "property_id": int(property_id),
                    "property_name": meta["name"],
                    "seed_url": seed_url,
                    "outcome": "SEED_PROPERTY_IDENTITY_UNPROVEN",
                    "session_calls": 1,
                    "seed_final_url": seed_final,
                    "seed_navigation_attempts": seed_attempts,
                    "seed_identity": seed_identity,
                }

            index_html = seed_html
            index_url = seed_final
            index_title = seed_title
            index_attempts: list[dict[str, Any]] = []
            if not any(marker in index_html.casefold() for marker in INVENTORY_MARKERS):
                candidates = _find_pp_conventional_index(index_html, index_url)
                exact = []
                name_words = [
                    token
                    for token in normalized(meta["name"]).split()
                    if token not in {"the", "apartments", "apartment", "at", "on", "of"}
                ]
                for candidate in candidates:
                    candidate_text = normalized(candidate)
                    if any(token in candidate_text.split() for token in name_words):
                        exact.append(candidate)
                exact = list(dict.fromkeys(exact))
                if len(exact) == 1:
                    index_html, index_url, index_title, index_attempts = await navigate(
                        page, exact[0]
                    )
            if not index_html:
                return {
                    "property_id": int(property_id),
                    "property_name": meta["name"],
                    "seed_url": seed_url,
                    "outcome": "INDEX_BLOCKED_OR_EMPTY",
                    "session_calls": 1,
                    "seed_final_url": seed_final,
                    "seed_navigation_attempts": seed_attempts,
                    "index_navigation_attempts": index_attempts,
                }
            index_identity = identity(index_html, meta, index_url)
            if not index_identity["pass"]:
                return {
                    "property_id": int(property_id),
                    "property_name": meta["name"],
                    "seed_url": seed_url,
                    "outcome": "INDEX_PROPERTY_IDENTITY_UNPROVEN",
                    "session_calls": 1,
                    "seed_final_url": seed_final,
                    "index_url": index_url,
                    "index_identity": index_identity,
                }

            plan_links = [
                link
                for link in find_entrata_pp_plan_links(index_html, index_url)
                if same_origin(link, index_url)
            ][:MAX_LINKS]
            vus_links = [
                link
                for _, link in _extract_vus_urls([(index_url, index_html)], index_url)
                if same_origin(link, index_url)
            ][:MAX_LINKS]
            rows: list[dict[str, Any]] = []
            fetches: list[dict[str, Any]] = []
            for url in vus_links:
                response = await inpage_fetch(page, url, index_url, True)
                body = response.pop("body", "")
                parsed = (
                    parse_prospectportal_unit_spaces(body, url)
                    if response["status"] == 200 and body and not challenge(body)
                    else []
                )
                augmented = augment_vus_rows(parsed, body, url)
                response["parsed_rows"] = len(parsed)
                response["strict_native_id_rows"] = len(augmented)
                fetches.append(response)
                rows.extend(augmented)
            for url in plan_links:
                response = await inpage_fetch(page, url, index_url, False)
                body = response.pop("body", "")
                parsed_rows: list[dict[str, Any]] = []
                parser_counts: dict[str, int] = {}
                if response["status"] == 200 and body and not challenge(body):
                    for parser in PARSERS:
                        parsed = parser(body, url)
                        parser_counts[parser.__name__] = len(parsed)
                        parsed_rows.extend(parsed)
                response["parser_counts"] = parser_counts
                response["parsed_rows"] = len(parsed_rows)
                fetches.append(response)
                rows.extend(parsed_rows)
            strict = strict_rows(rows, index_url)
            outcome = (
                "STRICT_UNIT_QUALIFIED"
                if strict
                else "NO_NATIVE_POSITIVE_RENT_ROWS"
            )
            return {
                "property_id": int(property_id),
                "property_name": meta["name"],
                "website": meta["website"],
                "seed_url": seed_url,
                "seed_final_url": seed_final,
                "seed_title": seed_title,
                "seed_navigation_attempts": seed_attempts,
                "seed_identity": seed_identity,
                "index_url": index_url,
                "index_title": index_title,
                "index_navigation_attempts": index_attempts,
                "index_identity": index_identity,
                "outcome": outcome,
                "session_calls": 1,
                "session_options": _session_options("render"),
                "property_identity_match": True,
                "published_plan_links": plan_links,
                "published_vus_links": vus_links,
                "fetches": fetches,
                "native_identity_rows": len(strict),
                "native_positive_rent_rows": len(strict),
                "contamination_verdict": (
                    "pass_exact_property_published_same_origin_native_ids_positive_rents"
                    if strict
                    else "strict_gate_not_satisfied"
                ),
                "source_urls": sorted(
                    {str(row.get("source_api_url") or "") for row in strict}
                ),
                "native_rows": json.loads(json.dumps(strict, default=str)),
            }
        finally:
            await session.close()


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = canonical_rows()
    semaphore = asyncio.Semaphore(2)
    tasks = [
        asyncio.create_task(audit_one(pid, url, metadata[pid], semaphore))
        for pid, url in TARGETS.items()
    ]
    results: list[dict[str, Any]] = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "property_id",
                        "property_name",
                        "outcome",
                        "native_positive_rent_rows",
                        "session_calls",
                    )
                }
            ),
            flush=True,
        )
    results.sort(key=lambda row: int(row["property_id"]))
    qualified = [row for row in results if row.get("outcome") == "STRICT_UNIT_QUALIFIED"]
    summary = {
        "result_type": "one_session_entrata_residual_followup_strict_audit",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "target_properties": len(results),
        "sessions_used": sum(int(row.get("session_calls") or 0) for row in results),
        "strict_unit_qualified_properties": len(qualified),
        "strict_unit_qualified_property_ids": [row["property_id"] for row in qualified],
        "native_positive_rent_rows": sum(
            int(row.get("native_positive_rent_rows") or 0) for row in qualified
        ),
        "captcha_solving": False,
    }
    OUTPUT.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
