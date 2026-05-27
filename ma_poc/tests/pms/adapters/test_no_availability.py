"""No-availability detection + placeholder synthesis tests (2026-05-23).

Pins the operator-says-zero-availability path:
  • Curated phrase list covers the krcapartments wild text and 9 sibling
    variants observed across WordPress, Squarespace, and custom CMS.
  • Hypothetical-clause guard rejects matches inside ``if … no
    available …`` / ``in case there are no vacancies …`` marketing prose.
  • Placeholder unit carries ``data_quality_flag="NO_AVAILABILITY_NOW"``
    + transparent ``data_gaps`` listing — verdict layer treats it as a
    successful scrape of a documented zero-availability state, not as
    FAILED_NO_DATA.
"""
from __future__ import annotations

from ma_poc.pms.adapters._no_availability import (
    build_no_availability_placeholder,
    detect_no_availability,
    matched_phrase,
)

# ─── happy paths: detect ─────────────────────────────────────────────


def test_detect_matches_krcapartments_wild_text() -> None:
    """The exact phrase pulled from krcapartments/2537-emerson."""
    html = (
        "<section><h1>2537 Emerson</h1>"
        "<p>Sorry, there are no available units at this time. </p>"
        "</section>"
    )
    assert detect_no_availability(html) is True


def test_detect_matches_when_phrase_split_across_tags() -> None:
    """Operators wrap each sub-phrase in its own ``<span>`` for styling.
    Tag stripping must collapse them so the detector still trips."""
    html = (
        "<div><span>Sorry,</span> <em>there are</em> "
        "<strong>no</strong> available units at this time.</div>"
    )
    assert detect_no_availability(html) is True


def test_detect_handles_nbsp_entities_between_words() -> None:
    """WordPress themes often inject ``&nbsp;`` for spacing — those
    must collapse to plain whitespace before phrase matching."""
    html = "<p>Sorry,&nbsp;there&nbsp;are&nbsp;no&nbsp;available&nbsp;units</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_no_units_currently_available() -> None:
    html = "<p>No units currently available. Check back soon!</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_no_apartments_currently_available() -> None:
    html = "<p>No apartments are currently available.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_currently_no_vacancies() -> None:
    html = "<p>Currently no vacancies — join our waitlist!</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_fully_leased() -> None:
    html = "<p>Fully leased at this time.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_100_percent_leased() -> None:
    html = "<p>This community is 100% leased.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_waitlist_only_variants() -> None:
    assert detect_no_availability("<p>Wait list only.</p>") is True
    assert detect_no_availability("<p>Join our waitlist!</p>") is True
    assert detect_no_availability("<p>Join the wait list</p>") is True


# ─── rejection: hypothetical guard ───────────────────────────────────


def test_detect_rejects_if_clause_hypothetical() -> None:
    """``If there are no available units, please call`` is marketing
    prose — operator is NOT saying it's currently sold out."""
    html = "<p>If there are no available units at this time, please call.</p>"
    assert detect_no_availability(html) is False


def test_detect_rejects_when_clause_hypothetical() -> None:
    html = "<p>When no units currently available, you can join the waitlist.</p>"
    assert detect_no_availability(html) is False


def test_detect_rejects_in_case_clause_hypothetical() -> None:
    html = "<p>In case there are no available units, we'll contact you.</p>"
    assert detect_no_availability(html) is False


def test_detect_rejects_should_clause_hypothetical() -> None:
    html = "<p>Should there be no available units in your size...</p>"
    assert detect_no_availability(html) is False


def test_detect_rejects_unless_clause_hypothetical() -> None:
    html = "<p>Unless no available units remain, prices apply.</p>"
    assert detect_no_availability(html) is False


# ─── rejection: not present ──────────────────────────────────────────


def test_detect_rejects_empty_html() -> None:
    assert detect_no_availability("") is False


def test_detect_rejects_normal_listing_page() -> None:
    """A property with actual unit data must not match."""
    html = (
        "<div class='units'><div class='unit'>Unit 101 — $1,500 — 750 sq ft"
        "</div><div class='unit'>Unit 102 — $1,600 — 800 sq ft</div></div>"
    )
    assert detect_no_availability(html) is False


def test_detect_rejects_unrelated_marketing_copy() -> None:
    html = "<p>Our luxury apartments feature available amenities.</p>"
    assert detect_no_availability(html) is False


# ─── matched_phrase ──────────────────────────────────────────────────


