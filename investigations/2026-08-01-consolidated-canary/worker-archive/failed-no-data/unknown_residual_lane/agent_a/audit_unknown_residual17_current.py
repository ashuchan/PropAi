from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Hard-disable every paid/escalating fetch path for this audit process.
os.environ["WEB_UNLOCKER_KEY"] = ""
os.environ["PROBE_PROXY_URL"] = ""
os.environ["HYPERBROWSER_API_KEY"] = ""

from ma_poc.core.identity import SYNTHETIC_ID_PREFIXES, unit_has_real_anchor
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters import knock as knock_mod
from ma_poc.pms.adapters._probe import probe_get


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT_DIR = ROOT / "unknown_residual_lane" / "agent_a"
OUTPUT = OUT_DIR / "evidence_unknown_residual17_current_strict.json"
REJECTIONS_OUTPUT = OUT_DIR / "unknown_residual17_explicit_rejections.json"
NET_NEW_OUTPUT = OUT_DIR / "unknown_residual17_net_new_ids.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"

TARGETS = {
    1617, 4124, 16509, 22962, 24982, 32978, 34362, 34785, 39198,
    42554, 48075, 52541, 53932, 71962, 75314, 235473, 272772,
}

# Same exact property routes with only a scheme normalization. The configured
# HTTP Yotta route is 403 while HTTPS is its live SPA; Tamarron's configured
# HTTPS route has a mismatched certificate while the same host answers on HTTP.
ROUTE_OVERRIDES = {
    34362: "http://www.thetamarronapts.com/",
    34785: "https://adaraportal.yottareal.com/pages/HomePage.aspx?Id=55",
}

SOURCE_FILES = (
    Path("ma_poc/pms/scraper.py"),
    Path("ma_poc/pms/adapters/knock.py"),
    Path("ma_poc/pms/adapters/onesite.py"),
    Path("ma_poc/pms/adapters/generic.py"),
    Path("ma_poc/core/identity.py"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blobs() -> dict[str, str]:
    return {
        str(path): subprocess.check_output(
            ["git", "hash-object", str(path)], text=True
        ).strip()
        for path in SOURCE_FILES
    }


def _metadata() -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            raw_id = str(row.get("apartmentid") or "")
            if raw_id.isdigit() and int(raw_id) in TARGETS:
                rows[int(raw_id)] = row
    return rows


def _current_residual_rows() -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with REMAINING.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_id = str(row.get("property_id") or "")
            if raw_id.isdigit() and int(raw_id) in TARGETS:
                rows[int(raw_id)] = row
    return rows


def _ledger_ids() -> set[int]:
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["property_id"])
            for row in csv.DictReader(handle)
            if str(row.get("property_id") or "").isdigit()
        }


def _normalize_url(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _plain_text(body: str) -> str:
    without_scripts = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        body,
        flags=re.I | re.S,
    )
    return re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without_scripts))
    ).strip()


def _identity_evidence(row: dict[str, str], body: str) -> dict[str, Any]:
    text = _key(_plain_text(body))
    words = set(text.split())
    name_tokens = _key(row.get("name") or "").split()
    core_name_tokens = [
        token
        for token in name_tokens
        if token not in {"the", "apartments", "apartment", "homes", "living"}
    ]
    address_tokens = _key(row.get("address") or "").split()
    street_number = address_tokens[0] if address_tokens else ""
    street_words = [
        token
        for token in address_tokens[1:]
        if token
        not in {
            "n", "s", "e", "w", "north", "south", "east", "west", "st",
            "street", "rd", "road", "ave", "avenue", "blvd", "boulevard",
            "pkwy", "parkway", "dr", "drive", "ln", "lane", "ct", "court",
        }
        and not token.isdigit()
    ]
    canonical_zip = re.sub(r"\D", "", row.get("zip") or "").lstrip("0") or "0"
    visible_zip_tokens = {
        re.sub(r"\D", "", token).lstrip("0") or "0"
        for token in text.split()
        if re.fullmatch(r"\d{4,5}(?:-\d{4})?", token)
    }
    name_match = bool(
        core_name_tokens and all(token in words for token in core_name_tokens)
    )
    address_match = bool(
        street_number
        and street_number in words
        and street_words
        and all(token in words for token in street_words)
    )
    zip_match = canonical_zip in visible_zip_tokens
    return {
        "canonical_name": row.get("name") or "",
        "canonical_address": row.get("address") or "",
        "canonical_city": row.get("city") or "",
        "canonical_state": row.get("state") or "",
        "canonical_zip": row.get("zip") or "",
        "name_visible_core_normalized": name_match,
        "street_number_and_words_visible": address_match,
        "zip_visible_normalized": zip_match,
        "property_identity_match": bool(name_match and address_match and zip_match),
    }


