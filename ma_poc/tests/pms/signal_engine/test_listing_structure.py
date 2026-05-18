"""Tests for ``count_listing_structural_signals`` / ``has_listing_structure``.

These pin the LISTING-STRUCTURE detector's match set so the
``STUB_AGGREGATE_COPY`` discriminator (playbook §8.10) keeps working for
known producers AND extends cleanly to Wordpress/Elementor sites that
ship inventory in ``floor_title`` / ``floor_details`` / ``floor_price_*``
divs without ``$NNN`` in the static HTML.

Origin: PID 21976 princetonmanagement.com / century-square-townhomes on
2026-05-18. The home page has 29 ``class="floor_..."`` divs (real
inventory; prices rendered client-side, so no ``$NNN`` in static HTML).
The pre-fix listing-structure detector matched ``unit-card`` /
``floorplan-card`` / ``listing-card`` only, so the page falsely scored
0 structural signals -> ``STUB_AGGREGATE_COPY`` gate -> LLM_DOM skipped
-> extraction missed real units.

The fix generalises the regex to (noun) x [-_] x (suffix) where noun is
in the listing-vocabulary list and suffix covers both container nouns
(card/row/item/wrapper) and per-listing detail nouns (title/details/
info/price). Adding a new vendor's noun extends the regex automatically.
"""

from __future__ import annotations

from ma_poc.pms.signal_engine.floor_plan_signals import (
    count_listing_structural_signals,
    has_listing_structure,
)


# ── Card / row container patterns (existing contract — must not regress) ────


def test_unit_card_class_counted_when_repeated() -> None:
    html = '<div class="unit-card">A</div><div class="unit-card">B</div>'
    assert count_listing_structural_signals(html) >= 1
    assert has_listing_structure(html)


def test_floorplan_card_class_counted() -> None:
    html = (
        '<div class="floorplan-card">A</div>'
        '<div class="floorplan-card">B</div>'
    )
    assert has_listing_structure(html)


def test_underscore_separator_counted() -> None:
    html = '<div class="unit_row a">A</div><div class="unit_row b">B</div>'
    assert has_listing_structure(html)


def test_id_attribute_counted_not_just_class() -> None:
    html = '<div id="unit-card-1">A</div><div id="unit-card-2">B</div>'
    assert has_listing_structure(html)


# ── Wordpress/Elementor `floor_*` detail patterns (Bug #2 fix) ──────────────


def test_elementor_floor_title_repeats_count() -> None:
    """The exact PID 21976 princetonmanagement.com pattern — 29 of these
    on the live home page, pre-fix matched 0."""
    html = (
        '<div class="floor_title">Plan A</div>'
        '<div class="floor_title">Plan B</div>'
        '<div class="floor_title">Plan C</div>'
    )
    assert count_listing_structural_signals(html) >= 1
    assert has_listing_structure(html)


def test_elementor_floor_details_repeats_count() -> None:
    html = (
        '<div class="floor_details">specs</div>'
        '<div class="floor_details">specs</div>'
    )
    assert has_listing_structure(html)


def test_elementor_floor_price_details_repeats_count() -> None:
    """Compound class name with multiple words after the noun-suffix
    pair — must still match because the regex consumes trailing chars."""
    html = (
        '<div class="floor_details floor_price_details">$1500</div>'
        '<div class="floor_details floor_price_details">$1600</div>'
    )
    assert has_listing_structure(html)


def test_elementor_only_one_floor_class_does_not_trip() -> None:
    """Single instance of a listing-class is not a listing — the gate
    requires REPETITION (>=2)."""
    html = '<div class="floor_title">One</div>'
    assert count_listing_structural_signals(html) == 0
    assert not has_listing_structure(html)


# ── Generic vocabulary extensions (apartment, plan, home, residence) ────────


def test_apartment_card_pattern() -> None:
    html = (
        '<div class="apartment-card">A</div>'
        '<div class="apartment-card">B</div>'
    )
    assert has_listing_structure(html)


def test_plan_item_pattern() -> None:
    html = (
        '<li class="plan-item">A</li>'
        '<li class="plan-item">B</li>'
    )
    assert has_listing_structure(html)


def test_home_row_pattern() -> None:
    """Some PMSes call them "homes" rather than "units"."""
    html = (
        '<div class="home_row">A</div>'
        '<div class="home_row">B</div>'
    )
    assert has_listing_structure(html)


def test_residence_card_pattern() -> None:
    """Luxury rentals use "residence" in the class noun."""
    html = (
        '<div class="residence-card">A</div>'
        '<div class="residence-card">B</div>'
    )
    assert has_listing_structure(html)


def test_model_block_pattern() -> None:
    """Some marketing-style sites call them "model homes" / model rows."""
    html = (
        '<div class="model-block">A</div>'
        '<div class="model-block">B</div>'
    )
    assert has_listing_structure(html)


def test_vacancy_card_pattern() -> None:
    html = (
        '<div class="vacancy-card">A</div>'
        '<div class="vacancy-card">B</div>'
    )
    assert has_listing_structure(html)


# ── False-positive guards ───────────────────────────────────────────────────


def test_no_match_on_unrelated_classes() -> None:
    """Garden-variety wrapper / hero / nav classes must NOT match — the
    noun-suffix pair is the discriminator."""
    html = (
        '<div class="hero-section">A</div>'
        '<div class="hero-section">B</div>'
        '<div class="content-wrapper">C</div>'
        '<div class="content-wrapper">D</div>'
        '<div class="nav-item">E</div>'
        '<div class="nav-item">F</div>'
    )
    assert count_listing_structural_signals(html) == 0
    assert not has_listing_structure(html)


def test_no_match_on_floor_without_suffix() -> None:
    """A bare ``floor`` class (no separator+suffix) must NOT match — too
    generic; could be a CSS modifier (e.g. ``class="floor"`` for a level
    indicator)."""
    html = '<div class="floor">A</div><div class="floor">B</div>'
    assert count_listing_structural_signals(html) == 0


def test_no_match_on_noun_in_text_only() -> None:
    """The noun must appear inside a class/id attribute — text content
    that mentions ``floor_title`` doesn't count."""
    html = "<p>We have floor_title and floor_details for each plan</p>"
    assert count_listing_structural_signals(html) == 0


def test_mixed_real_listing_and_false_positives() -> None:
    """A page with both real listing markup AND irrelevant repeated
    classes scores at least 1 — the real listing dominates."""
    html = (
        '<div class="hero-section">A</div>'
        '<div class="hero-section">B</div>'
        '<div class="floor_title">Plan A</div>'
        '<div class="floor_title">Plan B</div>'
    )
    assert has_listing_structure(html)


# ── Empty / malformed input ──────────────────────────────────────────────────


def test_empty_html_returns_zero() -> None:
    assert count_listing_structural_signals("") == 0
    assert not has_listing_structure("")


def test_none_html_returns_zero() -> None:
    # noqa: PT011 — function should not raise on None
    assert count_listing_structural_signals(None) == 0  # type: ignore[arg-type]
