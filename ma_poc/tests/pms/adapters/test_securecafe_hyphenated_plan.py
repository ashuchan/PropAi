"""SecureCafe availableunits parser must handle hyphenated plan names.

Prod 2026-07-12 (RENTCAFE_SHAPE_REJECTED-adjacent cohort): ``availableunits.aspx``
pages whose floor-plan header carries a hyphen in the plan NAME —
e.g. ``Floor Plan: 1bd x 1ba - 850sqft - The Birch - 1 Bedroom, 1 Bathroom``
(roundtree-mckinley) — parsed to ZERO units despite SSR ``AvailUnitRow`` rows
with rent being present, because ``_SECURECAFE_FP_HDR_RE``'s name group was
``[^<\\-]{1,80}?`` (hyphen forbidden). The section header never matched, so
``parse_securecafe_availableunits`` found no headers and returned ``[]`` — the
page then fell through to the LLM-DOM tier. Fix: name group widened to
``[^<]{1,120}?``; the lazy quantifier + the explicit ``- (Studio|N Bedroom),
N Bathroom`` anchor keeps the plan-name boundary unambiguous.
"""

from __future__ import annotations

from ma_poc.pms.adapters.rentcafe import parse_securecafe_availableunits


def _avail_unit_row(apt: str, sqft: str, rent: str) -> str:
    return (
        "<tr class='AvailUnitRow'>"
        f"<td data-label='Apartment'>#{apt}</td>"
        f"<td data-label='Sq.Ft.'>{sqft}</td>"
        f"<td data-label='Rent'>${rent}</td>"
        "<td data-label='Date Available'>Available</td>"
        "</tr>"
    )


# A page with a HYPHENATED plan name — the exact shape that returned 0 units
# pre-fix. Two priced units under one plan.
_HYPHENATED_PLAN_HTML = (
    "<html><body>"
    "<div>Floor Plan: 1bd x 1ba - 850sqft - The Birch - 1 Bedroom, 1 Bathroom</div>"
    "<table>"
    + _avail_unit_row("2963-A1", "850", "1,149")
    + _avail_unit_row("2923-B3", "850", "1,249")
    + "</table></body></html>"
)

_URL = "https://roundtree-roundtree-mckinley.securecafe.com/onlineleasing/roundtree/availableunits.aspx"


def test_hyphenated_plan_name_now_parses_units() -> None:
    units = parse_securecafe_availableunits(_HYPHENATED_PLAN_HTML, _URL)
    assert len(units) == 2, "hyphenated plan header must still yield its unit rows"
    numbers = {u["unit_number"] for u in units}
    assert numbers == {"2963-A1", "2923-B3"}
    rents = {u.get("market_rent_low") or u.get("rent_low") for u in units}
    assert rents == {1149, 1249}
    # plan name retains the hyphens, bed/bath resolved from the anchor clause
    assert units[0]["floor_plan_name"] == "1bd x 1ba - 850sqft - The Birch"
    assert str(units[0]["bedrooms"]) == "1"
    assert str(units[0]["bathrooms"]) in ("1", "1.0")


def test_plain_plan_name_still_parses() -> None:
    """Regression: the common no-hyphen header must be unaffected."""
    html = (
        "<html><body>"
        "<div>Floor Plan: The Birch - 1 Bedroom, 1 Bathroom</div>"
        "<table>" + _avail_unit_row("101", "720", "1,050") + "</table>"
        "</body></html>"
    )
    units = parse_securecafe_availableunits(html, _URL)
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "The Birch"
    assert units[0]["unit_number"] == "101"


def test_studio_hyphenated_plan_name_parses() -> None:
    html = (
        "<html><body>"
        "<div>Floor Plan: S1 - The Nook - Studio, 1 Bathroom</div>"
        "<table>" + _avail_unit_row("A-12", "480", "995") + "</table>"
        "</body></html>"
    )
    units = parse_securecafe_availableunits(html, _URL)
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "S1 - The Nook"
    assert str(units[0]["bedrooms"]) == "0"