def _provider_identity_match(
    row: dict[str, str], *, name: str, address: str, city: str, state: str, zip_code: str
) -> dict[str, Any]:
    canonical_name = _key(row.get("name") or "")
    provider_name = _key(name)
    canonical_name_tokens = [
        token
        for token in canonical_name.split()
        if token not in {"the", "apartments", "apartment", "homes", "living"}
    ]
    name_match = bool(
        canonical_name_tokens
        and all(token in provider_name.split() for token in canonical_name_tokens)
    )
    canonical_address = _key(row.get("address") or "")
    provider_address = _key(address)
    ignored = {
        "n", "s", "e", "w", "north", "south", "east", "west", "st",
        "street", "rd", "road", "ave", "avenue", "blvd", "boulevard",
        "pkwy", "parkway", "dr", "drive", "ln", "lane", "ct", "court",
    }
    address_tokens = [token for token in canonical_address.split() if token not in ignored]
    address_match = bool(
        address_tokens
        and all(token in provider_address.split() for token in address_tokens)
    )
    city_match = _key(row.get("city") or "") == _key(city)
    state_match = _key(row.get("state") or "") == _key(state)
    zip_match = (
        re.sub(r"\D", "", row.get("zip") or "").lstrip("0")
        == re.sub(r"\D", "", zip_code).lstrip("0")
    )
    return {
        "provider_name": name,
        "provider_address": address,
        "provider_city": city,
        "provider_state": state,
        "provider_zip": zip_code,
        "name_match": name_match,
        "address_match": address_match,
        "city_match": city_match,
        "state_match": state_match,
        "zip_match": zip_match,
        "property_identity_match": bool(
            name_match and address_match and city_match and state_match and zip_match
        ),
    }


def _positive_rent(unit: dict[str, Any]) -> bool:
    for field in (
        "market_rent_low", "market_rent_high", "rent_low", "rent_high",
        "asking_rent", "rent",
    ):
        value = unit.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _unit_anchor(unit: dict[str, Any]) -> str:
    uid = str(unit.get("unit_id") or "").strip()
    if uid and not uid.startswith(SYNTHETIC_ID_PREFIXES):
        return uid
    source_ids = unit.get("source_ids") if isinstance(unit.get("source_ids"), dict) else {}
    for key, value in source_ids.items():
        value = str(value or "").strip()
        if value:
            return f"{key}-{value}"
    return str(unit.get("unit_number") or "").strip()


def _sanitize_source_url(value: str) -> str:
    return re.sub(
        r"([?&]ClientSessionID=)[^&]+",
        r"\1<session-redacted>",
        str(value or ""),
        flags=re.I,
    )


