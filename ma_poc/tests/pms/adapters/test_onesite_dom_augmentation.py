"""F7d (2026-05-20 PID 11317): OneSite DOM availability augmentation.

When OneSite captures only the /floorplans endpoint (no /units), the
adapter emits plan-level rows with `availability_date=""`. The marketing
DOM often carries per-unit availability as `data-availability` /
`data-available-date` attributes on apartment cards. The augmentation
pass extracts those (unit_id, date) pairs from the page HTML and merges
them onto the units by unit_id — non-destructive, only fills empty
date fields.

Live evidence: dixonatstonegate.com 2026-05-20 — 11 data-availability
attrs in DOM, 11 plan-level units emitted by /floorplans API, zero
date merge before this fix.
"""
from __future__ import annotations

from ma_poc.pms.adapters.onesite import (
    _augment_units_with_dom_availability,
    _extract_dom_avail_pairs,
)


# ── _extract_dom_avail_pairs — pure DOM scan ──────────────────────────────


def test_extracts_pair_from_simple_data_attrs() -> None:
    """Standard live shape: `<article data-unit-id="N" data-availability="ISO">`."""
    html = """
    <html><body>
      <article data-unit-id="2604014" data-availability="2026-06-15"></article>
      <article data-unit-id="2604015" data-availability="2026-07-01"></article>
    </body></html>
    """
    pairs = _extract_dom_avail_pairs(html)
    assert pairs == {"2604014": "2026-06-15", "2604015": "2026-07-01"}


def test_alias_attribute_names_accepted() -> None:
    """`data-available-date`, `data-move-in-date`, `data-ready-date` all
    map to the same logical field. `data-unit-number`, `data-apartment-id`,
    `data-listing-id` all map to the same logical id."""
    html = """
    <html><body>
      <article data-listing-id="L1" data-move-in-date="6/15/2026"></article>
      <article data-apartment-id="A2" data-ready-date="July 1, 2026"></article>
      <article data-unit-number="U3" data-available-date="2026-08-15"></article>
    </body></html>
    """
    pairs = _extract_dom_avail_pairs(html)
    assert pairs == {
        "L1": "6/15/2026",
        "A2": "July 1, 2026",
        "U3": "2026-08-15",
    }


def test_orphan_date_without_unit_id_is_skipped() -> None:
    """An element with `data-availability` but no id attribute on the
    SAME element should NOT be paired with an id elsewhere in the doc.
    Cross-element pairing would cause id pollution."""
    html = """
    <html><body>
      <div data-unit-id="U1"></div>
      <div data-availability="2026-06-15"></div>
    </body></html>
    """
    pairs = _extract_dom_avail_pairs(html)
    assert pairs == {}


def test_first_occurrence_wins_when_id_duplicated() -> None:
    """RealPage sometimes renders the same card in mobile + desktop
    variants. The first-seen date wins so duplicates don't flip the value."""
    html = """
    <html><body>
      <div data-unit-id="U1" data-availability="2026-06-15"></div>
      <div data-unit-id="U1" data-availability="2026-06-20"></div>
    </body></html>
    """
    pairs = _extract_dom_avail_pairs(html)
    assert pairs == {"U1": "2026-06-15"}


def test_empty_or_huge_html_returns_empty_dict() -> None:
    """Bounded scan: empty input and runaway-size input both return {}."""
    assert _extract_dom_avail_pairs("") == {}
    assert _extract_dom_avail_pairs(None) == {}  # type: ignore[arg-type]
    huge = "<div></div>" * 1_000_000  # ~12 MB
    assert _extract_dom_avail_pairs(huge) == {}


# ── _augment_units_with_dom_availability — merge with units list ──────────


def test_merge_fills_empty_availability_date() -> None:
    """Units with no date pick up the DOM-sourced value by unit_id."""
    units = [
        {"unit_id": "U1", "availability_date": ""},
        {"unit_id": "U2", "availability_date": ""},
    ]
    html = """
    <html><body>
      <article data-unit-id="U1" data-availability="2026-06-15"></article>
      <article data-unit-id="U2" data-availability="2026-07-01"></article>
    </body></html>
    """
    _augment_units_with_dom_availability(units, html)
    assert units[0]["availability_date"] == "2026-06-15"
    assert units[1]["availability_date"] == "2026-07-01"


def test_merge_does_not_overwrite_existing_date() -> None:
    """API-set dates are authoritative — DOM augmentation must NOT
    overwrite them (the /units endpoint, if captured, has the ground truth)."""
    units = [
        {"unit_id": "U1", "availability_date": "2026-05-01"},  # API-set
        {"unit_id": "U2", "availability_date": ""},
    ]
    html = """
    <html><body>
      <article data-unit-id="U1" data-availability="2026-06-15"></article>
      <article data-unit-id="U2" data-availability="2026-07-01"></article>
    </body></html>
    """
    _augment_units_with_dom_availability(units, html)
    # U1: API value preserved
    assert units[0]["availability_date"] == "2026-05-01"
    # U2: DOM-augmented
    assert units[1]["availability_date"] == "2026-07-01"


def test_merge_by_unit_number_fallback() -> None:
    """When unit_id is absent, fall back to unit_number as the join key."""
    units = [
        {"unit_number": "138", "availability_date": ""},
    ]
    html = """
    <html><body>
      <article data-unit-number="138" data-availability="2026-09-01"></article>
    </body></html>
    """
    _augment_units_with_dom_availability(units, html)
    assert units[0]["availability_date"] == "2026-09-01"


def test_merge_with_no_pairs_is_noop() -> None:
    """No DOM availability attrs → units unchanged."""
    units = [{"unit_id": "U1", "availability_date": ""}]
    _augment_units_with_dom_availability(units, "<html><body></body></html>")
    assert units[0]["availability_date"] == ""


def test_merge_with_no_units_is_safe() -> None:
    """Empty units list shouldn't crash."""
    _augment_units_with_dom_availability(
        [],
        '<article data-unit-id="U1" data-availability="2026-06-15"></article>',
    )
    # No assertion needed — just confirms no exception.
