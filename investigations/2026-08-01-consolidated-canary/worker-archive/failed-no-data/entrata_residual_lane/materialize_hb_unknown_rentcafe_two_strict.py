from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


LANE = Path("/private/tmp/propai-fnd-vBkmT9/entrata_residual_lane")
INPUT = LANE / "hb_unknown_rentcafe_high_value3_current_full.json"
OUTPUT = LANE / "evidence_hb_unknown_rentcafe_two_current_strict.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(sample: dict[str, object]) -> bool:
    return any(
        isinstance(sample.get(field), (int, float))
        and not isinstance(sample.get(field), bool)
        and float(sample[field]) > 0
        for field in ("market_rent_low", "market_rent_high")
    )


def materialize(row: dict[str, object]) -> dict[str, object]:
    property_id = int(row["property_id"])
    samples = [
        sample
        for sample in (row.get("native_samples") or [])
        if isinstance(sample, dict)
    ]
    source_urls = [str(value) for value in (row.get("source_urls") or [])]
    source_ids = {
        str(sample.get("source_property_id") or "") for sample in samples
    }
    source_ids.discard("")
    source_names = {
        str(sample.get("source_property_name") or "") for sample in samples
    }
    source_names.discard("")
    common_gates = {
        "single_clean_hyperbrowser_session": row.get("session_calls") == 1,
        "configured_render_http_200": row.get("fetch_status") == 200
        and row.get("fetch_outcome") == "OK",
        "configured_name_street_zip_visible": all(
            (row.get("configured_identity") or {}).values()
        ),
        "current_full_pipeline_unit_candidate": row.get("outcome")
        == "UNIT_CANDIDATE",
        "all_emitted_rows_native_and_positive_rent": bool(
            int(row.get("strict_native_positive_rent_rows") or 0) > 0
            and int(row.get("strict_native_positive_rent_rows") or 0)
            == int(row.get("emitted_units") or 0)
            and row.get("all_emitted_rows_strict") is True
        ),
        "one_native_source_property_id": len(source_ids) == 1,
        "native_samples_have_unit_rent_and_same_property_id": bool(
            samples
            and all(
                str(sample.get("unit_number") or "").strip()
                and positive_rent(sample)
                and str(sample.get("source_property_id") or "") in source_ids
                for sample in samples
            )
        ),
        "source_urls_present": bool(source_urls),
    }

    if property_id == 48389:
        provider_gates = {
            "current_pipeline_selected_onesite_workflow": row.get("adapter")
            == "onesite"
            and row.get("tier") == "TIER_1_API_ONESITE_WORKFLOW",
            "sole_realpage_workflow_property_id_3943980": source_ids
            == {"3943980"}
            and all(
                (urlsplit(url).hostname or "").casefold()
                == "leasing.realpage.com"
                and "/workflowstartup/v1/3943980/" in url
                for url in source_urls
            ),
        }
        verdict = "pass_exact_configured_identity_realpage_workflow_property_3943980"
    elif property_id == 219752:
        query_ids = {
            value
            for url in source_urls
            for value in parse_qs(urlsplit(url).query).get("propertyId", [])
        }
        provider_gates = {
            "current_pipeline_selected_securecafe_applicant": row.get("adapter")
            == "rentcafe"
            and row.get("tier")
            == "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT",
            "sole_applicant_property_id_1674518": source_ids == {"1674518"}
            and query_ids == {"1674518"}
            and all(
                (urlsplit(url).hostname or "").casefold()
                == "203-main-llc-rentcafewebsite.securecafeapplicant.com"
                for url in source_urls
            ),
            "applicant_payload_names_lamphouse": source_names
            == {"Lamphouse Apartments"},
            "sample_native_securecafe_ids_present": all(
                str((sample.get("source_ids") or {}).get("securecafe_apartment_id") or "")
                and str((sample.get("source_ids") or {}).get("securecafe_floorplan_id") or "")
                for sample in samples
            ),
        }
        verdict = "pass_exact_configured_identity_securecafe_applicant_property_1674518"
    else:
        raise RuntimeError(f"unexpected candidate {property_id}")

    gates = {**common_gates, **provider_gates}
    if not all(gates.values()):
        failed = [key for key, passed in gates.items() if not passed]
        raise RuntimeError(f"strict gates failed for {property_id}: {failed}")

    return {
        "property_id": property_id,
        "property_name": row.get("property_name") or "",
        "website": row.get("website") or "",
        "outcome": "UNIT_QUALIFIED",
        "property_identity_match": True,
        "contamination_verdict": verdict,
        "units": int(row.get("strict_native_positive_rent_rows") or 0),
        "adapter": row.get("adapter") or "",
        "tier": row.get("tier") or "",
        "configured_final_url": row.get("configured_final_url") or "",
        "strict_gates": gates,
        "identity_evidence": {
            "rows_with_native_identity": int(
                row.get("strict_native_positive_rent_rows") or 0
            ),
            "rows_with_native_identity_and_positive_rent": int(
                row.get("strict_native_positive_rent_rows") or 0
            ),
            "source_property_ids": sorted(source_ids),
            "source_property_names": sorted(source_names),
            "source_urls": source_urls,
        },
        "native_samples": [
            {
                "identity": {
                    "unit_number": str(sample.get("unit_number") or ""),
                    **{
                        key: str(value)
                        for key, value in (sample.get("source_ids") or {}).items()
                        if str(value).strip()
                    },
                },
                "floor_plan_name": sample.get("floor_plan_name") or "",
                "availability_date": sample.get("availability_date") or "",
                "positive_rent_evidence": {
                    key: sample.get(key)
                    for key in ("market_rent_low", "market_rent_high")
                    if sample.get(key) not in (None, "", 0, 0.0)
                },
                "source_api_url": sample.get("source_api_url") or "",
                "source_property_id": sample.get("source_property_id") or "",
                "source_property_name": sample.get("source_property_name") or "",
            }
            for sample in samples
        ],
    }


def main() -> None:
    payload = json.loads(INPUT.read_text())
    candidates = [
        row
        for row in payload.get("results", [])
        if row.get("outcome") == "UNIT_CANDIDATE"
    ]
    if {int(row["property_id"]) for row in candidates} != {48389, 219752}:
        raise RuntimeError("expected exactly the two reviewed candidate ids")
    output = {
        "lane": "hb_unknown_rentcafe_two_current_source_strict",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_artifact": str(INPUT),
        "source_artifact_sha256": sha256(INPUT),
        "guardrails": payload.get("guardrails") or {},
        "results": [materialize(row) for row in candidates],
    }
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "net_new_ids": [row["property_id"] for row in output["results"]],
                "unit_counts": {
                    str(row["property_id"]): row["units"]
                    for row in output["results"]
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
