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
