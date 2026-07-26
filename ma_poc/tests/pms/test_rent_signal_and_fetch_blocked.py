"""Publish-ceiling guard integrity + fetch-blocked tier (EXTRACTION_MISS RCA 2026-07-25).

Two defects found while diagnosing 182 properties graded EXTRACTION_MISS
("rent is on the page and we missed it"):

1. ``_RENT_SIGNAL_RE`` required 3-4 digits immediately after "$", so it matched
   NONE of the comma-formatted amounts most US rents use ($1,950, $2,623,
   $1,110.00). That counter is the CARDINAL GUARD in reporting.publish_ceiling:
   zero rent tokens + empty cascade + operator signal is graded
   CONFIRMED_NO_DATA and is gold-eligible. So a page advertising "$1,950/month"
   on every plan could be certified as publishing nothing — false gold, in the
   direction that HIDES extraction bugs.

2. Entrata's ``_NO_RESPONSE`` tier conflated "page loaded, no XHR captured"
   with "page never loaded". 55 of 60 properties in that cohort were serving a
   Cloudflare challenge on static fetch — the roster was never on the wire —
   yet the tier name reads as an adapter bug and sent a whole diagnosis cycle
   hunting parser gaps that did not exist.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters.entrata import (
    _TIER_FETCH_BLOCKED,
    _TIER_NO_RESPONSE,
    _classify_entrata_failure,
    _looks_like_challenge_page,
)
from ma_poc.pms.scraper import _RENT_SIGNAL_RE, _count_rent_signals


@pytest.mark.parametrize(
    "amount",
    ["$1,950", "$1,295/mo", "$2,623", "$1,110.00", "$12,500", "$950", "$1950", "$830"],
)
def test_rent_regex_matches_real_world_formats(amount: str) -> None:
    """Comma-formatted rents MUST match — the old pattern missed every one."""
    assert _RENT_SIGNAL_RE.findall(amount), f"{amount} not recognised as a rent token"


@pytest.mark.parametrize("amount", ["$50", "$99", "$7"])
def test_rent_regex_ignores_sub_hundred_amounts(amount: str) -> None:
    """A >=3-digit magnitude keeps small fees and stray numbers out."""
    assert not _RENT_SIGNAL_RE.findall(amount)


@pytest.mark.parametrize(
    ("html", "expected", "label"),
    [
        ("<p>1 Bed from $1,950/month</p>", 1, "real asking rent"),
        ("<div>Get $500 OFF your first month!</div>", 0, "concession, word after"),
        ("<p>Save $500 today</p>", 0, "concession, word before"),
        ("<p>$220 IN WAIVED LEASING FEES</p>", 0, "waived fees"),
        ("<p>$350 deposit</p>", 0, "deposit"),
        ("<p>A1 $2,623</p><p>B2 $2,840</p><p>C3 $2,835</p>", 3, "three real rents"),
        ("Plain text $1,450 per month", 1, "no markup"),
        ("", 0, "empty"),
    ],
)
def test_concession_tokens_do_not_count_as_rent(html: str, expected: int, label: str) -> None:
    assert _count_rent_signals(html) == expected, label


def test_neighbouring_promo_does_not_poison_a_real_rent() -> None:
    """The window must not reach across an element boundary.

    "<p>Studio $1,295/mo</p><p>Save $500 today</p>" puts "Save" 16 characters
    from a genuine rent; a flat +/-60 char window discarded both.
    """
    assert _count_rent_signals("<p>Studio $1,295/mo</p><p>Save $500 today</p>") == 1


def test_challenge_page_detection() -> None:
    cf = (
        '<html><head><title>Just a moment...</title></head>'
        '<body><div id="cf-wrapper">cdn-cgi/challenge-platform</div></body></html>'
    )
    assert _looks_like_challenge_page(cf) is True
    # a LARGE page mentioning the phrase in copy is not a challenge
    big = "<html><body>" + ("<p>Just a moment while we show you around</p>" * 400) + "</body></html>"
    assert _looks_like_challenge_page(big) is False
    assert _looks_like_challenge_page(None) is False
    assert _looks_like_challenge_page("") is False


def test_fetch_blocked_is_distinct_from_no_response() -> None:
    """A blocked FETCH must not be reported as an extraction failure."""
    cf = '<html><title>Just a moment...</title><body>cdn-cgi/challenge-platform</body></html>'
    tier, msg = _classify_entrata_failure([], cf)
    assert tier == _TIER_FETCH_BLOCKED
    assert "interstitial" in msg.lower()

    # a real page with no Entrata XHR is still NO_RESPONSE
    tier2, _ = _classify_entrata_failure([], "<html><body>a real marketing page</body></html>")
    assert tier2 == _TIER_NO_RESPONSE

    # and with no html at all, behaviour is unchanged
    tier3, _ = _classify_entrata_failure([], None)
    assert tier3 == _TIER_NO_RESPONSE


@pytest.mark.parametrize(
    "amount,matches",
    [
        ("$760", True),
        ("$ 760", True),      # WPResidence "starting at: $ 760"
        ("$  760", True),     # two-space padding, live-observed
        ("$ 1,950", True),
        ("$   760", False),   # bounded at 2 — a lone "$" must not reach a distant number
    ],
)
def test_rent_regex_tolerates_padding_after_the_dollar_sign(amount: str, matches: bool) -> None:
    """princetonmanagement.com renders plan prices as "starting at: $ 760".

    The original ``\\$\\d`` form matched none of them, so a property whose only
    prices are plan-level scored ZERO rent tokens — which flips the
    publish-ceiling grade from EXTRACTION_MISS to a CONFIRMED (gold-eligible)
    ceiling. That is a false-gold, the one failure mode the cardinal guard
    exists to prevent. Live-probed 2026-07-25.
    """
    assert bool(_RENT_SIGNAL_RE.search(amount)) is matches


def test_padded_rent_still_respects_the_fee_guard() -> None:
    """Widening the padding must not let fee amounts count as rent."""
    assert _count_rent_signals("<p>starting at: $  760</p><p>$50 Admin Fee</p>") == 1
