"""Tests for Step 3c — rendered-DOM concession rescan.

The 100-prop VISION audit (2026-05-24) found 9 false negatives where
the concession banner is JS-injected and absent from static HTML. The
fix scans the rendered DOM's popup/modal/banner element set via
``page.evaluate(RENDERED_DOM_PROBE_JS)`` and re-runs the concession
regex on each visible block.

These tests cover:

  1. JS-snippet shape (parameters returned, defensive try/catch).
  2. ``_looks_like_cookie_banner`` junk-filter heuristic.
  3. ``extract_concession_window`` sentence-window logic mirrors
     scraper.py Step 3 (300-char cap, forward-walk).
  4. ``find_concession_in_blocks`` synchronous helper with the real
     9 FN-case texts (Cortland, Blossoms, Austin Midtown, Colina
     Ranch Hill, Prose Riviana, Quarry Alamo Heights, Museum Terrace,
     Jefferson Place, 42 West).
  5. ``scan_rendered_dom_for_concession`` async wrapper with a mock
     Playwright page; covers happy path, error-from-evaluate, blank
     page, malformed JS response.
  6. Integration with the live ``_PROPERTY_CONCESSION_RE``.
"""

from __future__ import annotations

import pytest
from ma_poc.core.rendered_dom_concession import (
    RENDERED_DOM_PROBE_JS,
    _looks_like_cookie_banner,
    _strip_leading_nav_junk,
    extract_concession_window,
    find_concession_in_blocks,
    scan_rendered_dom_for_concession,
)
from ma_poc.pms.scraper import _PROPERTY_CONCESSION_RE

# ─────────────────────────────────────────────────────────────────────
# 1) JS snippet shape
# ─────────────────────────────────────────────────────────────────────


def test_probe_js_returns_blocks_and_body_text_fields() -> None:
    """The JS snippet must return ``{blocks, body_text}`` on success and
    ``{error: ...}`` on failure. Pin both shapes so the Python caller's
    defensive parsing keeps working."""
    assert "blocks:" in RENDERED_DOM_PROBE_JS
    assert "body_text:" in RENDERED_DOM_PROBE_JS
    assert "error:" in RENDERED_DOM_PROBE_JS
    # The error path uses try/catch so a thrown exception in the page
    # context never escapes as an unhandled JS error to the caller.
    assert "try {" in RENDERED_DOM_PROBE_JS
    assert "} catch (e)" in RENDERED_DOM_PROBE_JS


def test_probe_js_queries_dialog_modal_popup_banner_special_promo() -> None:
    """The selector set must cover the popup/modal/banner element classes
    we observed in the 9 false-negative cases. Smoke-pin each one so a
    future shrinking of the selector set fails loudly."""
    for needle in (
        '[role="dialog"]',
        '[class*="popup"]',
        '[class*="modal"]',
        '[class*="banner"]',
        '[class*="announcement"]',
        '[class*="special"]',
        '[class*="promo"]',
        '[class*="offer"]',
        '[class*="notice"]',
    ):
        assert needle in RENDERED_DOM_PROBE_JS, (
            f"selector {needle!r} missing from RENDERED_DOM_PROBE_JS"
        )


# ─────────────────────────────────────────────────────────────────────
# 2) Cookie-banner junk filter
# ─────────────────────────────────────────────────────────────────────


def test_cookie_banner_filter_recognizes_typical_gdpr_copy() -> None:
    txt = (
        'By clicking "Accept All Cookies", you agree to the storing of '
        "cookies on your device to enhance site navigation, analyze "
        "site usage, and assist in our marketing efforts. "
        "Reject All Cookies | Cookie Preferences"
    )
    assert _looks_like_cookie_banner(txt) is True


def test_cookie_banner_filter_does_not_match_real_concession_with_one_cookie_word() -> None:
    """A concession banner that mentions 'cookies' once in fine print
    must NOT be filtered as a consent banner — the regex requires ≥2
    consent-y phrases."""
    txt = (
        "Limited Time Special! Up to 2 Months Free on select apartment homes. "
        "Restrictions apply. See site cookies policy."
    )
    assert _looks_like_cookie_banner(txt) is False


