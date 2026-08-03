from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import html
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters import knock as knock_mod
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters._probe import probe_get
from ma_poc.pms.detector import DetectedPMS, detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT_DIR = ROOT / "encore_knock_lane" / "knock_agent"
OUTPUT = OUT_DIR / "evidence_knock_residual6_current_strict.json"
NET_NEW_OUTPUT = OUT_DIR / "strict_knock_residual6_net_new_ids.json"
REJECTIONS_OUTPUT = OUT_DIR / "strict_knock_residual6_rejections.json"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
TARGETS = {540, 48946, 61459, 68497, 224888, 261116}

_DNI_ID_RE = re.compile(r"dniId\s*:\s*['\"]([A-Za-z0-9_-]{8,40})['\"]", re.I)
_DNI_KEY_RE = re.compile(r"dniApiKey\s*:\s*['\"]([A-Za-z0-9+/=_-]{20,60})['\"]", re.I)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata() -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with Path("ma_poc/config/properties.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            try:
                property_id = int(row.get("apartmentid") or "")
            except ValueError:
                continue
            if property_id in TARGETS:
                rows[property_id] = row
    return rows


def _ledger_ids() -> set[int]:
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["property_id"])
            for row in csv.DictReader(handle)
            if str(row.get("property_id") or "").isdigit()
        }


def _plain_text(body: str) -> str:
    without_scripts = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        body,
        flags=re.I | re.S,
    )
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _page_identity(row: dict[str, str], body: str) -> dict[str, Any]:
    text_key = _key(_plain_text(body))
    words = set(text_key.split())
    name_key = _key(row.get("name") or "")
    name_tokens = name_key.split()
    leading_articles = {"the"}
    trailing_markers = {"i", "ii", "iii", "apartments", "apartment", "homes"}
    core_tokens = list(name_tokens)
    while core_tokens and core_tokens[0] in leading_articles:
        core_tokens.pop(0)
    while core_tokens and core_tokens[-1] in trailing_markers:
        core_tokens.pop()

    address_tokens = _key(row.get("address") or "").split()
    street_number = address_tokens[0] if address_tokens else ""
    ignored = {
        "n", "s", "e", "w", "north", "south", "east", "west", "st",
        "street", "rd", "road", "ave", "avenue", "blvd", "boulevard",
        "pkwy", "parkway", "dr", "drive", "ln", "lane", "ct", "court",
    }
    street_words = [
        token
        for token in address_tokens[1:]
        if token not in ignored and not token.isdigit()
    ]
    exact_name = bool(name_key and name_key in text_key)
    core_name = bool(core_tokens and all(token in words for token in core_tokens))
    address_match = bool(
        street_number
        and street_number in words
        and street_words
        and all(token in words for token in street_words)
    )
    return {
        "canonical_name": row.get("name") or "",
        "canonical_address": row.get("address") or "",
        "canonical_city": row.get("city") or "",
        "canonical_state": row.get("state") or "",
        "canonical_zip": row.get("zip") or row.get("zipcode") or "",
        "name_visible_exact_normalized": exact_name,
        "name_visible_core_normalized": core_name,
        "street_number_and_words_visible": address_match,
        "property_identity_match": bool(core_name and address_match),
    }


def _positive_rent(unit: dict[str, Any]) -> bool:
    for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high", "rent"):
        value = unit.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _native_identity(unit: dict[str, Any]) -> tuple[str, str]:
    unit_number = str(unit.get("unit_number") or "").strip()
    source_ids = unit.get("source_ids") if isinstance(unit.get("source_ids"), dict) else {}
    native_id = str(source_ids.get("knock_unit_id") or "").strip()
    if not native_id:
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id.startswith("knock_unit_id-"):
            native_id = unit_id.removeprefix("knock_unit_id-")
    return native_id, unit_number


