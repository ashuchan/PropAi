"""Tests for ma_poc.core.concession_clean — quality classifier + cleaner.

Designed against the real-world dirty samples from feature canary
2026-05-19 (49,677 non-blank concessions, 49.9% had JS/CSS leak). Fixture
patterns are minimized verbatim slices of actual production bytes.
"""

from __future__ import annotations

from ma_poc.core.concession_clean import (
    classify_concession_quality,
    clean_concession_text,
)

# ─────────────────────────────────────────────────────────────────────
# classify_concession_quality
# ─────────────────────────────────────────────────────────────────────


def test_classify_clean_offer_text() -> None:
    """Normal concession text returns 'clean'."""
    assert classify_concession_quality(
        "Move in by May 30th and receive 6 weeks free on any new lease."
    ) == "clean"
    assert classify_concession_quality(
        "Save Up to $350/Month on Select Apartments 1BR: $200 Off"
    ) == "clean"
    assert classify_concession_quality(
        "Limited Time Move-In Special Available! 1 month free."
    ) == "clean"


def test_classify_empty_or_none() -> None:
    """None / empty / whitespace-only returns 'empty'."""
    assert classify_concession_quality(None) == "empty"
    assert classify_concession_quality("") == "empty"
    assert classify_concession_quality("   \t\n  ") == "empty"


def test_classify_script_leak_woodland_creek() -> None:
    """The exact Woodland Creek pattern — PropLeadSource JS prefix +
    'Limited Time Offer!' suffix. Real bytes from feature canary
    2026-05-19, cid 74567 (all 46 rows had the identical dirty value)."""
    text = (
        "-1) { if (href.indexOf(\"?\") == -1) { href = href + \"?\"; } "
        "else { href = href + \"&\"; } href = href + 'PropLeadSource_' + "
        "2335253 + \"=\" + propleadsource; el.setAttribute('href', href); "
        "} } } }); } }); Limited Time Offer!"
    )
    assert classify_concession_quality(text) == "unclean_script_leak"


def test_classify_style_leak() -> None:
    """CSS @media rules ahead of the real offer."""
    text = (
        "@media (max-width: 767px){#nudgeTop .nudgetop-item{padding:0.8rem "
        "!important}#nudgeTop{z-index:9 !important}} 6 weeks FREE on select "
        "apartment homes!*"
    )
    assert classify_concession_quality(text) == "unclean_style_leak"


def test_classify_dmapi_leak() -> None:
    """Duda CMS function definition leak."""
    text = (
        "Functions[\"750a6abf74f04b329d41b87b7516ad28~82\"] = function "
        "(element, data, api) { const SPECIAL_OFFER_COLLECTION_NAME = "
        "'Special Offers Collection'; }"
    )
    assert classify_concession_quality(text) == "unclean_dmapi"


def test_classify_orphan_prefix() -> None:
    """Truncated mid-function with orphan punctuation, no script/style
    markers — falls through to ``unclean_orphan_prefix``.

    NOTE: ``});`` is in ``_SCRIPT_LEAK_MARKERS``, so a value starting
    with ``});`` is classified as ``unclean_script_leak`` (more
    specific). The orphan-prefix category catches the cases where the
    truncation produced only punctuation (no recognized code tokens)."""
    assert classify_concession_quality(")] Move-in special available") == "unclean_orphan_prefix"
    # ; alone — not a recognized code token, but visibly orphan
    assert classify_concession_quality(";] new lease offer") == "unclean_orphan_prefix"


def test_classify_orphan_with_script_markers_prefers_script_leak() -> None:
    """When orphan punctuation co-occurs with a script-leak marker
    (``});``), the more-specific ``unclean_script_leak`` wins."""
    assert classify_concession_quality("}); }); Limited Time Offer") == "unclean_script_leak"


def test_classify_script_beats_style_on_first_appearance() -> None:
    """When both leak types are present, return the one that appears first."""
    # script first
    text_js_first = "function() { ... } @media (max-width: 767px) { ... } 1 month free"
    assert classify_concession_quality(text_js_first) == "unclean_script_leak"
    # style first
    text_css_first = "@media (max-width: 767px) { padding: 8px } function() {} 1 month free"
    assert classify_concession_quality(text_css_first) == "unclean_style_leak"


# ─────────────────────────────────────────────────────────────────────
# clean_concession_text
# ─────────────────────────────────────────────────────────────────────


