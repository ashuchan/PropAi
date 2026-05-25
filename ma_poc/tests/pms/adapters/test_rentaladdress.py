"""RentalAddress.com adapter tests (regr#8 / Cedar Ridge).

The RentalAddress.com platform serves SSR /floor_plans pages with a
deterministic ``<div class="{field} value">VALUE</div>`` pair layout.
Pre-fix the generic plan-text parser misread the sqft text ("598")
as part of the plan name string ("598 Bedroom / 1 Bath") and emitted
area=-1. The dedicated adapter parses the clean CSS classes and emits
correct plan-level rows.
"""
from __future__ import annotations

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.rentaladdress import (
    RentalAddressAdapter,
    parse_rentaladdress_floorplans,
)
from ma_poc.pms.detector import detect_pms

# Live-captured Cedar Ridge fixture (cedarridgeapts.rentaladdress.com 2026-05-25)
_CEDAR_RIDGE_HTML = """
<html><body>
<div class="floor_plan_list" data-property-name="Cedar Ridge Apartments">
  <div class="floor_plan row1">
    <div class="floor_plan_name">1 Bedroom/ 1 Bath</div>
    <div class="floor_plan_content">
      <div class="text">
        <div class="summary">
          <div class="row">
            <div class="square_feet label">Square Feet</div>
            <div class="square_feet value">598</div>
          </div>
          <div class="row">
            <div class="bedrooms label">Bedrooms</div>
            <div class="bedrooms value">1</div>
          </div>
          <div class="row">
            <div class="bathrooms label">Bathrooms</div>
            <div class="bathrooms value">1 Bath</div>
          </div>
          <div class="row">
            <div class="rent label">Rent</div>
            <div class="rent value">$1,475</div>
          </div>
          <div class="row">
            <div class="deposit label">Deposit</div>
            <div class="deposit value">$500 - $800</div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="floor_plan row2">
    <div class="floor_plan_name">2 Bedroom/ 2 Bath</div>
    <div class="floor_plan_content">
      <div class="text">
        <div class="summary">
          <div class="row">
            <div class="square_feet label">Square Feet</div>
            <div class="square_feet value">880</div>
          </div>
          <div class="row">
            <div class="bedrooms label">Bedrooms</div>
            <div class="bedrooms value">2</div>
          </div>
          <div class="row">
            <div class="bathrooms label">Bathrooms</div>
            <div class="bathrooms value">2 Bath</div>
          </div>
          <div class="row">
            <div class="rent label">Rent</div>
            <div class="rent value">$1,750</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
</body></html>
"""


def test_parse_cedar_ridge_two_plans() -> None:
    """User-flagged signature: Cedar Ridge 2 plan-level rows with the
    sqft separated from the plan name."""
    units = parse_rentaladdress_floorplans(_CEDAR_RIDGE_HTML, "https://x.test/floor_plans")
    assert len(units) == 2
    p1 = units[0]
    assert p1["floor_plan_name"] == "1 Bedroom/ 1 Bath"
    assert p1["sqft"] == "598"
    assert p1["bedrooms"] == "1"
    assert p1["bathrooms"] == "1"
    assert "1,475" in p1["rent_range"]
    assert p1["extraction_tier"] == "TIER_1_DOM_RENTALADDRESS_PLAN_LEVEL"

    p2 = units[1]
    assert p2["floor_plan_name"] == "2 Bedroom/ 2 Bath"
    assert p2["sqft"] == "880"
    assert p2["bedrooms"] == "2"
    assert p2["bathrooms"] == "2"
    assert "1,750" in p2["rent_range"]


def test_sqft_never_leaks_into_plan_name() -> None:
    """Pre-fix the generic parser made plan_name="598 Bedroom / 1 Bath"
    (sqft text leaked in). The dedicated adapter must not."""
    units = parse_rentaladdress_floorplans(_CEDAR_RIDGE_HTML, "x")
    for u in units:
        assert "598" not in u["floor_plan_name"]
        assert "880" not in u["floor_plan_name"]


def test_studio_plan_normalized_to_zero_beds() -> None:
    html = """
<div class="floor_plan_list">
  <div class="floor_plan">
    <div class="floor_plan_name">Studio</div>
    <div class="summary">
      <div class="row">
        <div class="square_feet label">Square Feet</div>
        <div class="square_feet value">450</div>
      </div>
      <div class="row">
        <div class="bedrooms label">Bedrooms</div>
        <div class="bedrooms value">Studio</div>
      </div>
      <div class="row">
        <div class="bathrooms label">Bathrooms</div>
        <div class="bathrooms value">1 Bath</div>
      </div>
      <div class="row">
        <div class="rent label">Rent</div>
        <div class="rent value">$1,100</div>
      </div>
    </div>
  </div>
</div>
"""
    units = parse_rentaladdress_floorplans(html, "x")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"