def test_matched_phrase_returns_first_match_lowercased() -> None:
    html = "<p>SORRY, THERE ARE NO AVAILABLE UNITS at this time.</p>"
    phrase = matched_phrase(html)
    assert phrase is not None
    assert "no available units" in phrase
    assert phrase == phrase.lower()


def test_matched_phrase_returns_none_when_no_match() -> None:
    assert matched_phrase("<p>Welcome!</p>") is None
    assert matched_phrase("") is None


def test_matched_phrase_skips_hypothetical_match() -> None:
    """The phrase IS present but inside an if-clause — we must NOT
    report it as a matched phrase."""
    html = "<p>If there are no available units, call us.</p>"
    assert matched_phrase(html) is None


# ─── placeholder synthesis ───────────────────────────────────────────


def test_placeholder_has_no_availability_flag() -> None:
    unit = build_no_availability_placeholder(
        source_url="https://krcapartments.com/property-detail/2537-emerson/",
        property_name="2537 Emerson",
        matched_text="sorry, there are no available units",
    )
    assert unit["data_quality_flag"] == "NO_AVAILABILITY_NOW"


def test_placeholder_lists_all_missing_fields_as_gaps() -> None:
    unit = build_no_availability_placeholder()
    gaps = unit["data_gaps"]
    assert "rent" in gaps
    assert "sqft" in gaps
    assert "unit_number" in gaps
    assert "availability_date" in gaps


def test_placeholder_has_no_numeric_rent_or_sqft() -> None:
    """Honest empty fields — the placeholder must NOT carry $0 / 0 sqft
    that would mislead downstream pricing logic."""
    unit = build_no_availability_placeholder()
    assert unit.get("market_rent_low") in (None, 0, "", 0.0) or unit["market_rent_low"] is None
    assert not unit.get("market_rent_high")
    assert unit.get("sqft") in (None, "", 0)


def test_placeholder_marks_unavailable_status() -> None:
    unit = build_no_availability_placeholder()
    assert unit["availability_status"] == "UNAVAILABLE"


def test_placeholder_carries_source_url_for_telemetry() -> None:
    unit = build_no_availability_placeholder(
        source_url="https://krcapartments.com/property-detail/2537-emerson/",
    )
    assert (
        unit["source_api_url"]
        == "https://krcapartments.com/property-detail/2537-emerson/"
    )


def test_placeholder_extraction_tier_is_marked() -> None:
    unit = build_no_availability_placeholder()
    assert unit["extraction_tier"] == "TIER_1_DOM_NO_AVAILABILITY"


def test_placeholder_property_name_falls_back_when_empty() -> None:
    unit = build_no_availability_placeholder(property_name="")
    # Fallback to a non-empty placeholder so downstream code that
    # requires a non-empty floor_plan_name doesn't barf.
    assert unit["floor_plan_name"]


def test_placeholder_records_matched_phrase_in_source_ids() -> None:
    unit = build_no_availability_placeholder(
        matched_text="sorry, there are no available units",
    )
    sid = unit["source_ids"]
    assert sid["operator_published_state"] == "no_availability_now"
    assert "no available units" in sid["matched_phrase"]


# ─── 2026-05-27 612-failure-grind cohort additions ─────────────────


def test_detect_matches_no_available_properties() -> None:
    """AppFolio empty-embed cohort (skyview-lofts, vistadelplaza)."""
    html = "<div class='empty'>There are no available properties at this time.</div>"
    assert detect_no_availability(html) is True


def test_detect_matches_currently_no_available_listings() -> None:
    """RentCafe empty-search cohort."""
    html = "<p>There are currently no available units matching your search.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_no_listings_matching() -> None:
    html = "<p>Sorry — no listings matching your filters right now.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_add_to_waitlist() -> None:
    html = "<p>Add to our waitlist and we'll notify you.</p>"
    assert detect_no_availability(html) is True


def test_detect_matches_all_units_leased() -> None:
    html = "<p>All units leased — applications closed.</p>"
    assert detect_no_availability(html) is True


# ─── Real-HTML fixtures (Chrome MCP / curl captured snippets) ─────


def _fixture(name: str) -> str:
    from pathlib import Path
    return (
        Path(__file__).parent / "fixtures" / "no_inventory" / name
    ).read_text(encoding="utf-8")


def test_detect_real_html_rentcafe_waitlist_livebrez() -> None:
    assert detect_no_availability(_fixture("rentcafe_waitlist_livebrez.html")) is True


def test_detect_real_html_appfolio_empty_skyview() -> None:
    assert detect_no_availability(_fixture("appfolio_empty_skyview.html")) is True
