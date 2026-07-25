"""Plan-vs-unit verdict guard must not depend on a field minted later.

Two defects, both measured against the 2026-07-25 4,982-property run, where
518 SUCCESS properties shipped rows that carry a rent but only a synthetic
``inferred_*`` id — 403 of them with a terminal tier whose own label ends in
``_PLAN_LEVEL``:

1. ORDERING. ``compute_verdict`` runs BEFORE ``_format_v2_unit``, and
   ``unit_id`` is minted inside that formatter by ``assign_fallback_unit_id``.
   The old guard tested ``str(u.get("unit_id", "")).startswith("inferred_")``,
   so at verdict time it read an ABSENT key, got ``""``, concluded "not
   inferred" => "real identity", and never demoted. The branch was effectively
   dead for every row whose id is minted at format time.

2. ``str(None)``. A row with an explicit ``unit_id: None`` stringifies to the
   literal ``"None"``, which also does not start with ``"inferred_"`` — so the
   emptiest possible row read as the STRONGEST identity, and a single such row
   was enough to defeat the ``all(...)``.

The regression these produce is silent: the success RATE is unaffected (both
verdicts are success-class), but SUCCESS is inflated with plan-level work and
SUCCESS_PLAN_LEVEL is understated, so "we extracted units" reads far higher
than it is. Hence the pre-format assertions below — testing only formatted
rows would have passed throughout.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.reporting.verdict import Verdict, compute

_TS = datetime(2026, 7, 25, 3, 0, 0)


class _ExtractResult:
    errors: list[str] = []

    def __init__(self, records: list[dict], tier: str = "TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL"):
        self.records = records
        self.tier_used = tier


def _verdict(rows: list[dict]) -> Verdict:
    return compute(
        fetch_outcome="OK",
        extract_result=_ExtractResult(rows),
        validated=None,
        carry_forward_applied=False,
        units_hollow=False,
        plan_summaries=None,
        verdict_quality_override=None,
        units=rows,
        operator_no_availability=False,
    ).verdict


def _plan_rows() -> list[dict]:
    """Internal (pre-format) plan rows: a plan name and a rent, no identity.

    This is the exact shape ``scrape_jugnu`` hands to the verdict layer — note
    there is no ``unit_id`` key at all. Modelled on The Villages at General
    Grant (thevillagesgg.com) from the 2026-07-25 run.
    """
    return [
        {"floor_plan_name": "Hadleigh Efficiency", "rent_low": 795.0, "rent_high": 795.0, "area": 377, "beds": 1},
        {"floor_plan_name": "Thornbury Efficiency", "rent_low": 955.0, "rent_high": 955.0, "area": 480, "beds": 1},
    ]


# ── Defect 1: ordering ───────────────────────────────────────────────────────


def test_plan_rows_demote_before_the_formatter_runs() -> None:
    """THE regression. Pre-format rows have no unit_id key whatsoever."""
    rows = _plan_rows()
    assert all("unit_id" not in r for r in rows), "fixture must mirror pre-format shape"
    assert _verdict(rows) == Verdict.SUCCESS_PLAN_LEVEL


def test_verdict_is_stable_across_the_formatter_boundary() -> None:
    """The same rows must not change verdict just by being formatted.

    Before the fix these two calls disagreed — SUCCESS pre-format,
    SUCCESS_PLAN_LEVEL post-format — which is how the bug hid: every unit test
    that built already-formatted rows passed.
    """
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    rows = _plan_rows()
    before = _verdict(rows)
    formatted = [_format_v2_unit(r, _TS, "P-TEST") for r in rows]
    assert all(str(f["unit_id"]).startswith("inferred_") for f in formatted), (
        "formatter should have minted synthetic ids for anchorless rows"
    )
    after = _verdict(formatted)
    assert before == after == Verdict.SUCCESS_PLAN_LEVEL


# ── Defect 2: str(None) == "None" ────────────────────────────────────────────


def test_explicit_none_unit_id_does_not_read_as_identity() -> None:
    """A row with unit_id=None is the WEAKEST row, not the strongest."""
    assert unit_has_real_anchor({"unit_id": None}) is False
    assert unit_has_real_anchor({"unit_id": ""}) is False
    assert unit_has_real_anchor({"unit_id": "None"}) is False
    assert unit_has_real_anchor({"unit_id": "null"}) is False


def test_one_none_row_cannot_rescue_an_all_plan_level_property() -> None:
    """Modelled on The Mansions at Sunset Ridge: DOM junk produced rows with
    unit_id None alongside inferred_ rows. One None row flipped `all(...)` to
    False and promoted the whole property to SUCCESS."""
    rows = [
        {"unit_id": "inferred_a6d9d29f9225124f", "floor_plan_name": "Milan", "rent_low": 1253.0, "area": 902},
        {"unit_id": None, "floor_plan_name": None, "rent_low": 2115.0, "area": 1310},
        {"unit_id": None, "floor_plan_name": None, "rent_low": 2093.0, "area": 1369},
    ]
    assert _verdict(rows) == Verdict.SUCCESS_PLAN_LEVEL


# ── The guard must not over-fire: real unit-level stays SUCCESS ──────────────


class TestGenuineUnitLevelIsNotDemoted:
    def test_pre_format_unit_number_is_real_identity(self) -> None:
        """Pre-format, identity lives in unit_number. Modelled on Grand Oaks
        (grandoakscommunity.com) unit D103, live-verified 2026-07-25."""
        rows = [
            {"unit_number": "D103", "floor_plan_name": "2X2A", "rent_low": 1939.0, "area": 961},
            {"unit_number": "F302", "floor_plan_name": "2X2A", "rent_low": 2049.0, "area": 961},
        ]
        assert unit_has_real_anchor(rows[0]) is True
        assert _verdict(rows) == Verdict.SUCCESS

    def test_post_format_real_unit_id_is_real_identity(self) -> None:
        rows = [{"unit_id": "D103", "floor_plan_name": "2X2A", "rent_low": 1939.0, "area": 961}]
        assert _verdict(rows) == Verdict.SUCCESS

    def test_per_unit_source_id_counts_as_identity(self) -> None:
        """A backend per-unit id is real identity even with no unit_number —
        the 2026-07-18 carve-out must survive this change."""
        rows = [
            {
                "floor_plan_name": "A1",
                "rent_low": 1500.0,
                "source_ids": {"appfolio_listing_id": "998877"},
            }
        ]
        assert unit_has_real_anchor(rows[0]) is True
        assert _verdict(rows) == Verdict.SUCCESS

    def test_mixed_property_with_one_real_unit_stays_success(self) -> None:
        """all_inferred requires EVERY row to be anchorless; one genuine
        apartment is enough to keep the property unit-level."""
        rows = [
            {"floor_plan_name": "A1", "rent_low": 1500.0},
            {"unit_number": "204", "floor_plan_name": "A1", "rent_low": 1550.0},
        ]
        assert _verdict(rows) == Verdict.SUCCESS


@pytest.mark.parametrize(
    "unit,expected",
    [
        ({"unit_number": "101"}, True),
        ({"unit_number": "  101  "}, True),
        ({"unit_number": ""}, False),
        ({"unit_number": "   "}, False),
        ({"unit_id": "inferred_deadbeef"}, False),
        ({"unit_id": "unkeyable_deadbeef"}, False),
        ({}, False),
        ({"source_ids": {}}, False),
        ({"source_ids": {"appfolio_listing_id": ""}}, False),
        ({"source_ids": "not-a-dict"}, False),
    ],
)
def test_anchor_predicate_table(unit: dict, expected: bool) -> None:
    assert unit_has_real_anchor(unit) is expected
