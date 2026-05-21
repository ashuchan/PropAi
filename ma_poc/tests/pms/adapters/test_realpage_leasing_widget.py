"""Phase 6.7 — RealPage Leasing widget detector + iframe-DOM parser.

2026-05-21: ~12 properties in the HAR ``needs_chrome_probe`` cluster
embed a same-origin RealPage Leasing iframe. The data only renders
inside the iframe, gated on per-property tokens minted at widget-load
time — no curl_cffi or HAR-replay path.

This module's contract:
  • ``detect_realpage_leasing_widget`` — detector that runs on raw
    property-page HTML.
  • ``parse_realpage_leasing_iframe_dom`` — parses the iframe's
    inner-HTML (the Playwright orchestrator in generic.py fetches it
    via ``page.frame_locator(...).inner_html()``).
  • ``iframe_url_for_realpage_id`` — pins the URL shape so the
    orchestrator and tests agree.

The browser orchestration itself is not unit-tested here — it lives in
generic.py and needs a live Playwright fixture to validate. These
tests pin the deterministic surface area.
"""

from __future__ import annotations

from ma_poc.pms.adapters._realpage_leasing import (
    detect_realpage_leasing_widget,
    iframe_url_for_realpage_id,
    parse_realpage_leasing_iframe_dom,
)

# ─────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────


def test_detect_canonical_widget_block() -> None:
    """The canonical shape observed in HAR probing —
    ``<div class="realpage widget">{"realpageId":"NNNN"}</div>``."""
    html = (
        '<html><body>'
        '<div class="realpage widget">{"realpageId":"12345"}</div>'
        '</body></html>'
    )
    assert detect_realpage_leasing_widget(html) == "12345"


def test_detect_class_order_varies() -> None:
    """``widget realpage`` order should also match — class tokens are a
    set, not an ordered list."""
    html = (
        '<html><body>'
        '<div class="widget realpage">{"realpageId":67890}</div>'
        '</body></html>'
    )
    assert detect_realpage_leasing_widget(html) == "67890"


def test_detect_extra_classes_tolerated() -> None:
    """Real properties add their own theme classes alongside
    ``realpage widget``."""
    html = (
        '<html><body>'
        '<div class="container themed-light realpage widget shadow">'
        '{"realpageId":"42"}</div>'
        '</body></html>'
    )
    assert detect_realpage_leasing_widget(html) == "42"


def test_detect_data_attribute_form() -> None:
    """A handful of properties carry the ID as ``data-realpage-id``
    rather than in the inline JSON. Detector must handle both."""
    html = (
        '<html><body>'
        '<div class="realpage widget" data-realpage-id="999"></div>'
        '</body></html>'
    )
    assert detect_realpage_leasing_widget(html) == "999"


def test_detect_returns_none_when_classes_missing() -> None:
    """A div with JUST ``widget`` (no ``realpage``) is something else
    (custom widget, chat bot, etc.) — must not match."""
    html = '<div class="widget">{"realpageId":"100"}</div>'
    assert detect_realpage_leasing_widget(html) is None


def test_detect_returns_none_when_no_id() -> None:
    """The classes are right but no realpageId payload — return None."""
    html = '<div class="realpage widget">Welcome to our community</div>'
    assert detect_realpage_leasing_widget(html) is None


def test_detect_handles_empty_html() -> None:
    assert detect_realpage_leasing_widget("") is None
    assert detect_realpage_leasing_widget("<html></html>") is None


# ─────────────────────────────────────────────────────────────────────
# Iframe URL constructor
# ─────────────────────────────────────────────────────────────────────


def test_iframe_url_includes_hash_route() -> None:
    """The widget routes ``/#!/oll/search-floorplan`` inside the iframe.
    Pin so the orchestrator agrees on URL shape."""
    url = iframe_url_for_realpage_id("https://example.com/floorplans", "12345")
    assert url.endswith("/#!/oll/search-floorplan?id=12345"), url


def test_iframe_url_adds_trailing_slash() -> None:
    """If base_url lacks trailing slash, append one before the hash."""
    url = iframe_url_for_realpage_id("https://example.com/fp", "7")
    assert url == "https://example.com/fp/#!/oll/search-floorplan?id=7"


# ─────────────────────────────────────────────────────────────────────
# Iframe-DOM parser
# ─────────────────────────────────────────────────────────────────────


