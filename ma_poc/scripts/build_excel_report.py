#!/usr/bin/env python3
"""Build a shareable Excel workbook from a Jugnu run's output.

A stopgap for the (not-yet-hosted) dashboard: reads ``properties.json`` (and
``report.json`` if present) from a run directory and writes ``report.xlsx``
with three sheets:

  * **Summary**    — run totals, verdict / tier distribution, field-fill rates,
                     data-quality mix (from report.json, else recomputed).
  * **Properties** — one row per property: identity + verdict + provenance
                     (confidence, adapter, winning tier, fetch method,
                     data-quality counts).
  * **Units**      — one row per unit, flattened, incl. per-unit
                     ``extraction_tier`` and ``is_floor_plan_level``.

Defensive: works on the v2 output shape and tolerates older runs that predate
the provenance surfacing (PR #95) — missing fields render blank.

Usage:
    python -m ma_poc.scripts.build_excel_report --run-dir data/v2/runs/2026-07-12
    python -m ma_poc.scripts.build_excel_report --properties props.json --out out.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ma_poc.core.schema_v2 import field_is_absent

_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=13)
_SUBTITLE_FONT = Font(bold=True, size=11, color="1F3864")

_FILL_FIELDS = (
    "unit_id", "beds", "baths", "area", "rent_low", "available_date",
    "availability_status", "floor_plan_name", "floor", "building",
    "concession_text", "source_ids",
)


# ── field extraction (defensive across v1/v2 + provenance-present-or-absent) ──

def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "null"):
            return v
    return None


def _prop_id(p: dict[str, Any]) -> str:
    meta = p.get("_meta") or {}
    return str(
        _first(p, "apartment_id", "Property ID", "Unique ID")
        or meta.get("canonical_id")
        or ""
    )


def _prop_name(p: dict[str, Any]) -> str:
    return str(_first(p, "proj_name", "Property Name", "name") or "")


def _property_row(p: dict[str, Any]) -> dict[str, Any]:
    meta = p.get("_meta") or {}
    prov = meta.get("provenance") or {}
    ex = p.get("_extract_result") or {}
    fetch = prov.get("fetch") or {}
    dq = prov.get("data_quality") or {}
    units = p.get("units") or []
    return {
        "property_id": _prop_id(p),
        "name": _prop_name(p),
        "city": _first(p, "city", "City") or "",
        "state": _first(p, "state", "State") or "",
        "zip": _first(p, "zip_code", "ZIP Code", "zip") or "",
        "website": _first(p, "website", "Website") or "",
        "verdict": meta.get("verdict") or "",
        "verdict_reason": meta.get("verdict_reason") or "",
        "units": len(units),
        "confidence": prov.get("confidence"),
        "adapter": prov.get("adapter") or "",
        "detected_pms": prov.get("detected_pms") or "",
        "winning_tier": prov.get("winning_tier") or (ex.get("tier_used") if isinstance(ex, dict) else "") or "",
        "fetch_outcome": fetch.get("outcome") or "",
        "render_mode": fetch.get("render_mode") or "",
        "proxied": fetch.get("proxied"),
        "page_load_ms": fetch.get("page_load_ms"),
        "real_id_units": dq.get("real_id_units"),
        "synthetic_id_units": dq.get("synthetic_id_units"),
        "plan_level_units": dq.get("plan_level_units"),
        "is_lease_up": prov.get("is_lease_up"),
        "llm_cost_usd": ex.get("llm_cost_usd") if isinstance(ex, dict) else None,
        "concession_banner": _concession_text(p.get("concessions")),
    }


def _concession_text(c: Any) -> str:
    if not c:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return str(c.get("text") or c.get("banner") or c.get("value") or "")
    return str(c)


def _unit_row(p: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
    sids = u.get("source_ids") or {}
    return {
        "property_id": _prop_id(p),
        "property_name": _prop_name(p),
        "unit_id": u.get("unit_id") or "",
        "is_floor_plan_level": u.get("is_floor_plan_level"),
        "extraction_tier": u.get("extraction_tier") or "",
        "beds": u.get("beds"),
        "baths": u.get("baths"),
        "area_sqft": u.get("area") or u.get("sqft"),
        "rent_low": u.get("rent_low") or u.get("market_rent_low"),
        "rent_high": u.get("rent_high") or u.get("market_rent_high"),
        "availability_status": u.get("availability_status") or "",
        "available_date": u.get("available_date") or "",
        "floor_plan_name": u.get("floor_plan_name") or "",
        "floor": u.get("floor"),
        "building": u.get("building") or "",
        "lease_term": u.get("lease_term") or "",
        "move_in_date": u.get("move_in_date") or "",
        "concession": u.get("concession_text_clean") or u.get("concession_text") or "",
        "source_ids": json.dumps(sids) if sids else "",
    }


# ── sheet writers ────────────────────────────────────────────────────────────

def _write_table(ws: Worksheet, headers: list[str], rows: list[dict[str, Any]], start_row: int = 1) -> None:
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=start_row, column=c, value=h)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    for r, row in enumerate(rows, start=start_row + 1):
        for c, h in enumerate(headers, start=1):
            ws.cell(row=r, column=c, value=row.get(h))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    last_col = get_column_letter(len(headers))
    ws.auto_filter.ref = f"A{start_row}:{last_col}{start_row + len(rows)}"
    _autosize(ws, headers, rows)


def _autosize(ws: Worksheet, headers: list[str], rows: list[dict[str, Any]]) -> None:
    for c, h in enumerate(headers, start=1):
        width = len(str(h))
        for row in rows[:200]:  # sample for speed on huge sheets
            v = row.get(h)
            if v is not None:
                width = max(width, len(str(v)))
        ws.column_dimensions[get_column_letter(c)].width = min(max(width + 2, 8), 60)


def _write_summary(ws: Worksheet, report: dict[str, Any], properties: list[dict[str, Any]]) -> None:
    row = 1

    def put(label: str, value: Any, *, title: bool = False, sub: bool = False) -> None:
        nonlocal row
        cell = ws.cell(row=row, column=1, value=label)
        if title:
            cell.font = _TITLE_FONT
        elif sub:
            cell.font = _SUBTITLE_FONT
        else:
            cell.font = Font(bold=True)
        if value is not None:
            ws.cell(row=row, column=2, value=value)
        row += 1

    totals = report.get("totals") or {}
    put(f"Run Report — {report.get('run_date', '')}", None, title=True)
    row += 1
    put("Properties", totals.get("properties", len(properties)))
    put("Succeeded", totals.get("succeeded"))
    put("Failed", totals.get("failed"))
    put("Success rate %", totals.get("success_rate_pct"))
    put("Carry-forward", totals.get("carry_forward"))
    put("Operator-transparency", totals.get("operator_transparency"))
    row += 1

    def put_counter(title: str, d: dict[str, Any]) -> None:
        nonlocal row
        put(title, None, sub=True)
        for k, v in (d or {}).items():
            ws.cell(row=row, column=1, value=str(k))
            ws.cell(row=row, column=2, value=v)
            row += 1
        row += 1

    put_counter("Verdict distribution", report.get("verdict_distribution", {}))
    put_counter("Tier distribution", report.get("tier_distribution", {}))

    dq = report.get("data_quality") or {}
    put("Data quality", None, sub=True)
    put("  total units", dq.get("total_units"))
    put("  real-id units", dq.get("real_id_units"))
    put("  synthetic-id units", dq.get("synthetic_id_units"))
    put("  plan-level units", dq.get("plan_level_units"))
    put_counter("  by tier family", dq.get("by_tier_family", {}))

    put_counter("Field fill rates (%)", report.get("field_fill_rates", {}))
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 40


def _recompute_report(properties: list[dict[str, Any]]) -> dict[str, Any]:
    """Minimal summary when report.json is absent — verdicts + units + fills."""
    verdicts: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    fill = dict.fromkeys(_FILL_FIELDS, 0)
    n_units = 0
    for p in properties:
        verdicts[str((p.get("_meta") or {}).get("verdict") or "UNKNOWN")] += 1
        for u in (p.get("units") or []):
            n_units += 1
            tiers[str(u.get("extraction_tier") or "NONE")] += 1
            for k in _FILL_FIELDS:
                # Same sentinel rule as run_report.build — ``area`` uses the
                # -1 ABSENT sentinel, which an inline emptiness test scores
                # as filled. See schema_v2.field_is_absent.
                if not field_is_absent(k, u.get(k)):
                    fill[k] += 1
    succeeded = sum(v for k, v in verdicts.items() if k.startswith("SUCCESS"))
    total = len(properties)
    return {
        "run_date": "",
        "totals": {
            "properties": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "success_rate_pct": round(100 * succeeded / total, 2) if total else 0,
        },
        "verdict_distribution": dict(verdicts.most_common()),
        "tier_distribution": dict(tiers.most_common()),
        "data_quality": {"total_units": n_units},
        "field_fill_rates": {
            k: (round(100 * fill[k] / n_units, 1) if n_units else 0.0) for k in _FILL_FIELDS
        },
    }


# ── workbook assembly ────────────────────────────────────────────────────────

_PROP_HEADERS = [
    "property_id", "name", "city", "state", "zip", "website", "verdict",
    "verdict_reason", "units", "confidence", "adapter", "detected_pms",
    "winning_tier", "fetch_outcome", "render_mode", "proxied", "page_load_ms",
    "real_id_units", "synthetic_id_units", "plan_level_units", "is_lease_up",
    "llm_cost_usd", "concession_banner",
]
_UNIT_HEADERS = [
    "property_id", "property_name", "unit_id", "is_floor_plan_level",
    "extraction_tier", "beds", "baths", "area_sqft", "rent_low", "rent_high",
    "availability_status", "available_date", "floor_plan_name", "floor",
    "building", "lease_term", "move_in_date", "concession", "source_ids",
]


def build_workbook(properties: list[dict[str, Any]], report: dict[str, Any] | None) -> Workbook:
    """Assemble the 3-sheet workbook. Pure; safe on empty input."""
    rep = report or _recompute_report(properties)
    wb = Workbook()
    _write_summary(wb.active, rep, properties)  # type: ignore[arg-type]
    wb.active.title = "Summary"

    ws_p = wb.create_sheet("Properties")
    _write_table(ws_p, _PROP_HEADERS, [_property_row(p) for p in properties])

    ws_u = wb.create_sheet("Units")
    unit_rows = [_unit_row(p, u) for p in properties for u in (p.get("units") or [])]
    _write_table(ws_u, _UNIT_HEADERS, unit_rows)
    return wb


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build an Excel report from a Jugnu run.")
    ap.add_argument("--run-dir", type=Path, help="Run dir containing properties.json + report.json")
    ap.add_argument("--properties", type=Path, help="Path to properties.json (overrides --run-dir)")
    ap.add_argument("--report", type=Path, help="Path to report.json (optional)")
    ap.add_argument("--out", type=Path, help="Output .xlsx (default: <run-dir>/report.xlsx)")
    args = ap.parse_args(argv)

    if args.properties:
        props_path = args.properties
        report_path = args.report
        out = args.out or props_path.with_name("report.xlsx")
    elif args.run_dir:
        props_path = args.run_dir / "properties.json"
        report_path = args.report or (args.run_dir / "report.json")
        out = args.out or (args.run_dir / "report.xlsx")
    else:
        ap.error("provide --run-dir or --properties")
        return 2

    if not props_path.exists():
        print(f"error: {props_path} not found", file=sys.stderr)
        return 1
    properties = json.loads(props_path.read_text(encoding="utf-8"))
    if isinstance(properties, dict):
        properties = properties.get("properties") or list(properties.values())
    report = None
    if report_path and report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = None

    wb = build_workbook(properties, report)
    wb.save(out)
    n_units = sum(len(p.get("units") or []) for p in properties)
    print(f"wrote {out}  ({len(properties)} properties, {n_units} units)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
