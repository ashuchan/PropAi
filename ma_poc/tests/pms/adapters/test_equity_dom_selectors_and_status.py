"""D23 / D23b (2026-05-19) — Equity Apartments DOM selector additions +
text-derived ``availability_status`` replacing the hard-coded ``"AVAILABLE"``.

PID 55910 (equityapartments.com 1111-belle-pre) canonical: dom_scan was
matching 49 generic ``.apartment``/``.listing`` marketing cards (no rent,
no per-unit identity) and labelling every one ``AVAILABLE`` regardless of
the card text. The 29 real per-unit ``.unit-availability-tile`` cards (under
the ``#unit-availability-tile`` anchor) were never reached because nothing
in ``_DOM_CONTAINER_SELECTORS`` matched them.

Two structural changes covered here:
  • New selectors: ``.unit-availability-tile``, ``[data-availability]``,
    ``div[class*='availability-tile']``, ``div[class*='unit-tile']`` — added
    before ``.unit-card``/``.apartment``/etc. so they win on Equity pages.
  • ``_container_yields_unit`` now infers ``availability_status`` from the
    card text instead of hard-coding ``AVAILABLE``. Three-state output via
    ``_detect_availability_status`` (UNAVAILABLE / AVAILABLE / UNKNOWN).
"""
from __future__ import annotations

# ── D23: selector cascade ─────────────────────────────────────────────────

class TestEquitySelectorAdditions:
    """The four new selectors must be present in ``_DOM_CONTAINER_SELECTORS``
    AND must be ordered BEFORE the generic ``.apartment``/``.listing``
    catch-alls so specific patterns win on Equity-style pages."""

    def test_unit_availability_tile_selector_present(self) -> None:
        from ma_poc.pms.adapters._html_extract import _DOM_CONTAINER_SELECTORS
        assert ".unit-availability-tile" in _DOM_CONTAINER_SELECTORS

    def test_data_availability_attribute_selector_present(self) -> None:
        from ma_poc.pms.adapters._html_extract import _DOM_CONTAINER_SELECTORS
        assert "[data-availability]" in _DOM_CONTAINER_SELECTORS

    def test_substring_class_selectors_present(self) -> None:
        from ma_poc.pms.adapters._html_extract import _DOM_CONTAINER_SELECTORS
        assert "div[class*='availability-tile']" in _DOM_CONTAINER_SELECTORS
        assert "div[class*='unit-tile']" in _DOM_CONTAINER_SELECTORS

    def test_equity_selectors_outrank_generic(self) -> None:
        """``.unit-availability-tile`` must appear BEFORE ``.apartment`` and
        ``.listing`` so specific selector wins the dom_scan cascade.
        Without this ordering, ``.apartment`` would match marketing cards
        first and short-circuit the more specific selector.
        """
        from ma_poc.pms.adapters._html_extract import _DOM_CONTAINER_SELECTORS
        tile_idx = _DOM_CONTAINER_SELECTORS.index(".unit-availability-tile")
        apartment_idx = _DOM_CONTAINER_SELECTORS.index(".apartment")
        listing_idx = _DOM_CONTAINER_SELECTORS.index(".listing")
        assert tile_idx < apartment_idx
        assert tile_idx < listing_idx


# ── D23b: availability-status detection ────────────────────────────────────