def test_cookie_banner_filter_handles_empty_and_none_safely() -> None:
    assert _looks_like_cookie_banner("") is False


# ─────────────────────────────────────────────────────────────────────
# 3) Sentence-window extractor
# ─────────────────────────────────────────────────────────────────────


def test_extract_concession_window_caps_at_300_chars() -> None:
    txt = "Lease today and enjoy up to $1000 off your first month! " * 20
    m = _PROPERTY_CONCESSION_RE.search(txt)
    assert m is not None
    out = extract_concession_window(txt, m)
    assert len(out) <= 300


def test_extract_concession_window_keeps_offer_phrase() -> None:
    """The matched offer phrase must be inside the returned window."""
    txt = (
        "Welcome to the Quarry. Spring Special! Up to 6 Weeks Free Base Rent! "
        "Park like a VIP. Tour today."
    )
    m = _PROPERTY_CONCESSION_RE.search(txt)
    assert m is not None
    out = extract_concession_window(txt, m)
    assert "6 Weeks Free Base Rent" in out


def test_extract_concession_window_walks_forward_past_header_only_sentence() -> None:
    """Mirror scraper.py's forward-walk: when the matched sentence is a
    bare header ('Limited Time Offer!') and the actionable body is in
    the next sentence, the window must include both — up to 2 forward
    sentences while ≤300 chars."""
    txt = (
        "Welcome home. Limited Time Offer! "
        "Move in by 6/15 and get 1 month free rent. Restrictions apply."
    )
    m = _PROPERTY_CONCESSION_RE.search(txt)
    assert m is not None
    out = extract_concession_window(txt, m)
    assert "Limited Time Offer" in out
    # Forward walk picks up the body sentence too.
    assert "1 month free rent" in out


# ─────────────────────────────────────────────────────────────────────
# 4) Real false-negative texts from the 100-prop audit
# ─────────────────────────────────────────────────────────────────────


# These strings are the *visible* DOM text Playwright captured on
# 2026-05-24 for each FN property. They're trimmed from the
# CONCESSION_100PROP_VISION_VERIFICATION.md report.

FN_CASES = [
    (
        "Cortland Brier Creek",
        "Skip to main content Move in and save! Receive up to 2 months free "
        "when you move into select apartment homes!* Terms and conditions apply.",
        "2 months free",
    ),
    (
        "Blossoms at Brentwood",
        "Skip to main content Now offering up to six weeks free on select "
        "homes! Minimum lease term applies. Other costs and fees excluded.",
        "six weeks free",
    ),
    (
        "Austin Midtown",
        "We're Here For You. Specials. LEASE TODAY & EARN UP TO 4 WEEKS* "
        "FREE! Terms and conditions may apply.",
        "4 WEEKS",
    ),
    (
        "Colina Ranch Hill",
        "Skip to main content Limited-Time Special Offers Available Now! "
        "Up to 2 Months Free on select apartment homes! Reduced Rates on "
        "Select Homes! Complimentary Storage Unit Look & Lease to receive "
        "50% off application and admin fees.",
        "2 Months Free",
    ),
    (
        "Prose Riviana",
        "Skip to main content Live Up to 8-Weeks Free Base Rent + Up to "
        "$1,500 Gift Card!* Live in Full Bloom. Stylish Katy living with "
        "newly reduced rates! Minimum Term Required.",
        "8-Weeks Free Base Rent",
    ),
    (
        "Quarry Alamo Heights",
        "All Rights Reserved. Spring Special! Up to 6 Weeks Free Base Rent! "
        "Park like a VIP with your own direct-access garage! BONUS: Waived "
        "App & Admin Fees!",
        "6 Weeks Free Base Rent",
    ),
    (
        "Museum Terrace",
        "CONTACT US MAP AND DIRECTIONS SPECIALS Current Specials Up to "
        "$1,500 Off Base Rent Look & Lease Special! Base Rent. Minimum "
        "lease term applies.",
        "$1,500 Off",
    ),
    (
        "Jefferson Place",
        "Home You've Imagined Is Within Reach Lease today and enjoy up to "
        "$1000 Off Restrictions apply. Subject to change at anytime.",
        "$1000 Off",
    ),
    (
        "42 West Apartments",
        "LOGIN Secure Your Home Today & Enjoy a $300 One-Time Rent "
        "Concession at Move-In Limited time only: Restrictions apply.",
        "$300",
    ),
]


