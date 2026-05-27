"""JetEngine repeater extractor tests (2026-05-23).

Pins the WordPress + RealPage-OLL pattern (Copper Pointe cohort):
  • Three-signal detection (jet class + rr_unit_terms_popup class +
    onlineleasing.realpage.com URL in body)
  • Row regex matches every ``<tr class="jet-listing-dynamic-repeater__item">``
  • Per-row extraction: unit_number / rent / sqft / availability_date
  • RealPage propertyId + unitId surfaced into source_ids
  • Bedroom count derived from URL slug or H2

Fixture: ``ma_poc/tests/fixtures/jetengine_realpage/copperpoint_3br.html``
(live page pulled 2026-05-23 from
copperpointeapts.com/property-floor-plans/c1-3-bedroom/).
"""
from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._jetengine_repeater import (
    detect_jetengine_repeater,
    extract_bedroom_count,
    extract_plan_h2_label,
    extract_plan_slug,
    parse_jetengine_rows,
)

_FIXTURE = Path(
    "ma_poc/tests/fixtures/jetengine_realpage/copperpoint_3br.html"
)


def _read_fixture() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


# ─── detector ────────────────────────────────────────────────────────


def test_detect_matches_live_copperpoint_fixture() -> None:
    assert detect_jetengine_repeater(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    ) is True


def test_detect_rejects_empty_html() -> None:
    assert detect_jetengine_repeater("", "") is False


def test_detect_rejects_jet_class_without_rr_popup() -> None:
    """Plain JetEngine repeater on a non-rental WP site → reject."""
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td>some content</td></tr>'
    )
    assert detect_jetengine_repeater(html, "") is False


def test_detect_rejects_jet_class_without_realpage_marker() -> None:
    """Has JetEngine + rr_unit_terms_popup but no RealPage backend
    URL — different leasing flow, don't claim it."""
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td><a class="rr_unit_terms_popup">unit</a></td></tr>'
    )
    assert detect_jetengine_repeater(html, "") is False


def test_detect_matches_minimal_three_signal_html() -> None:
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td><a class="rr_unit_terms_popup" '
        'data-unit-application-url="https://1234567.onlineleasing.realpage.com/">'
        '101</a></td><td>$1500</td><td>800</td></tr>'
    )
    assert detect_jetengine_repeater(html, "") is True


# ─── bedroom-count derivation ────────────────────────────────────────


def test_extract_bedroom_from_url_slug() -> None:
    assert (
        extract_bedroom_count(
            "",
            "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
        )
        == 3
    )
    assert (
        extract_bedroom_count(
            "",
            "https://x.com/property-floor-plans/a2-2-bedroom",
        )
        == 2
    )
    assert (
        extract_bedroom_count(
            "",
            "https://x.com/property-floor-plans/b1-1-bedroom/",
        )
        == 1
    )


def test_extract_bedroom_from_url_studio_slug() -> None:
    assert (
        extract_bedroom_count(
            "",
            "https://x.com/property-floor-plans/s1-studio/",
        )
        == 0
    )
    assert (
        extract_bedroom_count(
            "",
            "https://x.com/property-floor-plans/e1-efficiency/",
        )
        == 0
    )


def test_extract_bedroom_falls_back_to_h2() -> None:
    """URL has no bed hint — use the page H2 heading."""
    html = "<h2>2 Bedroom</h2>"
    assert extract_bedroom_count(html, "https://x.com/some-page/") == 2


def test_extract_bedroom_h2_studio() -> None:
    html = "<h2>Studio</h2>"
    assert extract_bedroom_count(html, "https://x.com/page/") == 0


def test_extract_bedroom_returns_none_when_no_signal() -> None:
    assert extract_bedroom_count("", "") is None
    assert extract_bedroom_count("<h2>Welcome</h2>", "https://x.com/") is None


def test_extract_plan_slug_from_url() -> None:
    assert (
        extract_plan_slug(
            "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/"
        )
        == "c1-3-bedroom"
    )


def test_extract_plan_slug_returns_empty_for_non_matching_url() -> None:
    assert extract_plan_slug("") == ""
    assert extract_plan_slug("https://x.com/about/") == ""


def test_extract_plan_h2_label_returns_canonical_heading() -> None:
    assert extract_plan_h2_label("<h2>3 Bedroom</h2>") == "3 Bedroom"
    assert extract_plan_h2_label("<h2>Studio</h2>") == "Studio"
    assert extract_plan_h2_label("<h1>Welcome</h1>") == ""


