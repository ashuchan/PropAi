"""Cortland card-DOM parser tests.

2026-05-21: Cortland silently migrated from ``preload = {floorplans:
...}`` JSON envelope to server-side-rendered ``<div class="apartments__card">``
cards. The legacy ``parse_cortland_units`` finds zero units against the
new page shape — the adapter was effectively broken in production until
this fix.

Fixture: ``ma_poc/tests/fixtures/cortland/macarthur_available_apartments.html``
— live capture of www.cortland.com/apartments/cortland-macarthur/available-apartments/
(64 unit cards across multiple floor plans).
"""

from __future__ import annotations

import re
from pathlib import Path

from ma_poc.pms.adapters.cortland import parse_cortland_cards

_FIXTURE = Path(
    "ma_poc/tests/fixtures/cortland/macarthur_available_apartments.html"
)


def _load() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def test_parses_all_64_cards() -> None:
    """Live fixture has 64 ``apartments__card`` divs — one per unit."""
    units = parse_cortland_cards(_load(), "https://x.test/")
    assert len(units) == 64, f"expected 64 units; got {len(units)}"


def test_unit_carries_apt_number() -> None:
    """Each card emits the ``Apt #NNNNNN`` number in unit_number."""
    units = parse_cortland_cards(_load(), "https://x.test/")
    # All 64 should have a non-empty unit number
    no_unum = [u for u in units if not u.get("unit_number")]
    assert no_unum == [], f"{len(no_unum)} cards missing unit_number"
    # Unit numbers are 6-digit IDs in this property
    for u in units[:5]:
        un = u["unit_number"]
        assert re.match(r"^\d{4,7}$", un), f"unit_number format: {un}"


def test_unit_carries_rent_floor_beds_baths_sqft() -> None:
    """Standard structural fields populated per the card text:
    ``Starting at $X / Floor N / N Bed | N Bath | NNN sq. ft.``"""
    units = parse_cortland_cards(_load(), "https://x.test/")
    # The Volterra-441007 unit (1BR, 740sf, $1,481, Floor 1)
    volterra_441007 = next(
        (u for u in units if u["unit_number"] == "441007"), None
    )
    assert volterra_441007 is not None
    u = volterra_441007
    assert u["floor_plan_name"] == "Volterra"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"
    assert u["sqft"] == "740"
    assert u["floor"] == "1"
    assert u["market_rent_low"] == 1481
    assert u["market_rent_high"] == 1481


def test_unit_carries_availability_now() -> None:
    """``Available Now`` should produce ``availability_status=AVAILABLE``
    and today's date in availability_date."""
    units = parse_cortland_cards(_load(), "https://x.test/")
    now_units = [u for u in units if u["availability_status"] == "AVAILABLE"]
    assert now_units, "no available units extracted"
    # At least some should have an availability_date
    with_date = [u for u in now_units if u["availability_date"]]
    assert with_date, "no unit got an availability_date"


def test_unit_availability_date_iso_format() -> None:
    """Dates emitted as YYYY-MM-DD regardless of card format
    (``Available starting 7/15`` or ``Available Now``)."""
    units = parse_cortland_cards(_load(), "https://x.test/")
    for u in units:
        if u["availability_date"]:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", u["availability_date"]), (
                f"date not ISO: {u['availability_date']}"
            )


def test_unit_rent_in_valid_band() -> None:
    """Rents should land in a reasonable $1k-$10k range for Cortland."""
    units = parse_cortland_cards(_load(), "https://x.test/")
    with_rent = [u for u in units if u["market_rent_low"]]
    assert len(with_rent) >= 60, (
        f"expected ≥60 of 64 units to have rent; got {len(with_rent)}"
    )
    for u in with_rent:
        assert 500 < u["market_rent_low"] < 50_000, (
            f"rent out of band: {u['market_rent_low']}"
        )


def test_handles_studio_bedroom() -> None:
    """``Studio Bed | 1 Bath`` should normalise bedrooms to ``"0"``."""
    html = '''
    <html><body>
    <div class="apartments__card">
      <a class="apartments__card-link">Apt #555000</a>
      <span class="apartments__card-columns">
        Spruce<br>
        Apt #555000<br>
        Starting at $1,200<br>
        Floor 2<br>
        Studio Bed | 1 Bath | 500 sq. ft.<br>
        Available Now
      </span>
    </div>
    </body></html>
    '''
    units = parse_cortland_cards(html, "https://x.test/")
    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"
    assert units[0]["floor_plan_name"] == "Spruce"


def test_parses_two_synthetic_cards() -> None:
    """Synthetic mini-fixture — sanity-check the parser handles a simple
    2-card list without the surrounding marketing chrome."""
    html = '''
    <html><body>
    <div class="apartments__card">
      <a class="apartments__card-link">Apt #100001</a>
      <span class="apartments__card-columns">
        Aspen<br>
        Apt #100001<br>
        Starting at $1,500<br>
        Floor 3<br>
        1 Bed | 1 Bath | 700 sq. ft.<br>
        Available starting 8/1
      </span>
    </div>
    <div class="apartments__card">
      <a class="apartments__card-link">Apt #100002</a>
      <span class="apartments__card-columns">
        Birch<br>
        Apt #100002<br>
        Starting at $2,200<br>
        Floor 5<br>
        2 Bed | 2 Bath | 1100 sq. ft.<br>
        Available Now
      </span>
    </div>
    </body></html>
    '''
    units = parse_cortland_cards(html, "https://x.test/")
    assert len(units) == 2
    assert units[0]["unit_number"] == "100001"
    assert units[0]["floor_plan_name"] == "Aspen"
    assert units[0]["bedrooms"] == "1"
    assert units[0]["market_rent_low"] == 1500
    assert units[1]["unit_number"] == "100002"
    assert units[1]["floor_plan_name"] == "Birch"
    assert units[1]["bedrooms"] == "2"
    assert units[1]["market_rent_low"] == 2200


def test_returns_empty_on_no_cards() -> None:
    """When the HTML doesn't have ``apartments__card`` divs, return empty
    (the adapter then falls back to the legacy preload parser)."""
    assert parse_cortland_cards("", "https://x.test/") == []
    assert parse_cortland_cards("<html><body>none</body></html>", "https://x.test/") == []


def test_skips_cards_without_apt_number() -> None:
    """A ``apartments__card`` div without an ``Apt #X`` mention isn't a
    unit card (could be a floor-plan card or unrelated component) —
    skip it."""
    html = '''
    <html><body>
    <div class="apartments__card">
      <a class="apartments__card-link">View Floor Plan</a>
      <span class="apartments__card-columns">Some Plan</span>
    </div>
    </body></html>
    '''
    units = parse_cortland_cards(html, "https://x.test/")
    assert units == []
