"""SUCCESS_NO_INVENTORY ScrapeOutcome (2026-05-27).

Pins the audit-log layer distinction between operator-transparent
zero-inventory state and real extraction failures:
  • ScrapeOutcome enum carries SUCCESS_NO_INVENTORY.
  • sync.run_to_pg._meta_to_outcome maps verdict
    SUCCESS_NO_AVAILABILITY → outcome SUCCESS_NO_INVENTORY.
  • slo_watcher counts the resulting verdict as success, so the
    failure_rate doesn't inflate on waitlist / fully-leased cohorts.
"""
from __future__ import annotations

from ma_poc.models.scrape_event import ScrapeOutcome
from ma_poc.observability.slo_watcher import SloThresholds, check
from ma_poc.scripts.sync.run_to_pg import _meta_to_outcome


def test_scrape_outcome_enum_has_success_no_inventory() -> None:
    assert ScrapeOutcome.SUCCESS_NO_INVENTORY.value == "SUCCESS_NO_INVENTORY"
    assert ScrapeOutcome("SUCCESS_NO_INVENTORY") == ScrapeOutcome.SUCCESS_NO_INVENTORY


def test_meta_to_outcome_maps_no_availability_verdict() -> None:
    # The sync module imports ScrapeOutcome via the alt ``models.``
    # package alias, so identity (``is``) doesn't hold across the two
    # paths. Compare by string value — StrEnum equality is value-based.
    assert _meta_to_outcome({"verdict": "SUCCESS_NO_AVAILABILITY"}) == "SUCCESS_NO_INVENTORY"


def test_meta_to_outcome_plain_success_unchanged() -> None:
    assert _meta_to_outcome({"verdict": "SUCCESS"}) == "SUCCESS"
    assert _meta_to_outcome({"verdict": "SUCCESS_PLAN_LEVEL"}) == "SUCCESS"


def test_meta_to_outcome_failed_unchanged() -> None:
    assert _meta_to_outcome({"verdict": "FAILED_NO_DATA"}) == "FAILED"


def test_slo_watcher_counts_no_availability_as_success() -> None:
    """5 SUCCESS + 2 SUCCESS_NO_AVAILABILITY + 1 FAILED_NO_DATA → failure_rate = 1/8."""
    properties = (
        [{"_meta": {"verdict": "SUCCESS", "canonical_id": f"s{i}"}} for i in range(5)]
        + [
            {"_meta": {"verdict": "SUCCESS_NO_AVAILABILITY", "canonical_id": f"n{i}"}}
            for i in range(2)
        ]
        + [{"_meta": {"verdict": "FAILED_NO_DATA", "canonical_id": "f0"}}]
    )
    strict = SloThresholds(success_rate_min=0.99)
    violations = check(
        cost_rollup={}, property_results=properties, thresholds=strict
    )
    success_rate_violation = next(
        v for v in violations if v.name == "success_rate"
    )
    # 7/8 successes, 1/8 failed = 0.875 observed — NOT 5/8 = 0.625.
    assert success_rate_violation.observed == 0.875


# ─── 2026-05-27 follow-up: report.json operator_transparency_detail ─


def test_run_report_emits_operator_transparency_detail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Per-PID list + phrase histogram surface in report.json and report.md.

    Without this trace, the bulk `operator_transparency` count is opaque —
    a reviewer can't see WHICH props were classified or WHICH phrase
    fired most. This is the validation-in-next-canary guarantee.
    """
    import json

    from ma_poc.reporting.run_report import build

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # No events.jsonl needed — build() tolerates its absence.
    properties = [
        {
            "Property ID": "P_REGULAR",
            "_meta": {"verdict": "SUCCESS", "canonical_id": "P_REGULAR"},
            "_extract_result": {"tier_used": "rentcafe:tier1"},
            "units": [{"unit_id": "101"}],
        },
        {
            "Property ID": "P_WAIT",
            "Website": "https://livebrez.example.com/",
            "_meta": {"verdict": "SUCCESS_NO_AVAILABILITY", "canonical_id": "P_WAIT"},
            "_extract_result": {"tier_used": "no_availability:matched"},
            "units": [
                {
                    "source_ids": {
                        "operator_published_state": "no_availability_now",
                        "matched_phrase": "add to our waitlist",
                    }
                }
            ],
        },
        {
            "Property ID": "P_LEASED",
            "Website": "https://skyview.example.com/",
            "_meta": {"verdict": "SUCCESS_NO_AVAILABILITY", "canonical_id": "P_LEASED"},
            "_extract_result": {"tier_used": "no_availability:matched"},
            "units": [
                {
                    "source_ids": {
                        "operator_published_state": "no_availability_now",
                        "matched_phrase": "fully leased",
                    }
                }
            ],
        },
        {
            "Property ID": "P_LEASED2",
            "_meta": {"verdict": "SUCCESS_NO_AVAILABILITY", "canonical_id": "P_LEASED2"},
            "_extract_result": {"tier_used": "no_availability:matched"},
            "units": [
                {
                    "source_ids": {
                        "operator_published_state": "no_availability_now",
                        "matched_phrase": "fully leased",
                    }
                }
            ],
        },
    ]
    report = build(
        properties=properties,
        run_dir=run_dir,
        run_date="2026-05-27",
        cost_rollup={},
        slo_violations=[],
    )

    detail = report["operator_transparency_detail"]
    assert detail["count"] == 3
    # Histogram is ordered most-common first.
    assert detail["phrase_histogram"] == {
        "fully leased": 2,
        "add to our waitlist": 1,
    }
    pids = [r["property_id"] for r in detail["properties"]]
    assert pids == ["P_LEASED", "P_LEASED2", "P_WAIT"]  # sorted by pid
    # The phrase round-trips per-property.
    assert detail["properties"][2]["matched_phrase"] == "add to our waitlist"
    assert detail["properties"][2]["url"] == "https://livebrez.example.com/"

    # report.md surfaces the histogram + per-PID list.
    md = (run_dir / "report.md").read_text()
    assert "## Operator Transparency (zero-inventory)" in md
    assert "| fully leased | 2 |" in md
    assert "| add to our waitlist | 1 |" in md
    assert "P_WAIT" in md and "P_LEASED" in md
    # report.json round-trip — the field is keyed correctly for downstream consumers.
    written = json.loads((run_dir / "report.json").read_text())
    assert written["operator_transparency_detail"]["count"] == 3


def test_run_report_omits_section_when_zero_classified(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Clean reports: no Operator Transparency section when 0 properties hit."""
    from ma_poc.reporting.run_report import build

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    properties = [
        {
            "Property ID": "P1",
            "_meta": {"verdict": "SUCCESS", "canonical_id": "P1"},
            "_extract_result": {"tier_used": "rentcafe:tier1"},
        }
    ]
    build(
        properties=properties,
        run_dir=run_dir,
        run_date="2026-05-27",
        cost_rollup={},
        slo_violations=[],
    )
    md = (run_dir / "report.md").read_text()
    assert "## Operator Transparency" not in md
