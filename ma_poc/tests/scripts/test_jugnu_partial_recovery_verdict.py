"""Verdict stamping on the per-property-timeout salvage path.

Background: 2026-05-27 jugnu-c612-may27 canary surfaced 368 properties with
``_meta.partial_recovery=True`` and ``scrape_tier_used="FAILED"`` but no
``_meta.verdict`` field. Full-fleet success was 82.20% reported vs 89.82%
adjusted — the silent-success gap is exactly these salvage records.

This test pins the salvage-path semantics by exercising
``ma_poc.reporting.verdict.compute`` with the exact arguments the timeout
handler in ``runners/jugnu.py`` now passes.
"""

from ma_poc.reporting.verdict import Verdict
from ma_poc.reporting.verdict import compute as compute_verdict
from ma_poc.scripts.runners.jugnu import (
    _neutralize_unearned_rent_gap,
    _salvage_unit_has_numeric_rent,
)


def _salvage_compute(partial_units):
    """Mirror the call shape in jugnu.py:_process_one TimeoutError handler."""
    extract = {"units": partial_units} if partial_units else None
    return compute_verdict(
        fetch_outcome=None,
        extract_result=extract,
        units=partial_units or None,
    )


def _neutralized_salvage_compute(partial_units):
    """Mirror the *full* timeout-handler flow: neutralize unearned rent-gap
    flags on a zero-numeric-rent salvage, then compute the verdict.
    """
    if partial_units and not any(
        _salvage_unit_has_numeric_rent(u) for u in partial_units if isinstance(u, dict)
    ):
        for u in partial_units:
            if isinstance(u, dict):
                _neutralize_unearned_rent_gap(u)
    return _salvage_compute(partial_units)


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


# ---------------------------------------------------------------------------
# 2026-07-11 quality sweep: false "operator doesn't publish rent" flag on
# TIMEOUT salvage. A geometry roster salvaged before the price join carries a
# data_gaps=["rent"] / RENT_NOT_PUBLISHED flag it never legitimately earned
# (the flag's contract requires *exhausting* enrichment, which the timeout
# pre-empted). Left in place it masks the no-rent-signal downgrade and leaves
# every row AVAILABLE. Repro cohort: cltexchange.com (647 phantom units),
# allora168, integrapalms, arthuronaberdeen … (16 props / 2,767 units).
# ---------------------------------------------------------------------------


def _phantom_salvage_units(n=5, flag="RENT_NOT_PUBLISHED", gaps=True):
    """Real-shaped cltexchange salvage: real unit_ids, no numeric rent, but a
    documented-rent-gap flag that (falsely) signals rent-present."""
    u = []
    for i in range(n):
        d = {
            "unit_id": f"1-110{i}",
            "floor_plan_name": "1A.4",
            "rent_range": "",
            "availability_status": "AVAILABLE",
            "source_ids": {"sightmap_unit_id": str(10113237 + i)},
        }
        if flag:
            d["data_quality_flag"] = flag
        if gaps:
            d["data_gaps"] = ["rent"]
        u.append(d)
    return u


def test_false_rent_gap_flag_masks_downgrade_before_fix():
    """Pin the BUG: the unearned flag makes the salvage look like SUCCESS."""
    units = _phantom_salvage_units()
    # without neutralization, _rent_gap_documented → _has_rent True → SUCCESS
    assert _salvage_compute(units).verdict == Verdict.SUCCESS


def test_neutralize_flips_phantom_salvage_to_plan_level():
    """The fix: zero-numeric-rent salvage → SUCCESS_PLAN_LEVEL, not SUCCESS."""
    units = _phantom_salvage_units()
    v = _neutralized_salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS_PLAN_LEVEL
    assert "no_rent_signal" in v.reason


def test_neutralize_demotes_availability_and_marks_held():
    units = _phantom_salvage_units()
    _neutralized_salvage_compute(units)
    for u in units:
        assert u["availability_status"] == "UNKNOWN"
        assert u["data_quality_flag"] == "QA_HELD"
        assert "rent" not in (u.get("data_gaps") or [])


def test_data_gaps_only_variant_also_neutralized():
    # some adapters set data_gaps without the RENT_NOT_PUBLISHED flag
    units = _phantom_salvage_units(flag=None, gaps=True)
    assert _salvage_compute(_phantom_salvage_units(flag=None, gaps=True)).verdict == Verdict.SUCCESS
    assert _neutralized_salvage_compute(units).verdict == Verdict.SUCCESS_PLAN_LEVEL


def test_priced_salvage_is_left_untouched():
    """The fix is a NO-OP for a salvage that DID capture real prices: the
    zero-numeric-rent gate is False, so availability/flags are never rewritten.
    (The verdict value itself is the verdict layer's concern — it does not
    parse ``rent_range`` strings, a pre-existing behaviour independent of
    this fix.)"""
    units = [
        {"unit_id": "1-1103", "rent_range": "$1,450", "availability_status": "AVAILABLE"},
        {"unit_id": "1-1104", "rent_range": "$1,500", "availability_status": "AVAILABLE"},
    ]
    assert any(_salvage_unit_has_numeric_rent(u) for u in units)  # gate blocks neutralize
    _neutralized_salvage_compute(units)
    for u in units:
        assert u["availability_status"] == "AVAILABLE"  # untouched
        assert "data_quality_flag" not in u  # not marked QA_HELD


def test_numeric_priced_salvage_keeps_success():
    """When real prices survived in numeric fields, the verdict stays SUCCESS
    and the fix does not touch the rows."""
    units = [
        {"unit_id": "1-1103", "market_rent_low": 1450, "availability_status": "AVAILABLE"},
        {"unit_id": "1-1104", "market_rent_low": 1500, "availability_status": "AVAILABLE"},
    ]
    v = _neutralized_salvage_compute(units)
    assert v.verdict == Verdict.SUCCESS
    assert units[0]["availability_status"] == "AVAILABLE"


def test_numeric_rent_detected_across_field_variants():
    assert _salvage_unit_has_numeric_rent({"market_rent_low": 1200})
    assert _salvage_unit_has_numeric_rent({"asking_rent": "1,350"})
    assert _salvage_unit_has_numeric_rent({"rent_range": "$1,450 - $1,600"})
    assert not _salvage_unit_has_numeric_rent({"rent_range": ""})
    assert not _salvage_unit_has_numeric_rent({"data_gaps": ["rent"]})  # gap ≠ numeric
    assert not _salvage_unit_has_numeric_rent({"market_rent_low": 0})