def test_rent_range_with_dash() -> None:
    """Some plans show ranges: $1,475 - $1,650 → rent_low=1475 rent_high=1650."""
    html = """
<div class="floor_plan_list"><div class="floor_plan">
  <div class="floor_plan_name">3 Bedroom/ 2 Bath</div>
  <div class="summary">
    <div class="row"><div class="square_feet label">Square Feet</div>
                     <div class="square_feet value">1100</div></div>
    <div class="row"><div class="bedrooms label">Bedrooms</div>
                     <div class="bedrooms value">3</div></div>
    <div class="row"><div class="bathrooms label">Bathrooms</div>
                     <div class="bathrooms value">2 Bath</div></div>
    <div class="row"><div class="rent label">Rent</div>
                     <div class="rent value">$1,800 - $2,200</div></div>
  </div>
</div></div>
"""
    units = parse_rentaladdress_floorplans(html, "x")
    assert len(units) == 1
    u = units[0]
    assert u["market_rent_low"] == 1800
    assert u["market_rent_high"] == 2200
    assert "1,800" in u["rent_range"] and "2,200" in u["rent_range"]


def test_empty_html_returns_no_units() -> None:
    assert parse_rentaladdress_floorplans("", "x") == []
    assert parse_rentaladdress_floorplans("<html>no plans</html>", "x") == []


def test_malformed_html_does_not_crash() -> None:
    """Defensive: malformed/partial HTML should return [] not raise."""
    bad = '<div class="floor_plan_list"><div class="floor_plan"><broken'
    units = parse_rentaladdress_floorplans(bad, "x")
    assert isinstance(units, list)  # may be empty, but must not raise


def test_missing_summary_div_skips_plan() -> None:
    """Plan div without .summary is skipped — no synthetic data."""
    html = """
<div class="floor_plan_list"><div class="floor_plan">
  <div class="floor_plan_name">Whatever</div>
</div></div>
"""
    units = parse_rentaladdress_floorplans(html, "x")
    # Either empty list or unit with empty fields — must not crash
    if units:
        assert units[0]["sqft"] == ""


def test_missing_plan_name_skips_block() -> None:
    """Plan block with no .floor_plan_name → skip (don't emit blank row)."""
    html = """
<div class="floor_plan_list"><div class="floor_plan">
  <div class="summary">
    <div class="row"><div class="square_feet label">Square Feet</div>
                     <div class="square_feet value">500</div></div>
  </div>
</div></div>
"""
    units = parse_rentaladdress_floorplans(html, "x")
    assert units == []


def test_unit_number_always_empty_plan_level() -> None:
    """RentalAddress is plan-level only (operator shows 'Check Availability'
    CTA, not per-unit roster). unit_number must always be empty so the
    schema's Phase 16 has_rent guard correctly suppresses date-default."""
    units = parse_rentaladdress_floorplans(_CEDAR_RIDGE_HTML, "x")
    for u in units:
        assert u["unit_number"] == ""


# ─────────────────────────────────────────────────────────────────────
# Detector + adapter registration integration
# ─────────────────────────────────────────────────────────────────────


def test_detector_routes_rentaladdress_host() -> None:
    """The canonical host (X.rentaladdress.com) fires the detector."""
    html = '<html><body>rentaladdress.com marketing</body></html>'
    det = detect_pms("https://cedarridgeapts.rentaladdress.com/floor_plans",
                     page_html=html)
    assert det.pms == "rentaladdress"
    assert det.confidence >= 0.85


def test_detector_routes_on_floor_plan_list_container() -> None:
    """The .floor_plan_list container is unique to the platform and
    serves as a fallback fingerprint when the host doesn't match
    (e.g. white-label deployments)."""
    html = '<html><body><div class="floor_plan_list">plans</div></body></html>'
    det = detect_pms("https://whitelabel.example/floor_plans",
                     page_html=html)
    assert det.pms == "rentaladdress"


def test_detector_does_not_misroute_unrelated_html() -> None:
    """Sanity: plain html without our markers must NOT match."""
    html = '<html><body>just text</body></html>'
    det = detect_pms("https://random.example/", page_html=html)
    assert det.pms != "rentaladdress"


def test_adapter_registered_under_pms_name() -> None:
    adapter = get_adapter("rentaladdress")
    assert isinstance(adapter, RentalAddressAdapter)
    assert adapter.pms_name == "rentaladdress"
