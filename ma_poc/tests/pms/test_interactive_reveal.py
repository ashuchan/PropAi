"""Tests for the Pattern-B interactive reveal pre-extraction step.

Pattern B = floor-plan cards that show no rents until you click a
"View Availability" / "Show Units" button. Without this step our
adapters see the placeholder cards but no rent text.

The tests use a ``FakePage`` test double rather than spinning up
Playwright — the module is pure orchestration over two primitives
(``page.content()`` and ``page.evaluate()``) so a JS-free Python
simulation exercises the same branches and gates.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.interactive_reveal import (
    _MIN_RENT_SIGNALS,
    _count_rent_signals,
    looks_like_pattern_b,
    maybe_reveal,
)

# ── Gating heuristics ─────────────────────────────────────────────────


def test_count_rent_signals_basic() -> None:
    assert _count_rent_signals("") == 0
    assert _count_rent_signals("no rents here") == 0
    assert _count_rent_signals("$1,500/mo") == 1
    assert _count_rent_signals("from $1,200 to $2,400 with $3500 special") == 3


def test_count_rent_signals_ignores_two_digit_amounts() -> None:
    # "$50 fee" should not count — too small to be rent.
    assert _count_rent_signals("$50 application fee, $75 admin") == 0


def test_looks_like_pattern_b_positive() -> None:
    html = (
        "<html><body>"
        "<div>Floor Plan A · 1 bed · 750 sqft</div>"
        '<button class="expand">View Availability</button>'
        "<div>Floor Plan B · 2 bed · 1100 sqft</div>"
        '<button class="expand">View Availability</button>'
        "</body></html>"
    )
    assert looks_like_pattern_b(html)


def test_looks_like_pattern_b_skips_page_with_rents() -> None:
    html = (
        "<html><body>"
        "<div>Plan A · 1/1 · $1,450/mo</div>"
        "<div>Plan B · 2/2 · $1,900/mo</div>"
        "<div>Plan C · 3/2 · $2,500/mo</div>"
        '<button>View Availability</button>'  # has trigger text but rents present
        "</body></html>"
    )
    # ≥ _MIN_RENT_SIGNALS rents → extractor will succeed, skip reveal.
    assert _count_rent_signals(html) >= _MIN_RENT_SIGNALS
    assert not looks_like_pattern_b(html)


def test_looks_like_pattern_b_skips_when_no_trigger_text() -> None:
    # No rents AND no Pattern-B button text → not Pattern B (probably
    # a bot-blocked page or a true no-pricing site).
    html = "<html><body>Call for pricing<br>Schedule a tour</body></html>"
    assert not looks_like_pattern_b(html)


def test_looks_like_pattern_b_handles_empty() -> None:
    assert not looks_like_pattern_b("")
    assert not looks_like_pattern_b("    ")


def test_reveal_text_anchored_at_word_boundary() -> None:
    # "see availability" should match; "overseeavailability" (no space)
    # should not. Word boundaries prevent matches inside attribute names
    # or concatenated words.
    assert looks_like_pattern_b('<button>See Availability</button>')
    # Negative: substring inside attribute name shouldn't trip the gate
    # (no rents, no button text — just an attribute called showavailability).
    html = '<div data-showavailability="1">Static content</div>'
    assert not looks_like_pattern_b(html)


# ── FakePage test double ──────────────────────────────────────────────


class _FakePage:
    """Minimal Playwright-Page stand-in for unit tests.

    Holds an HTML string and a list of (id, post_click_html) reveals.
    Each call to ``evaluate(_CLICK_TRIGGER_JS, id)`` replaces the HTML
    with ``post_click_html`` so subsequent ``content()`` calls see the
    expanded DOM.
    """

    def __init__(
        self,
        html: str,
        *,
        trigger_ids: list[int] | None = None,
        post_click_html: str | None = None,
        find_raises: Exception | None = None,
        click_fails_for: set[int] | None = None,
    ) -> None:
        self._html = html
        self._trigger_ids = trigger_ids or []
        self._post_click_html = post_click_html
        self._find_raises = find_raises
        self._click_fails_for = click_fails_for or set()
        self.evaluate_calls: list[tuple[str, Any]] = []
        self.clicked_ids: list[int] = []

    async def content(self) -> str:
        return self._html

    async def evaluate(self, js: str, *args: Any) -> Any:
        self.evaluate_calls.append((js[:30], args))
        # "find triggers" call has no args.
        if not args:
            if self._find_raises is not None:
                raise self._find_raises
            return list(self._trigger_ids)
        # "click trigger" call carries the id as arg[0].
        trigger_id = args[0]
        if trigger_id in self._click_fails_for:
            raise RuntimeError(f"simulated click failure on {trigger_id}")
        self.clicked_ids.append(trigger_id)
        # Replace HTML with the post-click version on the FIRST successful
        # click so the rent_delta calculation sees the expanded DOM.
        if self._post_click_html is not None and len(self.clicked_ids) == 1:
            self._html = self._post_click_html
        return True


# ── maybe_reveal: skip branches ───────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_reveal_no_page_returns_no_page() -> None:
    out = await maybe_reveal(None)
    assert out == {"triggered": False, "reason": "no_page"}


@pytest.mark.asyncio
async def test_maybe_reveal_empty_html_skipped() -> None:
    page = _FakePage("")
    out = await maybe_reveal(page)
    assert out["triggered"] is False
    assert out["reason"] == "empty_html"


@pytest.mark.asyncio
async def test_maybe_reveal_rents_present_skipped() -> None:
    html = (
        "<html><body>"
        "<div>Plan A · $1,450/mo</div>"
        "<div>Plan B · $1,900/mo</div>"
        "<div>Plan C · $2,500/mo</div>"
        "<button>View Availability</button>"
        "</body></html>"
    )
    page = _FakePage(html, trigger_ids=[0])
    out = await maybe_reveal(page)
    assert out["triggered"] is False
    assert out["reason"] == "rents_present"
    # We should NOT have called evaluate when the gate skipped us.
    assert page.evaluate_calls == []


@pytest.mark.asyncio
async def test_maybe_reveal_no_trigger_text_skipped() -> None:
    html = (
        "<html><body>"
        "<h1>Welcome to our property</h1>"
        "<p>Call for pricing</p>"
        "</body></html>"
    )
    page = _FakePage(html, trigger_ids=[0])
    out = await maybe_reveal(page)
    assert out["triggered"] is False
    assert out["reason"] == "no_reveal_text"
    assert page.evaluate_calls == []


@pytest.mark.asyncio
async def test_maybe_reveal_no_triggers_after_stamping() -> None:
    # The page HAS reveal text in markup but the JS-side selector found
    # zero matching elements (e.g. all triggers were inside a frame we
    # can't reach). Should report no_triggers_found, not crash.
    html = "<html><body><span>View Availability</span></body></html>"
    page = _FakePage(html, trigger_ids=[])
    out = await maybe_reveal(page)
    assert out["triggered"] is False
    assert out["reason"] == "no_triggers_found"
    # Exactly one evaluate call (the find pass).
    assert len(page.evaluate_calls) == 1


# ── maybe_reveal: success path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_maybe_reveal_happy_path_clicks_and_reports_delta() -> None:
    initial = (
        "<html><body>"
        "<div>Plan A</div><button>View Availability</button>"
        "<div>Plan B</div><button>View Availability</button>"
        "<div>Plan C</div><button>View Availability</button>"
        "</body></html>"
    )
    expanded = (
        "<html><body>"
        "<div>Plan A · $1,400/mo · Unit 101 · $1,450</div>"
        "<div>Plan B · $1,800/mo · Unit 201 · $1,900</div>"
        "<div>Plan C · $2,200/mo · Unit 301 · $2,500</div>"
        "</body></html>"
    )
    page = _FakePage(
        initial,
        trigger_ids=[0, 1, 2],
        post_click_html=expanded,
    )
    out = await maybe_reveal(page)
    assert out["triggered"] is True
    assert out["clicks"] == 3
    assert out["rent_signals_before"] == 0
    assert out["rent_signals_after"] >= 3  # ≥ 3 dollar amounts after reveal
    assert out["rent_delta"] >= 3
    assert out["errors"] == []
    assert page.clicked_ids == [0, 1, 2]


@pytest.mark.asyncio
async def test_maybe_reveal_continues_after_click_failure() -> None:
    initial = (
        "<html><body>"
        "<button>View Availability</button>"
        "<button>View Availability</button>"
        "<button>View Availability</button>"
        "</body></html>"
    )
    expanded = (
        "<html><body>"
        "Unit 101 · $1,400 · Unit 102 · $1,500"
        "</body></html>"
    )
    page = _FakePage(
        initial,
        trigger_ids=[0, 1, 2],
        post_click_html=expanded,
        click_fails_for={1},  # Middle click raises; loop must continue.
    )
    out = await maybe_reveal(page)
    assert out["triggered"] is True
    assert out["clicks"] == 2  # ids 0 and 2 succeeded
    assert len(out["errors"]) == 1
    assert "click_1" in out["errors"][0]


@pytest.mark.asyncio
async def test_maybe_reveal_find_triggers_exception_returns_skip() -> None:
    html = "<html><body><button>View Availability</button></body></html>"
    page = _FakePage(html, find_raises=RuntimeError("page closed"))
    out = await maybe_reveal(page)
    assert out["triggered"] is False
    assert "find_failed" in out["reason"]


@pytest.mark.asyncio
async def test_maybe_reveal_accepts_pre_fetched_html() -> None:
    # Caller may pass page_html separately to avoid a second content()
    # call (the scraper already has page_html in hand). The pre-fetched
    # HTML must drive the gate; we shouldn't re-fetch.
    html = "<html><body><button>View Availability</button></body></html>"
    page = _FakePage(html, trigger_ids=[])
    out = await maybe_reveal(page, page_html=html)
    # find pass ran (we have trigger text) and returned []
    assert out["triggered"] is False
    assert out["reason"] == "no_triggers_found"