def _units_gate(units: list[dict[str, Any]]) -> dict[str, Any]:
    native = [unit for unit in units if unit_has_real_anchor(unit)]
    qualified = [unit for unit in native if _positive_rent(unit)]
    anchors = [_unit_anchor(unit) for unit in qualified]
    property_ids = {
        str(unit.get("source_property_id") or "").strip()
        for unit in qualified
        if str(unit.get("source_property_id") or "").strip()
    }
    source_urls = sorted(
        {
            _sanitize_source_url(str(unit.get("source_api_url") or ""))
            for unit in qualified
            if unit.get("source_api_url")
        }
    )
    source_hosts = sorted(
        {urlparse(url).netloc.lower() for url in source_urls if urlparse(url).netloc}
    )
    duplicate_anchors = sorted(
        {anchor for anchor in anchors if anchor and anchors.count(anchor) > 1}
    )
    return {
        "rows": len(units),
        "rows_with_provider_native_id_or_number": len(native),
        "rows_with_native_identity_and_positive_rent": len(qualified),
        "distinct_native_anchors": len(set(anchors)),
        "duplicate_native_anchors": duplicate_anchors,
        "all_rows_native_priced": bool(
            units and len(units) == len(native) == len(qualified)
        ),
        "native_anchors_unique": bool(
            anchors and all(anchors) and len(anchors) == len(set(anchors))
        ),
        "source_property_ids": sorted(property_ids),
        "provider_boundary_single_property": bool(
            qualified and (len(property_ids) == 1 or (not property_ids and len(source_hosts) <= 1))
        ),
        "source_urls": source_urls,
        "source_hosts": source_hosts,
        "samples": [
            {
                "native_anchor": _unit_anchor(unit),
                "unit_number": str(unit.get("unit_number") or ""),
                "source_ids": unit.get("source_ids")
                if isinstance(unit.get("source_ids"), dict)
                else {},
                "source_property_id": str(unit.get("source_property_id") or ""),
                "source_api_url": _sanitize_source_url(
                    str(unit.get("source_api_url") or "")
                ),
                "floor_plan_name": str(unit.get("floor_plan_name") or ""),
                "positive_rent": next(
                    (
                        unit.get(field)
                        for field in (
                            "market_rent_low", "market_rent_high", "rent_low",
                            "rent_high", "asking_rent", "rent",
                        )
                        if isinstance(unit.get(field), (int, float))
                        and not isinstance(unit.get(field), bool)
                        and unit.get(field) > 0
                    ),
                    None,
                ),
                "availability_date": str(
                    unit.get("availability_date") or unit.get("available_date") or ""
                ),
                "availability_status": str(unit.get("availability_status") or ""),
            }
            for unit in qualified[:5]
        ],
    }


async def _fetch(url: str, timeout: int = 20) -> dict[str, Any]:
    try:
        response = await asyncio.to_thread(
            probe_get, url, timeout=timeout, unlocker=False, retries=1
        )
    except Exception as exc:
        return {
            "requested_url": url,
            "status": None,
            "final_url": "",
            "headers": {},
            "body": "",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    body = str(getattr(response, "text", "") or "")
    return {
        "requested_url": url,
        "status": int(getattr(response, "status_code", 0) or 0),
        "final_url": str(getattr(response, "url", "") or url),
        "headers": dict(getattr(response, "headers", {}) or {}),
        "body": body,
        "error": "",
    }


def _fetch_summary(capture: dict[str, Any]) -> dict[str, Any]:
    body = str(capture.get("body") or "")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return {
        "requested_url": capture.get("requested_url") or "",
        "status": capture.get("status"),
        "final_url": capture.get("final_url") or "",
        "body_bytes": len(body.encode()),
        "title": re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:200]
        if title_match
        else "",
        "error": capture.get("error") or "",
        "captcha_solving": False,
        "web_unlocker": False,
        "paid_proxy": False,
    }


def _fetch_result(capture: dict[str, Any]) -> FetchResult:
    status = capture.get("status")
    body = str(capture.get("body") or "")
    if status == 200 and body:
        outcome = FetchOutcome.OK
    elif status in {403, 429, 503}:
        outcome = FetchOutcome.BOT_BLOCKED
    elif status in {404, 410, 451}:
        outcome = FetchOutcome.DEAD_URL
    elif status is None:
        outcome = FetchOutcome.TRANSIENT
    else:
        outcome = FetchOutcome.HARD_FAIL
    return FetchResult(
        url=str(capture.get("requested_url") or ""),
        outcome=outcome,
        status=status,
        body=body.encode() if body else b"",
        headers=dict(capture.get("headers") or {}),
        render_mode=RenderMode.GET,
        final_url=str(capture.get("final_url") or capture.get("requested_url") or ""),
        attempts=1,
        elapsed_ms=0,
    )


