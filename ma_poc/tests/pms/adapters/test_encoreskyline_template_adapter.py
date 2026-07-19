"""EncoreSkylineTemplateAdapter (Jonah Digital widget per-plan expand) — 2026-05-19.

Live-verified ground-truth: encoreskyline.com (#B-302 / Spruce),
geneseepointe.com (#308/#209/#410/#310 / Floorplan B/BR),
highlineaustin.com (#2206/#1306/#7108 / A1). The adapter walks the
per-plan URLs, clicks ``Check Availability`` on each, waits for the
Jonah widget to insert its rendered rows, and parses
``document.body.innerText`` with ``parse_encoreskyline_units``.
"""

from __future__ import annotations

import types

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.encoreskyline_template import (
    EncoreSkylineTemplateAdapter,
    _get_page_html,
    _html_from_ctx,
    _plan_urls_from_html,
)
from ma_poc.pms.detector import detect_pms

# Live-shape page text (post-click) reused from the parser test fixtures.
_SPRUCE_RENDERED = (
    "Spruce 1 bed 1 bath 703 sq. ft. $1,750 - $1,790 Check Availability "
    "#B-302 Floor 3 703 sq. ft. $1,750 $400 Deposit Available Jul 3"
)
_BBR_RENDERED = (
    "Floorplan B/BR 1 bed 1 bath 820 sq. ft. Starting at $2,025 Check Availability "
    "#308 Floor 1 820 sq. ft. Starting at $2,025 Available Now Lease Now "
    "#209 Floor 1 820 sq. ft. Starting at $2,025 Available Jun 16 Lease Now "
    "#410 Floor 1 820 sq. ft. Starting at $2,025 Available Jul 08 Lease Now "
    "#310 Floor 1 820 sq. ft. Starting at $2,025 Available Jul 08 Lease Now"
)

# A page-level HTML that carries the Jonah marker + a per-plan anchor set.
_INDEX_HTML_JONAH = (
    "<html><body>"
    "<script>JonahWidget.meetelise({organization:'X',building:'Y'});</script>"
    "<a href='/floorplans/spruce/'>Spruce</a>"
    "<a href='/floorplans/floorplan-bbr/'>BBR</a>"
    "</body></html>"
)
_INDEX_HTML_PLAIN = "<html><body>plain marketing site</body></html>"


class _FakePage:
    """Mock the slice of Playwright the adapter actually uses.

    Per-URL ``rendered_text`` maps the URL → the post-click DOM innerText
    we want the adapter to "see" after ``Check Availability`` fires.
    Per-URL ``html`` maps the URL → the full page HTML returned by
    ``content()``. URLs missing from a map degrade to ``""`` so the
    adapter exercises its error paths cleanly.
    """

    def __init__(
        self,
        url: str,
        html_by_url: dict[str, str],
        rendered_by_url: dict[str, str],
    ) -> None:
        self.url = url
        self._html_by_url = html_by_url
        self._rendered_by_url = rendered_by_url
        self._clicked: bool = False
        self.click_count = 0
        self.goto_count = 0

    async def goto(self, url: str) -> object:
        self.url = url
        self._clicked = False
        self.goto_count += 1
        return None

    async def content(self) -> str:
        return self._html_by_url.get(self.url, "")

    async def click(self, _selector: str) -> object:
        self._clicked = True
        self.click_count += 1
        return None

    async def wait_for_timeout(self, _ms: int) -> object:
        return None

    async def evaluate(self, js: str, *args: object) -> object:
        # The adapter uses ``evaluate`` for three things:
        #   1. ``_PER_PLAN_URLS_JS`` — return per-plan hrefs from the page.
        #   2. ``() => document.body.innerText || ''`` — post-click read.
        #   3. ``() => new Promise(r => setTimeout(r, N))`` — wait fallback.
        # Pattern-match by JS body since we don't actually run any JS.
        if "querySelectorAll('a')" in js and "floorplans" in js:
            # Extract per-plan hrefs from the current page's HTML.
            html = self._html_by_url.get(self.url, "")
            import re as _re

            hrefs: list[str] = []
            for m in _re.finditer(r"href=['\"]([^'\"]+)['\"]", html):
                h = m.group(1)
                if _re.search(r"/floorplans/[a-z0-9-]+/?$", h, _re.I):
                    if h.startswith("/"):
                        from urllib.parse import urljoin

                        h = urljoin(self.url, h)
                    hrefs.append(h)
            return hrefs
        if "innerText" in js:
            # Return rendered text ONLY if the per-plan toggle has fired.
            # Pre-click reads return only the plan-level prose to keep the
            # parser-no-false-positive invariant tested end-to-end.
            if self._clicked:
                return self._rendered_by_url.get(self.url, "")
            html = self._html_by_url.get(self.url, "")
            # Strip simple tags for a "plan-level" innerText approximation.
            import re as _re

            return _re.sub(r"<[^>]+>", " ", html)
        if "setTimeout" in js:
            return None
        # JS-side click fallback path: pattern matches the adapter's
        # querySelector('a,button') + textContent /check availability/i loop.
        if "check availability" in js.lower():
            self._clicked = True
            self.click_count += 1
            return True
        return None


