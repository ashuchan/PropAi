#!/usr/bin/env python3
"""Materialize net-new strict Entrata residual rows without touching the ledger."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ma_poc.core.identity import unit_has_real_anchor


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")
OUT = ROOT / "entrata_residual_lane"
LEDGER = ROOT / "strict_recovery_ledger_current.csv"
PROPERTIES = Path("ma_poc/config/properties.csv")
LAKEWOOD = OUT / "evidence_entrata_1375_lakewood_current_strict.json"
TWO_LIGHT = OUT / "evidence_entrata_67684_two_light_current_strict.json"
CANYON_SPRINGS = OUT / "evidence_entrata_40398_canyon_springs_current_strict.json"
HB_AUDIT = OUT / "evidence_entrata_profile_details_hb_strict.json"
HB_FOLLOWUP_AUDIT = OUT / "evidence_entrata_residual_followup_hb_strict.json"
DIRECT_AUDIT = OUT / "evidence_entrata_residual_current_direct_audit.json"
CONSOLIDATED = OUT / "evidence_entrata_residual_current_strict_consolidated.json"
LEDGER_ROWS = OUT / "strict_entrata_residual_net_new_ledger_rows.csv"
SUMMARY = OUT / "strict_entrata_residual_net_new_summary.json"
STRICT_HB_IDS = {18764, 54936, 257761}
STRICT_FOLLOWUP_HB_IDS = {19877, 46257}
LEDGER_FIELDS = [
    "property_id",
    "property_name",
    "website",
    "evidence_lane",
    "artifact",
    "units",
    "property_identity_match",
    "contamination_verdict",
    "native_identity_rows",
    "native_positive_rent_rows",
    "source_urls",
    "sample_native_unit_ids",
    "local_validation",
]
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


def ledger_state() -> tuple[str, list[dict[str, str]], set[int]]:
    digest = file_sha(LEDGER)
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = {
        int(row["property_id"])
        for row in rows
        if str(row.get("property_id") or "").isdigit()
    }
    return digest, rows, ids


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


def validate_native_rows(
    rows: list[dict[str, Any]],
    source_urls: list[str],
    *,
    require_entrata_source_ids: bool,
) -> dict[str, Any]:
    assert rows
    assert source_urls
    units: list[str] = []
    entrata_uids: list[str] = []
    fpid_rows = 0
    for row in rows:
        unit = str(row.get("unit_number") or "").strip()
        assert unit
        assert unit_has_real_anchor(row)
        assert positive_rent(row)
        source = str(row.get("source_api_url") or "")
        assert source in source_urls
        units.append(unit)
        source_ids = row.get("source_ids")
        if isinstance(source_ids, dict):
            uid = str(source_ids.get("entrata_uid") or "").strip()
            fpid = str(source_ids.get("entrata_fpid") or "").strip()
            if uid:
                entrata_uids.append(uid)
            if fpid:
                fpid_rows += 1
            if require_entrata_source_ids:
                assert uid and fpid
    assert len(units) == len(set(units)) == len(rows)
    if entrata_uids:
        assert len(entrata_uids) == len(set(entrata_uids))
    if require_entrata_source_ids:
        assert len(entrata_uids) == fpid_rows == len(rows)
    first_origin = source_urls[0]
    assert all(same_origin(url, first_origin) for url in source_urls)
    return {
        "rows_with_real_native_anchor": len(rows),
        "rows_with_positive_rent": len(rows),
        "distinct_unit_numbers": len(set(units)),
        "distinct_entrata_uids": len(set(entrata_uids)),
        "rows_with_entrata_floorplan_id": fpid_rows,
        "source_urls_same_origin": True,
    }


def lakewood_entry() -> dict[str, Any]:
    payload = json.loads(LAKEWOOD.read_text(encoding="utf-8"))
    assert payload["property"]["property_id"] == 1375
    assert payload.get("property_identity_match") is True
    assert payload.get("contamination_verdict", "").startswith("pass_")
    rows = payload.get("native_rows") or []
    source_urls = payload.get("source_urls") or []
    gates = validate_native_rows(
        rows,
        source_urls,
        require_entrata_source_ids=True,
    )
    assert len(rows) == 19
    return {
        "property_id": 1375,
        "property_name": payload["property"]["property_name"],
        "website": payload["property"]["website"],
        "artifact": str(LAKEWOOD),
        "evidence_lane": "entrata_residual_exact_direct_revalidation",
        "source_urls": source_urls,
        "native_rows": rows,
        "strict_gates": {**payload.get("strict_gates", {}), **gates},
        "property_identity_match": True,
        "contamination_verdict": payload["contamination_verdict"],
        "local_validation": (
            "saved_hb_revalidated_by_current_exact_direct_session_no_paid_canary"
        ),
    }


def two_light_entry() -> dict[str, Any]:
    payload = json.loads(TWO_LIGHT.read_text(encoding="utf-8"))
    assert payload["property"]["property_id"] == 67684
    assert payload.get("property_identity_match") is True
    assert payload.get("contamination_verdict", "").startswith("pass_")
    assert int(payload.get("hyperbrowser_sessions_used") or 0) == 0
    rows = payload.get("native_rows") or []
    source_urls = payload.get("source_urls") or []
    gates = validate_native_rows(
        rows,
        source_urls,
        require_entrata_source_ids=True,
    )
    assert len(rows) == 2
    return {
        "property_id": 67684,
        "property_name": payload["property"]["property_name"],
        "website": payload["property"]["website"],
        "artifact": str(TWO_LIGHT),
        "evidence_lane": "entrata_residual_exact_profile_route_direct_vus",
        "source_urls": source_urls,
        "native_rows": rows,
        "strict_gates": {**payload.get("strict_gates", {}), **gates},
        "property_identity_match": True,
        "contamination_verdict": payload["contamination_verdict"],
        "local_validation": (
            "current_exact_profile_route_published_vus_no_hb_no_paid_canary"
        ),
    }


def canyon_springs_entry() -> dict[str, Any]:
    payload = json.loads(CANYON_SPRINGS.read_text(encoding="utf-8"))
    assert payload["property"]["property_id"] == 40398
    assert payload.get("property_identity_match") is True
    assert payload.get("contamination_verdict", "").startswith("pass_")
    assert int(payload.get("hyperbrowser_sessions_used") or 0) == 0
    rows = payload.get("native_rows") or []
    source_urls = payload.get("source_urls") or []
    gates = validate_native_rows(
        rows,
        source_urls,
        require_entrata_source_ids=True,
    )
    assert len(rows) == 1
    return {
        "property_id": 40398,
        "property_name": payload["property"]["property_name"],
        "website": payload["property"]["website"],
        "artifact": str(CANYON_SPRINGS),
        "evidence_lane": "entrata_residual_current_canonical_route_direct_vus",
        "source_urls": source_urls,
        "native_rows": rows,
        "strict_gates": {**payload.get("strict_gates", {}), **gates},
        "property_identity_match": True,
        "contamination_verdict": payload["contamination_verdict"],
        "local_validation": (
            "current_canonical_homepage_to_exact_grid_to_published_vus_no_hb_no_canary"
        ),
    }


def split_hb_entries(metadata: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    payload = json.loads(HB_AUDIT.read_text(encoding="utf-8"))
    assert payload["summary"]["captcha_solving"] is False
    by_id = {int(row["property_id"]): row for row in payload.get("results", [])}
    assert STRICT_HB_IDS.issubset(by_id)
    out: list[dict[str, Any]] = []
    for property_id in sorted(STRICT_HB_IDS):
        row = by_id[property_id]
        assert row.get("outcome") == "STRICT_UNIT_QUALIFIED"
        assert row.get("property_identity_match") is True
        assert row.get("contamination_verdict", "").startswith("pass_")
        assert int(row.get("session_calls") or 0) == 1
        assert row.get("session_options", {}).get("solveCaptchas") is False
        assert row.get("final_url") == row.get("target_url")
        assert row.get("identity_evidence", {}).get("pass") is True
        source_urls = row.get("source_urls") or []
        native_rows = row.get("native_rows") or []
        gates = validate_native_rows(
            native_rows,
            source_urls,
            require_entrata_source_ids=True,
        )
        assert len(native_rows) == int(row.get("native_identity_rows") or 0)
        assert len(native_rows) == int(row.get("native_positive_rent_rows") or 0)
        canonical = metadata[property_id]
        property_artifact = OUT / f"evidence_entrata_{property_id}_current_strict.json"
        property_payload = {
            "result_type": "strict_current_exact_profile_detail_hyperbrowser",
            "property": {
                "property_id": property_id,
                "property_name": canonical.get("name") or row["property_name"],
                "website": canonical.get("website") or row.get("website") or "",
                "address": canonical.get("address") or "",
                "city": canonical.get("city") or "",
                "state": canonical.get("state") or "",
                "zip": canonical.get("zip") or "",
            },
            "provider": "entrata_prospectportal",
            "source_audit_artifact": str(HB_AUDIT),
            "source_audit_sha256": file_sha(HB_AUDIT),
            "target_url": row["target_url"],
            "final_url": row["final_url"],
            "profile_provenance": row["profile_provenance"],
            "session_calls": 1,
            "session_options": row["session_options"],
            "navigation_attempts": row["navigation_attempts"],
            "property_identity_match": True,
            "identity_evidence": row["identity_evidence"],
            "strict_gates": {
                **gates,
                "exact_profile_url_no_redirect": True,
                "captcha_solving": False,
                "sibling_or_cross_property_rows": 0,
            },
            "contamination_verdict": row["contamination_verdict"],
            "native_identity_rows": len(native_rows),
            "native_positive_rent_rows": len(native_rows),
            "source_urls": source_urls,
            "native_rows": native_rows,
        }
        property_artifact.write_text(
            json.dumps(property_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        out.append(
            {
                "property_id": property_id,
                "property_name": property_payload["property"]["property_name"],
                "website": property_payload["property"]["website"],
                "artifact": str(property_artifact),
                "evidence_lane": "entrata_residual_exact_profile_detail_hb",
                "source_urls": source_urls,
                "native_rows": native_rows,
                "strict_gates": property_payload["strict_gates"],
                "property_identity_match": True,
                "contamination_verdict": row["contamination_verdict"],
                "local_validation": (
                    "one_bounded_hb_session_exact_profile_url_no_captcha_no_canary"
                ),
            }
        )
    return out


def split_followup_hb_entries(
    metadata: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    payload = json.loads(HB_FOLLOWUP_AUDIT.read_text(encoding="utf-8"))
    assert payload["summary"]["captcha_solving"] is False
    assert int(payload["summary"]["sessions_used"]) == 4
    by_id = {int(row["property_id"]): row for row in payload.get("results", [])}
    assert STRICT_FOLLOWUP_HB_IDS.issubset(by_id)
    out: list[dict[str, Any]] = []
    for property_id in sorted(STRICT_FOLLOWUP_HB_IDS):
        row = by_id[property_id]
        assert row.get("outcome") == "STRICT_UNIT_QUALIFIED"
        assert row.get("property_identity_match") is True
        assert row.get("contamination_verdict", "").startswith("pass_")
        assert int(row.get("session_calls") or 0) == 1
        assert row.get("session_options", {}).get("solveCaptchas") is False
        assert row.get("seed_identity", {}).get("pass") is True
        assert row.get("index_identity", {}).get("pass") is True
        source_urls = row.get("source_urls") or []
        native_rows = row.get("native_rows") or []
        gates = validate_native_rows(
            native_rows,
            source_urls,
            require_entrata_source_ids=True,
        )
        assert len(native_rows) == int(row.get("native_identity_rows") or 0)
        assert len(native_rows) == int(row.get("native_positive_rent_rows") or 0)
        index_url = str(row.get("index_url") or "")
        assert index_url
        assert all(same_origin(url, index_url) for url in source_urls)
        canonical = metadata[property_id]
        property_artifact = OUT / f"evidence_entrata_{property_id}_current_strict.json"
        property_payload = {
            "result_type": "strict_current_exact_property_hyperbrowser_followup",
            "property": {
                "property_id": property_id,
                "property_name": canonical.get("name") or row["property_name"],
                "website": canonical.get("website") or row.get("website") or "",
                "address": canonical.get("address") or "",
                "city": canonical.get("city") or "",
                "state": canonical.get("state") or "",
                "zip": canonical.get("zip") or "",
            },
            "provider": "entrata_prospectportal",
            "source_audit_artifact": str(HB_FOLLOWUP_AUDIT),
            "source_audit_sha256": file_sha(HB_FOLLOWUP_AUDIT),
            "seed_url": row["seed_url"],
            "seed_final_url": row["seed_final_url"],
            "index_url": index_url,
            "session_calls": 1,
            "session_options": row["session_options"],
            "seed_navigation_attempts": row["seed_navigation_attempts"],
            "index_navigation_attempts": row["index_navigation_attempts"],
            "published_plan_links": row["published_plan_links"],
            "published_vus_links": row["published_vus_links"],
            "fetches": row["fetches"],
            "property_identity_match": True,
            "seed_identity": row["seed_identity"],
            "index_identity": row["index_identity"],
            "strict_gates": {
                **gates,
                "seed_exact_property_identity": True,
                "index_exact_property_identity": True,
                "published_same_origin_routes_only": True,
                "captcha_solving": False,
                "sibling_or_cross_property_rows": 0,
            },
            "contamination_verdict": row["contamination_verdict"],
            "native_identity_rows": len(native_rows),
            "native_positive_rent_rows": len(native_rows),
            "source_urls": source_urls,
            "native_rows": native_rows,
        }
        property_artifact.write_text(
            json.dumps(property_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        out.append(
            {
                "property_id": property_id,
                "property_name": property_payload["property"]["property_name"],
                "website": property_payload["property"]["website"],
                "artifact": str(property_artifact),
                "evidence_lane": "entrata_residual_exact_property_hb_followup",
                "source_urls": source_urls,
                "native_rows": native_rows,
                "strict_gates": property_payload["strict_gates"],
                "property_identity_match": True,
                "contamination_verdict": row["contamination_verdict"],
                "local_validation": (
                    "one_bounded_hb_session_exact_property_no_captcha_no_canary"
                ),
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    metadata = canonical_rows()
    ledger_sha_before, ledger_before, ledger_ids = ledger_state()
    entries = [
        lakewood_entry(),
        *split_hb_entries(metadata),
        *split_followup_hb_entries(metadata),
        canyon_springs_entry(),
        two_light_entry(),
    ]
    entry_ids = {int(entry["property_id"]) for entry in entries}
    assert entry_ids == {
        1375,
        40398,
        67684,
        *STRICT_HB_IDS,
        *STRICT_FOLLOWUP_HB_IDS,
    }
    overlap = entry_ids & ledger_ids
    net_new = [entry for entry in entries if int(entry["property_id"]) not in ledger_ids]
    assert not overlap
    assert len(net_new) == 8

    ledger_rows: list[dict[str, str]] = []
    for entry in net_new:
        rows = entry["native_rows"]
        sample_ids = []
        for row in rows[:5]:
            source_ids = row.get("source_ids") or {}
            uid = str(source_ids.get("entrata_uid") or "")
            sample_ids.append(
                f"{row['unit_number']}:{uid}" if uid else str(row["unit_number"])
            )
        ledger_rows.append(
            {
                "property_id": str(entry["property_id"]),
                "property_name": entry["property_name"],
                "website": entry["website"],
                "evidence_lane": entry["evidence_lane"],
                "artifact": entry["artifact"],
                "units": str(len(rows)),
                "property_identity_match": "True",
                "contamination_verdict": entry["contamination_verdict"],
                "native_identity_rows": str(len(rows)),
                "native_positive_rent_rows": str(len(rows)),
                "source_urls": " | ".join(entry["source_urls"]),
                "sample_native_unit_ids": " | ".join(sample_ids),
                "local_validation": entry["local_validation"],
            }
        )
    ledger_rows.sort(key=lambda row: int(row["property_id"]))
    with LEDGER_ROWS.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(ledger_rows)

    consolidated = {
        "result_type": "strict_current_entrata_residual_consolidated",
        "strict_properties": len(net_new),
        "strict_property_ids": [int(row["property_id"]) for row in ledger_rows],
        "native_positive_rent_rows": sum(len(entry["native_rows"]) for entry in net_new),
        "property_artifacts": [entry["artifact"] for entry in net_new],
        "source_audits": {
            "direct_residual_audit": str(DIRECT_AUDIT),
            "exact_profile_detail_hb_audit": str(HB_AUDIT),
            "exact_property_followup_hb_audit": str(HB_FOLLOWUP_AUDIT),
            "lakewood_direct_revalidation": str(LAKEWOOD),
            "canyon_springs_direct_vus_revalidation": str(CANYON_SPRINGS),
            "two_light_direct_vus_revalidation": str(TWO_LIGHT),
        },
        "new_hyperbrowser_sessions_this_lane": 8,
        "captcha_solving": False,
        "paid_canary": False,
        "properties": net_new,
    }
    CONSOLIDATED.write_text(
        json.dumps(consolidated, indent=2) + "\n",
        encoding="utf-8",
    )

    ledger_sha_after, ledger_after, ledger_ids_after = ledger_state()
    assert ledger_sha_after == ledger_sha_before
    assert len(ledger_after) == len(ledger_before)
    assert ledger_ids_after == ledger_ids
    summary = {
        "result_type": "net_new_strict_entrata_residual",
        "net_new_properties": len(net_new),
        "net_new_property_ids": [int(row["property_id"]) for row in ledger_rows],
        "net_new_native_positive_rent_rows": sum(
            int(row["native_positive_rent_rows"]) for row in ledger_rows
        ),
        "overlap_with_latest_ledger": len(overlap),
        "latest_ledger_path": str(LEDGER),
        "latest_ledger_sha256": ledger_sha_after,
        "latest_ledger_rows": len(ledger_after),
        "latest_ledger_unique_property_ids": len(ledger_ids_after),
        "shared_ledger_modified": False,
        "new_hyperbrowser_sessions_this_lane": 8,
        "captcha_solving": False,
        "paid_canary": False,
        "consolidated_evidence_artifact": str(CONSOLIDATED),
        "net_new_ledger_rows_artifact": str(LEDGER_ROWS),
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