# ─── parser: live fixture ────────────────────────────────────────────


def test_parse_live_fixture_extracts_all_jet_rows() -> None:
    """The Copper Pointe 3 BR page has 4 jet repeater rows."""
    rows = parse_jetengine_rows(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    )
    assert len(rows) == 4


def test_parse_live_fixture_unit_10108_correct() -> None:
    """Canonical unit 10108: $1539 / 1185 sqft / Now."""
    rows = parse_jetengine_rows(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    )
    by_num = {r["unit_number"]: r for r in rows}
    assert "10108" in by_num
    u = by_num["10108"]
    assert u["market_rent_low"] == 1539
    assert u["sqft"] == "1185"
    assert u["availability_date"] == ""  # "Now" normalizes to empty
    assert u["bedrooms"] == "3"
    assert u["floor_plan_name"] == "3 Bedroom"


def test_parse_live_fixture_unit_11101_has_future_date() -> None:
    """Unit 11101 has availability_date 2026-06-11 — must preserve
    YYYY-MM-DD format."""
    rows = parse_jetengine_rows(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    )
    by_num = {r["unit_number"]: r for r in rows}
    assert "11101" in by_num
    assert by_num["11101"]["availability_date"] == "2026-06-11"
    assert by_num["11101"]["market_rent_low"] == 1517


def test_parse_live_fixture_realpage_property_id_extracted() -> None:
    """The data-unit-application-url contains 8875465.onlineleasing.realpage.com
    — that's the OneSite property ID. Must surface to source_ids."""
    rows = parse_jetengine_rows(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    )
    assert rows
    sid = rows[0]["source_ids"]
    assert sid["realpage_oll_property_id"] == "8875465"
    assert sid["realpage_oll_unit_id"].isdigit()


def test_parse_live_fixture_extraction_tier_is_marked() -> None:
    rows = parse_jetengine_rows(
        _read_fixture(),
        "https://copperpointeapts.com/property-floor-plans/c1-3-bedroom/",
    )
    assert rows
    assert (
        rows[0]["extraction_tier"]
        == "TIER_1_DOM_JETENGINE_REALPAGE_OLL"
    )


# ─── parser: edge cases ──────────────────────────────────────────────


def test_parse_returns_empty_when_no_rows_present() -> None:
    assert parse_jetengine_rows("<html><body>no rows</body></html>", "") == []
    assert parse_jetengine_rows("", "https://x.com/") == []


def test_parse_skips_rows_without_unit_anchor() -> None:
    """A JetEngine row missing the rr_unit_terms_popup anchor isn't
    a unit row — it might be a heading repeater. Skip rather than
    invent unit numbers."""
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td>just text</td><td>$1500</td><td>800</td><td>Now</td></tr>'
    )
    assert parse_jetengine_rows(html, "") == []


def test_parse_skips_rows_without_rent() -> None:
    """No rent → skip. The success bar requires both rent AND sqft."""
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td><a class="rr_unit_terms_popup">101</a></td>'
        '<td>—</td><td>800</td><td>Now</td></tr>'
    )
    assert parse_jetengine_rows(html, "") == []


def test_parse_skips_rows_without_sqft() -> None:
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td><a class="rr_unit_terms_popup">101</a></td>'
        '<td>$1500</td><td></td><td>Now</td></tr>'
    )
    assert parse_jetengine_rows(html, "") == []


def test_parse_handles_html_entity_in_unit_application_url() -> None:
    """Operator HTML often escapes & as &amp; — the parser must
    unescape before pattern-matching the RealPage propertyId."""
    html = (
        '<tr class="jet-listing-dynamic-repeater__item">'
        '<td><a class="rr_unit_terms_popup" '
        'data-unit-application-url="https://9999999.onlineleasing.realpage.com/'
        '?MoveInDate=Now&amp;UnitId=42&amp;Site=x">505</a></td>'
        '<td>$1200</td><td>700</td><td>Now</td></tr>'
    )
    rows = parse_jetengine_rows(html, "https://x.com/property-floor-plans/a1-1-bedroom/")
    assert len(rows) == 1
    assert rows[0]["source_ids"]["realpage_oll_property_id"] == "9999999"
    assert rows[0]["source_ids"]["realpage_oll_unit_id"] == "42"