def _units_gate(units: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [_native_identity(unit) for unit in units]
    native = [unit for unit, pair in zip(units, pairs) if all(pair)]
    qualified = [unit for unit in native if _positive_rent(unit)]
    ids = [_native_identity(unit)[0] for unit in qualified]
    numbers = [_native_identity(unit)[1] for unit in qualified]
    property_ids = {
        str(unit.get("source_property_id") or "").strip()
        for unit in qualified
        if str(unit.get("source_property_id") or "").strip()
    }
    source_urls = sorted(
        {
            str(unit.get("source_api_url") or "").strip()
            for unit in qualified
            if str(unit.get("source_api_url") or "").strip()
        }
    )
    return {
        "rows": len(units),
        "rows_with_native_id_and_number": len(native),
        "rows_with_native_id_number_and_positive_rent": len(qualified),
        "distinct_native_ids": len(set(ids)),
        "distinct_unit_numbers": len(set(numbers)),
        "duplicate_native_ids": sorted({item for item in ids if ids.count(item) > 1}),
        "source_property_ids": sorted(property_ids),
        "source_urls": source_urls,
        "all_rows_native_priced": bool(units and len(units) == len(native) == len(qualified)),
        "native_ids_unique": bool(ids and len(ids) == len(set(ids))),
        "provider_boundary_single_property": len(property_ids) == 1 if units else False,
        "samples": [
            {
                "knock_unit_id": _native_identity(unit)[0],
                "unit_number": _native_identity(unit)[1],
                "source_property_id": str(unit.get("source_property_id") or ""),
                "source_api_url": str(unit.get("source_api_url") or ""),
                "market_rent_low": unit.get("market_rent_low"),
                "floor_plan_name": str(unit.get("floor_plan_name") or ""),
                "availability_date": str(
                    unit.get("availability_date") or unit.get("available_date") or ""
                ),
            }
            for unit in qualified[:5]
        ],
    }


async def _fetch(url: str) -> tuple[int, str, str, dict[str, str]]:
    response = await asyncio.to_thread(
        probe_get, url, timeout=30, unlocker=False, retries=1
    )
    return (
        int(getattr(response, "status_code", 0) or 0),
        str(getattr(response, "text", "") or ""),
        str(getattr(response, "url", "") or url),
        dict(getattr(response, "headers", {}) or {}),
    )


def _archived_body(pid: int) -> str:
    path = ROOT / "raw_all" / f"{pid}.html.gz"
    if not path.exists():
        return ""
    with gzip.open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def _community_identity(raw_responses: list[dict[str, Any]]) -> dict[str, Any]:
    for response in raw_responses:
        body = response.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("property"), dict):
            continue
        prop = body["property"]
        data = prop.get("data") if isinstance(prop.get("data"), dict) else {}
        location = data.get("location") if isinstance(data.get("location"), dict) else {}
        address = location.get("address") if isinstance(location.get("address"), dict) else {}
        return {
            "numeric_property_id": str(prop.get("id") or ""),
            "provider_property_name": str(location.get("name") or ""),
            "provider_address": str(address.get("raw") or address.get("street") or ""),
            "provider_city": str(address.get("city") or ""),
            "provider_state": str(address.get("state") or ""),
            "provider_zip": str(address.get("zip") or ""),
        }
    return {}


def _competing_provider_signals(body: str) -> dict[str, Any]:
    urls = sorted(
        {
            html.unescape(value)
            for value in re.findall(
                r"https?://[^\"'<>\s]*onlineleasing\.realpage\.com[^\"'<>\s]*",
                body,
                flags=re.I,
            )
        }
    )
    lo = body.lower()
    return {
        "realpage_oll_urls": urls,
        "realpage_oll_present": bool(urls or "api.ws.realpage.com" in lo),
        "rentcafe_present": bool(
            "resource.rentcafe.com" in lo or "securecafe.com" in lo
        ),
        "knock_widget_marker_present": bool(
            "knockdoorway" in lo or "doorway.knck.io" in lo
        ),
    }