@pytest.mark.parametrize("name, block_text, expected_anchor", FN_CASES)
def test_find_concession_in_blocks_catches_real_fn_cases(
    name: str, block_text: str, expected_anchor: str
) -> None:
    """All 9 confirmed false negatives from the 100-prop vision audit
    must now match _PROPERTY_CONCESSION_RE when surfaced via Step 3c."""
    out = find_concession_in_blocks(
        blocks=[block_text],
        body_text="",
        concession_re=_PROPERTY_CONCESSION_RE,
    )
    assert out is not None, (
        f"{name}: rendered-DOM text not matched by concession regex. "
        f"Block was: {block_text!r}"
    )
    assert expected_anchor.lower() in out.lower(), (
        f"{name}: returned window {out!r} doesn't contain anchor "
        f"{expected_anchor!r}"
    )


def test_find_concession_in_blocks_skips_cookie_consent_blocks_first() -> None:
    """When blocks contain a cookie consent (which contains 'offer' in
    its tracking-preferences copy) followed by a real banner, the real
    banner must win — not the cookie block. Sites in practice have BOTH
    a cookie consent AND a popup banner on first load."""
    cookie = (
        'By clicking "Accept All Cookies", you agree to the storing of '
        "cookies on your device. We may share data with marketing offer "
        "partners. Reject All Cookies | Manage Preferences."
    )
    real = "Spring Special! Up to 6 Weeks Free Base Rent!"
    out = find_concession_in_blocks(
        blocks=[cookie, real],
        body_text="",
        concession_re=_PROPERTY_CONCESSION_RE,
    )
    assert out is not None
    assert "6 Weeks Free Base Rent" in out
    assert "cookies" not in out.lower()


def test_find_concession_in_blocks_falls_back_to_body_text() -> None:
    """If no popup-classed block matches (the banner uses a non-standard
    class like ``apartments__notice``), the body_text fallback must
    rescue the match."""
    body = (
        "Welcome to Cortland Brier Creek. Receive up to 2 months free "
        "when you move into select apartment homes. Tour today."
    )
    out = find_concession_in_blocks(
        blocks=[],
        body_text=body,
        concession_re=_PROPERTY_CONCESSION_RE,
    )
    assert out is not None
    assert "2 months free" in out


def test_find_concession_in_blocks_returns_none_when_no_match_anywhere() -> None:
    out = find_concession_in_blocks(
        blocks=["Welcome to our community. Studios, 1, & 2-Bed homes."],
        body_text="Apartments in downtown. Schedule a tour.",
        concession_re=_PROPERTY_CONCESSION_RE,
    )
    assert out is None


def test_find_concession_in_blocks_ignores_non_string_block() -> None:
    """Defensive: a malformed block (None, dict, int) must not crash."""
    out = find_concession_in_blocks(
        blocks=[None, 42, {"foo": "bar"}, "Up to 2 Months Free!"],  # type: ignore[list-item]
        body_text="",
        concession_re=_PROPERTY_CONCESSION_RE,
    )
    assert out is not None
    assert "2 Months Free" in out


# ─────────────────────────────────────────────────────────────────────
# 5) Async scan wrapper with a mock Playwright page
# ─────────────────────────────────────────────────────────────────────


class _MockPage:
    """Minimal stand-in for a Playwright Page — implements only
    ``await page.evaluate(js)`` returning a pre-set value."""

    def __init__(self, ret: object = None, raise_exc: Exception | None = None) -> None:
        self._ret = ret
        self._raise = raise_exc
        self.last_js: str | None = None

    async def evaluate(self, js: str) -> object:
        self.last_js = js
        if self._raise is not None:
            raise self._raise
        return self._ret


@pytest.mark.asyncio
async def test_scan_rendered_dom_returns_text_on_happy_path() -> None:
    page = _MockPage(
        ret={
            "blocks": ["Limited-Time Special! Up to 2 Months Free!"],
            "body_text": "",
        }
    )
    out = await scan_rendered_dom_for_concession(page, _PROPERTY_CONCESSION_RE)
    assert out is not None
    assert "2 Months Free" in out


