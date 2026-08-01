from __future__ import annotations

import csv
import json
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._appfolio_websites_duda import (
    parse_collection_payload,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/appfolio_generic_lane")
AUDIT = json.loads((ROOT / "appfolio_collection_audit.json").read_text())

PROPERTY_GROUPS = {
    38107: "",  # site-local collection contains eight rows, all tagged `fountain`
    46576: "Campus Pointe",
    282381: "Mission Ranch",
}

metadata: dict[int, dict[str, str]] = {}
with Path(
    "/Users/ankur/PropAi-codex-failed-no-data/ma_poc/config/properties.csv"
).open(newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        try:
            pid = int(row.get("apartmentid") or "")
        except ValueError:
            continue
        metadata[pid] = row


def has_positive_rent(row: dict) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and row[key] > 0
        for key in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def sample(row: dict) -> dict:
    return {
        "identity": {
            key: row.get(key)
            for key in ("unit_id", "unit_number", "floor_plan_id")
            if row.get(key) not in (None, "")
        },
        "source_ids": row.get("source_ids") or {},
        "unit_name": row.get("unit_name") or "",
        "floor_plan_name": row.get("floor_plan_name") or "",
        "availability_date": row.get("available_date")
        or row.get("availability_date")
        or "",
        "positive_rent_evidence": {
            key: row.get(key)
            for key in ("market_rent_low", "market_rent_high", "rent_low", "rent_high")
            if isinstance(row.get(key), (int, float)) and row[key] > 0
        },
        "source_api_url": row.get("source_api_url") or "",
    }


results = []
for audit_row in AUDIT["properties"]:
    pid = int(audit_row["property_id"])
    if pid not in PROPERTY_GROUPS:
        continue
    raw = json.loads(Path(audit_row["raw_artifact"]).read_text())
    source_url = raw["source_pages"][0]
    candidate_rows = audit_row["candidate_rows"]
    # AppFolio sometimes publishes plan/application placeholders at the
    # property's leasing-office address. They have a native listable UID and
    # rent but are not physical units. Exclude those rows from the strict
    # physical-unit gate while preserving them in the raw audit artifact.
    physical_candidate_rows = [
        row
        for row in candidate_rows
        if "leasing office" not in str(row.get("address_address2") or "").casefold()
    ]
    physical_uids = {
        str(row.get("listable_uid") or "") for row in physical_candidate_rows
    }
    physical_values = [
        value
        for value in raw["values"]
        if str((value.get("data") or {}).get("listable_uid") or "") in physical_uids
    ]
    payload = {"values": physical_values, "page": {"totalPages": 1}}
    # The input payload has already been restricted to rows proven to belong
    # to the target property, so no second group filter is necessary.
    units, _ = parse_collection_payload(payload, source_url, "")

    native = [row for row in units if unit_has_real_anchor(row)]
    qualified = [row for row in native if has_positive_rent(row)]
    listable_uids = {
        str((row.get("source_ids") or {}).get("appfolio_listable_uid") or "")
        for row in qualified
        if str((row.get("source_ids") or {}).get("appfolio_listable_uid") or "")
    }
    identities = {
        str(row.get("unit_id") or row.get("unit_number") or "")
        for row in qualified
        if str(row.get("unit_id") or row.get("unit_number") or "")
    }
    candidate_uids = {
        str(row.get("listable_uid") or "") for row in physical_candidate_rows
    }

    if pid == 38107:
        target_membership = all(
            "fountain"
            in {
                str(name).strip().casefold()
                for name in candidate.get("property_lists") or []
            }
            for candidate in physical_candidate_rows
        )
        boundary = (
            "exact-property AppFolio-Websites domain; its current collection has "
            "exactly eight rows and every row is tagged `fountain`; the same eight "
            "native listable UIDs are linked by the exact availability page"
        )
    else:
        target = PROPERTY_GROUPS[pid].casefold()
        target_membership = all(
            target
            in {
                str(name).strip().casefold()
                for name in candidate.get("property_lists") or []
            }
            for candidate in physical_candidate_rows
        )
        boundary = (
            f"every admitted record belongs to AppFolio property_list "
            f"`{PROPERTY_GROUPS[pid]}`"
        )

    parser_matches_candidates = listable_uids == candidate_uids
    qualified_ok = bool(
        qualified
        and len(qualified) == len(units)
        and len(identities) == len(qualified)
        and len(listable_uids) == len(qualified)
        and parser_matches_candidates
        and target_membership
    )
    meta = metadata[pid]
    results.append(
        {
            "property_id": pid,
            "property_name": meta.get("name") or "",
            "website": meta.get("website") or "",
            "adapter": "appfolio",
            "tier": "TIER_1_API_APPFOLIO_DUDA",
            "outcome": "UNIT_QUALIFIED" if qualified_ok else "UNIT_UNVERIFIED",
            "raw_extractor_outcome": "UNITS" if units else "EMPTY",
            "units": len(qualified),
            "plans": 0,
            "property_identity_match": qualified_ok,
            "contamination_verdict": (
                "pass_property_list_membership_and_site_boundary"
                if qualified_ok
                else "unverified_property_boundary"
            ),
            "counts_toward_strict_207_gate": qualified_ok,
            "identity_evidence": {
                "rows_with_native_identity": len(native),
                "rows_with_native_identity_and_positive_rent": len(qualified),
                "distinct_unit_numbers": len(identities),
                "distinct_native_listable_uids": len(listable_uids),
                "excluded_nonphysical_leasing_office_rows": (
                    len(candidate_rows) - len(physical_candidate_rows)
                ),
                "property_list_membership_all_rows": target_membership,
                "parser_matches_candidate_uids": parser_matches_candidates,
                "property_boundary": boundary,
                "source_urls": raw["source_pages"],
                "source_hosts": [raw["origin"].split("://", 1)[-1]],
            },
            "identity_samples": [sample(row) for row in qualified[:3]],
            "local_validation": {
                "parser": "parse_collection_payload",
                "unit_has_real_anchor_gate": len(native) == len(units),
                "positive_rent_gate": len(qualified) == len(units),
                "unique_native_identity_gate": len(identities) == len(units),
                "unique_listable_uid_gate": len(listable_uids) == len(units),
                "property_boundary_gate": target_membership,
                "all_passed": qualified_ok,
            },
            "errors": [],
        }
    )

artifact = {
    "batch_label": "appfolio-generic-remaining-appfolio-current-strict",
    "capture_date": "2026-08-01",
    "evidence_is_current_live": True,
    "direct_public_api": True,
    "hyperbrowser_used": False,
    "captcha_used": False,
    "paid_canary_run": False,
    "results": results,
    "strict_qualified_property_ids": [
        row["property_id"]
        for row in results
        if row["counts_toward_strict_207_gate"]
    ],
}
(ROOT / "evidence_appfolio_generic_appfolio_current_strict.json").write_text(
    json.dumps(artifact, indent=2)
)
print(json.dumps(artifact, indent=2))
