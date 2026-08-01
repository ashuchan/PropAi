"""Universal recovery must also fire on PLAN-LEVEL results (2026-07-26).

The gate was ``if not adapter_result.units``, so universal recovery — which
contains the PMS portal hop — ran only when extraction came back completely
EMPTY. A plan-level result HAS units (floor-plan rows), so it never ran.

MEASURED on the 2026-07-26-plancohort canary (1,127 properties, 40/40 shards,
zero failures): of the 835 that failed to reach unit level, 368 (44%) carry a
SecureCafe fingerprint on their own page. Properties that DID reach the portal
converted 117 of 117 — a 100% rate. So those 368 were proven-recoverable and
simply never offered the route. They died on:

    144  TIER_1_API_RENTCAFE_NO_RESPONSE_PLAN_LEVEL
     72  TIER_3_DOM_GENERIC
     53  TIER_1_DOM_GENERIC_PLAN_TEXT
     33  TIER_1_API_RENTCAFE_SHAPE_REJECTED_PLAN_LEVEL

— all of which return plan rows and so slipped past the gate.

Same defect shape as the Path-B retry trigger (d33cd42): "has units" treated
as "done", when plan rows are not done.

The second half of this change matters as much as the first. Because the
success path does ``adapter_result.units = recovered``, firing on plan-level
baselines means a WORSE recovery could destroy real plan data. So a
plan-level baseline only accepts a genuinely unit-level replacement.
"""

from __future__ import annotations

from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.scraper import _retry_result_is_plan_level, rows_are_plan_level

# ── The predicate the gate keys on ──────────────────────────────────────────


def test_plan_rows_are_plan_level() -> None:
    """The 368-property shape: floor-plan names and rents, no apartments."""
    assert rows_are_plan_level(
        [{"floor_plan_name": "A1", "rent_low": 1500}, {"floor_plan_name": "B2", "rent_low": 1800}]
    ) is True


def test_real_apartments_are_not_plan_level() -> None:
    assert rows_are_plan_level([{"unit_number": "D103", "rent_low": 1939}]) is False


def test_one_real_unit_suppresses_the_gate() -> None:
    """A partial roster is not plan-level — re-running recovery risks losing it."""
    assert rows_are_plan_level(
        [{"floor_plan_name": "A1"}, {"unit_number": "204", "rent_low": 1550}]
    ) is False


def test_empty_is_not_plan_level() -> None:
    """Empty is the ORIGINAL gate's business. Returning True here would make
    the two conditions overlap and muddy which one fired."""
    assert rows_are_plan_level([]) is False
    assert rows_are_plan_level(None) is False


def test_channel_split_plan_result_is_plan_level() -> None:
    result = AdapterResult(
        units=[],
        plan_summaries=[{"floor_plan_name": "A1", "rent_low": 1500}],
    )

    assert _retry_result_is_plan_level(result) is True


def test_post_format_inferred_ids_read_as_plan_level() -> None:
    """Must work on both sides of the formatter boundary."""
    assert rows_are_plan_level([{"unit_id": "inferred_abc", "rent_low": 795}]) is True


def test_backend_source_id_is_real_identity() -> None:
    assert rows_are_plan_level(
        [{"floor_plan_name": "A1", "source_ids": {"appfolio_listing_id": "998877"}}]
    ) is False


# ── The acceptance rule, mirrored ───────────────────────────────────────────


def _accepts(recovered: list[dict], entered_plan_level: bool) -> bool:
    """Mirror of the in-scraper acceptance condition."""
    return bool(recovered) and (
        not entered_plan_level or not rows_are_plan_level(recovered)
    )


def test_plan_level_baseline_rejects_a_plan_level_recovery() -> None:
    """THE destructive case. The success path assigns
    ``adapter_result.units = recovered``, so accepting a plan-level recovery
    over a plan-level baseline would overwrite real data for no gain — and
    could replace the better of the two with the worse."""
    assert _accepts([{"floor_plan_name": "X1", "rent_low": 1400}], entered_plan_level=True) is False


def test_plan_level_baseline_accepts_a_unit_level_recovery() -> None:
    """The whole point — this is the 117/117 SecureCafe case."""
    assert _accepts([{"unit_number": "311", "rent_low": 2670}], entered_plan_level=True) is True


def test_empty_baseline_still_accepts_plan_level() -> None:
    """Entering on EMPTY keeps the original rule: anything beats nothing.
    Tightening this too would regress properties that legitimately recover
    only plan data."""
    assert _accepts([{"floor_plan_name": "X1", "rent_low": 1400}], entered_plan_level=False) is True


def test_nothing_recovered_is_never_accepted() -> None:
    assert _accepts([], entered_plan_level=True) is False
    assert _accepts([], entered_plan_level=False) is False


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_scraper_gate_admits_plan_level_and_guards_the_overwrite() -> None:
    """Source-level pins. Both halves must be present: firing on plan-level
    WITHOUT the acceptance guard would be a data-loss bug across 368
    properties, which is worse than the miss it fixes."""
    import inspect

    from ma_poc.pms import scraper

    src = inspect.getsource(scraper)
    assert "_ur_plan_level_only = _retry_result_is_plan_level(adapter_result)" in src, (
        "universal recovery no longer computes the plan-level entry condition"
    )
    assert "_retry_real_unit_count(adapter_result) == 0" in src, (
        "the gate reverted to empty-only; the 368 SecureCafe properties lose their route"
    )
    assert "_ur_accept" in src, (
        "the overwrite guard is gone — a plan-level recovery could destroy a "
        "plan-level baseline"
    )


def test_predicate_has_exactly_one_definition() -> None:
    """It was a closure in the retry block and is now module-level, shared by
    the retry trigger and the recovery gate. A second copy would drift, and
    copy drift is already a recurring defect class here."""
    import inspect

    from ma_poc.pms import scraper

    src = inspect.getsource(scraper)
    assert src.count("def rows_are_plan_level(") == 1
    assert "def _rows_are_plan_level(" not in src, "the old closure is back"
