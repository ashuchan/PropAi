from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path("/private/tmp/propai-fnd-vBkmT9/encore_knock_lane")
SOURCE = ROOT / "entrata_snippet_cluster_current_full.json"
OUTPUT = ROOT / "evidence_entrata_snippet_cluster_current_strict.json"
EXPECTED_IDS = {59649, 252116, 258789}
GENERIC_PLAN_NAMES = {
    "availability",
    "check availability",
    "details",
    "learn more",
    "view details",
    "view floor plan",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_child_host(website: str) -> str:
    marketing = (urlsplit(website).hostname or "").casefold().removeprefix("www.")
    return f"entratasnipit.{marketing}"


def main() -> None:
    payload = json.loads(SOURCE.read_text())
    if payload.get("lane") != "entrata_snippet_three_property_current_full_configured_pipeline":
        raise SystemExit("unexpected source lane")
    guardrails = payload.get("guardrails") or {}
    expected_guardrails = {
        "llm_enabled": False,
        "hyperbrowser": False,
        "captcha_solving": False,
        "web_unlocker": False,
        "flaresolverr": False,
        "fingerprint_rotation": False,
        "paid_canary": False,
    }
    if guardrails != expected_guardrails:
        raise SystemExit(f"guardrails mismatch: {guardrails!r}")

    results = payload.get("results") or []
    if {int(row.get("property_id") or 0) for row in results} != EXPECTED_IDS:
        raise SystemExit("source property set mismatch")

    strict_results = []
    for row in results:
        gates = row.get("strict_gates") or {}
        samples = row.get("native_samples") or []
        count = int(row.get("strict_native_positive_rows") or 0)
        property_id = int(row.get("property_id") or 0)
        child_host = expected_child_host(str(row.get("website") or ""))
        source_urls = [str(url) for url in (row.get("source_urls") or [])]
        portal_urls = [str(url) for url in (row.get("portal_urls") or [])]
        source_property_ids = [
            str(value) for value in (row.get("source_property_ids") or [])
        ]
        floor_plan_names = [
            str(sample.get("floor_plan_name") or "").strip() for sample in samples
        ]
        strict_shape = (
            row.get("outcome") == "UNIT_QUALIFIED"
            and row.get("adapter") == "entrata"
            and row.get("tier") == "TIER_1_DOM_ENTRATA_SNIPPET_UNIT_LEVEL"
            and bool(gates)
            and all(value is True for value in gates.values())
            and count > 0
            and count == int(row.get("all_emitted_rows") or 0)
            and count == int(row.get("distinct_visible_unit_numbers") or 0)
            and len(source_property_ids) == 1
            and bool(source_property_ids[0])
            and bool(source_urls)
            and all(
                (urlsplit(url).hostname or "").casefold() == child_host
                for url in source_urls
            )
            and len(portal_urls) == 1
            and (urlsplit(portal_urls[0]).hostname or "").casefold() == child_host
            and bool(samples)
            and all(
                str(sample.get("unit_number") or "").strip()
                and str(sample.get("entrata_uid") or "").strip()
                and str(sample.get("source_property_id") or "")
                == source_property_ids[0]
                and isinstance(sample.get("rent_low"), (int, float))
                and not isinstance(sample.get("rent_low"), bool)
                and float(sample["rent_low"]) > 0
                for sample in samples
            )
            and all(
                name and name.casefold() not in GENERIC_PLAN_NAMES
                for name in floor_plan_names
            )
        )
        if not strict_shape:
            raise SystemExit(f"strict shape failed for {property_id}")

        strict_results.append(
            {
                "property_id": property_id,
                "property_name": row.get("property_name") or "",
                "website": row.get("website") or "",
                "outcome": "UNIT_QUALIFIED",
                "property_identity_match": True,
                "contamination_verdict": row.get("contamination_verdict"),
                "adapter": row.get("adapter"),
                "tier": row.get("tier"),
                "units": count,
                "strict_gates": gates,
                "identity_evidence": {
                    "rows_with_native_identity": count,
                    "rows_with_native_identity_and_positive_rent": count,
                    "source_property_ids": source_property_ids,
                    "source_urls": source_urls,
                    "portal_urls": portal_urls,
                    "floor_plan_name_samples": floor_plan_names,
                },
                "native_samples": [
                    {
                        "identity": {
                            "unit_number": str(sample.get("unit_number") or ""),
                            "entrata_uid": str(sample.get("entrata_uid") or ""),
                        },
                        "positive_rent_evidence": {
                            "market_rent_low": sample.get("rent_low")
                        },
                        "floor_plan_name": sample.get("floor_plan_name") or "",
                        "availability_date": sample.get("availability_date") or "",
                        "source_property_id": sample.get("source_property_id") or "",
                        "source_api_url": source_urls[0],
                    }
                    for sample in samples
                ],
            }
        )

    output = {
        "lane": "entrata_snippet_three_property_current_source_strict",
        "source_artifact": str(SOURCE),
        "source_artifact_sha256": sha256(SOURCE),
        "guardrails": expected_guardrails,
        "results": strict_results,
    }
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(OUTPUT),
                "artifact_sha256": sha256(OUTPUT),
                "qualified_property_ids": [
                    row["property_id"] for row in strict_results
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