async def _one(pid: int, row: dict[str, str]) -> dict[str, Any]:
    url = row["website"]
    status, body, final_url, headers = await _fetch(url)
    identity = _page_identity(row, body)
    archived = _archived_body(pid)
    public_key, kind, live_cid = knock_mod.find_knock_ids(body)
    archived_public_key, archived_kind, archived_cid = knock_mod.find_knock_ids(archived)
    dni_cid_match = _DNI_ID_RE.search(body)
    dni_key_match = _DNI_KEY_RE.search(body)
    dynamic_cid = dni_cid_match.group(1) if dni_cid_match else None
    dynamic_public_key = dni_key_match.group(1) if dni_key_match else None

    fetch_result = FetchResult(
        url=url,
        outcome=(
            FetchOutcome.OK
            if status == 200
            else FetchOutcome.BOT_BLOCKED
            if status == 403
            else FetchOutcome.HARD_FAIL
        ),
        status=status,
        body=body.encode(),
        headers=headers,
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )
    detected = DetectedPMS(pms="knock", confidence=0.99, evidence=["audit-forced-knock"])
    ctx = AdapterContext(
        base_url=url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=str(pid),
        fetch_result=fetch_result,
        property_name=row.get("name") or "",
        address=row.get("address") or "",
        city=row.get("city") or "",
        state=row.get("state") or "",
        zip_code=row.get("zip") or row.get("zipcode") or "",
        pmc=row.get("management_company") or row.get("managementcompany") or "",
        budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
            "_cost_cap_usd": 0,
        },
    )
    adapter_result = await knock_mod.KnockAdapter().extract(page=None, ctx=ctx)  # type: ignore[arg-type]
    adapter_units = list(adapter_result.units or [])
    adapter_gate = _units_gate(adapter_units)

    offline = detect_pms(final_url or url, csv_row=row, page_html=body)
    pipeline_result: dict[str, Any] = {}
    pipeline_error = ""
    if status == 200 and body:
        try:
            pipeline_result = await scraper_mod.scrape(
                url,
                page=None,
                fetch_result=fetch_result,
                csv_row=row,
                property_id=str(pid),
                shared_budget={
                    "llm_api_calls": 0,
                    "llm_dom_calls": 0,
                    "llm_monolithic": 0,
                    "link_hop": 0,
                    "_cost_cap_usd": 0,
                },
            )
        except Exception as exc:
            pipeline_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    pipeline_units = list(pipeline_result.get("units") or [])
    pipeline_gate = _units_gate(pipeline_units)
    adapter_native_ids = {
        _native_identity(unit)[0] for unit in adapter_units if _native_identity(unit)[0]
    }
    pipeline_native_ids = {
        _native_identity(unit)[0] for unit in pipeline_units if _native_identity(unit)[0]
    }
    pipeline_e2e_pass = bool(
        (pipeline_result.get("_detected_pms") or {}).get("pms") == "knock"
        and pipeline_result.get("_adapter_used") == "knock"
        and pipeline_gate["all_rows_native_priced"]
        and pipeline_gate["native_ids_unique"]
        and pipeline_gate["provider_boundary_single_property"]
        and adapter_native_ids == pipeline_native_ids
    )

    # Supplemental current API check. For a live literal init use that ID; for
    # Bridgepoint use the exact current inline config values; for a 403 route,
    # the 2026-07-31 exact-route capture supplies only the stable community ID.
    candidate_cid = live_cid or dynamic_cid or archived_cid
    if live_cid:
        candidate_cid_source = "current_exact_route_literal_init"
    elif dynamic_cid:
        candidate_cid_source = "current_exact_route_inline_dni_config"
    elif archived_cid:
        candidate_cid_source = "2026-07-31_exact_route_capture_revalidated_current_api"
    else:
        candidate_cid_source = "none"
    manual_units: list[dict[str, Any]] = []
    manual_raw: list[dict[str, Any]] = []
    manual_error = ""
    if candidate_cid:
        try:
            manual_units = await knock_mod._fetch_knock_units(
                candidate_cid, live_cid and (kind or "community") or archived_kind or "community"
            )
            manual_raw = list(knock_mod.LAST_FETCH_RAW_RESPONSES)
        except Exception as exc:
            manual_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    manual_gate = _units_gate(manual_units)
    community_identity = _community_identity(manual_raw)
    competing_signals = _competing_provider_signals(body)

    adapter_pass = bool(
        status == 200
        and identity["property_identity_match"]
        and adapter_gate["all_rows_native_priced"]
        and adapter_gate["native_ids_unique"]
        and adapter_gate["provider_boundary_single_property"]
        and pipeline_e2e_pass
    )

    reasons: list[str] = []
    if status != 200:
        reasons.append(f"exact_route_http_{status}")
    if not identity["property_identity_match"]:
        reasons.append("exact_route_visible_name_address_identity_not_both_proven")
    if not adapter_units:
        reasons.append("current_knock_adapter_e2e_emitted_zero_units")
    if adapter_units and not adapter_gate["all_rows_native_priced"]:
        reasons.append("not_all_adapter_rows_have_native_id_number_positive_rent")
    if adapter_units and not adapter_gate["native_ids_unique"]:
        reasons.append("duplicate_native_knock_unit_ids")
    if adapter_units and not adapter_gate["provider_boundary_single_property"]:
        reasons.append("adapter_rows_not_bounded_to_one_provider_property")
    if adapter_units and not pipeline_e2e_pass:
        reasons.append("current_detect_detect_adapter_pipeline_did_not_preserve_exact_native_rows")
    if manual_units and not adapter_units:
        reasons.append("supplemental_api_has_units_but_current_adapter_route_does_not_reach_them")
    if candidate_cid and not manual_units:
        reasons.append("current_knock_api_has_zero_eligible_native_priced_units")
    if not candidate_cid:
        reasons.append("no_exact_route_knock_community_id")
    if competing_signals["realpage_oll_present"] and not candidate_cid:
        reasons.append("exact_page_points_to_realpage_inventory_not_knock")
    if competing_signals["rentcafe_present"] and not candidate_cid:
        reasons.append("exact_page_points_to_rentcafe_inventory_not_knock")

    return {
        "property_id": pid,
        "property_name": row.get("name") or "",
        "website": url,
        # Builder-facing strict fields are repeated at the result root so the
        # explicit manifest can consume this artifact without weakening or
        # duplicating its generic qualification gate.
        "property_identity_match": identity["property_identity_match"],
        "units": adapter_gate["rows"],
        "identity_evidence": {
            "rows_with_native_identity": adapter_gate[
                "rows_with_native_id_and_number"
            ],
            "rows_with_native_identity_and_positive_rent": adapter_gate[
                "rows_with_native_id_number_and_positive_rent"
            ],
            "source_urls": adapter_gate["source_urls"],
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": sample["unit_number"],
                    "knock_unit_id": sample["knock_unit_id"],
                },
                "positive_rent_evidence": {
                    "market_rent_low": sample["market_rent_low"]
                },
                "source_property_id": sample["source_property_id"],
                "source_api_url": sample["source_api_url"],
            }
            for sample in adapter_gate["samples"]
        ],
        "exact_route_fetch": {
            "status": status,
            "final_url": final_url,
            "body_bytes": len(body.encode()),
            "captcha_solving": False,
            "unlocker": False,
        },
        "page_identity": identity,
        "current_detector": {
            "pms": offline.pms,
            "confidence": offline.confidence,
            "evidence": list(offline.evidence),
        },
        "knock_signals": {
            "live_literal_init": bool(live_cid),
            "live_dynamic_dni_config": bool(dynamic_cid and dynamic_public_key),
            "archived_literal_init": bool(archived_cid),
            "community_id": candidate_cid or "",
            "community_id_source": candidate_cid_source,
            "public_key_present": bool(public_key or dynamic_public_key or archived_public_key),
        },
        "competing_provider_signals": competing_signals,
        "knock_adapter_e2e": {
            "adapter": "KnockAdapter",
            "tier": adapter_result.tier_used,
            "winning_url": adapter_result.winning_url or "",
            "units_gate": adapter_gate,
            "errors": list(adapter_result.errors),
            "subpage_hints": list(
                getattr(adapter_result, "_embedded_floorplan_subpage_hints", []) or []
            ),
            "strict_pass": adapter_pass,
        },
        "full_local_pipeline_observation": {
            "detected_pms": (pipeline_result.get("_detected_pms") or {}).get("pms"),
            "adapter_used": pipeline_result.get("_adapter_used"),
            "tier": pipeline_result.get("extraction_tier_used"),
            "units": len(pipeline_units),
            "units_gate": pipeline_gate,
            "native_ids_equal_direct_adapter": adapter_native_ids == pipeline_native_ids,
            "strict_pass": pipeline_e2e_pass,
            "plans": len(pipeline_result.get("plan_summaries") or []),
            "errors": list(pipeline_result.get("errors") or [])[-8:],
            "exception": pipeline_error,
            "llm_budget": 0,
        },
        "supplemental_current_knock_api_probe": {
            "community_id_source": candidate_cid_source,
            "community_identity": community_identity,
            "units_gate": manual_gate,
            "api_responses": [
                {
                    "url": str(item.get("url") or ""),
                    "status": item.get("status"),
                    "via": str(item.get("via") or ""),
                }
                for item in manual_raw
            ],
            "error": manual_error,
            "strict_adapter_accept": False,
            "note": (
                "Supplemental helper output is diagnostic only; it cannot pass "
                "the required full KnockAdapter E2E gate."
            ),
        },
        "outcome": "UNIT_QUALIFIED" if adapter_pass else "REJECTED",
        "explicit_rejection_reasons": reasons,
        "contamination_verdict": (
            "pass_exact_property_native_ids_positive_rents_single_provider_property"
            if adapter_pass
            else "not_accepted_no_full_property_bounded_native_knock_adapter_output"
        ),
    }


