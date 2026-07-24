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


def test_empty_and_garbage_never_raise() -> None:
    assert parse_marketing_plan_text("", "") == []
    assert parse_marketing_plan_text("<html>no plans here, just $5 coffee</html>", "") == []
    assert parse_marketing_plan_text("<<<not html", "") == []
