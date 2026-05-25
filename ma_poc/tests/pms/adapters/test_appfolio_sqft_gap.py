"""AppFolio operator-data-gap sqft flagging (2026-05-25).

Probe finding (cluster A): 11/11 sampled
``TIER_1_DOM_APPFOLIO_VANITY`` / ``..._VANITY_PLAN_LEVEL`` units with
sqft=-1 were verified as true OPERATOR data gaps — the AppFolio listing
just doesn't carry sqft on the operator's site. Cohort: 1,095 units
across ~104 props (843 SSR/VANITY + 252 PLAN_LEVEL).

These tests pin all three AppFolio emit paths:
  * parse_appfolio_detail_page  (raw-dict path → TIER_1_DOM_APPFOLIO_DETAIL)
  * parse_appfolio_listings_ssr (make_unit_dict → TIER_1_DOM_APPFOLIO_SSR
    AND TIER_1_DOM_APPFOLIO_VANITY[_PLAN_LEVEL] via shared parser)
  * parse_appfolio_listings     (make_unit_dict → TIER_1_API_APPFOLIO)

Each path is checked twice — with sqft present (no flag) and with sqft
absent (data_gaps=["sqft"] + data_quality_flag="SQFT_NOT_PUBLISHED").

The flag contract is honored by ``validation.schema_gate._has_area``:
a documented sqft gap counts as area-present, so the no_area retry
doesn't fire on legitimately-incomplete-but-extracted units, and the
verdict ships as SUCCESS instead of SUCCESS_PLAN_LEVEL.
"""
from __future__ import annotations

from ma_poc.pms.adapters.appfolio import (
    parse_appfolio_detail_page,
    parse_appfolio_listings,
    parse_appfolio_listings_ssr,
)
from ma_poc.validation.schema_gate import _has_area

# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_listings_ssr  (TIER_1_DOM_APPFOLIO_SSR / _VANITY)
# ─────────────────────────────────────────────────────────────────────

_SSR_FRAGMENT_WITH_SQFT = """
<html><body>
<article class="listing-item result js-listing-item" data-listing-id="100">
  <div class="js-listing-blurb-rent">$1,800</div>
  <div class="js-listing-blurb-bed-bath">2 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 950</div>
  <div class="js-listing-available">6/1/26</div>
  <div class="js-listing-address"><span>123 Main St #2A, Anytown, CA 90001</span></div>
</article>
</body></html>
"""

# Verified live 2026-05-25 against scottsdale5th.com (and 10 other
# sampled cohort members): the SSR card lacks any js-listing-square-feet
# div entirely — sqft just isn't published.
_SSR_FRAGMENT_WITHOUT_SQFT = """
<html><body>
<article class="listing-item result js-listing-item" data-listing-id="200">
  <div class="js-listing-blurb-rent">$2,100</div>
  <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
  <div class="js-listing-available">6/15/26</div>
  <div class="js-listing-address"><span>456 Oak Ave #3B, Anytown, CA 90001</span></div>
</article>
</body></html>
"""


def test_ssr_with_sqft_emits_no_data_gap() -> None:
    """Happy path — operator publishes sqft, so no data_gaps flag is set."""
    units = parse_appfolio_listings_ssr(
        _SSR_FRAGMENT_WITH_SQFT, "https://example.appfolio.com/listings"
    )
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == "950"
    assert u.get("data_gaps", []) == []
    assert u.get("data_quality_flag", "") == ""
    # Sanity: schema_gate._has_area returns True because the numeric
    # sqft is present (the documented-gap path is not exercised here).
    assert _has_area(u) is True


def test_ssr_without_sqft_flags_operator_data_gap() -> None:
    """The cohort-A signature: AppFolio SSR card with rent + beds + baths
    + address but NO js-listing-square-feet div. The adapter must stamp
    data_gaps=['sqft'] + data_quality_flag='SQFT_NOT_PUBLISHED' so the
    verdict gate treats the unit as area-present (operator gap, not
    parser miss)."""
    units = parse_appfolio_listings_ssr(
        _SSR_FRAGMENT_WITHOUT_SQFT, "https://example.appfolio.com/listings"
    )
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == ""
    assert u["data_gaps"] == ["sqft"]
    assert u["data_quality_flag"] == "SQFT_NOT_PUBLISHED"
    # The documented-gap contract: schema_gate._has_area returns True
    # even with no numeric sqft, because the flag tells the gate the
    # operator simply doesn't publish it.
    assert _has_area(u) is True