@pytest.mark.asyncio
async def test_scan_rendered_dom_passes_the_probe_js_through() -> None:
    page = _MockPage(ret={"blocks": [], "body_text": ""})
    await scan_rendered_dom_for_concession(page, _PROPERTY_CONCESSION_RE)
    assert page.last_js is RENDERED_DOM_PROBE_JS


@pytest.mark.asyncio
async def test_scan_rendered_dom_returns_none_on_evaluate_exception() -> None:
    """Page closed / browser crashed / CDP timeout — any error in
    evaluate must swallow to None so Step 3c never fails the scrape."""
    page = _MockPage(raise_exc=RuntimeError("evaluate timed out"))
    out = await scan_rendered_dom_for_concession(page, _PROPERTY_CONCESSION_RE)
    assert out is None


@pytest.mark.asyncio
async def test_scan_rendered_dom_returns_none_on_malformed_payload() -> None:
    """Page returned a string, an int, None, or a dict with error key —
    each must yield None rather than blow up downstream."""
    for bad in (None, "not a dict", 42, [1, 2, 3], {"error": "page exception"}):
        page = _MockPage(ret=bad)
        out = await scan_rendered_dom_for_concession(page, _PROPERTY_CONCESSION_RE)
        assert out is None, f"expected None for payload {bad!r}, got {out!r}"


@pytest.mark.asyncio
async def test_scan_rendered_dom_returns_none_when_page_is_none() -> None:
    out = await scan_rendered_dom_for_concession(None, _PROPERTY_CONCESSION_RE)
    assert out is None


@pytest.mark.asyncio
async def test_scan_rendered_dom_uses_body_text_fallback() -> None:
    """No popup blocks match, but body_text contains the offer — the
    body_text fallback must rescue."""
    page = _MockPage(
        ret={
            "blocks": ["Welcome home. Studios available."],
            "body_text": (
                "Welcome home. Now offering up to six weeks free on select "
                "homes! Tour today."
            ),
        }
    )
    out = await scan_rendered_dom_for_concession(page, _PROPERTY_CONCESSION_RE)
    assert out is not None
    assert "six weeks free" in out


# ─────────────────────────────────────────────────────────────────────
# 6) Wiring sanity check — Step 3c lives in scraper.py
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# 7) Nav-junk prefix stripping (post-1000-prop-sweep quality fix)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dirty, clean", [
    # Real shapes from the 1000-prop sweep
    (
        "Skip to main content\n2 Months Free + 2 Months Free Parking!",
        "2 Months Free + 2 Months Free Parking!",
    ),
    (
        "Skip to main content Move-In Special! One Month Free on Select Units!",
        "Move-In Special! One Month Free on Select Units!",
    ),
    (
        "Skip to main content $500 Off Rent! Receive $500 off your first month.",
        "$500 Off Rent! Receive $500 off your first month.",
    ),
    (
        "___\nSkip to main content\nNow offering 6 weeks free base rent on select homes!",
        "Now offering 6 weeks free base rent on select homes!",
    ),
    (
        "___ Skip to main content Live Up to 8-Weeks Free Base Rent",
        "Live Up to 8-Weeks Free Base Rent",
    ),
    (
        "Skip to Main Content Skip to Footer Enable Accessibility *$600 OFF",
        "*$600 OFF",
    ),
    # Menu toggles
    (
        "MENU $1000 Off if you apply within 48 hours",
        "$1000 Off if you apply within 48 hours",
    ),
    (
        "Open menu — Special Offer: 2 weeks free",
        "Special Offer: 2 weeks free",
    ),
    # Mixed case + accessibility widget
    (
        "Accessibility menu Skip Navigation 1 Month Free!",
        "1 Month Free!",
    ),
    # No prefix — passthrough unchanged
    (
        "Limited Time Special! Up to 2 Months Free.",
        "Limited Time Special! Up to 2 Months Free.",
    ),
    # Empty input
    ("", ""),
    # Only junk — stays empty
    ("Skip to main content", ""),
    ("___", ""),
])
def test_strip_leading_nav_junk_matrix(dirty: str, clean: str) -> None:
    """Pin every nav-junk shape we observed in the 1000-prop sweep.
    49/164 lift captures (~30 %) had at least one of these prefixes."""
    assert _strip_leading_nav_junk(dirty) == clean


