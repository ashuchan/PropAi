#!/usr/bin/env python3
"""Consolidate strict net-new Entrata recoveries without editing the ledger."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ma_poc.core.identity import unit_has_real_anchor


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
REMAINING = ROOT / "strict_recovery_remaining_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
DIRECT = OUT / "evidence_entrata_embedded_direct_current_strict.json"
HB_BATCH2 = OUT / "evidence_entrata_remaining29_hb_batch2_strict.json"
HB_REPLAY = OUT / "evidence_entrata_remaining29_hb_batch3_positive_replay_strict.json"
DIRECT_AUDIT = OUT / "evidence_entrata_residual_current_direct_audit.json"
CONSOLIDATED = OUT / "evidence_entrata_remaining29_current_strict_consolidated.json"
NET_NEW_IDS = OUT / "strict_entrata_remaining29_net_new_ids.json"
HB_IDS = {43908, 247119}
HB_REPLAY_IDS = {70993, 276162}
DIRECT_IDS = {9473, 72391, 298586}
EXPECTED_IDS = HB_IDS | HB_REPLAY_IDS | DIRECT_IDS
RENT_FIELDS = (
    "market_rent_low",
    "market_rent_high",
    "rent_low",
    "rent_high",
    "asking_rent",
    "rent",
)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_rows() -> dict[int, dict[str, str]]:
    with PROPERTIES.open(encoding="utf-8-sig", newline="") as handle:
        return {
            int(row["apartmentid"]): row
            for row in csv.DictReader(handle)
            if str(row.get("apartmentid") or "").isdigit()
        }


def positive_rent(row: dict[str, Any]) -> bool:
    for key in RENT_FIELDS:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)) and float(value) > 0:
            return True
    return False


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


def validate_rows(rows: list[dict[str, Any]], source_urls: list[str]) -> dict[str, int]:
    assert rows and source_urls
    units: list[str] = []
    uids: list[str] = []
    for row in rows:
        unit = str(row.get("unit_number") or "").strip()
        ids = row.get("source_ids")
        assert isinstance(ids, dict)
        uid = str(ids.get("entrata_uid") or "").strip()
        fpid = str(ids.get("entrata_fpid") or "").strip()
        assert unit and uid and fpid
        assert unit_has_real_anchor(row) and positive_rent(row)
        assert str(row.get("source_api_url") or "") in source_urls
        units.append(unit)
        uids.append(uid)
    assert len(units) == len(set(units)) == len(rows)
    assert len(uids) == len(set(uids)) == len(rows)
    return {
        "rows_with_native_unit_number": len(rows),
        "rows_with_native_entrata_uid": len(rows),
        "rows_with_native_entrata_fpid": len(rows),
        "rows_with_positive_rent": len(rows),
        "distinct_unit_numbers": len(rows),
        "distinct_entrata_uids": len(rows),
    }


def split_hb(
    path: Path,
    strict_ids: set[int],
    metadata: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["captcha_solving"] is False
    by_id = {int(row["property_id"]): row for row in payload["results"]}
    assert strict_ids.issubset(by_id)
    entries: list[dict[str, Any]] = []
    for property_id in sorted(strict_ids):
        row = by_id[property_id]
        assert row["outcome"] == "STRICT_UNIT_QUALIFIED"
        assert row["property_identity_match"] is True
        assert row["seed_identity"]["pass"] is True
        assert row["index_identity"]["pass"] is True
        assert row["session_calls"] == 1
        assert row["session_options"]["solveCaptchas"] is False
        assert row["contamination_verdict"].startswith("pass_")
        rows = row["native_rows"]
        sources = row["source_urls"]
        gates = validate_rows(rows, sources)
        assert len(rows) == row["native_identity_rows"]
        assert len(rows) == row["native_positive_rent_rows"]
        assert all(same_origin(source, row["index_url"]) for source in sources)
        meta = metadata[property_id]
        artifact = OUT / f"evidence_entrata_{property_id}_remaining29_current_strict.json"
        property_payload = {
            "result_type": "strict_current_exact_property_entrata_hyperbrowser",
            "capture_timestamp_utc": payload["summary"]["capture_timestamp_utc"],
            "property": {
                "property_id": property_id,
                "property_name": meta["name"],
                "website": meta["website"],
                "address": meta["address"],
                "city": meta["city"],
                "state": meta["state"],
                "zip": meta["zip"],
            },
            "provider": "entrata_prospectportal",
            "source_audit_artifact": str(path),
            "source_audit_sha256": file_sha(path),
            "seed_url": row["seed_url"],
            "seed_final_url": row["seed_final_url"],
            "index_url": row["index_url"],
            "seed_navigation_attempts": row["seed_navigation_attempts"],
            "index_navigation_attempts": row["index_navigation_attempts"],
            "published_plan_links": row["published_plan_links"],
            "published_vus_links": row["published_vus_links"],
            "fetches": row["fetches"],
            "session_calls": 1,
            "session_options": row["session_options"],
            "property_identity_match": True,
            "seed_identity": row["seed_identity"],
            "index_identity": row["index_identity"],
            "strict_gates": {
                **gates,
                "exact_seed_and_index_property_identity": True,
                "published_same_origin_routes_only": True,
                "captcha_solving": False,
                "sibling_or_cross_property_rows": 0,
            },
            "contamination_verdict": row["contamination_verdict"],
            "native_identity_rows": len(rows),
            "native_positive_rent_rows": len(rows),
            "source_urls": sources,
            "native_rows": rows,
        }
        artifact.write_text(json.dumps(property_payload, indent=2) + "\n")
        entries.append({**property_payload, "artifact": str(artifact)})
    return entries


def direct_entries() -> list[dict[str, Any]]:
    payload = json.loads(DIRECT.read_text(encoding="utf-8"))
    assert payload["captcha_solving"] is False
    assert payload["hyperbrowser_sessions_used"] == 0
    assert payload["paid_canary"] is False
    entries: list[dict[str, Any]] = []
    for item in payload["properties"]:
        property_id = int(item["property"]["property_id"])
        assert property_id in DIRECT_IDS
        assert item["property_identity_match"] is True
        assert item["contamination_verdict"].startswith("pass_")
        gates = validate_rows(item["native_rows"], item["source_urls"])
        assert len(item["native_rows"]) == item["native_positive_rent_rows"]
        artifact = {
            9473: OUT / "evidence_entrata_9473_village_cliffs_current_strict.json",
            72391: OUT / "evidence_entrata_72391_lumina_current_strict.json",
            298586: OUT / "evidence_entrata_298586_gateway_lofts_current_strict.json",
        }[property_id]
        assert artifact.exists()
        entries.append({**item, "strict_gates": {**item["strict_gates"], **gates}, "artifact": str(artifact)})
    assert {int(item["property"]["property_id"]) for item in entries} == DIRECT_IDS
    return entries


def main() -> None:
    metadata = canonical_rows()
    ledger_sha_before = file_sha(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger_ids = {int(row["property_id"]) for row in ledger_rows}
    with REMAINING.open(encoding="utf-8-sig", newline="") as handle:
        residual = [
            row
            for row in csv.DictReader(handle)
            if row.get("current_detected_adapter") == "entrata"
        ]
    residual_ids = {int(row["property_id"]) for row in residual}

    entries = [
        *split_hb(HB_BATCH2, HB_IDS, metadata),
        *split_hb(HB_REPLAY, HB_REPLAY_IDS, metadata),
        *direct_entries(),
    ]
    entry_ids = {int(item["property"]["property_id"]) for item in entries}
    assert entry_ids == EXPECTED_IDS
    assert EXPECTED_IDS.issubset(residual_ids)
    overlap = entry_ids & ledger_ids
    net_new = [
        item
        for item in entries
        if int(item["property"]["property_id"]) not in ledger_ids
    ]
    net_new_ids = sorted(int(item["property"]["property_id"]) for item in net_new)

    consolidated = {
        "result_type": "strict_current_remaining29_entrata_consolidated",
        "capture_timestamp_utc": datetime.now(UTC).isoformat(),
        "authoritative_ledger": {
            "path": str(LEDGER),
            "sha256": ledger_sha_before,
            "rows": len(ledger_rows),
            "unique_property_ids": len(ledger_ids),
        },
        "residual_source": {
            "path": str(REMAINING),
            "entrata_properties_at_materialization": len(residual),
        },
        "strict_properties_before_ledger_overlap": len(entries),
        "strict_property_ids_before_ledger_overlap": sorted(entry_ids),
        "overlap_with_latest_ledger_ids": sorted(overlap),
        "net_new_properties": len(net_new),
        "net_new_property_ids": net_new_ids,
        "net_new_native_positive_rent_rows": sum(
            int(item["native_positive_rent_rows"]) for item in net_new
        ),
        "source_audits": {
            "all_29_current_direct": str(DIRECT_AUDIT),
            "hb_batch2": str(HB_BATCH2),
            "hb_positive_replay": str(HB_REPLAY),
            "embedded_direct": str(DIRECT),
        },
        "hyperbrowser_session_accounting": {
            "new_sessions_total_this_rebuilt_ledger_pass": 25,
            "sessions_in_persisted_strict_audits": 10,
            "sessions_in_nonmaterialized_or_negative_aborted_batches": 15,
            "note": (
                "Three batch processes were terminated after local regex-runtime "
                "stalls; their unpersisted results were not used as evidence."
            ),
        },
        "captcha_solving": False,
        "llm_used": False,
        "paid_canary": False,
        "shared_ledger_modified": False,
        "properties": net_new,
    }
    CONSOLIDATED.write_text(json.dumps(consolidated, indent=2) + "\n")
    ids_payload = {
        "result_type": "net_new_strict_remaining29_entrata_ids_only",
        "capture_timestamp_utc": consolidated["capture_timestamp_utc"],
        "net_new_property_ids": net_new_ids,
        "net_new_properties": len(net_new_ids),
        "net_new_native_positive_rent_rows": consolidated[
            "net_new_native_positive_rent_rows"
        ],
        "latest_ledger_sha256": ledger_sha_before,
        "latest_ledger_rows": len(ledger_rows),
        "shared_ledger_modified": False,
        "consolidated_evidence_artifact": str(CONSOLIDATED),
    }
    NET_NEW_IDS.write_text(json.dumps(ids_payload, indent=2) + "\n")
    assert file_sha(LEDGER) == ledger_sha_before
    print(json.dumps(ids_payload, indent=2))


if __name__ == "__main__":
    main()
