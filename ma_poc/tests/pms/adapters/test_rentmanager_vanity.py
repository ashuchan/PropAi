"""RentManager-vanity SSR adapter tests (2026-05-24).

Pins the HAR-driven RentManagerVanity parser for marketing sites
that use the ``.suite-group`` SSR layout from operators on
``*.twa.rentmanager.com``.

Live fixture from wilmingtonpointe.com (688 KB, 30 suite-groups,
many with rate ranges like "From: $790 - $860").
"""
from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._rentmanager_vanity import (
    _has_rm_marker,
    parse_rentmanager_vanity_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_marker_detects_suite_floorplans_class() -> None:
    body = '<div class="suite-floorplans"><div class="suite-group"></div></div>'
    assert _has_rm_marker(body) is True


def test_marker_detects_twa_rentmanager_link() -> None:
    body = '<a href="https://ccg.twa.rentmanager.com/portal">Resident Portal</a>'
    assert _has_rm_marker(body) is True


def test_marker_detects_suite_group_rate_class() -> None:
    body = '<p class="suite-group-rate"><span class="value">$1,200</span></p>'
    assert _has_rm_marker(body) is True


def test_marker_returns_false_for_unrelated_body() -> None:
    assert _has_rm_marker("<html><body>nothing</body></html>") is False


def test_marker_handles_empty_body() -> None:
    assert _has_rm_marker("") is False


def test_parse_live_wilmingtonpointe_fixture() -> None:
    """Live HAR-captured marketing body — 30 suite-groups, many with
    real rate ranges."""
    html = (
        FIXTURES / "rentmanager_vanity_wilmingtonpointe.html"
    ).read_text()
    units = parse_rentmanager_vanity_html(
        html, "https://www.wilmingtonpointe.com"
    )

    # Should extract at least 2 real plans (out of 30 raw containers,
    # after filtering placeholder $0 groups and deduping by name).
    # wilmingtonpointe publishes 2 real floorplans + 28 placeholder
    # bedroom-bucket groups with $0 rent.
    assert len(units) >= 2, (
        f"expected >=2 plans, got {len(units)}"
    )

    # Spot-check one with rent
    rated = [u for u in units if u.get("market_rent_low")]
    assert rated, "no plans with rent — parser broken"
    u0 = rated[0]
    assert u0["extraction_tier"] == "TIER_1_DOM_RENTMANAGER_VANITY"
    assert int(u0["market_rent_low"]) > 0


def test_parse_skips_zero_rate_placeholder_groups() -> None:
    """RentManager emits empty placeholder ``.suite-group`` for every
    bedroom bucket (rate=$0). Parser must skip those."""
    html = """
    <html><body>
      <div class="suite-floorplans">
        <div class="suite-group" data-bedrooms="0" id="suite-group-0-bedrooms">
          <h3 class="suite-group-type">Bachelor</h3>
          <p class="suite-group-bath"><span class="value">0</span></p>
          <p class="suite-group-rate"><span class="value">$0</span></p>
        </div>
        <div class="suite-group" data-bedrooms="1">
          <h3 class="suite-group-type">One Bedroom</h3>
          <p class="suite-group-bath"><span class="value">1</span></p>
          <p class="suite-group-sqft"><span class="value">700</span></p>
          <p class="suite-group-rate"><span class="value">$1,200 - $1,400</span></p>
          <p class="suite-group-availability"><span class="value">Now</span></p>
        </div>
      </div>
    </body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "One Bedroom"
    assert int(units[0]["market_rent_low"]) == 1200
    assert int(units[0]["market_rent_high"]) == 1400
    assert units[0]["sqft"] == "700"


def test_parse_uses_data_bedrooms_attr() -> None:
    """The data-bedrooms attribute is the most reliable bedroom signal
    (the floor_plan_name might be 'Bachelor', 'Studio', etc.)."""
    html = """
    <html><body><div class="suite-group" data-bedrooms="2">
      <h3 class="suite-group-type">Deluxe</h3>
      <p class="suite-group-bath"><span class="value">2</span></p>
      <p class="suite-group-sqft"><span class="value">950</span></p>
      <p class="suite-group-rate"><span class="value">$1,800</span></p>
    </div></body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "2"
    assert units[0]["bathrooms"] == "2"


def test_parse_sqft_range_takes_lower_bound() -> None:
    """When sqft is published as a range like "570 - 875", emit the
    lower bound (consistent with how rent_low is handled)."""
    html = """
    <html><body><div class="suite-group" data-bedrooms="1">
      <h3 class="suite-group-type">A1</h3>
      <p class="suite-group-bath"><span class="value">1</span></p>
      <p class="suite-group-sqft"><span class="value">570 - 875</span></p>
      <p class="suite-group-rate"><span class="value">$790 - $860</span></p>
    </div></body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    assert len(units) == 1
    assert units[0]["sqft"] == "570"  # lower bound
    assert int(units[0]["market_rent_low"]) == 790
    assert int(units[0]["market_rent_high"]) == 860


def test_parse_waitlist_marks_unavailable() -> None:
    html = """
    <html><body><div class="suite-group" data-bedrooms="1">
      <h3 class="suite-group-type">B1</h3>
      <p class="suite-group-sqft"><span class="value">800</span></p>
      <p class="suite-group-rate"><span class="value">$1,500</span></p>
      <p class="suite-group-availability"><span class="value">Waitlist</span></p>
    </div></body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    assert len(units) == 1
    assert units[0]["availability_status"] == "UNAVAILABLE"


def test_parse_returns_empty_for_no_suite_groups() -> None:
    assert parse_rentmanager_vanity_html(
        "<html><body>no suite groups</body></html>", "u"
    ) == []


def test_parse_returns_empty_for_blank_html() -> None:
    assert parse_rentmanager_vanity_html("", "u") == []


def test_parse_dedupes_same_floorplan_name() -> None:
    """Some sites emit the same suite-group twice (carousel, etc.).
    Dedupe by floor_plan_name (case-insensitive)."""
    html = """
    <html><body>
      <div class="suite-group" data-bedrooms="1">
        <h3 class="suite-group-type">Sedona</h3>
        <p class="suite-group-sqft"><span class="value">700</span></p>
        <p class="suite-group-rate"><span class="value">$1,500</span></p>
      </div>
      <div class="suite-group" data-bedrooms="1">
        <h3 class="suite-group-type">sedona</h3>
        <p class="suite-group-sqft"><span class="value">700</span></p>
        <p class="suite-group-rate"><span class="value">$1,500</span></p>
      </div>
    </body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    assert len(units) == 1


def test_parse_skips_groups_with_no_numeric_dimension() -> None:
    """Plans with neither rent nor sqft can't clear the validity gate
    — skip them so we don't emit bare-name rows."""
    html = """
    <html><body><div class="suite-group" data-bedrooms="1">
      <h3 class="suite-group-type">Empty Plan</h3>
      <p class="suite-group-bath"><span class="value">1</span></p>
      <p class="suite-group-rate"><span class="value">Call for pricing</span></p>
    </div></body></html>
    """
    units = parse_rentmanager_vanity_html(html, "u")
    # No rent, no sqft → skipped
    assert units == []