def test_ssr_with_zero_sqft_flags_operator_data_gap() -> None:
    """Some SSR pages emit ``Square Feet: 0`` or an empty inner span.
    The _parse_sqft_blurb returns '' for both, so the gap-flagging
    branch fires the same way as for a fully missing div."""
    html = """
    <article class="listing-item result js-listing-item" data-listing-id="300">
      <div class="js-listing-blurb-rent">$1,950</div>
      <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
      <div class="js-listing-square-feet">Square Feet: 0</div>
      <div class="js-listing-available">7/1/26</div>
      <div class="js-listing-address"><span>789 Pine Rd #5C, Anytown, CA 90001</span></div>
    </article>
    """
    units = parse_appfolio_listings_ssr(html, "https://example.appfolio.com/listings")
    assert len(units) == 1
    u = units[0]
    assert u["data_gaps"] == ["sqft"]
    assert u["data_quality_flag"] == "SQFT_NOT_PUBLISHED"


# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_listings  (TIER_1_API_APPFOLIO)
# ─────────────────────────────────────────────────────────────────────


def test_listings_api_with_sqft_emits_no_data_gap() -> None:
    """API path: when the response carries sq_ft, no gap flag fires."""
    items = [{
        "name": "Unit 101",
        "bed": "2",
        "bath": "1",
        "sq_ft": "900",
        "price": "1800",
        "unit_number": "101",
        "available_date": "2026-06-01",
        "status": "available",
        "id": "abc-123",
    }]
    units = parse_appfolio_listings(items, "https://example.appfolio.com/api/v1/listings")
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == "900"
    assert u.get("data_gaps", []) == []
    assert u.get("data_quality_flag", "") == ""


def test_listings_api_without_sqft_flags_operator_data_gap() -> None:
    """API path: when the operator's response object has no sq_ft / sqft
    / square_feet / area at all, the adapter must flag it. This is the
    PLAN_LEVEL_VANITY shape the cohort-A probe identified."""
    items = [{
        "name": "Unit 202",
        "bed": "1",
        "bath": "1",
        # No sq_ft / sqft / square_feet / area at all.
        "price": "2100",
        "unit_number": "202",
        "available_date": "2026-06-15",
        "status": "available",
        "id": "def-456",
    }]
    units = parse_appfolio_listings(items, "https://example.appfolio.com/api/v1/listings")
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] in ("", None)
    assert u["data_gaps"] == ["sqft"]
    assert u["data_quality_flag"] == "SQFT_NOT_PUBLISHED"
    assert _has_area(u) is True


def test_listings_api_with_alt_sqft_field_emits_no_data_gap() -> None:
    """The /floorplans/all endpoint uses ``sq_ft`` but other endpoints
    use ``square_feet`` or ``squareFeet`` — get_field picks the first
    truthy one. If any of them is present, no flag fires."""
    items = [{
        "name": "Plan A",
        "bedrooms": "0",
        "bathrooms": "1",
        "squareFeet": "525",
        "rent": "1450",
        "unit_id": "ghi-789",
        "available_date": "2026-07-01",
    }]
    units = parse_appfolio_listings(items, "https://example.appfolio.com/api/v1/floorplans/all")
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == "525"
    assert u.get("data_gaps", []) == []


# ─────────────────────────────────────────────────────────────────────
# parse_appfolio_detail_page  (TIER_1_DOM_APPFOLIO_DETAIL)
# ─────────────────────────────────────────────────────────────────────


def test_detail_page_with_sqft_emits_no_data_gap() -> None:
    """The detail-page regex parser must NOT flag a unit when the
    ``\\d+ sq ft`` token is present in the main content."""
    html = """
    <html><body>
    <main>
      <h1>2 BR / 1 BA Apartment</h1>
      <p>Rent: $1,750 / month</p>
      <p>Bedrooms: 2 bd, Bathrooms: 1 ba, 875 sq ft</p>
    </main>
    </body></html>
    """
    units = parse_appfolio_detail_page(html, "https://example.appfolio.com/listings/detail/uuid")
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == "875"
    assert "data_gaps" not in u or u.get("data_gaps", []) == []
    assert "data_quality_flag" not in u or u.get("data_quality_flag", "") == ""


def test_detail_page_without_sqft_flags_operator_data_gap() -> None:
    """When the detail page has rent + beds + baths but no recognisable
    sqft token, the adapter must flag the operator data gap inline on
    the raw dict (this path bypasses make_unit_dict)."""
    html = """
    <html><body>
    <main>
      <h1>1 BR / 1 BA Apartment</h1>
      <p>Rent: $1,650 / month</p>
      <p>1 bd, 1 ba</p>
    </main>
    </body></html>
    """
    units = parse_appfolio_detail_page(html, "https://example.appfolio.com/listings/detail/uuid")
    assert len(units) == 1
    u = units[0]
    assert u["sqft"] == ""
    assert u["data_gaps"] == ["sqft"]
    assert u["data_quality_flag"] == "SQFT_NOT_PUBLISHED"
    assert _has_area(u) is True
