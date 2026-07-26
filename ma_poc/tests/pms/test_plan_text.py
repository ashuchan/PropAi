"""Marketing-page floor-plan TEXT parser (task #21 plan-level recovery).

Pins: the free-text 2-line form (plan-name line, then "<sqft> sq ft | from $X") →
plan-level records with beds/baths/sqft/rent; fee lines are NOT treated as plans;
sqft/rent formatting variety; empty on no pattern; never raises.
"""

from __future__ import annotations

from ma_poc.pms.adapters.plan_text import parse_marketing_plan_text

# mirrors cottagesatsanford.com (446): plan-name line + "sqft ... | from $rent",
# with fee lines mixed in that MUST be ignored.
_HTML = """
<html><body>
<h2>Unit Layouts</h2>
<p>Studio</p>
<p>288 sq. ft. | from $1125</p>
<p>1 Bed 1 Bath:</p>
<p>576 sq. ft. | from $1250</p>
<p>2 Bed 1 Bath:</p>
<p>864 sq. ft. | from $1550</p>
<p>3 Bed 2 Bath:</p>
<p>1152 sq ft | from $1700</p>
<p>Application Fee: $50 per applicant | Admin fee: $250 | Holding Fee: $300</p>
<p>Pet Fee: First Pet: $300 | Second Pet: $150</p>
</body></html>
"""


def test_extracts_free_text_plans() -> None:
    plans = parse_marketing_plan_text(_HTML, "https://x.com/")
    names = [p["_floor_plan"] for p in plans]
    assert names == ["Studio", "1 Bed 1 Bath", "2 Bed 1 Bath", "3 Bed 2 Bath"]
    by = {p["_floor_plan"]: p for p in plans}
    # canonical int-typed keys (area/beds/baths/rent) so they survive the v2 transform
    assert by["Studio"]["area"] == 288 and by["Studio"]["market_rent_low"] == 1125
    assert by["Studio"]["beds"] == 0
    assert by["1 Bed 1 Bath"]["market_rent_low"] == 1250 and by["1 Bed 1 Bath"]["beds"] == 1
    assert by["3 Bed 2 Bath"]["area"] == 1152 and by["3 Bed 2 Bath"]["baths"] == 2.0
    # all are plan-level (no unit numbers) → SUCCESS_PLAN_LEVEL downstream
    assert all(p["unit_number"] == "" for p in plans)
    assert all(p["extraction_tier"] == "TIER_3_PLAN_TEXT" for p in plans)


def test_fee_lines_not_treated_as_plans() -> None:
    plans = parse_marketing_plan_text(_HTML, "")
    # $50/$250/$300/$150 fee amounts must never become plan rents
    rents = {p["market_rent_low"] for p in plans}
    assert rents == {1125, 1250, 1550, 1700}
    assert 50 not in rents and 250 not in rents and 150 not in rents


def test_sqft_rent_format_variety() -> None:
    html = "<p>Two Bed</p><p>950 ft² starting at $1,095</p>"
    plans = parse_marketing_plan_text(html, "")
    assert len(plans) == 1
    assert plans[0]["_sqft"] == "950" and plans[0]["market_rent_low"] == 1095


def test_prose_marketing_is_not_a_plan() -> None:
    # FALSE-POSITIVE guard: marketing PROSE that mentions bed types + sqft must
    # NOT become a junk single "plan" (measured 2026-07-24: this was ~44% of
    # firings before the guard).
    prose = (
        "<p>Choose from studio to 2-bedroom layouts, ranging from 600 to 850 "
        "square feet. Each floor plan features comfortable finishes.</p>"
    )
    assert parse_marketing_plan_text(prose, "") == []
    prose2 = (
        "<p>Behind its classic facade you'll find one, two, and three-bedroom "
        "floor plans ranging from 726 to 3,100 square feet.</p>"
    )
    assert parse_marketing_plan_text(prose2, "") == []


def test_v11_rent_on_separate_line_captured() -> None:
    # v1.1: rent published on a SEPARATE line (Rent:/Starting at) inside the
    # plan's window is now attached (measured lift 0/6 → 5/6 firings-with-rent).
    html = (
        "<p>1 Bed</p><p>636 sq ft</p><p>Rent: $815</p>"
        "<p>2 Bed</p><p>844 sq ft</p><p>Starting at $945</p>"
    )
    by = {p["_floor_plan"]: p for p in parse_marketing_plan_text(html, "")}
    assert by["1 Bed"]["area"] == 636 and by["1 Bed"]["market_rent_low"] == 815
    assert by["2 Bed"]["area"] == 844 and by["2 Bed"]["market_rent_low"] == 945