def _ctx(base_url: str) -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


# --- Happy path: index has Jonah marker + 2 per-plan links --------------


@pytest.mark.asyncio
async def test_geneseepointe_style_full_recovery() -> None:
    """Index page links 2 per-plans; click fires; both render units."""
    base = "https://geneseepointe.com"
    page = _FakePage(
        url=base + "/floorplans/",
        html_by_url={
            base + "/floorplans/": _INDEX_HTML_JONAH,
            base + "/floorplans/spruce/": (
                "<html><body><script>jonahwidget</script>"
                "<a href='/floorplans/spruce/'>x</a></body></html>"
            ),
            base + "/floorplans/floorplan-bbr/": (
                "<html><body><script>jonahwidget</script>"
                "<a href='/floorplans/floorplan-bbr/'>x</a></body></html>"
            ),
        },
        rendered_by_url={
            base + "/floorplans/spruce/": _SPRUCE_RENDERED,
            base + "/floorplans/floorplan-bbr/": _BBR_RENDERED,
        },
    )
    result = await EncoreSkylineTemplateAdapter().extract(page, _ctx(base + "/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"
    # 1 from spruce + 4 from BBR = 5 unique unit_numbers across plans
    assert len(result.units) == 5
    nums = sorted(u.get("unit_number") for u in result.units)
    assert nums == ["209", "308", "310", "410", "B-302"]
    assert result.confidence > 0.7
    assert page.click_count == 2  # one click per per-plan page


# --- Marker-gate behavior ------------------------------------------------


@pytest.mark.asyncio
async def test_no_jonah_marker_is_a_pass_through() -> None:
    """Adapter must NOT fire when the Jonah marker is missing.

    This is the strict gate that keeps the RealPage-onlineleasing variant
    of the same visual marketing template from routing here.
    """
    base = "https://rent-portofino.com"
    page = _FakePage(
        url=base + "/",
        html_by_url={base + "/": _INDEX_HTML_PLAIN},
        rendered_by_url={},
    )
    result = await EncoreSkylineTemplateAdapter().extract(page, _ctx(base + "/"))  # type: ignore[arg-type]
    assert result.tier_used == "NOT_ENCORESKYLINE_TEMPLATE"
    assert result.units == []
    assert page.click_count == 0


# --- No per-plan anchors → safe error tier ------------------------------


@pytest.mark.asyncio
async def test_no_per_plan_links_returns_safe_error() -> None:
    """Jonah marker present but no /floorplans/{slug}/ anchors anywhere."""
    base = "https://example.com"
    html = (
        "<html><body>"
        "<script>JonahWidget.meetelise({});</script>"
        "<a href='/'>home</a>"
        "</body></html>"
    )
    page = _FakePage(
        url=base + "/",
        html_by_url={base + "/": html, base + "/floorplans/": html},
        rendered_by_url={},
    )
    result = await EncoreSkylineTemplateAdapter().extract(page, _ctx(base + "/"))  # type: ignore[arg-type]
    assert result.tier_used == "ENCORESKYLINE_NO_PLAN_LINKS"
    assert result.units == []


# --- Click-fired-but-rendered-empty (template-capable, 0 live units) ---


@pytest.mark.asyncio
async def test_click_fires_but_no_units_render() -> None:
    """The kittermanwoods / thealtavistaapts pattern: control present, 0 units."""
    base = "https://example.com"
    page = _FakePage(
        url=base + "/floorplans/",
        html_by_url={
            base + "/floorplans/": (
                "<html><body><script>JonahWidget.meetelise({});</script>"
                "<a href='/floorplans/a1/'>a1</a></body></html>"
            ),
            base + "/floorplans/a1/": (
                "<html><body><script>jonahwidget</script>"
                "<a href='/floorplans/a1/'>x</a></body></html>"
            ),
        },
        rendered_by_url={
            # Post-click but nothing to render — exactly what we saw on
            # kittermanwoods / thealtavistaapts: Check-Availability button
            # exists, click fires, no unit rows materialize.
            base + "/floorplans/a1/": (
                "A1 1 bed 1 bath 720 sq. ft. Starting at $1,723 Check Availability"
            ),
        },
    )
    result = await EncoreSkylineTemplateAdapter().extract(page, _ctx(base + "/"))  # type: ignore[arg-type]
    assert result.tier_used == "ENCORESKYLINE_NO_UNITS"
    assert result.units == []


# --- Duplicate unit_number across plans (defensive dedupe) --------------


@pytest.mark.asyncio
async def test_duplicate_unit_number_across_plans_is_deduped() -> None:
    """Defensive: if two plans share a unit_number, emit it once."""
    base = "https://example.com"
    html_idx = (
        "<html><body><script>JonahWidget.meetelise({});</script>"
        "<a href='/floorplans/a1/'>a1</a>"
        "<a href='/floorplans/a2/'>a2</a>"
        "</body></html>"
    )
    page = _FakePage(
        url=base + "/floorplans/",
        html_by_url={
            base + "/floorplans/": html_idx,
            base + "/floorplans/a1/": html_idx,
            base + "/floorplans/a2/": html_idx,
        },
        rendered_by_url={
            base + "/floorplans/a1/": (
                "#308 Floor 1 820 sq. ft. $2,025 Available Now Lease Now"
            ),
            base + "/floorplans/a2/": (
                # Same #308 — should NOT be re-emitted from the second plan.
                "#308 Floor 1 820 sq. ft. $2,025 Available Now Lease Now "
                "#410 Floor 1 820 sq. ft. $2,025 Available Jul 08 Lease Now"
            ),
        },
    )
    result = await EncoreSkylineTemplateAdapter().extract(page, _ctx(base + "/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_ENCORESKYLINE_TEMPLATE"
    nums = sorted(u.get("unit_number") for u in result.units)
    assert nums == ["308", "410"]


# --- Registry / detector wiring sanity ----------------------------------


def test_detector_routes_jonah_marker_to_encoreskyline_template() -> None:
    """A page carrying the Jonah marker must detect as the new pms label."""
    from ma_poc.pms.detector import detect_pms

    result = detect_pms(
        "https://encoreskyline.com/",
        page_html="<html><body>JonahWidget.meetelise({})</body></html>",
    )
    assert result.pms == "encoreskyline_template"
    assert result.confidence >= 0.8


def test_adapter_is_registered() -> None:
    """End-to-end: the adapter is wired into the registry by import time."""
    from ma_poc.pms.adapters import get_adapter

    adapter = get_adapter("encoreskyline_template")
    assert adapter.pms_name == "encoreskyline_template"


# ── 2026-07-19 (task #37): fetched-body fallback under page=None dispatch ──
# Production calls adapter.extract(page=None, ctx); the adapter previously read
# HTML only from page.content() → gate saw "" → every encore prop falsely
# failed NOT_ENCORESKYLINE_TEMPLATE. It must fall back to ctx.fetch_result.body.

_RENTPRESS_BODY = (
    "<html><body>"
    "<script>window.jonahdigital = 1;</script>"
    "<div id='rentpress-app' data-floorplans='"
    '[{"floorplan_name":"A1","floorplan_bedrooms":"1","units":['
    '{"unit_name":"101","unit_rent_best":"1500","unit_bedrooms":"1",'
    '"unit_sqft":"650","unit_available":"1","unit_available_on":"2026-09-01"},'
    '{"unit_name":"102","unit_rent_best":"1600","unit_bedrooms":"1",'
    '"unit_sqft":"675","unit_available":"1","unit_available_on":"2026-10-01"}]}]'
    "'></div></body></html>"
)
_JONAH_ONLY_BODY = "<html><body><script>jonahdigital.init()</script>no plans here</body></html>"
_PLAIN_BODY = "<html><body>plain marketing site, no widget</body></html>"


def _ctx_with_body(body, url="https://www.example-encore.com/"):
    from ma_poc.pms.detector import detect_pms
    fr = types.SimpleNamespace(body=body)
    return AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="enc-test",
        fetch_result=fr,
    )


def test_html_from_ctx_decodes_bytes_and_str() -> None:
    assert _html_from_ctx(_ctx_with_body(b"<html>hi</html>")) == "<html>hi</html>"
    assert _html_from_ctx(_ctx_with_body("<html>hi</html>")) == "<html>hi</html>"
    assert _html_from_ctx(_ctx_with_body(None)) == ""
    assert _html_from_ctx(None) == ""


@pytest.mark.asyncio
async def test_get_page_html_falls_back_to_ctx_when_page_none() -> None:
    assert await _get_page_html(None, _ctx_with_body("<html>body</html>")) == "<html>body</html>"


@pytest.mark.asyncio
async def test_get_page_html_prefers_live_page() -> None:
    page = _FakePage("u", {"u": "<html>live</html>"}, {})
    # live page content wins over the ctx body
    assert await _get_page_html(page, _ctx_with_body("<html>fetched</html>")) == "<html>live</html>"


def test_plan_urls_from_html_harvests_and_dedups() -> None:
    html = (
        "<a href='/floorplans/a1/'>A1</a><a href=\"/floorplans/b2/\">B2</a>"
        "<a href='/floorplans/a1/'>dup</a><a href='/floorplans/'>index-backlink</a>"
        "<a href='/gallery/'>no</a>"
    )
    out = _plan_urls_from_html(html, "https://x.com/")
    assert out == ["https://x.com/floorplans/a1/", "https://x.com/floorplans/b2/"]
    assert _plan_urls_from_html("", "https://x.com/") == []


@pytest.mark.asyncio
async def test_extract_page_none_recovers_rentpress_from_body() -> None:
    """THE WIN: production page=None but the fetched body carries the static
    RentPress inventory → recovers unit-level, no live page needed."""
    adapter = EncoreSkylineTemplateAdapter()
    res = await adapter.extract(None, _ctx_with_body(_RENTPRESS_BODY))
    assert res.tier_used == "TIER_1_DOM_RENTPRESS"
    assert len(res.units) == 2
    assert {u["unit_number"] for u in res.units} == {"101", "102"}


@pytest.mark.asyncio
async def test_extract_page_none_gate_passes_on_body_marker() -> None:
    """Gate must no longer false-fail: a jonahdigital body with no plans now
    gets PAST the marker gate to an honest no-plan-links verdict."""
    adapter = EncoreSkylineTemplateAdapter()
    res = await adapter.extract(None, _ctx_with_body(_JONAH_ONLY_BODY))
    assert res.tier_used != "NOT_ENCORESKYLINE_TEMPLATE"
    assert res.tier_used in ("ENCORESKYLINE_NO_PLAN_LINKS", "ENCORESKYLINE_NO_UNITS")


@pytest.mark.asyncio
async def test_extract_page_none_plain_body_still_rejected() -> None:
    """Regression: a genuinely non-Jonah body is still correctly rejected."""
    adapter = EncoreSkylineTemplateAdapter()
    res = await adapter.extract(None, _ctx_with_body(_PLAIN_BODY))
    assert res.tier_used == "NOT_ENCORESKYLINE_TEMPLATE"
