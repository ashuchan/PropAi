from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
from pathlib import Path

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.extraction.post_process import post_process
from ma_poc.pms.adapters._encoreskyline_units import (
    parse_rentpress_data_floorplans,
)


ROOT = Path("/private/tmp/propai-fnd-vBkmT9/edgefield_48075_hb_lane")
REPO = Path("/Users/ankur/PropAi-codex-failed-no-data")
OUTPUT = ROOT / "evidence_edgefield_48075_configured_e2e.json"
PROPOSAL = ROOT / "proposal_edgefield_48075_strict_admission.json"
REMAINDER = Path("/private/tmp/propai-fnd-vBkmT9/strict_recovery_remaining_current.csv")
REPEATS = [ROOT / f"configured_e2e_gate_repeat_{index}.json" for index in range(1, 4)]
CAPTURED_BODY = ROOT / "final_landing_raw.html.gz"
FINAL_URL = (
    "https://lawsoncompanies.com/apartments/edgefield-apartments/"
    "?utm_source=redirect&utm_medium=redirect&utm_campaign=edgefieldaptsva.com"
)
SOURCE_FILES = [
    REPO / "ma_poc/fetch/fetcher.py",
    REPO / "ma_poc/fetch/hyperbrowser_backend.py",
    REPO / "ma_poc/tests/fetch/test_fetcher_render_captcha_hb_rescue.py",
    REPO / "ma_poc/tests/fetch/test_hyperbrowser_raw_get_redirect.py",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def positive_rent(row: dict[str, object]) -> bool:
    return any(
        isinstance(row.get(key), (int, float))
        and not isinstance(row.get(key), bool)
        and float(row[key]) > 0
        for key in ("market_rent_low", "market_rent_high")
    )


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=REPO,
        text=True,
    ).strip()


def validate_repeat(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text())
    guardrails = payload["guardrails"]
    assert guardrails["captcha_solving"] is False
    assert guardrails["web_unlocker"] is False
    assert guardrails["flaresolverr"] is False
    assert guardrails["fingerprint_rotation"] is False
    assert guardrails["llm"] is False
    assert guardrails["paid_canary"] is False
    assert guardrails["hyperbrowser_max_calls_per_property"] == 1
    assert guardrails["session_options"]["solveCaptchas"] is False
    assert guardrails["session_options"]["useStealth"] is False
    assert guardrails["session_options"]["useProxy"] is True

    fetched = payload["fetch"]
    assert fetched["outcome"] == "OK"
    assert fetched["exact_name_visible"] is True
    assert fetched["exact_street_visible"] is True
    assert fetched["exact_city_state_zip_visible"] is True
    assert fetched["published_site_ids"] == ["1060300"]
    assert fetched["published_onlineleasing_hosts"] == [
        "6359.onlineleasing.realpage.com"
    ]

    scrape = payload["scrape"]
    assert scrape["detected"]["pms"] == "onesite"
    assert scrape["adapter"] == "onesite"
    assert scrape["tier"] == "TIER_1_API_ONESITE_WORKFLOW"
    assert scrape["units"] == 3
    assert scrape["strict_native_positive_rent_rows"] == 3
    assert scrape["plan_summaries"] == 0
    assert scrape["errors"] == []
    assert scrape["all_rows_have_expected_source_property_id"] is True
    assert scrape["all_rows_have_distinct_native_anchor"] is True
    rows = scrape["rows"]
    assert {str(row["unit_number"]) for row in rows} == {"13", "38", "108"}
    assert all(row["floor_plan_name"] == "Two Bedroom" for row in rows)
    assert all(row["market_rent_low"] == 1475 for row in rows)
    assert all(row["source_property_id"] == "1060300" for row in rows)
    assert all(
        row["source_property_provenance"] == "marketing_page_site_id"
        for row in rows
    )
    assert all(
        "/workflowstartup/v1/1060300/English" in row["source_api_url"]
        for row in rows
    )
    return payload