def test_v11_does_not_steal_fee_or_next_plan_rent() -> None:
    # window bounded at the next anchor + fee-filtered: a fee $ is never taken as
    # rent, and a plan can't steal the next plan's rent.
    html = (
        "<p>1 Bed</p><p>600 sq ft</p><p>Application Fee: $50</p>"
        "<p>2 Bed</p><p>800 sq ft</p><p>Rent: $1200</p>"
    )
    by = {p["_floor_plan"]: p for p in parse_marketing_plan_text(html, "")}
    assert by["1 Bed"]["area"] == 600 and by["1 Bed"].get("market_rent_low") is None
    assert by["2 Bed"]["market_rent_low"] == 1200


def test_empty_and_garbage_never_raise() -> None:
    assert parse_marketing_plan_text("", "") == []
    assert parse_marketing_plan_text("<html>no plans here, just $5 coffee</html>", "") == []
    assert parse_marketing_plan_text("<<<not html", "") == []


# ── unit-table parser: a per-apartment grid must yield APARTMENTS ───────────

_UNIT_GRID = """
<html><body>
<div class="apts-units"><div class="apts-units__container">
  <div class="units-head">
    <div>Bldg/Unit</div><div>Bed</div><div>Bath</div>
    <div>Rents From</div><div>Available Date</div><div></div>
  </div>
  <div class="units-body">
    <div class="row"><div>03-0712</div><div>1 Bedroom</div><div>1 Bath</div>
        <div>$2,623</div><div>07/27/2026</div><div>Apply Now</div></div>
    <div class="row"><div>02-0604</div><div>1 Bedroom</div><div>1 Bath</div>
        <div>$2,622</div><div>08/17/2026</div><div>Apply Now</div></div>
    <div class="row"><div>02-0414</div><div>2 Bedroom</div><div>2 Bath</div>
        <div>$2,593</div><div>10/08/2026</div><div>Apply Now</div></div>
  </div>
</div></div>
</body></html>
"""


def test_unit_grid_yields_unit_level_rows() -> None:
    """Mirrors majesticvernonhills.com (user-validated 2026-07-25).

    The plan-text reader captured this page's RENTS correctly but had no unit
    column, so every row shipped SUCCESS_PLAN_LEVEL with an inferred_* id — a
    gold property demoted purely because "03-0712" was discarded.
    """
    from ma_poc.pms.adapters.plan_text import parse_unit_table

    rows = parse_unit_table(_UNIT_GRID, "")
    assert [r["unit_number"] for r in rows] == ["03-0712", "02-0604", "02-0414"]
    by = {r["unit_number"]: r for r in rows}
    assert by["03-0712"]["market_rent_low"] == 2623
    assert by["03-0712"]["beds"] == 1 and by["03-0712"]["baths"] == 1.0
    assert by["03-0712"]["available_date"] == "07/27/2026"
    assert by["02-0414"]["beds"] == 2 and by["02-0414"]["market_rent_low"] == 2593
    # unit-level, NOT plan-level → downstream stamps gold, not SUCCESS_PLAN_LEVEL
    assert all(r["unit_number"] and r["extraction_tier"] == "TIER_1_DOM_UNIT_TABLE" for r in rows)


def test_unit_grid_not_confused_by_a_floor_plan_table() -> None:
    """A PLAN comparison table has no unit column — must yield nothing here.

    Guards the demotion boundary in the other direction: floor-plan codes like
    'JRA1' / 'C23B' are layouts, not apartments, and must never be emitted as
    unit numbers.
    """
    from ma_poc.pms.adapters.plan_text import parse_unit_table

    plan_table = """
    <div><div class="head"><div>Floor Plan</div><div>Beds</div><div>Rent</div></div>
      <div class="body">
        <div><div>JRA1</div><div>1 Bed</div><div>$1,951</div></div>
        <div><div>C23B</div><div>1 Bed</div><div>$3,375</div></div>
      </div></div>
    """
    assert parse_unit_table(plan_table, "") == []


def test_unit_grid_requires_more_than_one_row_and_never_raises() -> None:
    from ma_poc.pms.adapters.plan_text import parse_unit_table

    single = _UNIT_GRID.replace(
        '<div class="row"><div>02-0604</div><div>1 Bedroom</div><div>1 Bath</div>\n'
        '        <div>$2,622</div><div>08/17/2026</div><div>Apply Now</div></div>', ''
    )
    # a lone stray row is noise, not a roster
    assert len(parse_unit_table(single, "")) != 1
    assert parse_unit_table("", "") == []
    assert parse_unit_table("<<<not html", "") == []
    assert parse_unit_table("<div>no grid here</div>", "") == []
