"""Unit tests for the Entrata plan→unit render trigger (task #42).

Covers the two pure helpers that gate the render escalation added to
``_process_property``:

* ``_is_entrata_plan_level`` — Entrata-scoped detector: an Entrata SUCCESS
  whose rows are all floorplan-level (``unit_number=""``), the signal that the
  per-apartment jd-fp roster is client-rendered and a render can recover it.
* ``_unit_level_row_count`` — counts rows carrying a real ``unit_number``, used
  by the accept check so a render is only kept on a strict unit-level upgrade.
"""
from types import SimpleNamespace

from ma_poc.scripts.runners.jugnu import (
    _is_entrata_plan_level,
    _unit_level_row_count,
)


def _res(tier, units, use_obj=True):
    """Build a result dict shaped like scrape_jugnu output. ``_extract_result``
    is an object with ``.tier_used`` (real shape); ``use_obj=False`` exercises
    the ``extraction_tier_used`` string fallback path."""
    r: dict = {"units": units}
    if use_obj:
        r["_extract_result"] = SimpleNamespace(tier_used=tier)
    else:
        r["extraction_tier_used"] = tier
    return r


def _plan_row(fp="A2"):
    # PP_SSR / plan-level parsers emit unit_number="".
    return {"unit_number": "", "floor_plan_name": fp, "rent_low": 1500, "rent_high": 1700}


def _unit_row(num="3100"):
    return {"unit_number": num, "floor_plan_name": "S1", "rent_low": 2400, "rent_high": 2400}


class TestUnitLevelRowCount:
    def test_counts_only_real_unit_numbers(self):
        r = {"units": [_unit_row("3100"), _unit_row("3082"), _plan_row()]}
        assert _unit_level_row_count(r) == 2

    def test_empty_whitespace_and_none_unit_number_not_counted(self):
        r = {"units": [{"unit_number": ""}, {"unit_number": "   "}, {"unit_number": None}, {}]}
        assert _unit_level_row_count(r) == 0

    def test_no_units_key(self):
        assert _unit_level_row_count({}) == 0

    def test_malformed_rows_do_not_raise(self):
        r = {"units": [None, 5, "x", _unit_row("A1")]}
        assert _unit_level_row_count(r) == 1  # only the one valid dict row counts


class TestIsEntrataPlanLevel:
    def test_entrata_ppssr_all_empty_unitnum_is_plan_level(self):
        r = _res("TIER_1_DOM_ENTRATA_PP_SSR", [_plan_row(), _plan_row("B1")])
        assert _is_entrata_plan_level(r) is True

    def test_entrata_unit_level_is_not_plan_level(self):
        # already has a real unit-level row → nothing to recover
        r = _res("TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", [_unit_row("3100"), _plan_row()])
        assert _is_entrata_plan_level(r) is False

    def test_non_entrata_plan_level_is_scoped_out(self):
        # generic plan-text is legitimately plan-only; must never be re-rendered
        r = _res("TIER_1_DOM_GENERIC_PLAN_TEXT", [_plan_row(), _plan_row()])
        assert _is_entrata_plan_level(r) is False

    def test_entrata_zero_units_is_render_on_empty_path_not_this(self):
        r = _res("TIER_1_DOM_ENTRATA_PP_SSR", [])
        assert _is_entrata_plan_level(r) is False

    def test_tier_read_from_extraction_tier_used_fallback(self):
        r = _res("TIER_1_DOM_ENTRATA_PP_SSR", [_plan_row()], use_obj=False)
        assert _is_entrata_plan_level(r) is True

    def test_missing_tier_is_not_plan_level(self):
        assert _is_entrata_plan_level({"units": [_plan_row()]}) is False
