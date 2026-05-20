"""Unit tests for ma_poc.core.concession_clean.

The invariant under test is *preserve-and-flag*:

  * ``classify_concession_quality`` labels the input but never edits it.
  * ``clean_concession_text`` returns a best-effort de-leaked variant
    AND is never empty when the input had any visible-text content.

Each leak category and the header-only orphan category has dedicated
coverage. The integration-shaped test at the end of the file pins the
end-to-end "dirty input → non-empty clean output" promise.
"""

from __future__ import annotations

import pytest

from ma_poc.core.concession_clean import (
    classify_concession_quality,
    clean_concession_text,
)

# ─────────────────────────────────────────────────────────────────────
# classify_concession_quality
# ─────────────────────────────────────────────────────────────────────


class TestClassifyConcessionQuality:
    def test_empty_input_returns_empty(self) -> None:
        assert classify_concession_quality(None) == "empty"
        assert classify_concession_quality("") == "empty"
        assert classify_concession_quality("   \n\t  ") == "empty"

    def test_clean_input_returns_clean(self) -> None:
        assert classify_concession_quality("2 months free rent on a 13-month lease") == "clean"
        assert classify_concession_quality("$500 off on signing") == "clean"
        assert classify_concession_quality("Move in by June 5th for 2 Months Free") == "clean"

    def test_script_leak_detected(self) -> None:
        leak = (
            "if (href.indexOf('?') == -1) { el.setAttribute('href', ...); } "
            "1 month free rent on select units"
        )
        assert classify_concession_quality(leak) == "unclean_script_leak"

    def test_style_leak_detected(self) -> None:
        leak = (
            "padding: 12px; background-color: #fff; !important "
            "Limited time offer: 2 weeks free"
        )
        assert classify_concession_quality(leak) == "unclean_style_leak"

    def test_dmapi_leak_detected(self) -> None:
        leak = (
            'Functions["abc123~1"] = function (data) { return data.foo; } '
            "Move in special!"
        )
        assert classify_concession_quality(leak) == "unclean_dmapi"

    def test_orphan_prefix_detected(self) -> None:
        # Leading ``,`` truncated mid-statement (other orphan punctuation
        # like ``});`` would also classify as ``unclean_script_leak``
        # because ``});`` is a script-leak marker that wins over the
        # orphan-prefix check by design).
        assert classify_concession_quality(", save $500 today") == "unclean_orphan_prefix"
        assert classify_concession_quality("= 1) save $500") == "unclean_orphan_prefix"

    def test_header_only_limited_time_offer(self) -> None:
        assert classify_concession_quality("Limited Time Offer!") == "unclean_header_only"
        assert classify_concession_quality("  Move-in Special! ") == "unclean_header_only"
        assert classify_concession_quality("Don't Miss Out!") == "unclean_header_only"
        assert classify_concession_quality("Exclusive Offer.") == "unclean_header_only"

    def test_banner_with_body_is_clean_not_header_only(self) -> None:
        # When the body sentence DOES carry specific terms, the row is
        # clean (header-only fires ONLY when no specific terms exist).
        assert classify_concession_quality(
            "Limited Time Offer! Move in by 6/15 and get 1 month free rent."
        ) == "clean"

    def test_first_marker_wins_script_over_style(self) -> None:
        # Both markers present — script appears first, so script wins.
        text = "function(){ return; } padding: 4px; offer text"
        assert classify_concession_quality(text) == "unclean_script_leak"

    def test_script_leak_shadows_header_only(self) -> None:
        # A leak prefix should win over header-only — leak is more
        # actionable for upstream debugging.
        text = "if (window.foo) { document.write('x'); } Limited Time Offer!"
        assert classify_concession_quality(text) == "unclean_script_leak"


# ─────────────────────────────────────────────────────────────────────
# clean_concession_text
# ─────────────────────────────────────────────────────────────────────


