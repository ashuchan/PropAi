from __future__ import annotations

import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import _find_pp_conventional_index
from ma_poc.pms.adapters.rentcafe import _find_all_securecafe_bases
from ma_poc.pms.detector import detect_pms


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
RP_PATH = Path("/Users/ankur/Downloads/rp_unit_detail_0731.csv")

STRICT_IDS = {
    540, 567, 2198, 2948, 3169, 3912, 4124, 4554, 6477, 6701, 7980,
    9128, 11543, 12398, 12989, 14295, 15358, 16172, 17586, 18194, 18379,
    18736, 20262, 22934, 27125, 27349, 27594, 32134, 32793, 34303, 35590,
    35683, 38314, 38345, 38378, 38797, 39995, 40743, 41175, 42371, 43520,
    44513, 45755, 47750, 49921, 50172, 55729, 56182, 58939, 59649, 60125,
    64545, 64673, 67598, 67684, 67697, 68691, 68952, 69378, 72542, 74384,
    74488, 78593, 120193, 217738, 217796, 218786, 221995, 223547, 224720,
    224888, 228073, 231355, 237787, 240193, 251908, 251974, 253326, 253646,
    253966, 254122, 258584, 258661, 260564, 260697, 261116, 262174, 262717,
    263127, 265143, 270367, 277774, 281767, 284199, 291774, 293741, 297708,
    300327, 300689,
}

ONESITE = {
    2948, 4554, 12398, 14295, 15358, 16172, 18194, 18736, 39995, 43520,
    224888, 251908, 251974, 253326, 253646, 261116, 265143, 270367, 284199,
    291774,
}
RENTCAFE = {
    567, 3912, 6701, 17586, 22934, 27594, 35590, 38314, 38797, 40743,
    41175, 44513, 55729, 60125, 64673, 67697, 68691, 69378, 120193,
    223547, 224720, 240193, 253966, 258584, 260564, 262174, 277774,
    281767, 297708, 300327, 300689,
}
GENERIC = {20262, 45755, 49921, 231355}
ENTRATA_DIRECT_OR_PRIOR_HB = {3169, 7980, 11543, 35683, 64545, 228073}
JONAH_OR_GSC = {6477, 34303, 74488, 78593, 217796, 221995}
PAGE_LOCAL = {
    2198, 9128, 15358, 22934, 27125, 27349, 32134, 38378, 74384,
    262717, 263127, 300327,
}

KNOWN_RECOVERY = {
    3912: ("unit", 11),
    6477: ("unit", 10),
    34303: ("unit", 155),
    74488: ("unit", 140),
    78593: ("unit", 32),
    217796: ("unit", 3),
    221995: ("unit", 7),
    20262: ("plan", 3),
    45755: ("plan", 2),
    49921: ("plan", 4),
    231355: ("unit", 31),
    11543: ("plan", 8),
    35683: ("plan", 3),
    228073: ("plan", 7),
    67684: ("plan", 2),
    218786: ("plan", 44),
}

CAVEATS = {
    540: "UI/blank plan names; property-scoped numeric data retained",
    4124: "UI/blank plan names; property-scoped numeric data retained",
    237787: "UI/blank plan names; property-scoped numeric data retained",
    218786: "44-plan result retained because RP independently has the same 44 names",
    67684: "exact embedded floorPlans script; plan-level",
}


def body_for(property_id: int) -> str:
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return ""
    return gzip.open(path, "rb").read().decode("utf-8", "replace")