def rentpress_quality_diagnostic() -> dict[str, object]:
    raw = gzip.decompress(CAPTURED_BODY.read_bytes())
    rows = parse_rentpress_data_floorplans(raw.decode("utf-8", "replace"), FINAL_URL)
    admitted = post_process(rows, property_id="48075").admitted
    strict = [row for row in admitted if unit_has_real_anchor(row) and positive_rent(row)]
    assert len(rows) == len(admitted) == len(strict) == 9
    assert all(row.get("availability_date") for row in strict)
    assert all(
        str((row.get("source_ids") or {}).get("rentpress_unit_code") or "").startswith(
            "1060300_"
        )
        for row in strict
    )
    return {
        "status": "read_only_quality_diagnostic_not_current_e2e_admission",
        "captured_body": {
            "path": str(CAPTURED_BODY),
            "gzip_file_sha256": sha(CAPTURED_BODY),
            "decompressed_body_sha256": hashlib.sha256(raw).hexdigest(),
            "source_url": FINAL_URL,
        },
        "existing_parser": "parse_rentpress_data_floorplans",
        "native_positive_rows": len(strict),
        "all_rows_have_availability_date": True,
        "rows": [
            {
                "unit_number": row.get("unit_number"),
                "floor_plan_name": row.get("floor_plan_name"),
                "bedrooms": row.get("bedrooms"),
                "bathrooms": row.get("bathrooms"),
                "sqft": row.get("sqft"),
                "rent": row.get("market_rent_low"),
                "availability_date": row.get("availability_date"),
                "source_ids": row.get("source_ids"),
            }
            for row in strict
        ],
        "limitation": (
            "Current configured detector routes the page to OneSite workflow, which "
            "emits three native rows but no availability_date. The nine-row RentPress "
            "payload is documented only; no unapproved routing change was made."
        ),
    }


payloads = [validate_repeat(path) for path in REPEATS]
unit_sets = [
    sorted(str(row["unit_number"]) for row in payload["scrape"]["rows"])
    for payload in payloads
]
assert unit_sets == [["108", "13", "38"]] * 3

