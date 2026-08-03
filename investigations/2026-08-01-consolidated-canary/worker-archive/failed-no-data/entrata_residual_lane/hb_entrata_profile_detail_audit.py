#!/usr/bin/env python3
"""One-session strict audit of exact Entrata profile detail URLs.

Each target URL was learned from a prior property-scoped profile as a winning
or known page-authored unit detail.  This helper opens at most one clean
Hyperbrowser session per target, never enables CAPTCHA solving, and fails
closed on redirects, property-identity mismatch, non-native rows, or missing
positive rent.
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
from urllib.parse import urlsplit

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.fetch.hyperbrowser_backend import (
    _HbSession,
    _NAV_TIMEOUT_MS,
    _hb_try_reserve_property,
    _session_options,
)
from ma_poc.pms.adapters.entrata import (
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
OUTPUT = OUT / "evidence_entrata_profile_details_hb_strict.json"
TARGETS = {
    "18764": (
        "https://www.americana-apts.com/floorplans/las-vegas-NV/"
        "apv-americana/studios-850303-1/"
    ),
    "35192": (
        "https://www.enclaveongoldentriangle.com/floorplans/fort-worth-TX/"
        "enclave-on-golden-triangle/the-baltic-776780-1/"
    ),
    "54936": (
        "https://brentwoodsquareapts.prospectportal.com/seattle/"
        "22-brentwood-square/floorplans/2-bed2-bath-839-1155597/"
        "fp_name/occupancy_type/conventional/"
    ),
    "257761": (
        "https://www.theemoryplano.com/floorplans/plano-TX/the-emory/"
        "parkwood-1159180-1/"
    ),
}
RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)
PARSERS = (
    parse_entrata_pp_unit_cards,
    parse_entrata_pp_jd_fp_cards,
    parse_entrata_modern_units_data,
    parse_entrata_floorplan_html_jsonld,
    parse_entrata_available_units,
    parse_prospectportal_unit_spaces,
)


def canonical_rows() -> dict[str, dict[str, str]]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("apartmentid") or ""): row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "") in TARGETS
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


def positive_rent(row: dict[str, Any]) -> bool:
    for key in RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


def strict_rows(rows: list[dict[str, Any]], target_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        unit_number = str(row.get("unit_number") or "").strip()
        building = str(row.get("building") or "").strip().casefold()
        source = str(row.get("source_api_url") or target_url)
        key = (building, unit_number.casefold())
        if (
            not unit_number
            or key in seen
            or not unit_has_real_anchor(row)
            or not positive_rent(row)
            or not same_origin(source, target_url)
        ):
            continue
        seen.add(key)
        out.append(row)
    return out


def identity_evidence(html: str, meta: dict[str, str], target_url: str) -> dict[str, Any]:
    text = normalized(html)
    address = normalized(meta.get("address") or "")
    name_tokens = [
        token
        for token in normalized(meta.get("name") or "").split()
        if token not in {"the", "apartments", "apartment", "at", "on", "of"}
    ]
    street_number = (address.split() or [""])[0]
    zip_code = normalized(meta.get("zip") or "")
    address_match = bool(address) and address in text
    street_zip_match = bool(street_number and zip_code) and (
        street_number in set(text.split()) and zip_code in set(text.split())
    )
    name_match = bool(name_tokens) and all(token in set(text.split()) for token in name_tokens)
    url_text = normalized(target_url)
    url_name_overlap = sum(token in set(url_text.split()) for token in name_tokens)
    return {
        "canonical_name": meta.get("name") or "",
        "canonical_address": meta.get("address") or "",
        "canonical_city_state_zip": " ".join(
            str(meta.get(key) or "") for key in ("city", "state", "zip")
        ).strip(),
        "name_match": name_match,
        "full_address_match": address_match,
        "street_number_and_zip_match": street_zip_match,
        "url_name_token_overlap": url_name_overlap,
        "pass": bool(
            (address_match or street_zip_match)
            and (name_match or url_name_overlap >= 1)
        ),
    }


def profile_provenance(property_id: str, target_url: str) -> dict[str, Any]:
    path = ROOT / "profiles" / f"{property_id}.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    navigation = profile.get("navigation") or {}
    known = [
        str(item.get("url_pattern") or "")
        for item in (profile.get("api_hints") or {}).get("known_endpoints", [])
        if isinstance(item, dict)
    ]
    winning = str(navigation.get("winning_page_url") or "")
    assert target_url == winning or target_url in known
    return {
        "profile_path": str(path),
        "profile_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "target_is_winning_url": target_url == winning,
        "target_is_known_endpoint": target_url in known,
        "last_unit_count": (profile.get("confidence") or {}).get("last_unit_count"),
        "last_quality": (profile.get("quality") or {}).get("last_quality_flag"),
    }


async def audit_one(
    property_id: str,
    target_url: str,
    meta: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        provenance = profile_provenance(property_id, target_url)
        if not _hb_try_reserve_property(property_id):
            return {
                "property_id": int(property_id),
                "property_name": meta.get("name") or "",
                "target_url": target_url,
                "outcome": "SESSION_CAP_EXHAUSTED",
                "session_calls": 0,
            }
        session = _HbSession(mode="render")
        attempts: list[dict[str, Any]] = []
        html = ""
        final_url = ""
        page_title = ""
        try:
            page = await session.open()
            for attempt in range(1, 3):
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=_NAV_TIMEOUT_MS,
                    )
                    await asyncio.sleep(5)
                    body = await page.content()
                    title = str(await page.title() or "")
                    observed_url = str(getattr(page, "url", "") or target_url)
                    is_challenge = challenge(body, title)
                    accepted = bool(
                        body
                        and not is_challenge
                        and same_origin(observed_url, target_url)
                    )
                    attempts.append(
                        {
                            "attempt": attempt,
                            "final_url": observed_url,
                            "title": title,
                            "body_bytes": len(body.encode("utf-8", "replace")),
                            "body_sha256": hashlib.sha256(
                                body.encode("utf-8", "replace")
                            ).hexdigest(),
                            "challenge_detected": is_challenge,
                            "accepted": accepted,
                        }
                    )
                    if accepted:
                        html = body
                        final_url = observed_url
                        page_title = title
                        break
                except Exception as exc:
                    attempts.append(
                        {
                            "attempt": attempt,
                            "error": type(exc).__name__,
                            "accepted": False,
                        }
                    )
            if not html:
                return {
                    "property_id": int(property_id),
                    "property_name": meta.get("name") or "",
                    "target_url": target_url,
                    "profile_provenance": provenance,
                    "outcome": "BLOCKED_EMPTY_OR_REDIRECTED",
                    "session_calls": 1,
                    "navigation_attempts": attempts,
                    "native_rows": [],
                }
            identity = identity_evidence(html, meta, target_url)
            parser_counts: dict[str, int] = {}
            raw_rows: list[dict[str, Any]] = []
            for parser in PARSERS:
                parsed = parser(html, target_url)
                parser_counts[parser.__name__] = len(parsed)
                raw_rows.extend(parsed)
            units = strict_rows(raw_rows, target_url) if identity["pass"] else []
            outcome = (
                "STRICT_UNIT_QUALIFIED"
                if units
                else "PROPERTY_IDENTITY_UNPROVEN"
                if not identity["pass"]
                else "NO_NATIVE_POSITIVE_RENT_ROWS"
            )
            return {
                "property_id": int(property_id),
                "property_name": meta.get("name") or "",
                "website": meta.get("website") or "",
                "target_url": target_url,
                "profile_provenance": provenance,
                "outcome": outcome,
                "session_calls": 1,
                "session_options": _session_options("render"),
                "navigation_attempts": attempts,
                "final_url": final_url,
                "page_title": page_title,
                "property_identity_match": identity["pass"],
                "identity_evidence": identity,
                "parser_counts": parser_counts,
                "native_identity_rows": len(units),
                "native_positive_rent_rows": len(units),
                "contamination_verdict": (
                    "pass_exact_profile_url_same_origin_identity_native_positive_rent"
                    if units
                    else "strict_gate_not_satisfied"
                ),
                "source_urls": sorted(
                    {str(row.get("source_api_url") or target_url) for row in units}
                ),
                "native_rows": json.loads(json.dumps(units, default=str)),
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
    qualified = [
        row for row in results if row.get("outcome") == "STRICT_UNIT_QUALIFIED"
    ]
    summary = {
        "result_type": "one_session_per_exact_profile_detail_hb_strict_audit",
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