def rp_counts() -> dict[int, dict]:
    counts: dict[int, dict] = defaultdict(
        lambda: {"unit_rows": 0, "plans": set(), "dated_rows": 0}
    )
    with RP_PATH.open(encoding="cp1252", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                property_id = int(row["apartmentid"])
            except (TypeError, ValueError):
                continue
            entry = counts[property_id]
            entry["unit_rows"] += 1
            name = str(row.get("floorplanname") or "").strip()
            if name:
                entry["plans"].add(name)
            if str(row.get("availabledate") or "").strip():
                entry["dated_rows"] += 1
    return counts


def evidence_lane(property_id: int) -> str:
    if property_id in RENTCAFE:
        return "rentcafe_direct_e2e"
    if property_id in ONESITE:
        return "onesite_direct_e2e"
    if property_id in GENERIC:
        return "generic_direct_e2e"
    if property_id in ENTRATA_DIRECT_OR_PRIOR_HB:
        return "entrata_exact_property_e2e"
    if property_id in JONAH_OR_GSC:
        return "jonah_gsc_or_rentpress_e2e"
    if property_id in PAGE_LOCAL:
        return "page_local_archived_replay"
    return "other_current_code_e2e_or_archived_replay"


def securecafe_bases(record: dict, html: str) -> list[str]:
    website = str(record.get("website") or "")
    try:
        ctx = AdapterContext(
            base_url=website,
            detected=detect_pms(website, page_html=html),
            profile=None,
            expected_total_units=None,
            property_id=str(record["property_id"]),
            fetch_result=SimpleNamespace(body=html.encode(), final_url=website),
            property_name=str(record.get("proj_name") or ""),
            address=str(record.get("address") or ""),
            city=str(record.get("city") or ""),
            state=str(record.get("state") or ""),
            zip_code=str(record.get("zip_code") or ""),
        )
        return _find_all_securecafe_bases(html.replace("\\/", "/"), ctx)
    except Exception:
        return []


def main() -> None:
    records = json.loads((ROOT / "failed344.json").read_text())
    by_id = {int(row["property_id"]): row for row in records}
    oracle = rp_counts()
    ledger = []
    for property_id in sorted(STRICT_IDS):
        row = by_id[property_id]
        html = body_for(property_id)
        detected = detect_pms(str(row.get("website") or ""), page_html=html).pms
        known_level, known_count = KNOWN_RECOVERY.get(property_id, ("unknown", None))
        if property_id in RENTCAFE and property_id != 3912:
            known_level = "unit"
        rp = oracle.get(property_id, {"unit_rows": 0, "plans": set(), "dated_rows": 0})
        ledger.append(
            {
                "property_id": property_id,
                "property_name": str(row.get("proj_name") or ""),
                "website": str(row.get("website") or ""),
                "source_adapter_0731": row.get("adapter"),
                "current_detected_adapter": detected,
                "evidence_lane": evidence_lane(property_id),
                "recovery_level": known_level,
                "recovered_native_or_plan_row_count": known_count,
                "rp_oracle_native_unit_rows": int(rp["unit_rows"]),
                "rp_oracle_distinct_floorplans": len(rp["plans"]),
                "rp_oracle_rows_with_availability_date": int(rp["dated_rows"]),
                "quality_caveat": CAVEATS.get(property_id, ""),
            }
        )

    residual = []
    for row in records:
        property_id = int(row["property_id"])
        if property_id in STRICT_IDS:
            continue
        html = body_for(property_id)
        website = str(row.get("website") or "")
        detected = detect_pms(website, page_html=html).pms
        entrata = _find_pp_conventional_index(html, website)
        securecafe = securecafe_bases(row, html) if row.get("adapter") == "rentcafe" else []
        rp = oracle.get(property_id, {"unit_rows": 0, "plans": set(), "dated_rows": 0})
        if entrata:
            disposition = "hyperbrowser_exact_entrata_anchor_pending_native_unit_drill"
        elif securecafe:
            disposition = "securecafe_exact_base_direct_blocked_or_pending_hb"
        elif detected in {"appfolio", "rentmanager", "generic_plan_text", "wix_nopms"}:
            disposition = "direct_or_archived_lane_exhausted_no_strict_win"
        else:
            disposition = "residual_unconverted"
        residual.append(
            {
                "property_id": property_id,
                "property_name": str(row.get("proj_name") or ""),
                "website": website,
                "source_adapter_0731": row.get("adapter"),
                "current_detected_adapter": detected,
                "exact_entrata_anchor": entrata[0] if entrata else "",
                "securecafe_base_count": len(securecafe),
                "rp_oracle_native_unit_rows": int(rp["unit_rows"]),
                "rp_oracle_distinct_floorplans": len(rp["plans"]),
                "disposition": disposition,
            }
        )

    ledger_path = ROOT / "strict99_authoritative_ledger.csv"
    with ledger_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ledger[0]))
        writer.writeheader()
        writer.writerows(ledger)
    residual_path = ROOT / "strict99_residual245_classification.csv"
    with residual_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(residual[0]))
        writer.writeheader()
        writer.writerows(residual)

    summary = {
        "cohort": "2026-07-31-fetchfix-5k FAILED_NO_DATA exact cohort",
        "strict_nonfailed_count": len(ledger),
        "strict_rate": round(len(ledger) / len(records), 6),
        "residual_count": len(residual),
        "goal_target": 207,
        "canary_confirmed": 0,
        "provenance": "local current-code exact evidence only; RP is validation oracle, never extraction source",
        "known_recovery_level_counts": dict(Counter(row["recovery_level"] for row in ledger)),
        "evidence_lane_counts": dict(Counter(row["evidence_lane"] for row in ledger)),
        "residual_detected_adapter_counts": dict(Counter(row["current_detected_adapter"] for row in residual)),
        "residual_disposition_counts": dict(Counter(row["disposition"] for row in residual)),
        "notes": [
            "Recovery level is unknown where prior terminal-only probe output did not retain per-property row counts.",
            "RP counts are same-day validation-oracle counts and are not recovered output counts.",
            "Plan-only rows remain FAILED_NO_DATA conversions but do not satisfy the native-unit-qualified 207 gate.",
        ],
    }
    (ROOT / "strict99_reconciliation_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ledger": str(ledger_path), "residual": str(residual_path), "summary": summary}))


if __name__ == "__main__":
    main()