def test_clean_returns_clean_text_unchanged() -> None:
    """Already-clean input passes through (with whitespace trim)."""
    text = "Move in by May 30th and receive 6 weeks free on any new lease."
    assert clean_concession_text(text) == text


def test_clean_empty_returns_empty_string() -> None:
    """None / empty input returns empty string (not None)."""
    assert clean_concession_text(None) == ""
    assert clean_concession_text("") == ""
    assert clean_concession_text("   ") == ""


def test_clean_extracts_offer_from_woodland_creek_leak() -> None:
    """The real Woodland Creek dirty value yields 'Limited Time Offer!'
    after cleaning — the actual offer signal preserved."""
    text = (
        "-1) { if (href.indexOf(\"?\") == -1) { href = href + \"?\"; } "
        "else { href = href + \"&\"; } href = href + 'PropLeadSource_' + "
        "2335253 + \"=\" + propleadsource; el.setAttribute('href', href); "
        "} } } }); } }); Limited Time Offer!"
    )
    cleaned = clean_concession_text(text)
    assert "Limited Time Offer" in cleaned, (
        f"expected 'Limited Time Offer' in cleaned output; got {cleaned!r}"
    )
    # Should NOT contain the JS leak any more
    assert "PropLeadSource" not in cleaned
    assert "href.indexOf" not in cleaned
    assert "setAttribute" not in cleaned


def test_clean_extracts_weeks_free_from_css_leak() -> None:
    """CSS @media prefix + '6 weeks FREE' offer — cleaner extracts the offer."""
    text = (
        "nt}@media (max-width: 767px){#nudgeTopExtra .nudgetop-item"
        "{padding:0.8rem !important} 6 weeks FREE on select apartment homes!"
    )
    cleaned = clean_concession_text(text)
    assert "6 weeks FREE" in cleaned or "6 weeks free" in cleaned.lower()
    assert "@media" not in cleaned
    assert "!important" not in cleaned


def test_clean_extracts_dollar_off() -> None:
    """$X off pattern extraction."""
    text = (
        "function() { console.log('init'); }(); $250 OFF MOVE-IN + REDUCED "
        "DEPOSIT — receive $250 off your move-in"
    )
    cleaned = clean_concession_text(text)
    assert "$250" in cleaned or "250" in cleaned
    assert "console.log" not in cleaned
    assert "function()" not in cleaned


def test_clean_extracts_month_free() -> None:
    """1 month free phrase recovery."""
    text = (
        "import MEChat from 'https://cdn.eliseai.com/@meetelise/chat'; "
        "MEChat.start({ organization: \"...\" }); 1 MONTH FREE!"
    )
    cleaned = clean_concession_text(text)
    assert "MONTH FREE" in cleaned.upper()


def test_clean_no_recognizable_offer_returns_normalized_input() -> None:
    """When no offer phrase matches, the cleaner returns whitespace-
    normalized input — the caller still has the raw via concession_text,
    and the quality flag tells reporting to display with caution."""
    text = "function() {} Some unrecognized marketing copy here"
    cleaned = clean_concession_text(text)
    # Cleaner couldn't find an offer phrase but returns something usable.
    assert cleaned  # non-empty
    assert "  " not in cleaned  # whitespace normalized


def test_clean_strips_leading_orphan_punctuation() -> None:
    """Leading orphan punctuation (truncated mid-function) is removed
    from the cleaned output. Either route — offer-phrase extraction
    (which trims the leading orphan automatically) or the boundary
    split — yields a leading-letter result."""
    text = "}); }); 6 weeks free rent!"
    cleaned = clean_concession_text(text)
    assert not cleaned.startswith(")")
    assert not cleaned.startswith("}")
    assert "weeks free" in cleaned.lower()


# ─────────────────────────────────────────────────────────────────────
# Integration: classify + clean preserve the user invariant —
# "error on side of unclean rather than discard"
# ─────────────────────────────────────────────────────────────────────


