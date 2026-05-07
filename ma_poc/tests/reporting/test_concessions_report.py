"""Phase 7 — concessions observation report tests.

Includes F10 regression tests asserting the schema_v2 → reporting wire is
intact: a unit emitted from _format_v2_unit with concession_text populated
must surface in the report (which was empty for all 4,981 properties on
the 2026-05-05 production run before F10).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ma_poc.reporting.observation_reports import build_concessions_report
from ma_poc.validation.schema_gate import check


def test_concessions_no_validation_gating() -> None:
    """H7: missing concession fields do not gate validator acceptance."""
    rec = check({"unit_id": "U1", "asking_rent": 1500}, property_id="P1")
    assert rec.accepted is not None
    rec2 = check(
        {
            "unit_id": "U2",
            "asking_rent": 1500,
            "concession_text": None,
            "concession_value": None,
            "concession_source": None,
        },
        property_id="P1",
    )
    assert rec2.accepted is not None


def test_concession_value_parsed_from_dollar_amount(tmp_path: Path) -> None:
    """Numeric value coerced when emitted by the LLM as a string."""
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_3_DOM",
            "units": [
                {
                    "concession_text": "$500 off first month",
                    "concession_value": "500",
                    "concession_source": "specials_section",
                }
            ],
        }
    ]
    report = build_concessions_report(properties, tmp_path, run_date="2026-05-05")
    rec = report["per_property"][0]
    assert rec["concession_value"] == 500.0


def test_concession_value_null_for_percentage_off(tmp_path: Path) -> None:
    """The LLM is instructed to leave value null when only a % off is shown."""
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_3_DOM",
            "units": [
                {
                    "concession_text": "15% off first month",
                    "concession_value": None,
                    "concession_source": "description_text",
                }
            ],
        }
    ]
    report = build_concessions_report(properties, tmp_path, run_date="2026-05-05")
    summary = report["summary"]
    assert summary["value_distribution"]["text_only_no_value"] == 1
    assert summary["value_distribution"]["with_numeric_value"] == 0


def test_concessions_report_emitted_to_correct_path(tmp_path: Path) -> None:
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_3_DOM",
            "units": [
                {
                    "concession_text": "1 month free on a 13-month lease",
                    "concession_value": 2000,
                    "concession_source": "strikethrough_dom",
                }
            ],
        },
        {"_property_id": "P2", "extraction_tier_used": "TIER_1_API", "units": []},
    ]
    build_concessions_report(properties, tmp_path, run_date="2026-05-05")
    out_path = tmp_path / "concessions_report.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["summary"]["properties_with_any_concession"] == 1


def test_f10_schema_v2_unit_surfaces_in_concessions_report(tmp_path: Path) -> None:
    """F10 regression: a unit dict carrying concession_text passes through
    _format_v2_unit and lands in the report. Pre-F10 the v2 transform
    dropped these keys, producing the empty-stub report seen on 2026-05-05.
    """
    import sys
    from pathlib import Path as _P

    scripts_dir = _P(__file__).resolve().parent.parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from ma_poc.core.schema_v2 import _format_v2_unit

    ts = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    raw_unit = {
        "unit_id": "u101",
        "floor_plan_name": "A1",
        "beds": 1,
        "area": 750,
        "rent_low": 1500,
        "concession_text": "$500 off first month",
        "concession_value": 500.0,
        "concession_source": "specials_section",
    }
    formatted = _format_v2_unit(raw_unit, ts)
    properties = [
        {
            "_property_id": "P1",
            "extraction_tier_used": "TIER_3_DOM",
            "units": [formatted],
        }
    ]
    report = build_concessions_report(properties, tmp_path, run_date="2026-05-05")
    assert report["summary"]["properties_with_any_concession"] == 1
    assert report["summary"]["value_distribution"]["with_numeric_value"] == 1
    rec = report["per_property"][0]
    assert rec["concession_text"] == "$500 off first month"
    assert rec["concession_value"] == 500.0


def test_concessions_report_includes_source_breakdown(tmp_path: Path) -> None:
    properties = [
        {
            "_property_id": f"P{i}",
            "extraction_tier_used": "TIER_3_DOM",
            "units": [
                {
                    "concession_text": "1 month free",
                    "concession_value": 1500,
                    "concession_source": src,
                }
            ],
        }
        for i, src in enumerate(
            ["strikethrough_dom", "strikethrough_dom", "specials_section", "api_field"]
        )
    ]
    report = build_concessions_report(properties, tmp_path, run_date="2026-05-05")
    breakdown = report["summary"]["concession_extraction_breakdown"]
    assert breakdown["strikethrough_dom"] == 2
    assert breakdown["specials_section"] == 1
    assert breakdown["api_field"] == 1
