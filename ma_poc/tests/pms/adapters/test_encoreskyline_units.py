"""Encoreskyline-template per-plan units parser (2026-05-19).

Verified-live ground-truth (rendered text captured after the per-plan
``Check Availability`` JS toggle fired):

  - encoreskyline.com/floorplans/spruce/ → "#B-302 Floor 3 703 sq. ft.
    $1,750 $400 Deposit Available Jul 3"
  - geneseepointe.com/floorplans/floorplan-bbr/ → 4 units
    (#308 / #209 / #410 / #310 Floor 1 820 sq. ft. $2,025 …)
  - highlineaustin.com/floorplans/a1/ → 3 units
    (#2206 / #1306 / #7108 668 sq. ft. $1,2XX $150 Deposit Available …)
"""

from __future__ import annotations

from ma_poc.pms.adapters._encoreskyline_units import (
    is_encoreskyline_template_page,
    parse_encoreskyline_units,
)

# --- is_encoreskyline_template_page (detection) ---------------------------


def test_detector_fires_on_jonah_widget_initialiser() -> None:
    """The Jonah Digital initialiser is the stable marker across the family."""
    html = (
        '<html><body><script>JonahWidget.meetelise({'
        'organization:"29b19576",building:"534b3cba"});</script></body></html>'
    )
    assert is_encoreskyline_template_page(html) is True


def test_detector_fires_on_meetelise_token() -> None:
    """Any of the Jonah markers is sufficient."""
    assert is_encoreskyline_template_page('<script src="//meetelise.com/x"></script>')


def test_detector_is_quiet_on_unrelated_page() -> None:
    """Genuinely no-Jonah page → no false positive."""
    html = "<html><body>Plain marketing site, no widget</body></html>"
    assert is_encoreskyline_template_page(html) is False


def test_detector_handles_empty() -> None:
    assert is_encoreskyline_template_page("") is False


# --- parse_encoreskyline_units (the unit-row regex) -----------------------


def test_encoreskyline_spruce_single_unit() -> None:
    """encoreskyline.com/floorplans/spruce/ — 1 real unit, with Floor + Deposit."""
    rendered = (
        " Floor plans « Back Spruce 1 bed 1 bath 703 sq. ft. $1,750 - $1,790 "
        "2D 3D Virtual Tour Carport $35 and SmartRent $30 in addition to the "
        "monthly rent amount. Book a Tour Check Availability "
        "#B-302 Floor 3 703 sq. ft. $1,750 $400 Deposit Available Jul 3 "
        "Floor plans are artist's rendering."
    )
    units = parse_encoreskyline_units(
        rendered, "https://encoreskyline.com/floorplans/spruce/"
    )
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "B-302"
    assert u["floor"] == "3"
    assert u["sqft"] == "703"
    assert u["market_rent_low"] == 1750
    assert u["market_rent_high"] == 1750
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "Jul 3"
    assert u["extraction_tier"] == "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"


def test_geneseepointe_floorplan_bbr_four_units() -> None:
    """geneseepointe.com/floorplans/floorplan-bbr/ — 4 real units, "Available Now"."""
    rendered = (
        " Floorplan B/BR 1 bed 1 bath 820 sq. ft. Starting at $2,025 "
        "Book a Tour Check Availability "
        "#308 Floor 1 820 sq. ft. Starting at $2,025 Available Now Lease Now "
        "#209 Floor 1 820 sq. ft. Starting at $2,025 Available Jun 16 Lease Now "
        "#410 Floor 1 820 sq. ft. Starting at $2,025 Available Jul 08 Lease Now "
        "#310 Floor 1 820 sq. ft. Starting at $2,025 Available Jul 08 Lease Now "
        "Floor plans are artist's rendering."
    )
    units = parse_encoreskyline_units(
        rendered, "https://geneseepointe.com/floorplans/floorplan-bbr/"
    )
    assert len(units) == 4
    nums = [u["unit_number"] for u in units]
    assert nums == ["308", "209", "410", "310"]
    dates = [u["availability_date"] for u in units]
    assert dates == ["Now", "Jun 16", "Jul 08", "Jul 08"]
    assert all(u["market_rent_low"] == 2025 for u in units)
    assert all(u["floor"] == "1" for u in units)


def test_highlineaustin_a1_three_units_with_deposit() -> None:
    """highlineaustin.com/floorplans/a1/ — 3 real units, Deposit, no Floor."""
    rendered = (
        " 1 bath 668 sq. ft. Only 3 left! Starting at $1,234 "
        "Book a Tour Check Availability "
        "#2206 668 sq. ft. Starting at $1,284 $150 Deposit Available May 20 Lease Now "
        "#1306 668 sq. ft. Starting at $1,234 $150 Deposit Available Jul 14 Lease Now "
        "#7108 668 sq. ft. Starting at $1,364 $150 Deposit Available Jul 21 Lease Now"
    )
    units = parse_encoreskyline_units(
        rendered, "https://highlineaustin.com/floorplans/a1/"
    )
    assert len(units) == 3
    assert [u["unit_number"] for u in units] == ["2206", "1306", "7108"]
    assert [u["market_rent_low"] for u in units] == [1284, 1234, 1364]
    # No Floor token on this site → ``floor`` is empty string
    assert all(u["floor"] == "" for u in units)
    # All carry the same sqft
    assert all(u["sqft"] == "668" for u in units)


def test_pre_click_text_returns_empty() -> None:
    """Pre-click text has no ``#<unit>`` rows → no false positives.

    Critical safety: if the recovery caller fails to fire Check Availability,
    we MUST NOT synthesize fake unit rows from the floorplan-level prose.
    """
    rendered = (
        "Spruce 1 bed 1 bath 703 sq. ft. $1,750 - $1,790 "
        "2D 3D Virtual Tour Book a Tour Check Availability "
        "Floor plans are artist's rendering."
    )
    assert parse_encoreskyline_units(rendered, "https://x.com/") == []


def test_bare_floorplan_starting_at_lines_are_not_matched() -> None:
    """``Starting at $X`` without a leading ``#<unit>`` is plan-level, not unit."""
    rendered = (
        "Brewster 1 bed 1 bath 826 sq. ft. Starting at $1,008 Book a Tour "
        "Madison 1 bed 1 bath 918 sq. ft. Starting at $1,033 Book a Tour"
    )
    assert parse_encoreskyline_units(rendered, "https://x.com/") == []


def test_duplicate_unit_number_is_deduped() -> None:
    """Same ``#<unit>`` repeated (re-render artefact) → emitted once."""
    rendered = (
        "#308 Floor 1 820 sq. ft. $2,025 Available Now Lease Now "
        "#308 Floor 1 820 sq. ft. $2,025 Available Now Lease Now "
        "#209 Floor 1 820 sq. ft. $2,025 Available Jun 16 Lease Now"
    )
    units = parse_encoreskyline_units(rendered, "https://x.com/")
    assert [u["unit_number"] for u in units] == ["308", "209"]


def test_handles_empty_text() -> None:
    assert parse_encoreskyline_units("", "https://x.com/") == []
    assert parse_encoreskyline_units(None, "https://x.com/") == []  # type: ignore[arg-type]


def test_slash_date_format_supported() -> None:
    """Some sites render ``Available 5/20`` instead of ``Available May 20``."""
    rendered = "#101 668 sq. ft. $1,200 Available 5/20"
    units = parse_encoreskyline_units(rendered, "https://x.com/")
    assert len(units) == 1
    assert units[0]["availability_date"] == "5/20"
