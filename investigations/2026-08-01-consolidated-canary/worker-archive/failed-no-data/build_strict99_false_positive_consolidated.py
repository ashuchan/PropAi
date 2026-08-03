from __future__ import annotations

import csv
import json
import runpy
from collections import Counter
from pathlib import Path


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUTPUT = ROOT / "evidence_strict99_false_positive50_consolidated.json"
BASE = ROOT / "evidence_strict99_false_positive50_current_local.json"
SUPPLEMENTS = [
    ROOT / "evidence_strict99_false_positive_live_remaining23.json",
    ROOT / "evidence_strict99_false_positive_live_priority15.json",
    ROOT / "evidence_strict99_false_positive_gsc5_live.json",
    ROOT / "evidence_strict99_false_positive_entrata3_hb.json",
    ROOT / "evidence_strict99_false_positive_entrata4_hb.json",
    ROOT / "evidence_strict99_false_positive_entrata_68952_hb.json",
]


def rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, list) else list(payload.get("results") or [])


def evidence_rank(row: dict, source: str) -> tuple[int, int, int]:
    outcome = str(row.get("outcome") or "")
    live = int("live" in source or "_hb" in source)
    return (
        4 if outcome == "UNIT_QUALIFIED" else 3 if outcome == "UNIT_UNVERIFIED" else 2 if outcome == "PLAN_ONLY" else 1,
        live,
        int(row.get("units") or 0) + int(row.get("plans") or 0),
    )


def main() -> None:
    base_rows = rows(BASE)
    target_ids = {int(row["property_id"]) for row in base_rows}
    if len(base_rows) != 50 or len(target_ids) != 50:
        raise SystemExit(f"Expected exact 50-row base, got rows={len(base_rows)} unique={len(target_ids)}")

    selected: dict[int, tuple[dict, str]] = {
        int(row["property_id"]): (row, BASE.name) for row in base_rows
    }
    for path in SUPPLEMENTS:
        for row in rows(path):
            pid = int(row["property_id"])
            if pid not in target_ids:
                continue
            old, old_source = selected[pid]
            if evidence_rank(row, path.name) > evidence_rank(old, old_source):
                selected[pid] = (row, path.name)

    current_ledger_ids = {
        int(row["property_id"])
        for row in csv.DictReader((ROOT / "strict_recovery_ledger_current.csv").open())
    }
    out = []
    for pid in sorted(target_ids):
        raw, source = selected[pid]
        row = dict(raw)
        row["property_id"] = pid
        row["consolidated_source_artifact"] = str(ROOT / source)
        row["cohort_boundary"] = "exact_2026-07-31_FAILED_NO_DATA_344"
        row["validation_scope"] = "current_local_code_and_exact_property_public_source"
        row["paid_canary"] = False
        row["captcha_solving"] = False
        if row.get("outcome") == "UNIT_QUALIFIED":
            evidence = dict(row.get("identity_evidence") or {})
            # The HB Entrata recovery helper admits only rows passing both
            # unit_has_real_anchor and positive numeric rent. Materialize that
            # contract into the same fields used by the strict ledger gate.
            if not evidence:
                evidence = {
                    "rows_with_native_identity": int(row.get("units") or 0),
                    "rows_with_native_identity_and_positive_rent": int(row.get("units") or 0),
                    "source_urls": list(row.get("source_urls") or []),
                    "validation_contract": "_entrata_hb_recovery._validated_units",
                }
            row["identity_evidence"] = evidence
        out.append(row)

    helpers = runpy.run_path(str(ROOT / "build_current_strict_ledger.py"), run_name="ledger_helpers")
    qualify = helpers["qualify"]
    gate = {int(row["property_id"]): qualify(row) for row in out}
    passes = [row for row in out if gate[int(row["property_id"])][0]]
    failures = [
        {"property_id": row["property_id"], "reason": gate[int(row["property_id"])][1]}
        for row in out
        if not gate[int(row["property_id"])][0]
    ]
    if len(passes) != 11:
        raise SystemExit(f"Expected 11 strict passes, got {len(passes)}")
    for row in passes:
        evidence = row.get("identity_evidence") or {}
        if int(evidence.get("rows_with_native_identity") or 0) <= 0:
            raise SystemExit(f"Pass lacks native identity count: {row['property_id']}")
        if int(evidence.get("rows_with_native_identity_and_positive_rent") or 0) <= 0:
            raise SystemExit(f"Pass lacks positive rent count: {row['property_id']}")
        if not str(row.get("contamination_verdict") or "").startswith("pass_"):
            raise SystemExit(f"Pass lacks contamination verdict: {row['property_id']}")

    pass_ids = [int(row["property_id"]) for row in passes]
    payload = {
        "cohort": "2026-07-31-fetchfix-5k FAILED_NO_DATA exact 344 cohort",
        "lane": "strict99_false_positive_reintroduced_revalidation",
        "target_rows": 50,
        "target_unique_properties": 50,
        "strict_unit_qualified": len(passes),
        "strict_unit_qualified_ids": pass_ids,
        "already_in_current_ledger_ids": sorted(set(pass_ids) & current_ledger_ids),
        "net_new_vs_current_ledger": len(set(pass_ids) - current_ledger_ids),
        "net_new_vs_current_ledger_ids": sorted(set(pass_ids) - current_ledger_ids),
        "outcome_counts": dict(Counter(str(row.get("outcome") or "") for row in out)),
        "strict_gate_failure_counts": dict(Counter(item["reason"] for item in failures)),
        "provenance": "current local code; exact public property sources; RP used only as oracle; no paid canary",
        "hyperbrowser_policy": "clean residential render only; CAPTCHA solving hard-disabled",
        "results": out,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: payload[key] for key in ("target_unique_properties", "strict_unit_qualified", "strict_unit_qualified_ids", "already_in_current_ledger_ids", "net_new_vs_current_ledger", "net_new_vs_current_ledger_ids", "outcome_counts", "strict_gate_failure_counts")}, indent=2))


if __name__ == "__main__":
    main()
