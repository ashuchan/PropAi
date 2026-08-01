from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters.entrata import parse_entrata_pp_unit_cards


OUT = Path("/private/tmp/propai-fnd-vBkmT9/hb_scully_residual3_detail_probe")
E2E = OUT / "current_configured_scrape_jugnu_e2e.json"
PARENT = OUT / "current_parent_boundaries.json"
PROBE = OUT / "summary.json"
EVIDENCE = OUT / "evidence_scully_three_current_strict.json"
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")

EXPECTED = {
    43995: {
        "name": "Hamilton Hall",
        "website": "http://www.scullycompany.com/hamilton-hall-18.html",
        "address": "449 Hamilton St",
        "city": "Norristown",
        "zip": "19401",
        "provider_host": "hamiltonhall.scullycompany.com",
        "provider_property_id": "100003046",
        "unit_numbers": {"213", "505", "216", "617", "406", "606"},
        "plan_count": 3,
    },
    60141: {
        "name": "Bridgeview",
        "website": "http://www.scullycompany.com/bridgeview-14.html",
        "address": "701 Harrison St",
        "city": "Allentown",
        "zip": "18103",
        "provider_host": "bridgeview.scullycompany.com",
        "provider_property_id": "100002842",
        "unit_numbers": {"132", "415", "215", "013", "423", "224", "229", "005"},
        "plan_count": 2,
    },
    63191: {
        "name": "Avenir on Fifteenth",
        "website": "http://avenirphilly.com/",
        "address": "42 S 15th St",
        "city": "Philadelphia",
        "zip": "19102",
        "provider_host": "avenir.scullycompany.com",
        "provider_property_id": "100002834",
        "unit_numbers": {
            "1402",
            "0207",
            "1104",
            "0504",
            "1303",
            "1605",
            "1610",
            "1710",
            "0310",
            "1611",
            "1308",
        },
        "plan_count": 7,
    },
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(row: dict[str, Any]) -> bool:
    return any(
        isinstance(row.get(field), (int, float))
        and not isinstance(row.get(field), bool)
        and math.isfinite(float(row[field]))
        and float(row[field]) > 0
        for field in (
            "market_rent_low",
            "market_rent_high",
            "rent_low",
            "rent_high",
            "asking_rent",
            "rent",
        )
    )


def main() -> None:
    e2e = json.loads(E2E.read_text(encoding="utf-8"))
    parent = json.loads(PARENT.read_text(encoding="utf-8"))
    probe = json.loads(PROBE.read_text(encoding="utf-8"))

    assert e2e["summary"] == {
        "configured_fetch_ok": 3,
        "entrata_detected": 3,
        "properties": 3,
        "strict_native_priced_properties": 3,
        "strict_native_priced_rows": 25,
    }
    guardrails = e2e["guardrails"]
    assert guardrails["captcha_solving"] is False
    assert guardrails["fingerprint_rotation"] is False
    assert guardrails["flaresolverr"] is False
    assert guardrails["llm_enabled"] is False
    assert guardrails["paid_canary"] is False
    assert guardrails["web_unlocker"] is False
    assert guardrails["web_unlocker_call_count"] == 0
    assert guardrails["hyperbrowser_property_call_counts"] == {
        "43995": 1,
        "60141": 1,
        "63191": 1,
    }
    assert guardrails["hyperbrowser_session_options"] == {
        "solveCaptchas": False,
        "useProxy": True,
        "useStealth": False,
    }
    assert parent["guardrails"] == {
        "captcha_solving": False,
        "direct_http_only": True,
        "hyperbrowser": False,
        "llm": False,
        "paid_canary": False,
        "web_unlocker": False,
    }
    assert probe["guardrails"] == {
        "captcha_solving": False,
        "fingerprint_rotation": False,
        "flaresolverr": False,
        "llm": False,
        "paid_canary": False,
        "web_unlocker": False,
    }

    e2e_by_id = {int(row["property_id"]): row for row in e2e["results"]}
    parent_by_id = {int(row["property_id"]): row for row in parent["results"]}
    probe_by_id = {int(row["property_id"]): row for row in probe["results"]}
    assert set(e2e_by_id) == set(parent_by_id) == set(probe_by_id) == set(EXPECTED)

    results = []
    for property_id, expected in EXPECTED.items():
        current = e2e_by_id[property_id]
        boundary = parent_by_id[property_id]
        browser_probe = probe_by_id[property_id]
        expected_units = expected["unit_numbers"]
        expected_count = len(expected_units)

        assert current["property_name"] == expected["name"]
        assert current["configured_url"] == expected["website"]
        assert current["canonical_address"] == expected["address"]
        assert current["canonical_city"] == expected["city"]
        assert current["canonical_zip"] == expected["zip"]
        assert current["configured_fetch"]["outcome"] == "OK"
        assert current["configured_fetch"]["status"] == 200
        assert current["current_detected_pms"] == "entrata"
        assert current["adapter"] == "entrata"
        assert current["tier"] == "TIER_1_DOM_ENTRATA_PP_HYPERBROWSER_UNIT_LEVEL"
        assert current["emitted_unit_rows"] == expected_count
        assert current["native_identity_rows"] == expected_count
        assert current["strict_native_positive_rent_rows"] == expected_count
        assert current["source_property_ids"] == [expected["provider_property_id"]]
        assert not current["errors"] and not current["exception"]
        assert not current["llm_interactions"]
        assert (
            urlsplit(current["winning_page_url"]).hostname
            == expected["provider_host"]
        )

        final_rows = current["strict_shape_rows"]
        assert len(final_rows) == expected_count
        unit_numbers = [str(row.get("unit_number") or "") for row in final_rows]
        entrata_uids = [
            str((row.get("source_ids") or {}).get("entrata_uid") or "")
            for row in final_rows
        ]
        assert set(unit_numbers) == expected_units
        assert len(unit_numbers) == len(set(unit_numbers))
        assert all(entrata_uids) and len(entrata_uids) == len(set(entrata_uids))
        assert all(positive_rent(row) for row in final_rows)
        assert all(
            row.get("source_property_id") == expected["provider_property_id"]
            and row.get("source_property_name") == expected["name"]
            and (row.get("source_ids") or {}).get("entrata_fpid")
            and urlsplit(str(row.get("source_api_url") or "")).hostname
            == expected["provider_host"]
            and f"/property[id]/{expected['provider_property_id']}/"
            in unquote(urlsplit(str(row.get("source_api_url") or "")).path)
            for row in final_rows
        )

        assert boundary["provider_host"] == expected["provider_host"]
        assert boundary["provider_property_id"] == expected["provider_property_id"]
        assert boundary["status"] == 200
        assert boundary["parent_name_token_match"] is True
        assert boundary["parent_street_number_match"] is True
        assert boundary["parent_street_token_match"] is True
        assert boundary["parent_city_match"] is True
        assert boundary["parent_zip_match"] is True
        assert boundary["published_inventory_iframe_id"] == (
            f"website_{expected['provider_property_id']}"
        )
        assert (
            urlsplit(boundary["published_inventory_iframe_url"]).hostname
            == expected["provider_host"]
        )
        raw_parent = Path(boundary["raw_artifact"])
        assert raw_parent.exists() and sha256(raw_parent) == boundary["raw_artifact_sha256"]

        assert browser_probe["entrata_property_id"] == expected["provider_property_id"]
        assert browser_probe["expected_host"] == expected["provider_host"]
        assert browser_probe["hyperbrowser_sessions"] == 1
        assert browser_probe["session_options"]["solveCaptchas"] is False
        assert browser_probe["session_options"]["useStealth"] is False
        assert urlsplit(browser_probe["root"]["final_url"]).hostname == expected["provider_host"]
        assert len(browser_probe["details"]) == expected["plan_count"]

        raw_rows: list[dict[str, Any]] = []
        detail_artifacts = []
        for detail in browser_probe["details"]:
            raw_path = Path(detail["raw_path"])
            assert raw_path.exists()
            assert detail["status"] == 200
            assert detail["same_property_host"] is True
            assert urlsplit(detail["final_url"]).hostname == expected["provider_host"]
            assert (
                f"/property[id]/{expected['provider_property_id']}/"
                in unquote(urlsplit(detail["final_url"]).path)
            )
            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                detail_html = handle.read()
            raw_rows.extend(
                parse_entrata_pp_unit_cards(detail_html, detail["final_url"])
            )
            detail_artifacts.append(
                {
                    "plan_id": detail["plan_id"],
                    "url": detail["final_url"],
                    "artifact": str(raw_path),
                    "artifact_sha256": sha256(raw_path),
                }
            )
        raw_rows = [
            row
            for row in raw_rows
            if unit_has_real_anchor(row) and positive_rent(row)
        ]
        assert len(raw_rows) == expected_count
        assert {str(row.get("unit_number") or "") for row in raw_rows} == expected_units
        raw_by_unit = {str(row["unit_number"]): row for row in raw_rows}
        final_by_unit = {str(row["unit_number"]): row for row in final_rows}
        assert all(
            raw_by_unit[unit]["market_rent_low"]
            == final_by_unit[unit]["market_rent_low"]
            and str(raw_by_unit[unit].get("sqft") or "")
            == str(final_by_unit[unit].get("sqft") or "")
            and raw_by_unit[unit]["source_ids"] == final_by_unit[unit]["source_ids"]
            for unit in expected_units
        )

        source_urls = sorted(
            {str(row["source_api_url"]) for row in final_rows}
        )
        results.append(
            {
                "property_id": property_id,
                "property_name": expected["name"],
                "website": expected["website"],
                "outcome": "UNIT_QUALIFIED",
                "units": expected_count,
                "property_identity_match": True,
                "contamination_verdict": (
                    "pass_exact_configured_property_owned_entrata_iframe_"
                    "single_provider_property_id_full_pipeline_native_priced_units"
                ),
                "identity_evidence": {
                    "canonical_name": expected["name"],
                    "canonical_address": expected["address"],
                    "canonical_city": expected["city"],
                    "canonical_zip": expected["zip"],
                    "current_parent_name_street_city_zip_match": True,
                    "current_parent_published_exact_inventory_iframe": True,
                    "sole_provider_property_id": expected["provider_property_id"],
                    "configured_scrape_jugnu_unit_level": True,
                    "rows_with_native_identity": expected_count,
                    "rows_with_native_identity_and_positive_rent": expected_count,
                    "distinct_unit_numbers": expected_count,
                    "distinct_entrata_uids": expected_count,
                    "source_urls": source_urls,
                },
                "native_samples": [
                    {
                        "identity": {
                            "unit_number": row["unit_number"],
                            "entrata_uid": row["source_ids"]["entrata_uid"],
                            "entrata_fpid": row["source_ids"]["entrata_fpid"],
                        },
                        "positive_rent_evidence": {
                            "market_rent_low": row["market_rent_low"],
                            "market_rent_high": row["market_rent_high"],
                        },
                        "source_property_id": row["source_property_id"],
                        "source_api_url": row["source_api_url"],
                    }
                    for row in final_rows
                ],
                "native_rows": final_rows,
                "configured_pipeline": {
                    "artifact": str(E2E),
                    "artifact_sha256": sha256(E2E),
                    "detected_pms": current["current_detected_pms"],
                    "adapter": current["adapter"],
                    "tier": current["tier"],
                    "winning_page_url": current["winning_page_url"],
                    "hyperbrowser_sessions": 1,
                },
                "property_boundary": {
                    "artifact": str(PARENT),
                    "artifact_sha256": sha256(PARENT),
                    "raw_parent_artifact": str(raw_parent),
                    "raw_parent_artifact_sha256": sha256(raw_parent),
                    "published_iframe_url": boundary["published_inventory_iframe_url"],
                },
                "raw_provider_crosscheck": {
                    "probe_artifact": str(PROBE),
                    "probe_artifact_sha256": sha256(PROBE),
                    "detail_artifacts": detail_artifacts,
                    "native_priced_rows": len(raw_rows),
                    "exact_match_to_configured_pipeline": True,
                },
            }
        )

    critical_files = {
        str(path.relative_to(REPO)): sha256(path)
        for path in (
            REPO / "ma_poc/pms/detector.py",
            REPO / "ma_poc/pms/adapters/entrata.py",
            REPO / "ma_poc/pms/adapters/_entrata_hb_recovery.py",
        )
    }
    payload = {
        "summary": {
            "result_type": "strict_current_scully_property_owned_entrata_configured_e2e",
            "capture_timestamp_utc": datetime.now(UTC).isoformat(),
            "strict_unit_qualified_properties": len(results),
            "strict_unit_qualified_property_ids": sorted(EXPECTED),
            "native_positive_rent_rows": sum(row["units"] for row in results),
            "captcha_solving": False,
            "fingerprint_rotation": False,
            "web_unlocker": False,
            "llm_used": False,
            "paid_canary_run": False,
            "critical_source_sha256": critical_files,
        },
        "results": results,
    }
    EVIDENCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "artifact": str(EVIDENCE),
                "artifact_sha256": sha256(EVIDENCE),
                "properties": len(results),
                "property_ids": sorted(EXPECTED),
                "native_positive_rent_rows": sum(row["units"] for row in results),
            }
        )
    )


if __name__ == "__main__":
    main()