evidence = {
    "scope": {
        "cohort": "exact 2026-07-31 FAILED_NO_DATA remainder",
        "property_id": 48075,
        "property": "Edgefield",
        "configured_url": "https://www.edgefieldaptsva.com/",
        "configured_identity": {
            "address": "5699 Craneybrook Ln",
            "city": "Portsmouth",
            "state": "VA",
            "zip": "23703",
        },
        "paid_canary": False,
        "ledger_or_builder_modified": False,
        "current_remainder_artifact": {
            "path": str(REMAINDER),
            "sha256": sha(REMAINDER),
            "candidate_row": (
                "48075,,https://www.edgefieldaptsva.com/,,unknown,0,0,"
                "residual_unconverted"
            ),
        },
    },
    "implementation": {
        "branch": git("branch", "--show-current"),
        "head": git("rev-parse", "HEAD"),
        "origin_main": git("rev-parse", "origin/main"),
        "source_files": [
            {"path": str(path), "sha256": sha(path)} for path in SOURCE_FILES
        ],
        "changes": [
            "hb_raw_get uses the actual HTTP(S) final landing path/query after goto",
            "COMPLIANCE_MODE forces Hyperbrowser solveCaptchas=false and useStealth=false",
            "late RENDER CAPTCHA promotion gets one compliant direct-to-HB rescue",
            "exact RENDER HTTP202+empty-body shape gets the same bounded rescue",
            "clean rescue forbids PROBE_PROXY fallback and caps HB at one property session",
            "rescued CAPTCHA bodies are rejected fail-closed",
        ],
    },
    "verification": {
        "focused_and_fetch_regression_tests": {
            "result": "71 passed",
            "command": (
                "python -m pytest -q test_fetcher_render_captcha_hb_rescue.py "
                "test_compliance_mode.py test_fetcher_curl_cffi_fallback.py "
                "test_fetcher_render_unlocker_fallback.py test_dead_proxy_defenses.py "
                "test_hyperbrowser_raw_get_redirect.py test_hyperbrowser_backend.py"
            ),
        },
        "configured_e2e_gate": "3/3 strict pass",
        "repeat_artifacts": [
            {
                "path": str(path),
                "sha256": sha(path),
                "fetch_status": payload["fetch"]["status"],
                "fetch_body_bytes": payload["fetch"]["body_bytes"],
                "fetch_body_sha256": payload["fetch"]["body_sha256"],
                "strict_rows": payload["scrape"][
                    "strict_native_positive_rent_rows"
                ],
                "unit_numbers": sorted(
                    str(row["unit_number"]) for row in payload["scrape"]["rows"]
                ),
            }
            for path, payload in zip(REPEATS, payloads, strict=True)
        ],
    },
    "contamination_controls": {
        "exact_first_party_identity_in_all_repeats": True,
        "sole_published_site_id_in_all_repeats": "1060300",
        "sole_published_onlineleasing_host_in_all_repeats": (
            "6359.onlineleasing.realpage.com"
        ),
        "all_rows_bound_to_published_site_id": True,
        "all_rows_have_distinct_native_unit_numbers": True,
        "stable_unit_set_across_three_repeats": ["13", "38", "108"],
        "all_rows_positive_rent": True,
        "plan_summaries_all_repeats": 0,
        "negative_code_controls": [
            "cross-host redirect uses final path/query, not operator root",
            "same-host path/query remains unchanged",
            "navigation/evaluation failure returns (0, '') and closes session",
            "non-CAPTCHA RENDER is unchanged",
            "empty HTTP200 RENDER does not enter the precise 202 rescue",
            "failed rescue remains BOT_BLOCKED",
        ],
    },
    "guardrails": {
        "compliance_mode": True,
        "solve_captchas": False,
        "hyperbrowser_stealth": False,
        "hyperbrowser_proxy": True,
        "hyperbrowser_max_calls_per_property": 1,
        "web_unlocker": False,
        "flaresolverr": False,
        "fingerprint_rotation": False,
        "llm": False,
        "paid_canary": False,
    },
    "quality_limitation": {
        "current_workflow_floor_plan_name": "Two Bedroom",
        "current_workflow_availability_dates_present": False,
        "current_workflow_native_rows": 3,
    },
    "rentpress_quality_diagnostic": rentpress_quality_diagnostic(),
}

OUTPUT.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
proposal = {
    "action": "propose_strict_admission_without_mutating_ledger_or_builder",
    "property_id": 48075,
    "property": "Edgefield",
    "configured_url": "https://www.edgefieldaptsva.com/",
    "verdict": "pass_3_of_3_configured_e2e_strict_native_positive",
    "strict_rows": 3,
    "unit_numbers": ["13", "38", "108"],
    "floor_plan_name": "Two Bedroom",
    "rent": 1475,
    "source_property_id": "1060300",
    "availability_date_limitation": "blank in current OneSite workflow rows",
    "read_only_rentpress_diagnostic_rows_with_dates": 9,
    "evidence": {"path": str(OUTPUT), "sha256": sha(OUTPUT)},
    "source_files": evidence["implementation"]["source_files"],
    "guardrails": evidence["guardrails"],
}
PROPOSAL.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
print(
    json.dumps(
        {
            "output": str(OUTPUT),
            "sha256": sha(OUTPUT),
            "configured_e2e_gate": evidence["verification"]["configured_e2e_gate"],
            "strict_rows_each": [
                item["strict_rows"]
                for item in evidence["verification"]["repeat_artifacts"]
            ],
            "proposal": str(PROPOSAL),
            "proposal_sha256": sha(PROPOSAL),
        }
    )
)
