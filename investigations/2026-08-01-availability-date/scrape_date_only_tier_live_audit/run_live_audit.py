#!/usr/bin/env python3
# ruff: noqa: E402
"""Normalize the five scrape-date-only availability families into one audit.

This investigation is intentionally read-only with respect to production code.
It reuses the same-day current-live evidence in ``no_rp_oracle_live_audit`` and
adds three direct supplemental probes needed to meet the requested minimum of
three exact configured properties for OneSite Workflow and Squarespace.

No LLM, CAPTCHA solver, unlocker, FlareSolverr, fingerprint rotation, paid
canary, or production mutation is used.  The only files written are evidence
artifacts beside this script.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
AUDIT_DIR = SCRIPT_PATH.parent
REPO_ROOT = SCRIPT_PATH.parents[3]
BASE_DIR = SCRIPT_PATH.parent.parent / "no_rp_oracle_live_audit"
BASE_RUNNER = BASE_DIR / "run_no_rp_oracle_live_audit.py"
BASE_PROPERTY_CSV = BASE_DIR / "current_live_property_audit.csv"
BASE_UNIT_CSV = BASE_DIR / "current_live_unit_evidence.csv"
BASE_SUMMARY_JSON = BASE_DIR / "summary.json"
PROPERTIES_CSV = REPO_ROOT / "properties.csv"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ma_poc.fetch.contracts import (
    FetchOutcome,
    FetchResult,
    RenderMode,
)
from ma_poc.pms.adapters.appfolio import (
    find_appfolio_property_group,
    parse_appfolio_listings_ssr,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.squarespace_nopms import (
    SquarespaceNoPmsAdapter,
)
from ma_poc.pms.appfolio_urls import scoped_listings_url
from ma_poc.pms.detector import detect_pms

RESULT_TYPE = "local_current_live_scrape_date_only_tier_audit_not_canary"
CAPTURE_DATE = "2026-08-01"
CLASSIFICATIONS = {
    "native_future_preserved",
    "native_future_lost/defaulted_to_scrape_date",
    "available_now_normalized",
    "no_native_date",
    "no_inventory",
    "fetch_failed",
    "ambiguous",
}

FAMILY_BY_CATEGORY = {
    "REALPAGE_OLL": "RealPage OLL API",
    "ENTRATA_API": "Entrata API",
    "ONESITE_API": "OneSite / OneSite Workflow",
    "ONESITE_WORKFLOW": "OneSite / OneSite Workflow",
    "ASPENSQUARE_OPERATOR": "AspenSquare",
    "SQUARESPACE_UNIT_BLOCK": "Squarespace",
    "SQUARESPACE_SHELL_APPFOLIO": "Squarespace",
}

# Base evidence already captured >=3 exact properties for every family except
# OneSite Workflow (historical population 2) and Squarespace unit-block
# (historical population 1).  These direct probes are supplemental to those
# historical denominators; they are never represented as additional members of
# the July extraction-tier population.
SUPPLEMENTAL_WORKFLOW_ID = "12398"
SUPPLEMENTAL_SQUARESPACE = {
    "19955": "https://www.brooksidejohnsoncreek.com/listings",
    "49171": "https://www.melrosegates.com/availability",
}

APPFOLIO_TENANT_RE = re.compile(
    r"(?:(?:https?:)?//)([a-z0-9][a-z0-9-]*)\.appfolio\.com/",
    re.IGNORECASE,
)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty evidence: {path}")
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_base_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "availability_base_audit", BASE_RUNNER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load base audit: {BASE_RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def bool_text(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def int_value(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def normalized_addr(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def evidence_primary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows if bool_text(row.get("primary_property_evidence", True))
    ]


def build_fetch_result(url: str, result: Any) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=int(result.status),
        body=(result.text or "").encode(),
        headers={},
        render_mode=RenderMode.GET,
        final_url=str(result.final_url),
        attempts=int(result.attempts or 1),
        elapsed_ms=0,
    )


def unit_anchor(row: dict[str, Any]) -> str:
    return str(row.get("unit_number") or row.get("unit_id") or "").strip()


async def probe_squarespace_appfolio(
    base: Any,
    meta: dict[str, str],
    page_url: str,
    captured_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Probe a configured Squarespace shell and replay its current adapter.

    The native source is the exact operator-published ``propertyGroup`` from
    the configured property's own Squarespace page plus the server-filtered
    AppFolio listings response.  The current ``SquarespaceNoPmsAdapter`` is
    replayed with LLM disabled against that same current page body.
    """

    property_id = str(meta["apartmentid"])
    page = base.fetch_get(page_url, 30.0)
    html = page.text or ""
    group = str(find_appfolio_property_group(html) or "").strip()
    tenant_match = APPFOLIO_TENANT_RE.search(html)
    tenant = tenant_match.group(1).lower() if tenant_match else ""
    listings_url = (
        scoped_listings_url(f"https://{tenant}.appfolio.com/listings", group)
        if tenant and group
        else ""
    )
    listings = base.fetch_get(listings_url, 30.0) if listings_url else None
    raw_units = (
        parse_appfolio_listings_ssr(listings.text or "", listings_url)
        if listings is not None and listings.status == 200
        else []
    )

    fetch_result = build_fetch_result(page_url, page)
    detected = detect_pms(str(page.final_url), page_html=html)
    ctx = AdapterContext(
        base_url=str(page.final_url),
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        address=str(meta.get("address") or ""),
        city=str(meta.get("city") or ""),
        state=str(meta.get("state") or ""),
        zip_code=str(meta.get("zip") or ""),
        fetch_result=fetch_result,
    )
    adapter_result = await SquarespaceNoPmsAdapter().extract(None, ctx)  # type: ignore[arg-type]
    adapter_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in adapter_result.units:
        adapter_by_unit[unit_anchor(row)].append(row)

    text = base.compact_text(base.BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    expected_name = str(meta.get("name") or "")
    expected_address = str(meta.get("address") or "")
    name_visible = base.normalize_text(expected_name) in base.normalize_text(text)
    address_visible = base.address_match(expected_address, text)

    expected_tokens = set(normalized_addr(expected_address).split())
    expected_zip = str(meta.get("zip") or "").lstrip("0")
    unit_identity_hits = 0
    contamination_conflicts = 0
    for unit in raw_units:
        addr = normalized_addr(unit.get("unit_name"))
        tokens = set(addr.split())
        street_overlap = bool(
            {
                token
                for token in expected_tokens
                if len(token) >= 4 and not token.isdigit()
            }
            & tokens
        )
        zip_match = not expected_zip or expected_zip in addr.replace(" ", "")
        if street_overlap and zip_match:
            unit_identity_hits += 1
        elif addr:
            contamination_conflicts += 1

    identity_ok = bool(
        group
        and tenant
        and name_visible
        and (address_visible or unit_identity_hits > 0)
        and contamination_conflicts == 0
    )
    identity_reason = (
        "operator_published_property_group_plus_name_and_address_family_match"
        if identity_ok
        else (
            f"name_visible={name_visible};address_visible={address_visible};"
            f"property_group={bool(group)};tenant={bool(tenant)};"
            f"unit_identity_hits={unit_identity_hits};"
            f"contamination_conflicts={contamination_conflicts}"
        )
    )

    rows: list[dict[str, Any]] = []
    for raw in raw_units:
        anchor = unit_anchor(raw)
        candidates = adapter_by_unit.get(anchor) or []
        adapter_row = candidates.pop(0) if candidates else None
        raw_date = str(raw.get("availability_date") or "").strip()
        evidence = base.evidence_row(
            captured_at=captured_at,
            category="SQUARESPACE_SHELL_APPFOLIO",
            property_id=property_id,
            configured_name=expected_name,
            source_kind="squarespace_operator_scoped_appfolio_listings_ssr",
            evidence_url=listings_url,
            source_row_id=anchor,
            floor_plan_name=str(raw.get("floor_plan_name") or ""),
            raw_availability=raw_date,
            source_field="AppFolio .js-listing-available",
            raw_evidence={
                "unit_number": anchor,
                "unit_name": raw.get("unit_name"),
                "availability_date": raw_date,
                "rent_low": raw.get("market_rent_low") or raw.get("rent_low"),
                "property_group": group,
                "tenant": tenant,
            },
            adapter_parser="SquarespaceNoPmsAdapter -> recover_appfolio_embed",
            adapter_trace_method="exact_current_adapter_replay_llm_off",
            adapter_row=adapter_row,
            adapter_route_selected=adapter_row is not None,
            adapter_row_absence_reason=(
                "protocol_relative_appfolio_tenant_not_discovered_by_current_route"
                if adapter_row is None and raw_units
                else ""
            ),
            trace_note=(
                "Exact current Squarespace adapter replay against the current "
                "operator page; native rows come from its published propertyGroup."
            ),
        )
        # AppFolio's literal sentinel is ``NOW``. The shared base audit's
        # semantic helper recognizes "Available Now" but not the bare token;
        # retain the raw value while assigning its exact meaning here.
        if raw_date.upper() == "NOW":
            evidence["availability_semantic"] = "available_now"
            evidence["normalized_availability_date"] = CAPTURE_DATE
            if adapter_row is not None:
                evidence["pipeline_outcome"] = "AVAILABLE_NOW_PRESERVED_END_TO_END"
                evidence["loss_classification"] = "none"
            else:
                evidence["pipeline_outcome"] = "NATIVE_SOURCE_ROUTE_NOT_WIRED"
                evidence["loss_classification"] = "adapter_route_selection_gap"
        rows.append(evidence)

    status = "PROBED_DATA" if raw_units else "PROBED_NO_CURRENT_INVENTORY"
    if page.status != 200 or listings is None or listings.status != 200:
        status = "FETCH_FAILED"
    return (
        {
            "status": status,
            "error": (
                ""
                if status != "FETCH_FAILED"
                else (
                    f"page_http={page.status};listings_http="
                    f"{getattr(listings, 'status', 0)}"
                )
            ),
            "identity_name": expected_name if name_visible else "",
            "identity_address": expected_address if address_visible else "",
            "identity_match": identity_ok,
            "identity_reason": identity_reason,
            "contamination_check": (
                "pass_server_scoped_property_group_and_address_family"
                if identity_ok
                else "unproven"
            ),
            "evidence_urls": [
                str(page.final_url),
                listings_url,
            ],
            "access_path": "direct_current_page_and_scoped_appfolio_ssr",
            "source_items": len(raw_units),
            "source_detail": (
                f"property_group={group};tenant={tenant};"
                f"current_adapter_tier={adapter_result.tier_used};"
                f"current_adapter_rows={len(adapter_result.units)};"
                f"unit_identity_hits={unit_identity_hits};"
                f"contamination_conflicts={contamination_conflicts}"
            ),
            "current_adapter_tier": adapter_result.tier_used,
            "current_adapter_errors": list(adapter_result.errors),
        },
        rows,
    )


def compact_property_from_result(
    *,
    category: str,
    meta: dict[str, str],
    result: dict[str, Any],
    rows: list[dict[str, Any]],
    captured_at: str,
    historical_tier_member: bool,
) -> dict[str, Any]:
    primary = evidence_primary(rows)
    semantics = Counter(str(row.get("availability_semantic") or "") for row in primary)
    outcomes = Counter(str(row.get("pipeline_outcome") or "") for row in primary)
    losses = Counter(str(row.get("loss_classification") or "") for row in primary)
    return {
        "result_type": RESULT_TYPE,
        "capture_timestamp_utc": captured_at,
        "capture_date": CAPTURE_DATE,
        "family": FAMILY_BY_CATEGORY[category],
        "adapter_variant": category,
        "property_id": str(meta["apartmentid"]),
        "property_name": str(meta.get("name") or ""),
        "configured_address": str(meta.get("address") or ""),
        "website": str(meta.get("website") or ""),
        "historical_exact_tier_member": historical_tier_member,
        # These July-output fields are populated by ``normalize_base_property``
        # only for exact historical tier members.  Keeping them blank for the
        # supplemental probes prevents the live sample from silently enlarging
        # the historical denominator.
        "july_output_rows": "",
        "july_scrape_date_rows": "",
        "july_blank_date_rows": "",
        "probe_status": str(result.get("status") or ""),
        "access_path": str(result.get("access_path") or ""),
        "identity_match": bool(result.get("identity_match")),
        "identity_reason": str(result.get("identity_reason") or ""),
        "contamination_check": str(
            result.get("contamination_check")
            or (
                "pass_exact_identity_no_conflict"
                if result.get("identity_match")
                else "unproven"
            )
        ),
        "evidence_urls": ";".join(result.get("evidence_urls") or []),
        "native_rows": len(primary),
        "native_future_rows": semantics["explicit_future"],
        "native_available_now_rows": semantics["available_now"],
        "native_explicit_capture_date_rows": semantics["explicit_capture_date"],
        "native_historical_date_rows": semantics["historical_embedded"],
        "native_sentinel_date_rows": semantics["sentinel_future"]
        + semantics["historical_sentinel"],
        "native_no_date_rows": semantics["source_blank"]
        + semantics["available_state_no_date"],
        "future_preserved_rows": sum(
            bool_text(row.get("explicit_future_preserved_by_formatter"))
            for row in primary
        ),
        "available_now_normalized_rows": sum(
            row.get("availability_semantic") == "available_now"
            and str(row.get("formatter_available_date") or "") == CAPTURE_DATE
            for row in primary
        ),
        "scrape_date_default_rows": sum(
            bool_text(row.get("formatter_capture_date_default")) for row in primary
        ),
        "current_replay_matched_rows": sum(
            bool_text(row.get("adapter_row_present")) for row in primary
        ),
        "current_replay_capture_date_rows": sum(
            str(row.get("formatter_available_date") or "") == CAPTURE_DATE
            for row in primary
        ),
        "current_capture_defaults_from_source_no_date_rows": sum(
            str(row.get("availability_semantic") or "")
            in {"source_blank", "available_state_no_date"}
            and str(row.get("formatter_available_date") or "") == CAPTURE_DATE
            for row in primary
        ),
        "pipeline_outcomes_json": json.dumps(
            dict(sorted(outcomes.items())), sort_keys=True
        ),
        "loss_classifications_json": json.dumps(
            dict(sorted(losses.items())), sort_keys=True
        ),
        "source_detail": str(result.get("source_detail") or ""),
        "error": str(result.get("error") or ""),
    }


def classification_for(row: dict[str, Any]) -> tuple[str, str]:
    if not bool(row.get("identity_match")):
        return "ambiguous", "property identity or contamination boundary is unproven"
    if row.get("probe_status") == "FETCH_FAILED":
        return "fetch_failed", str(row.get("error") or "native source fetch failed")
    if row.get("probe_status") == "PROBED_NO_CURRENT_INVENTORY":
        return "no_inventory", "current exact native source returned no inventory"

    future = int_value(row.get("native_future_rows"))
    future_preserved = int_value(row.get("future_preserved_rows"))
    if future:
        if future_preserved == future:
            return (
                "native_future_preserved",
                f"all {future} native future-date row(s) preserved by current replay",
            )
        return (
            "native_future_lost/defaulted_to_scrape_date",
            (
                f"{future - future_preserved} of {future} native future-date row(s) "
                "lost or rewritten by the current route/parser/formatter"
            ),
        )

    available_now = int_value(row.get("native_available_now_rows"))
    now_normalized = int_value(row.get("available_now_normalized_rows"))
    if available_now:
        if now_normalized == available_now:
            return (
                "available_now_normalized",
                f"all {available_now} visible available-now row(s) normalized to capture date",
            )
        return "ambiguous", "available-now rows were not consistently normalized"

    native_rows = int_value(row.get("native_rows"))
    no_date_rows = int_value(row.get("native_no_date_rows"))
    if native_rows and no_date_rows == native_rows:
        return (
            "no_native_date",
            "native inventory exists but publishes no usable availability-date field",
        )

    if native_rows:
        return (
            "ambiguous",
            "native sample has only historical, sentinel, unavailable, or same-day dates",
        )
    return "no_inventory", "no current native inventory rows"


def normalize_base_property(
    row: dict[str, str], unit_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    category = str(row["category"])
    result = {
        "status": row.get("probe_status"),
        "error": row.get("error"),
        "identity_match": bool_text(row.get("identity_match")),
        "identity_reason": row.get("identity_reason"),
        "evidence_urls": str(row.get("evidence_urls") or "").split(";")
        if row.get("evidence_urls")
        else [],
        "access_path": row.get("access_path"),
        "source_detail": row.get("source_detail"),
        "contamination_check": (
            "pass_exact_identity_no_conflicting_source"
            if bool_text(row.get("identity_match"))
            else "unproven"
        ),
    }
    meta = {
        "apartmentid": row["property_id"],
        "name": row["property_name"],
        "address": row["configured_address"],
        "website": row["website"],
    }
    compact = compact_property_from_result(
        category=category,
        meta=meta,
        result=result,
        rows=unit_rows,
        captured_at=str(row["capture_timestamp_utc"]),
        historical_tier_member=True,
    )
    compact.update(
        {
            "july_output_rows": int_value(row.get("july_rows")),
            "july_scrape_date_rows": int_value(row.get("july_capture_date_rows")),
            "july_blank_date_rows": int_value(row.get("july_blank_date_rows")),
        }
    )
    return compact


def adapter_summary(property_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in property_rows:
        grouped[str(row["adapter_variant"])].append(row)
    out: list[dict[str, Any]] = []
    for adapter, rows in sorted(grouped.items()):
        future_oracle = [
            row for row in rows if int_value(row["native_future_rows"]) > 0
        ]
        defect = [
            row
            for row in rows
            if row["classification"] == "native_future_lost/defaulted_to_scrape_date"
        ]
        counts = Counter(str(row["classification"]) for row in rows)
        out.append(
            {
                "result_type": RESULT_TYPE,
                "family": FAMILY_BY_CATEGORY[adapter],
                "adapter_variant": adapter,
                "probed_properties": len(rows),
                "historical_exact_tier_properties": sum(
                    bool(row["historical_exact_tier_member"]) for row in rows
                ),
                "identity_proven_properties": sum(
                    bool(row["identity_match"]) for row in rows
                ),
                "inventory_properties": sum(
                    row["probe_status"] == "PROBED_DATA" for row in rows
                ),
                "future_oracle_properties": len(future_oracle),
                "future_defect_properties": len(defect),
                "future_defect_rate": (
                    round(len(defect) / len(future_oracle), 6)
                    if future_oracle
                    else None
                ),
                "native_future_rows": sum(
                    int_value(row["native_future_rows"]) for row in rows
                ),
                "future_preserved_rows": sum(
                    int_value(row["future_preserved_rows"]) for row in rows
                ),
                "scrape_date_default_rows": sum(
                    int_value(row["scrape_date_default_rows"]) for row in rows
                ),
                "classification_counts_json": json.dumps(
                    dict(sorted(counts.items())), sort_keys=True
                ),
                "defect_property_ids": ";".join(
                    str(row["property_id"]) for row in defect
                ),
            }
        )
    return out


def family_summary(property_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in property_rows:
        grouped[str(row["family"])].append(row)
    out: list[dict[str, Any]] = []
    for family, rows in sorted(grouped.items()):
        future_oracle = [
            row for row in rows if int_value(row["native_future_rows"]) > 0
        ]
        defects = [
            row
            for row in rows
            if row["classification"] == "native_future_lost/defaulted_to_scrape_date"
        ]
        out.append(
            {
                "family": family,
                "adapter_variants": sorted(
                    {str(row["adapter_variant"]) for row in rows}
                ),
                "probed_properties": len(rows),
                "identity_proven_properties": sum(
                    bool(row["identity_match"]) for row in rows
                ),
                "future_oracle_properties": len(future_oracle),
                "future_defect_properties": len(defects),
                "future_defect_rate": (
                    round(len(defects) / len(future_oracle), 6)
                    if future_oracle
                    else None
                ),
                "classification_counts": dict(
                    sorted(Counter(str(row["classification"]) for row in rows).items())
                ),
                "defect_property_ids": [str(row["property_id"]) for row in defects],
            }
        )
    return out


async def main() -> None:
    # Guard the audit lane explicitly even if a caller inherited permissive env.
    os.environ["ENABLE_TIER4_LLM"] = "false"
    os.environ["ENABLE_BODY_RESOLVER"] = "false"
    os.environ["ENABLE_TIER_ESCALATION"] = "false"
    os.environ["ENABLE_UNLOCKER_TIER"] = "false"
    os.environ["ENABLE_FLARESOLVERR_TIER"] = "false"
    os.environ["COMPLIANCE_MODE"] = "1"

    base = load_base_module()
    captured_at = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    properties = {row["apartmentid"]: row for row in load_csv(PROPERTIES_CSV)}
    base_properties = load_csv(BASE_PROPERTY_CSV)
    base_units = load_csv(BASE_UNIT_CSV)
    base_summary = json.loads(BASE_SUMMARY_JSON.read_text(encoding="utf-8"))

    units_by_property: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in base_units:
        units_by_property[str(row["property_id"])].append(dict(row))

    property_rows: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = [dict(row) for row in base_units]
    for row in base_properties:
        property_rows.append(
            normalize_base_property(row, units_by_property[str(row["property_id"])])
        )

    # Third exact OneSite Workflow property: direct workflowstartup source and
    # exact current parser/formatter replay. This property is supplemental to
    # the two-property July tier population.
    workflow_meta = properties[SUPPLEMENTAL_WORKFLOW_ID]
    workflow_result, workflow_rows = base.probe_onesite_workflow(
        workflow_meta, captured_at, 30.0
    )
    workflow_result["contamination_check"] = (
        "pass_exact_configured_name_and_address"
        if workflow_result.get("identity_match")
        else "unproven"
    )
    unit_rows.extend(workflow_rows)
    property_rows.append(
        compact_property_from_result(
            category="ONESITE_WORKFLOW",
            meta=workflow_meta,
            result=workflow_result,
            rows=workflow_rows,
            captured_at=captured_at,
            historical_tier_member=False,
        )
    )

    # Two exact configured Squarespace shells supplement Cricket Flats, the
    # sole July SQUARESPACE_UNIT_BLOCK member. Keep the AppFolio-backed variant
    # explicit so it is never conflated with the native Squarespace block.
    for property_id, page_url in SUPPLEMENTAL_SQUARESPACE.items():
        result, rows = await probe_squarespace_appfolio(
            base, properties[property_id], page_url, captured_at
        )
        unit_rows.extend(rows)
        property_rows.append(
            compact_property_from_result(
                category="SQUARESPACE_SHELL_APPFOLIO",
                meta=properties[property_id],
                result=result,
                rows=rows,
                captured_at=captured_at,
                historical_tier_member=False,
            )
        )

    for row in property_rows:
        classification, reason = classification_for(row)
        if classification not in CLASSIFICATIONS:
            raise AssertionError(classification)
        row["classification"] = classification
        row["classification_reason"] = reason
        row["proven_defect"] = (
            classification == "native_future_lost/defaulted_to_scrape_date"
        )
        row["scrape_date_only_output_alone_is_defect"] = False
        row["defect_gate"] = (
            "native future date exists and current replay fails preservation"
            if row["proven_defect"]
            else "not proven by scrape-date-only output alone"
        )

    property_rows.sort(
        key=lambda row: (
            str(row["family"]),
            str(row["adapter_variant"]),
            int(row["property_id"]),
        )
    )
    adapter_rows = adapter_summary(property_rows)
    family_rows = family_summary(property_rows)
    evidence_capture_timestamps = sorted(
        {str(row["capture_timestamp_utc"]) for row in property_rows}
    )

    requested_minimums = {
        family: len(
            {
                str(row["property_id"])
                for row in property_rows
                if row["family"] == family
            }
        )
        for family in sorted({str(row["family"]) for row in property_rows})
    }
    if any(count < 3 for count in requested_minimums.values()):
        raise AssertionError(f"sample-size requirement not met: {requested_minimums}")
    if any(not bool(row["identity_match"]) for row in property_rows):
        raise AssertionError("identity-unproven property in final denominator")

    summary = {
        "result_type": RESULT_TYPE,
        "artifact_generated_at_utc": captured_at,
        "capture_timestamp_utc": captured_at,
        "capture_date": CAPTURE_DATE,
        "evidence_capture_window_utc": {
            "start": evidence_capture_timestamps[0],
            "end": evidence_capture_timestamps[-1],
            "distinct_capture_batches": len(evidence_capture_timestamps),
        },
        "scope": {
            "properties": len(property_rows),
            "families": len(requested_minimums),
            "adapter_variants": len(adapter_rows),
            "minimum_exact_configured_properties_per_family": 3,
            "observed_properties_by_family": requested_minimums,
            "historical_denominator_note": (
                "The July Squarespace unit-block tier has one property and the "
                "July OneSite Workflow tier has two. Supplemental exact configured "
                "properties satisfy live-probe sample size but never enlarge those "
                "historical tier denominators."
            ),
        },
        "classification_counts": dict(
            sorted(Counter(str(row["classification"]) for row in property_rows).items())
        ),
        "proven_future_date_defect_properties": sum(
            bool(row["proven_defect"]) for row in property_rows
        ),
        "family_summary": family_rows,
        "adapter_summary": adapter_rows,
        "guardrails": {
            "production_code_edits": False,
            "llm_enabled": False,
            "paid_canary": False,
            "new_hyperbrowser_sessions": 0,
            "reused_base_audit_hyperbrowser_sessions": int(
                base_summary.get("guardrails", {}).get("hyperbrowser_sessions", 0)
            ),
            "captcha_solving": False,
            "web_unlocker": False,
            "flaresolverr": False,
            "fingerprint_rotation": False,
        },
        "inputs": {
            "base_property_evidence": str(BASE_PROPERTY_CSV),
            "base_property_evidence_sha256": sha256(BASE_PROPERTY_CSV),
            "base_unit_evidence": str(BASE_UNIT_CSV),
            "base_unit_evidence_sha256": sha256(BASE_UNIT_CSV),
            "properties": str(PROPERTIES_CSV),
            "properties_sha256": sha256(PROPERTIES_CSV),
            "trace_code": "current availability-date worktree including existing uncommitted parent changes",
        },
        "artifacts": {
            "property_csv": "property_level_audit.csv",
            "property_json": "property_level_audit.json",
            "unit_csv": "unit_level_evidence.csv",
            "unit_json": "unit_level_evidence.json",
            "adapter_summary_csv": "adapter_summary.csv",
            "adapter_summary_json": "adapter_summary.json",
            "summary_markdown": "SUMMARY.md",
        },
    }

    write_csv(AUDIT_DIR / "property_level_audit.csv", property_rows)
    write_json(AUDIT_DIR / "property_level_audit.json", property_rows)
    write_csv(AUDIT_DIR / "unit_level_evidence.csv", unit_rows)
    write_json(AUDIT_DIR / "unit_level_evidence.json", unit_rows)
    write_csv(AUDIT_DIR / "adapter_summary.csv", adapter_rows)
    write_json(AUDIT_DIR / "adapter_summary.json", adapter_rows)
    write_json(AUDIT_DIR / "summary.json", summary)

    md = [
        "# Scrape-date-only tier live availability audit",
        "",
        f"Capture: `{CAPTURE_DATE}`; local current-live audit, not a paid canary.",
        "",
        (
            "A scrape-date-only output is not itself a defect. A property is counted "
            "as defective only when its exact native source publishes a future date "
            "and the current LLM-off replay fails to preserve it."
        ),
        "",
        "| Family | Probed | Future oracle | Defects | Defect rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in family_rows:
        rate = row["future_defect_rate"]
        rate_text = "n/a" if rate is None else f"{100 * rate:.1f}%"
        md.append(
            f"| {row['family']} | {row['probed_properties']} | "
            f"{row['future_oracle_properties']} | {row['future_defect_properties']} | "
            f"{rate_text} |"
        )
    md.extend(
        [
            "",
            (
                "Historical-denominator caveat: the July Squarespace unit-block tier "
                "contains one property and OneSite Workflow contains two. Supplemental "
                "exact configured probes meet the live sample rule but are labeled "
                "out-of-denominator."
            ),
            "",
            (
                "All final properties passed an explicit property-identity and "
                "contamination boundary. No production file, canary, or external "
                "solver was changed or invoked by this audit."
            ),
            "",
        ]
    )
    (AUDIT_DIR / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
