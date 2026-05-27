"""Verdict stamping on the per-property-timeout salvage path.

Background: 2026-05-27 jugnu-c612-may27 canary surfaced 368 properties with
``_meta.partial_recovery=True`` and ``scrape_tier_used="FAILED"`` but no
``_meta.verdict`` field. Full-fleet success was 82.20% reported vs 89.82%
adjusted — the silent-success gap is exactly these salvage records.

This test pins the salvage-path semantics by exercising
``ma_poc.reporting.verdict.compute`` with the exact arguments the timeout
handler in ``runners/jugnu.py`` now passes.
"""

from ma_poc.reporting.verdict import Verdict, compute as compute_verdict


def _salvage_compute(partial_units):
    """Mirror the call shape in jugnu.py:_process_one TimeoutError handler."""
    extract = {"units": partial_units} if partial_units else None
    return compute_verdict(
        fetch_outcome=None,
        extract_result=extract,
        units=partial_units or None,
    )


def test_partial_units_with_rent_yields_success():
    units = [
        {"unit_id": "u1", "rent": 1500},
        {"unit_id": "u2", "rent": 1600},
        {"unit_id": "u3", "rent": 1700},
    ]
    v = _salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS


def test_partial_units_mixed_rent_yields_success():
    # 3 with rent, 2 without — overall has rent signal → SUCCESS
    units = [
        {"unit_id": "u1", "rent": 1500},
        {"unit_id": "u2", "rent": 1600},
        {"unit_id": "u3", "rent": 1700},
        {"unit_id": "u4"},
        {"unit_id": "u5"},
    ]
    v = _salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS


def test_partial_units_no_rent_yields_plan_level():
    units = [
        {"unit_id": "u1"},
        {"unit_id": "u2"},
    ]
    v = _salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS_PLAN_LEVEL


def test_partial_units_all_inferred_yields_plan_level():
    units = [
        {"unit_id": "inferred_a", "rent": 1500},
        {"unit_id": "inferred_b", "rent": 1600},
    ]
    v = _salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS_PLAN_LEVEL


def test_zero_partial_units_yields_failed_no_data():
    v = _salvage_compute([])
    assert v.verdict == Verdict.FAILED_NO_DATA