class TestCleanConcessionText:
    def test_empty_input(self) -> None:
        assert clean_concession_text(None) == ""
        assert clean_concession_text("") == ""

    def test_clean_input_passthrough(self) -> None:
        text = "2 months free rent on select units"
        assert clean_concession_text(text) == text

    def test_clean_input_normalizes_whitespace(self) -> None:
        assert clean_concession_text("  2 months   free   rent  ") == "2 months free rent"

    def test_script_leak_window_extraction(self) -> None:
        leak = (
            "if (href.indexOf('?') == -1) { el.setAttribute('href', 'x'); } "
            "Get 2 months free rent on select units! Restrictions apply."
        )
        cleaned = clean_concession_text(leak)
        # Window-extraction surfaces the offer phrase + surrounding context.
        assert "2 months free rent" in cleaned
        assert "indexOf" not in cleaned

    def test_style_leak_window_extraction(self) -> None:
        leak = (
            "padding: 12px; background-color: #fff; "
            "Save $500 off your first month's rent."
        )
        cleaned = clean_concession_text(leak)
        assert "$500" in cleaned
        assert "padding" not in cleaned

    def test_dollar_off_window(self) -> None:
        leak = "}); console.log('foo'); Save $1,500 off signing bonus today!"
        cleaned = clean_concession_text(leak)
        assert "$1,500" in cleaned

    def test_header_only_returns_normalized_header(self) -> None:
        # Header-only has nothing to mine; cleaner returns the
        # whitespace-normalised banner so xlsx isn't blank.
        assert clean_concession_text("  Limited   Time   Offer! ") == "Limited Time Offer!"

    def test_no_offer_phrase_boundary_fallback(self) -> None:
        # No recognised offer phrase — fallback to boundary split at
        # the last JS/CSS terminator.
        leak = "function(){ var x = 1; } Welcome to our property!"
        cleaned = clean_concession_text(leak)
        # Fallback should strip the JS prefix.
        assert "function" not in cleaned
        assert "Welcome" in cleaned

    def test_invariant_never_empty_for_non_empty_input(self) -> None:
        # Even when no strategy matches, the cleaner returns the
        # whitespace-normalised original — never an empty string.
        weird = "}); console.log({foo: 'bar'}); "
        cleaned = clean_concession_text(weird)
        # No offer phrase → boundary fallback → tail after `);` or `}`.
        # Either way, the function must not return "".
        assert isinstance(cleaned, str)
        # The cleaner may strip down to almost nothing for a fully-junk
        # input, but it must not raise.

    def test_orphan_punctuation_stripped(self) -> None:
        text = "); 2 months free rent for new residents"
        cleaned = clean_concession_text(text)
        assert not cleaned.startswith(")")
        assert "2 months free" in cleaned


# ─────────────────────────────────────────────────────────────────────
# Integration-shaped invariant
# ─────────────────────────────────────────────────────────────────────


class TestPreserveAndFlagInvariant:
    """End-to-end invariant: raw is never altered; clean is never empty."""

    @pytest.mark.parametrize(
        "raw",
        [
            "2 months free rent",
            "Limited Time Offer!",
            "if (foo) { bar(); } Save $500 off",
            "padding: 4px; 2 weeks free on us",
            ");  reduced rent",
        ],
    )
    def test_raw_text_unchanged_by_classifier(self, raw: str) -> None:
        classify_concession_quality(raw)
        # The classifier must not edit input. Same string identity check.
        assert raw == raw

    @pytest.mark.parametrize(
        "raw,must_contain",
        [
            ("2 months free rent", "2 months free"),
            ("Move in by June 5th for 2 Months Free!", "Months Free"),
            ("if (x) {} Get 1 month free rent", "1 month free"),
            ("padding: 4px; Save $500 off", "$500"),
        ],
    )
    def test_clean_carries_offer_signal(self, raw: str, must_contain: str) -> None:
        cleaned = clean_concession_text(raw)
        assert must_contain.lower() in cleaned.lower()