def test_dirty_input_always_yields_non_empty_clean_output() -> None:
    """Any non-empty input yields a non-empty cleaned output. We never
    silently drop a row — the user's "preserve, don't discard" rule."""
    samples = [
        "-1) { href = href + \"?\"; } Limited Time Offer!",
        "@media (max-width: 767px) { display: none; } 6 weeks free",
        "Functions[\"abc~1\"] = function (a, b, c) { return; } 1 month free",
        "}); }); }); Move-in special",
        "function() { var x = 1; } Random copy with no offer phrase here",
    ]
    for s in samples:
        c = clean_concession_text(s)
        assert c, f"clean output must be non-empty for input {s!r}; got {c!r}"


def test_quality_flag_distinguishes_clean_from_dirty() -> None:
    """The quality flag values are stable and disjoint.

    Note: ``});`` is itself a script-leak marker (more specific than
    ``unclean_orphan_prefix``), so a value starting with ``});`` is
    classified as ``unclean_script_leak``. Orphan-prefix catches the
    cases where the leading punctuation is the ONLY signal of
    truncation (no other code tokens)."""
    pairs = [
        ("1 month free", "clean"),
        ("function() {} 1 month free", "unclean_script_leak"),
        # Real-world CSS form: ``@media (...)`` (with space)
        ("@media (max-width: 767px) { padding: 8px } 1 month free", "unclean_style_leak"),
        ('Functions["abc~1"] = function (a) {}', "unclean_dmapi"),
        ("});  1 month free", "unclean_script_leak"),  # `});` is a script marker
        (";] 1 month free", "unclean_orphan_prefix"),  # bare orphan, no markers
        (None, "empty"),
        ("", "empty"),
    ]
    for text, expected in pairs:
        actual = classify_concession_quality(text)
        assert actual == expected, (
            f"classify({text!r}) = {actual!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# 2026-07-12 cookie-consent chrome (unclean_cookie_chrome).
#
# The rendered-DOM banner walker's [class*="banner"]/[class*="notice"]-style
# selectors also match OneTrust/CookieYes consent bars; adjacent renders
# concatenate consent UI with the marketing banner. Pre-fix these classified
# "clean" (no code-leak markers) so clean_concession_text returned the
# polluted text unchanged. Fixtures below are live captures (2026-07-11
# canary + 2026-07-12 repro).
# ---------------------------------------------------------------------------

_LEESQUARE = (
    "Privacy Policy Accept Deny Non-Essential Close Cookie Preferences X "
    "2 Months Free Base Rent on 2-Bedroom Homes. Live Grand. Save Big."
)
_BANYAN = (
    "ACCEPT DECLINE Up to 10 weeks free on select luxury apartment "
    "homes!* *Restrictions apply"
)


def test_cookie_chrome_classified():
    assert classify_concession_quality(_LEESQUARE) == "unclean_cookie_chrome"
    assert classify_concession_quality(_BANYAN) == "unclean_cookie_chrome"


def test_cookie_chrome_leading_run_stripped():
    assert clean_concession_text(_LEESQUARE) == (
        "2 Months Free Base Rent on 2-Bedroom Homes. Live Grand. Save Big."
    )
    assert clean_concession_text(_BANYAN) == (
        "Up to 10 weeks free on select luxury apartment homes!* "
        "*Restrictions apply"
    )


def test_pure_cookie_text_cleans_to_empty():
    t = (
        "We use cookies to improve your experience. Accept All Cookies "
        "Reject All Cookie Settings"
    )
    assert classify_concession_quality(t) == "unclean_cookie_chrome"
    assert clean_concession_text(t) == ""


def test_offer_before_chrome_head_preserved():
    t = (
        "x Apply today for ONLY $99.00 + FREE RENT! X How we use cookies "
        "We use cookies and similar technologies to analyze traffic"
    )
    out = clean_concession_text(t)
    assert "$99.00" in out and "FREE RENT" in out
    assert "cookies" not in out.lower()


def test_trailing_chrome_truncated_from_offer_window():
    t = "2 Months Free Base Rent! Restrictions apply. Cookie Preferences"
    out = clean_concession_text(t)
    assert out == "2 Months Free Base Rent! Restrictions apply."


def test_bare_accept_in_marketing_copy_untouched():
    # "accept" alone is NOT a strong consent anchor — legit copy survives.
    t = "Accept our gift: 1 month free on select homes!"
    assert classify_concession_quality(t) == "clean"
    assert clean_concession_text(t) == t


def test_script_leak_still_wins_over_cookie_chrome():
    t = "function() {} Cookie Preferences 1 month free"
    assert classify_concession_quality(t) == "unclean_script_leak"
