"""Anchor keyword expansion — blockwall v2 Action 5.

The 300-property A/B (investigations/2026-05-21-t3-grind/artifacts/
blockwall_v2/STRATEGY.md) found 61/300 (20%) with no floor-plan anchor
discoverable from homepage. The DEFAULT_ANCHOR_KEYWORDS list was
missing common marketing-CMS labels: ``Models``, ``Homes``,
``Residences``, ``Rates``, ``Explore``, ``Shop``, ``Vacancies``,
``Now Leasing``, etc.

These tests pin the expanded keyword list so a future cleanup pass
doesn't accidentally regress the cohort coverage.
"""
from __future__ import annotations

from ma_poc.pms.signal_engine.defaults import DEFAULT_ANCHOR_KEYWORDS


_KEYWORDS = {kw: weight for kw, weight in DEFAULT_ANCHOR_KEYWORDS}


def test_models_anchor_present() -> None:
    """Apartment marketing sites often label their floor-plan section
    'Models' — was in PATH_KEYWORDS but missing as anchor text. The
    61 no_target_anchor cohort included several Greystar/Camden CMS
    sites that use this label exclusively."""
    assert "models" in _KEYWORDS
    assert _KEYWORDS["models"] >= 70, (
        "models weight too low — must score above generic anchors "
        "like 'apartment' (60)"
    )


def test_residences_family_present() -> None:
    """High-end / lifestyle apartment brands use 'Residences' /
    'Our Residences' / 'View Residences' instead of 'Apartments'."""
    for kw in ("residences", "our residences", "view residences"):
        assert kw in _KEYWORDS, f"missing residences-family anchor: {kw!r}"


def test_rates_family_present() -> None:
    """Common-area-focused PMC sites use Rates / Rates & Availability
    where conventional sites use Pricing / Floor Plans."""
    assert "rates" in _KEYWORDS
    assert "rates & availability" in _KEYWORDS
    # Compound phrase must out-score the bare 'rates' single token
    assert _KEYWORDS["rates & availability"] > _KEYWORDS["rates"]


def test_homes_family_present() -> None:
    """Single-family / townhome PMC sites label floor plans as 'Homes'
    rather than 'Apartments' — 'Our Homes' / 'See Homes' / 'Explore
    Homes' are the common dispatch labels."""
    for kw in ("homes", "our homes", "see homes", "explore homes"):
        assert kw in _KEYWORDS, f"missing homes-family anchor: {kw!r}"


def test_vacancies_anchor_present() -> None:
    """RentManager / rental-management CMS sites use 'Vacancies' as
    the standard label for the current-availability list."""
    assert "vacancies" in _KEYWORDS
    assert "see vacancies" in _KEYWORDS


def test_now_leasing_and_shop_anchors_present() -> None:
    """Pre-lease-up properties advertise 'Now Leasing' as the primary
    CTA; some newer-CMS sites use 'Shop' / 'Shop Available'."""
    assert "now leasing" in _KEYWORDS
    assert "shop available" in _KEYWORDS


def test_compound_phrases_outscore_bare_tokens() -> None:
    """Compound phrases like 'rates & availability' or 'see all units'
    are stronger signals than the bare token. Verify the relative
    ordering so a future weight tuning doesn't invert the ranking."""
    # 'rates & availability' (90) > 'rates' (70)
    assert _KEYWORDS["rates & availability"] > _KEYWORDS["rates"]
    # 'pricing & availability' (90) > 'pricing' (80)
    assert _KEYWORDS["pricing & availability"] > _KEYWORDS["pricing"]
    # 'see all units' (88) > 'unit' (55)
    assert _KEYWORDS["see all units"] > _KEYWORDS["unit"]


def test_no_duplicate_keywords() -> None:
    """A regression we don't want: two entries for the same keyword
    with different weights creates non-deterministic scoring."""
    seen: dict[str, int] = {}
    duplicates: list[tuple[str, int, int]] = []
    for kw, weight in DEFAULT_ANCHOR_KEYWORDS:
        if kw in seen and seen[kw] != weight:
            duplicates.append((kw, seen[kw], weight))
        seen[kw] = weight
    assert not duplicates, f"duplicate anchor keywords with diff weights: {duplicates}"


def test_anchor_keywords_all_lowercase() -> None:
    """The matcher lowercases the anchor text before lookup. Any
    upper-case entry here is dead — guard against future typos."""
    for kw, _ in DEFAULT_ANCHOR_KEYWORDS:
        assert kw == kw.lower(), f"non-lowercase anchor keyword: {kw!r}"