class TestDetectAvailabilityStatus:
    """``_detect_availability_status`` infers status from text.

    Three-state output:
      • UNAVAILABLE — explicit phrase (waitlist / leased / sold out / not
        available / coming soon / on request)
      • AVAILABLE  — explicit phrase OR (silent text AND rent present)
      • UNKNOWN    — silent text AND no rent
    """

    def test_unavailable_phrase_wins_even_with_rent(self) -> None:
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        text = "1 Bed / 1 Bath  $1,980/mo  Joined the waitlist"
        assert _detect_availability_status(text, has_rent=True) == "UNAVAILABLE"

    def test_explicit_available_phrase(self) -> None:
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status(
            "Available Now!  1 Bed / 1 Bath", has_rent=False
        ) == "AVAILABLE"
        assert _detect_availability_status(
            "Now Leasing  Studio  500 sqft", has_rent=False
        ) == "AVAILABLE"
        assert _detect_availability_status(
            "Move-in Ready  $1500/mo", has_rent=True
        ) == "AVAILABLE"

    def test_silent_text_with_rent_defaults_available(self) -> None:
        """Card text doesn't mention availability but rent is shown — assume
        AVAILABLE so RentCafe-style option-row data (which rarely uses the
        word "available") still flows into §8.20 promotion.
        """
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        text = "1 Bed | 1 Bath | 750 sqft | $1,500/mo"
        assert _detect_availability_status(text, has_rent=True) == "AVAILABLE"

    def test_silent_text_without_rent_returns_unknown(self) -> None:
        """Pre-fix this returned ``AVAILABLE``. Now UNKNOWN — these rows
        will reach ``classify()`` and route to ``plan_summaries`` unless
        per-unit identity is present elsewhere (the right behaviour for
        marketing aggregate cards).
        """
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        text = "Studio  500 sqft"  # no rent, no availability phrase
        assert _detect_availability_status(text, has_rent=False) == "UNKNOWN"

    def test_leased_keyword_returns_unavailable(self) -> None:
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status(
            "1 Bed / 1 Bath  Currently leased", has_rent=False
        ) == "UNAVAILABLE"

    def test_coming_soon_returns_unavailable(self) -> None:
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status(
            "1 Bed / 1 Bath  Coming Soon", has_rent=True
        ) == "UNAVAILABLE"

    def test_sold_out_with_hyphen(self) -> None:
        """Both `sold out` and `sold-out` should resolve to UNAVAILABLE."""
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status(
            "Studio  450 sqft  $1200/mo  Sold-Out", has_rent=True
        ) == "UNAVAILABLE"
        assert _detect_availability_status(
            "Studio  450 sqft  $1200/mo  Sold Out", has_rent=True
        ) == "UNAVAILABLE"

    def test_empty_text_returns_unknown_without_rent(self) -> None:
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status("", has_rent=False) == "UNKNOWN"

    def test_empty_text_with_rent_returns_available(self) -> None:
        """Defensive default — pure rent without context still implies
        availability for backward compat with sites whose card text is
        truncated to just a price."""
        from ma_poc.pms.adapters._html_extract import _detect_availability_status
        assert _detect_availability_status("", has_rent=True) == "AVAILABLE"


# ── D23b end-to-end: _container_yields_unit emits derived status ──────────

class TestContainerYieldsUnitDerivedStatus:
    """End-to-end check that ``_container_yields_unit`` propagates the
    inferred status onto the unit dict. Before the fix every match emitted
    ``"AVAILABLE"`` regardless of card text content.
    """

    def test_card_with_rent_and_no_phrase_is_available(self) -> None:
        from ma_poc.pms.adapters._html_extract import _container_yields_unit
        text = "1 Bed | 1 Bath | 750 sqft | $1,500/mo"
        unit = _container_yields_unit(text)
        assert unit is not None
        assert unit["availability_status"] == "AVAILABLE"
        assert unit["market_rent_low"] == 1500

    def test_card_with_rent_and_waitlist_phrase_is_unavailable(self) -> None:
        from ma_poc.pms.adapters._html_extract import _container_yields_unit
        text = "1 Bed | 1 Bath | 750 sqft | $1,500/mo | Join the waitlist"
        unit = _container_yields_unit(text)
        assert unit is not None
        assert unit["availability_status"] == "UNAVAILABLE"

    def test_card_with_no_rent_and_no_phrase_is_unknown(self) -> None:
        """Pre-fix this returned AVAILABLE; agent's PID 55910 forensic
        showed this is the false-attribution bug that masks regressions
        in the rent-extraction path.
        """
        from ma_poc.pms.adapters._html_extract import _container_yields_unit
        text = "1 Bed | 1 Bath | 750 sqft"  # no rent, no availability phrase
        unit = _container_yields_unit(text)
        assert unit is not None
        assert unit["market_rent_low"] is None
        assert unit["availability_status"] == "UNKNOWN"

    def test_returns_none_when_no_floor_plan_signals(self) -> None:
        """Backward-compat check: the gate at the top of the function still
        rejects text with insufficient floor-plan signal density."""
        from ma_poc.pms.adapters._html_extract import _container_yields_unit
        assert _container_yields_unit("just some marketing copy") is None