async def main() -> None:
    metadata = _metadata()
    missing = TARGETS - metadata.keys()
    if missing:
        raise SystemExit(f"Missing canonical metadata: {sorted(missing)}")

    ledger_sha_start = _sha256(LEDGER)
    ledger_ids_start = _ledger_ids()
    # Knock's existing LAST_FETCH_RAW_RESPONSES capture buffer is module-global.
    # Run properties sequentially so diagnostic community metadata cannot bleed
    # across concurrent adapter/helper calls in this strict evidence artifact.
    results = []
    for pid in sorted(TARGETS):
        results.append(await _one(pid, metadata[pid]))
    ledger_sha_end = _sha256(LEDGER)
    if ledger_sha_start != ledger_sha_end:
        raise SystemExit("Ledger changed during audit; refusing to materialize stale net-new IDs")
    accepted = sorted(
        int(item["property_id"])
        for item in results
        if item["outcome"] == "UNIT_QUALIFIED"
    )
    net_new = sorted(set(accepted) - ledger_ids_start)
    rejected = [item for item in results if item["outcome"] == "REJECTED"]
    source_hash = subprocess.check_output(
        ["git", "hash-object", "ma_poc/pms/adapters/knock.py"], text=True
    ).strip()
    payload = {
        "lane": "knock_residual6_current_strict_read_only",
        "cohort_boundary": "current_residual_after_187_of_344_ledger",
        "target_ids": sorted(TARGETS),
        "target_count": len(TARGETS),
        "policy": {
            "llm_calls": 0,
            "captcha_solving": False,
            "unlocker": False,
            "paid_canary": False,
            "repo_source_edits": True,
            "shared_ledger_mutated": False,
        },
        "strict_gate": (
            "HTTP 200 exact route + visible exact-property name/address + current detector "
            "selects Knock + full detect-to-adapter pipeline preserves the direct adapter's "
            "exact native row set + every row has native Knock UUID and unit number + positive "
            "rent + unique native UUIDs + one provider property boundary"
        ),
        "adapter_source_git_blob": source_hash,
        "ledger": {
            "path": str(LEDGER),
            "sha256_start": ledger_sha_start,
            "sha256_end": ledger_sha_end,
            "rows": len(ledger_ids_start),
            "target_overlap_before": sorted(TARGETS & ledger_ids_start),
        },
        "accepted_ids": accepted,
        "accepted_count": len(accepted),
        "net_new_ids": net_new,
        "net_new_count": len(net_new),
        "rejected_ids": sorted(int(item["property_id"]) for item in rejected),
        "rejected_count": len(rejected),
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    NET_NEW_OUTPUT.write_text(
        json.dumps(
            {
                "source_artifact": str(OUTPUT),
                "ledger_sha256": ledger_sha_end,
                "net_new_ids": net_new,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
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
    print(json.dumps({
        "artifact": str(OUTPUT),
        "accepted_ids": accepted,
        "net_new_ids": net_new,
        "rejected_ids": payload["rejected_ids"],
    }))


if __name__ == "__main__":
    asyncio.run(main())