async def _pipeline(
    row: dict[str, str], pid: int, capture: dict[str, Any], *, api_responses: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if capture.get("status") != 200 or not capture.get("body"):
        return {
            "run": False,
            "reason": "fetch_not_http_200_with_body",
            "detected_pms": None,
            "adapter": None,
            "tier": None,
            "units": 0,
            "plans": 0,
            "errors": [],
            "units_gate": _units_gate([]),
        }, []
    try:
        result = await scraper_mod.scrape(
            str(capture["requested_url"]),
            page=None,
            fetch_result=_fetch_result(capture),
            api_responses=api_responses,
            csv_row=row,
            property_id=str(pid),
            shared_budget={
                "llm_api_calls": 0,
                "llm_dom_calls": 0,
                "llm_monolithic": 0,
                "link_hop": 3,
                "_cost_cap_usd": 0,
            },
        )
    except Exception as exc:
        return {
            "run": True,
            "reason": "pipeline_exception",
            "exception": f"{type(exc).__name__}: {str(exc)[:300]}",
            "detected_pms": None,
            "adapter": None,
            "tier": None,
            "units": 0,
            "plans": 0,
            "errors": [],
            "units_gate": _units_gate([]),
        }, []
    units = list(result.get("units") or [])
    detected = result.get("_detected_pms") or {}
    return {
        "run": True,
        "reason": "completed",
        "detected_pms": detected.get("pms"),
        "detected_confidence": detected.get("confidence"),
        "detected_evidence": list(detected.get("evidence") or []),
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
        "units": len(units),
        "plans": len(result.get("plan_summaries") or []),
        "winning_url": result.get("_winning_page_url") or "",
        "fallback_chain": list(result.get("_fallback_chain") or []),
        "errors": list(result.get("errors") or [])[-10:],
        "llm_budget": 0,
        "units_gate": _units_gate(units),
    }, units


async def _fetch_json(url: str) -> tuple[int | None, dict[str, Any], str]:
    try:
        response = await asyncio.to_thread(
            probe_get,
            url,
            timeout=25,
            unlocker=False,
            retries=1,
            headers={"Accept": "application/json"},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        return status, response.json() if status == 200 else {}, ""
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {str(exc)[:300]}"


async def _knock_provider_identity(
    row: dict[str, str], body: str
) -> dict[str, Any]:
    _, _, community_id = knock_mod.find_knock_ids(body)
    if not community_id:
        return {"community_id": "", "property_identity_match": False}
    url = (
        "https://doorway-api.knockrentals.com/v1/property/community/"
        f"{community_id}"
    )
    status, payload, error = await _fetch_json(url)
    prop = payload.get("property") if isinstance(payload.get("property"), dict) else {}
    data = prop.get("data") if isinstance(prop.get("data"), dict) else {}
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    address = location.get("address") if isinstance(location.get("address"), dict) else {}
    evidence = _provider_identity_match(
        row,
        name=str(location.get("name") or ""),
        address=str(address.get("raw") or address.get("street") or ""),
        city=str(address.get("city") or ""),
        state=str(address.get("state") or ""),
        zip_code=str(address.get("zip") or ""),
    )
    return {
        "community_id": community_id,
        "community_url": url,
        "community_status": status,
        "numeric_property_id": str(prop.get("id") or ""),
        "error": error,
        **evidence,
    }


async def _yotta_diagnostic(
    row: dict[str, str], capture: dict[str, Any]
) -> dict[str, Any]:
    details_url = "https://residentapis.yottareal.com/api/DBA/GetDBADetails/55"
    units_url = "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/55/1"
    details_status, details, details_error = await _fetch_json(details_url)
    units_status, payload, units_error = await _fetch_json(units_url)
    raw_units = payload.get("hotSheetUnitsModel") or []
    raw_units = [item for item in raw_units if isinstance(item, dict)]
    raw_ids = [str(item.get("unitId") or "").strip() for item in raw_units]
    raw_numbers = [str(item.get("unitNumber") or "").strip() for item in raw_units]
    raw_priced = [
        item
        for item in raw_units
        if isinstance(item.get("rent"), (int, float))
        and not isinstance(item.get("rent"), bool)
        and item.get("rent") > 0
    ]
    identity = _provider_identity_match(
        row,
        name=str(details.get("dbaName") or ""),
        address=" ".join(
            value
            for value in (
                str(details.get("address1") or ""),
                str(details.get("address2") or ""),
            )
            if value
        ),
        city=str(details.get("city") or ""),
        state=str(details.get("stateCode") or ""),
        zip_code=str(details.get("zip") or ""),
    )
    injected_summary, injected_units = await _pipeline(
        row,
        34785,
        capture,
        api_responses=[{"url": units_url, "status": units_status, "body": payload}],
    )
    injected_with_source_ids = sum(
        1
        for unit in injected_units
        if isinstance(unit.get("source_ids"), dict) and unit.get("source_ids")
    )
    return {
        "details_url": details_url,
        "details_status": details_status,
        "details_error": details_error,
        "units_url": units_url,
        "units_status": units_status,
        "units_error": units_error,
        "provider_identity": identity,
        "raw_unit_rows": len(raw_units),
        "raw_rows_with_unit_id_number_positive_rent": sum(
            1
            for item in raw_units
            if str(item.get("unitId") or "").strip()
            and str(item.get("unitNumber") or "").strip()
            and isinstance(item.get("rent"), (int, float))
            and not isinstance(item.get("rent"), bool)
            and item.get("rent") > 0
        ),
        "distinct_raw_unit_ids": len({value for value in raw_ids if value}),
        "distinct_raw_unit_numbers": len({value for value in raw_numbers if value}),
        "raw_positive_rent_rows": len(raw_priced),
        "raw_samples": [
            {
                "unit_id": str(item.get("unitId") or ""),
                "unit_number": str(item.get("unitNumber") or ""),
                "rent": item.get("rent"),
                "floor_plan_id": str(item.get("dbaUnitTypeId") or ""),
                "available_date": str(
                    item.get("MoveInDateAvailable")
                    or item.get("availableDate")
                    or item.get("dateAvailable")
                    or ""
                ),
                "online_path": str(item.get("onlinePath") or ""),
            }
            for item in raw_priced[:5]
        ],
        "current_generic_api_injection_e2e": injected_summary,
        "current_normalized_rows_with_preserved_source_ids": injected_with_source_ids,
        "strict_entrypoint_accept": False,
        "strict_rejection": (
            "Current exact-route baseline scraper emits zero units. Injecting the "
            "public provider payload yields rows but current generic normalization "
            "drops native unitId/source_ids, so this is a code lever, not an acceptance."
        ),
    }


async def main() -> None:
    metadata = _metadata()
    residual_at_start = _current_residual_rows()
    if TARGETS - metadata.keys():
        raise SystemExit(f"Missing metadata: {sorted(TARGETS - metadata.keys())}")
    if TARGETS - residual_at_start.keys():
        raise SystemExit(
            f"Targets no longer all current residuals: {sorted(TARGETS - residual_at_start.keys())}"
        )

    source_blobs_start = _git_blobs()
    canonical_urls = {
        pid: _normalize_url(metadata[pid].get("website") or "")
        for pid in TARGETS
    }
    all_urls = set(canonical_urls.values()) | set(ROUTE_OVERRIDES.values())
    captures_list = await asyncio.gather(*(_fetch(url) for url in sorted(all_urls)))
    captures = {str(item["requested_url"]): item for item in captures_list}

    results: list[dict[str, Any]] = []
    for pid in sorted(TARGETS):
        row = metadata[pid]
        canonical_url = canonical_urls[pid]
        selected_url = ROUTE_OVERRIDES.get(pid, canonical_url)
        canonical_capture = captures[canonical_url]
        selected_capture = captures[selected_url]
        identity = _identity_evidence(row, str(selected_capture.get("body") or ""))
        pipeline, units = await _pipeline(row, pid, selected_capture)
        knock_identity = await _knock_provider_identity(
            row, str(selected_capture.get("body") or "")
        )
        provider_identity_match = bool(knock_identity.get("property_identity_match"))
        exact_identity = bool(identity["property_identity_match"] or provider_identity_match)
        gate = pipeline["units_gate"]
        strict_pass = bool(
            selected_capture.get("status") == 200
            and exact_identity
            and gate["all_rows_native_priced"]
            and gate["native_anchors_unique"]
            and gate["provider_boundary_single_property"]
            and pipeline.get("run")
        )
        if strict_pass and gate["source_property_ids"] and knock_identity.get("numeric_property_id"):
            strict_pass = gate["source_property_ids"] == [
                str(knock_identity["numeric_property_id"])
            ]

        yotta = await _yotta_diagnostic(row, selected_capture) if pid == 34785 else None
        body_lower = str(selected_capture.get("body") or "").lower()
        realpage_urls = sorted(
            {
                html.unescape(value)
                for value in re.findall(
                    r"https?://[^\"'<>\s]*onlineleasing\.realpage\.com[^\"'<>\s]*",
                    str(selected_capture.get("body") or ""),
                    flags=re.I,
                )
            }
        )

        reasons: list[str] = []
        status = selected_capture.get("status")
        final_url = str(selected_capture.get("final_url") or "")
        if status != 200:
            reasons.append(
                "exact_route_fetch_error" if status is None else f"exact_route_http_{status}"
            )
        if status == 200 and not exact_identity:
            reasons.append("exact_property_identity_not_proven_on_final_content")
        if pipeline.get("run") and pipeline.get("units", 0) == 0:
            reasons.append("current_full_scraper_e2e_emitted_zero_units")
        if pipeline.get("plans", 0) and pipeline.get("units", 0) == 0:
            reasons.append("current_source_is_plan_level_only")
        if units and not gate["all_rows_native_priced"]:
            reasons.append("not_all_e2e_rows_have_native_identity_and_positive_rent")
        if units and not gate["native_anchors_unique"]:
            reasons.append("duplicate_or_missing_native_unit_anchors")
        if units and not gate["provider_boundary_single_property"]:
            reasons.append("provider_property_boundary_not_single")
        if "nonpayment.spherexx.com" in final_url:
            reasons.append("configured_property_host_redirects_to_nonpayment_suspension")
        if "camdenliving.com/" == final_url.rstrip("/") + "/" and pid == 32978:
            reasons.append("retired_property_route_redirects_to_corporate_homepage")
        if "block.charter-prod.hosted.cujo.io" in final_url:
            reasons.append("network_security_interstitial_not_property_content")
        if len(str(selected_capture.get("body") or "").encode()) < 500 and status == 200:
            reasons.append("http_200_body_too_small_for_property_inventory")
        if pid == 34785:
            reasons.append(
                "yotta_public_api_has_native_rows_but_current_entrypoint_e2e_and_source_id_preservation_fail"
            )

        results.append(
            {
                "property_id": pid,
                "property_name": row.get("name") or "",
                "canonical_website": canonical_url,
                "selected_exact_route": selected_url,
                "route_normalization": (
                    "scheme_only" if selected_url != canonical_url else "none"
                ),
                "rp_oracle_native_unit_rows": int(
                    residual_at_start[pid].get("rp_oracle_native_unit_rows") or 0
                ),
                "canonical_route_fetch": _fetch_summary(canonical_capture),
                "selected_route_fetch": _fetch_summary(selected_capture),
                "page_identity": identity,
                "provider_identity": knock_identity,
                "competing_provider_signals": {
                    "realpage_oll_urls": realpage_urls,
                    "rentcafe_present": bool(
                        "resource.rentcafe.com" in body_lower
                        or "securecafe.com" in body_lower
                    ),
                    "knock_marker_present": bool(
                        "knockdoorway" in body_lower or "doorway.knck.io" in body_lower
                    ),
                },
                "full_current_scraper_e2e": pipeline,
                "yotta_near_miss": yotta,
                "outcome": "UNIT_QUALIFIED" if strict_pass else "REJECTED",
                "strict_units": gate["rows"] if strict_pass else 0,
                "contamination_verdict": (
                    "pass_exact_property_single_provider_native_identity_positive_rent"
                    if strict_pass
                    else "not_accepted_under_exact_property_native_identity_boundary_gate"
                ),
                "explicit_rejection_reasons": [] if strict_pass else reasons,
            }
        )

    source_blobs_end = _git_blobs()
    if source_blobs_start != source_blobs_end:
        raise SystemExit("Local source changed during audit; refusing mixed-source artifact")

    accepted_ids = sorted(
        int(result["property_id"])
        for result in results
        if result["outcome"] == "UNIT_QUALIFIED"
    )
    rejected = [result for result in results if result["outcome"] == "REJECTED"]

    # Reconcile only after expensive live work, then assert the shared ledger
    # remains byte-identical through materialization.
    ledger_sha_start = _sha256(LEDGER)
    ledger_ids = _ledger_ids()
    remaining_now = _current_residual_rows()
    net_new_ids = sorted(set(accepted_ids) - ledger_ids)

    payload = {
        "lane": "unknown_residual17_current_strict_read_only",
        "captured_at": datetime.now(UTC).isoformat(),
        "target_ids": sorted(TARGETS),
        "target_count": len(TARGETS),
        "policy": {
            "current_live_exact_property_routes_only": True,
            "llm_calls": 0,
            "captcha_solving": False,
            "web_unlocker": False,
            "paid_proxy": False,
            "paid_canary": False,
            "firecrawl_external_scrape": False,
            "firecrawl_reason": (
                "External credit/rendered scraping was excluded to preserve direct/no-unlocker provenance."
            ),
            "repo_source_edits": False,
            "shared_ledger_or_builder_mutated": False,
        },
        "strict_gate": (
            "Current exact route HTTP 200 + exact property name/address/ZIP (page or "
            "provider identity) + current full local scraper/adapter output + every "
            "row has provider-native unit ID/number and positive rent + unique native "
            "anchors + one provider property boundary. Diagnostic endpoint injection "
            "cannot qualify an entrypoint miss."
        ),
        "source_git_blobs_start": source_blobs_start,
        "source_git_blobs_end": source_blobs_end,
        "ledger": {
            "path": str(LEDGER),
            "sha256_start": ledger_sha_start,
            "rows": len(ledger_ids),
            "target_overlap_before": sorted(TARGETS & ledger_ids),
            "targets_still_in_current_residual": sorted(TARGETS & remaining_now.keys()),
        },
        "accepted_ids": accepted_ids,
        "accepted_count": len(accepted_ids),
        "accepted_units": sum(
            int(result["strict_units"])
            for result in results
            if result["outcome"] == "UNIT_QUALIFIED"
        ),
        "net_new_ids": net_new_ids,
        "net_new_count": len(net_new_ids),
        "rejected_ids": sorted(int(result["property_id"]) for result in rejected),
        "rejected_count": len(rejected),
        "minimal_code_levers": [
            {
                "property_id": 34785,
                "estimated_current_native_rows": next(
                    (
                        int(result["yotta_near_miss"]["raw_unit_rows"])
                        for result in results
                        if result["property_id"] == 34785 and result["yotta_near_miss"]
                    ),
                    0,
                ),
                "lever": (
                    "Upgrade the exact Yotta route from HTTP to HTTPS; detect Id=55; "
                    "GET GetDBADetails/55 for managementCompanyId; GET "
                    "GetFloorPlans/55/1; map unitId into a registered yotta_unit_id, "
                    "unitNumber, rent, floor plan and available date."
                ),
                "why_not_counted_now": (
                    "The baseline exact-route scraper emits zero; API injection is not "
                    "entrypoint E2E and the current generic mapper drops native unitId."
                ),
            }
        ],
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    REJECTIONS_OUTPUT.write_text(
        json.dumps(
            {
                "source_artifact": str(OUTPUT),
                "rejections": [
                    {
                        "property_id": item["property_id"],
                        "property_name": item["property_name"],
                        "reasons": item["explicit_rejection_reasons"],
                    }
                    for item in rejected
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    NET_NEW_OUTPUT.write_text(
        json.dumps(
            {
                "source_artifact": str(OUTPUT),
                "ledger_sha256": ledger_sha_start,
                "accepted_ids": accepted_ids,
                "net_new_ids": net_new_ids,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    ledger_sha_end = _sha256(LEDGER)
    if ledger_sha_end != ledger_sha_start:
        raise SystemExit("Shared ledger changed during materialization")

    # Add the closing SHA only after proving byte identity, then rewrite the
    # evidence artifact (the ledger itself is never touched).
    payload["ledger"]["sha256_end"] = ledger_sha_end
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if _sha256(LEDGER) != ledger_sha_end:
        raise SystemExit("Shared ledger changed after final evidence write")

    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "accepted_ids": accepted_ids,
                "accepted_units": payload["accepted_units"],
                "net_new_ids": net_new_ids,
                "rejected_count": len(rejected),
                "ledger_rows": len(ledger_ids),
                "ledger_sha256": ledger_sha_end,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