_THREE_CARDS_IFRAME = """
<html><body>
<section>
  <article role="article">
    <h3>The Aspen</h3>
    <p>1 Bed | 1 Bath | 720 sq ft</p>
    <p>$1,450*</p>
    <p>(3) Available</p>
  </article>
  <article role="article">
    <h3>The Birch</h3>
    <p>2 Bed | 2 Bath | 980 sq ft</p>
    <p>$1,850*</p>
    <p>(2) Available</p>
  </article>
  <article role="article">
    <h3>The Cedar</h3>
    <p>3 Bed | 2.5 Bath | 1,240 sq ft</p>
    <p>$2,250*</p>
    <p>(1) Available</p>
  </article>
</section>
</body></html>
"""


def test_parse_iframe_three_canonical_cards() -> None:
    units = parse_realpage_leasing_iframe_dom(
        _THREE_CARDS_IFRAME, "https://x.test/"
    )
    assert len(units) == 3, f"expected 3 units; got {len(units)}"
    aspen = units[0]
    assert aspen["floor_plan_name"] == "The Aspen"
    assert aspen["bedrooms"] == "1"
    assert aspen["bathrooms"] == "1"
    assert aspen["sqft"] == "720"
    assert aspen["market_rent_low"] == 1450
    assert aspen["available_units"] == "3"
    assert aspen["source"] == "realpage_leasing_widget"

    cedar = units[2]
    assert cedar["bedrooms"] == "3"
    assert cedar["bathrooms"] == "2.5"
    assert cedar["sqft"] == "1240"


def test_parse_iframe_handles_call_for_pricing() -> None:
    """A card with ``Call for Pricing`` instead of a $ amount — rent
    stays blank, availability_status records the policy."""
    iframe = """
    <article role="article">
      <h3>The Maple</h3>
      <p>1 Bed | 1 Bath | 720 sq ft</p>
      <p>Call for Pricing</p>
      <p>(1) Available</p>
    </article>
    <article role="article">
      <h3>The Oak</h3>
      <p>2 Bed | 2 Bath | 980 sq ft</p>
      <p>$1,850*</p>
      <p>(2) Available</p>
    </article>
    """
    units = parse_realpage_leasing_iframe_dom(iframe, "")
    assert len(units) == 2
    maple = units[0]
    assert maple["market_rent_low"] is None
    assert maple["rent_range"] == ""
    assert "call for pricing" in maple["availability_status"].lower()
    assert maple["available_units"] == "1"


def test_parse_iframe_handles_studio_card() -> None:
    iframe = """
    <article role="article">
      <h3>The Spruce</h3>
      <p>Studio Bed | 1 Bath | 500 sq ft</p>
      <p>$1,200*</p>
      <p>(2) Available</p>
    </article>
    <article role="article">
      <h3>The Fir</h3>
      <p>Studio Bed | 1 Bath | 520 sq ft</p>
      <p>$1,250*</p>
      <p>(1) Available</p>
    </article>
    """
    units = parse_realpage_leasing_iframe_dom(iframe, "")
    assert len(units) == 2
    assert units[0]["bedrooms"] == "0"
    assert units[0]["sqft"] == "500"


def test_parse_iframe_alternate_card_class() -> None:
    """If ``[role="article"]`` doesn't match, fall back through the
    selector list. ``.floorplan-card`` is the documented secondary."""
    iframe = """
    <div class="floorplan-card">
      <h3>Plan A</h3>
      <span>1 Bed | 1 Bath | 700 sq ft</span>
      <span>$1,400*</span>
      <span>(2) Available</span>
    </div>
    <div class="floorplan-card">
      <h3>Plan B</h3>
      <span>2 Bed | 2 Bath | 950 sq ft</span>
      <span>$1,800*</span>
      <span>(3) Available</span>
    </div>
    """
    units = parse_realpage_leasing_iframe_dom(iframe, "")
    assert len(units) == 2
    assert units[0]["floor_plan_name"] == "Plan A"


def test_parse_iframe_returns_empty_on_no_cards() -> None:
    """The widget JS hasn't rendered yet (or returned no inventory) —
    the parser must return empty without raising. The orchestrator's
    drift detector decides whether to fall through to LLM after N
    consecutive empties."""
    iframe = "<html><body><p>Loading...</p></body></html>"
    assert parse_realpage_leasing_iframe_dom(iframe, "") == []


def test_parse_iframe_returns_empty_on_empty_input() -> None:
    assert parse_realpage_leasing_iframe_dom("", "") == []