def test_extract_concession_window_strips_skip_to_main_content() -> None:
    """End-to-end: a real rendered-DOM text with 'Skip to main content'
    glued to the front should return a clean offer window."""
    # Exact shape from sweep pid 62782 (Cortland Alameda Station)
    rendered = (
        "Skip to main content\n2 Months Free + 2 Months Free Parking! "
        "Two months of free rent & two months of free parking on select homes."
    )
    from ma_poc.pms.scraper import _PROPERTY_CONCESSION_RE
    m = _PROPERTY_CONCESSION_RE.search(rendered)
    assert m is not None
    out = extract_concession_window(rendered, m)
    assert not out.lower().startswith("skip to"), (
        f"nav junk leaked through: {out!r}"
    )
    assert "2 Months Free" in out


# ─────────────────────────────────────────────────────────────────────
# 8) Regex blind-spot fixes from 1000-prop "many-blocks-no-match"
#    Chrome MCP probing on 2026-05-24. The probes surfaced 2 systematic
#    regex misses across the 89-prop sample.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected_substr", [
    # Plural 'specials' — Abberly Centerpointe banner.
    # The prior \bspecial\b required a non-word boundary AFTER 'special'
    # which 'specials' (s is word-char) violated.
    (
        "Select apartment homes are now offering move-in specials. "
        "Don't miss out—apply today and make your move!",
        "move-in specials",
    ),
    ("Check out our current specials this month!", "current specials"),
    ("Limited-Time Specials on select homes", "Limited-Time Specials"),
    ("Featured Specials — see leasing office for details", "Featured Specials"),
    ("New Leasing Specials for May!", "Leasing Specials"),
    # 'N Month/Months/Week/Weeks Off' WITHOUT $ prefix — Shea Apartments
    # ('Up to 1 Month Off' header callout on York on City Park /
    # City Lights at Town Center). The pre-fix regex required $.
    ("Up to 1 Month Off — apply by 5/31!", "1 Month Off"),
    ("Get 2 Months Off your first year", "2 Months Off"),
    ("3 Weeks Off select apartments", "3 Weeks Off"),
])
def test_regex_catches_new_blind_spots(text: str, expected_substr: str) -> None:
    """Pin the post-1000-prop-sweep regex extensions: plural 'specials'
    + 'N Month/Week Off' without $."""
    from ma_poc.pms.scraper import _PROPERTY_CONCESSION_RE
    m = _PROPERTY_CONCESSION_RE.search(text)
    assert m is not None, (
        f"regex failed to match {text!r}; expected anchor {expected_substr!r}"
    )


def test_scraper_has_step_3c_invocation() -> None:
    """Source-level contract: scraper.py must call
    ``scan_rendered_dom_for_concession`` somewhere AND tag
    ``concession_source = 'DOM_POPUP_RENDERED'``. Pins the wiring so a
    future refactor that removes the call fails this test loudly."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    ).read_text(encoding="utf-8")
    assert "scan_rendered_dom_for_concession" in src, (
        "scraper.py no longer invokes scan_rendered_dom_for_concession — "
        "Step 3c is missing; JS-injected popup banners will be missed again."
    )
    assert '"DOM_POPUP_RENDERED"' in src, (
        "scraper.py no longer tags concession_source='DOM_POPUP_RENDERED' — "
        "Step 3c's provenance attribution is broken."
    )


def test_property_concession_re_half_off_and_percent():
    """2026-07-12: worded-fraction + percent discounts (greenarchtulsa
    "Half off first month rent…" — the only confirmed recall miss in a
    37-prop no-capture sample)."""
    from ma_poc.pms.scraper import _PROPERTY_CONCESSION_RE as R

    assert R.search("Half off first month rent when you lease our Greenwood unit!")
    assert R.search("Get 50% off your second month!")
    assert not R.search("Our halfway house is off the main road")
