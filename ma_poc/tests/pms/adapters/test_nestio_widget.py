"""Nestio contact-widget rendered-DOM extractor.

2026-05-21: live-probed www.dermotcompany.com's unit-11C URL via
Chrome MCP. The page is a React shell — curl_cffi alone gets an empty
``apt-results`` container; the contact widget JS bundle
(``integrations.nestio.com/contact-widget/v1/integration.js``) renders
the unit detail client-side into a fixed DOM template.

This adapter handles **the rendered DOM**: the orchestrator must pass
the Playwright-rendered body (not the L1 curl_cffi shell). The
Funnel/Essex adapter (``_funnel.py``) handles the other Nestio-customer
integration shape (server-side API proxy at ``/api/properties/{id}``);
this one handles the Dermot-style client-side widget.

Fixture: ``ma_poc/tests/fixtures/nestio_widget/dermot_11c_rendered.html``
— DOM IDs + values are verbatim from the live page (Chrome MCP
``getElementById`` enumeration on 2026-05-21).
"""

from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._nestio_widget import (
    detect_widget_rendered,
    detect_widget_will_render,
    parse_widget_dom,
)

# Anchor on this file, not the process CWD — ``pytest tests/pms`` from inside
# ma_poc/ must resolve fixtures the same way a repo-root run does.
_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "nestio_widget"


def _load(name: str) -> str:
    return (_FIXTURE / name).read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────


def test_detect_rendered_matches_real_dermot_dom() -> None:
    """The Dermot 11C rendered DOM must trip the rendered-DOM detector."""
    assert detect_widget_rendered(_load("dermot_11c_rendered.html")) is True


def test_detect_rendered_rejects_l1_shell() -> None:
    """An L1 React shell (no apt-* IDs yet rendered) must NOT match the
    rendered-DOM detector — that's the orchestrator's signal to escalate
    to a Playwright render."""
    shell = (
        '<html><body>'
        '<div id="apt-results" data-react-root></div>'
        '<script src="https://integrations.nestio.com/contact-widget/v1/integration.js"></script>'
        '</body></html>'
    )
    assert detect_widget_rendered(shell) is False


def test_detect_rendered_requires_two_markers() -> None:
    """A page with only ONE apt-* id (e.g. an unrelated CMS that
    happens to use ``id="apt-price"`` for something else) must NOT
    trigger us. Pin the ≥2 marker requirement so false-positive
    routing stays out."""
    one_marker = '<html><body><div id="apt-price">$1,000</div></body></html>'
    assert detect_widget_rendered(one_marker) is False


def test_detect_will_render_matches_shell_with_widget_script() -> None:
    """The shell detector trips on the WIDGET-LOAD marker even when the
    rendered IDs aren't there yet."""
    shell = (
        '<html><body>'
        '<div id="apt-results"></div>'
        '<script src="https://integrations.nestio.com/contact-widget/v1/integration.js"></script>'
        '</body></html>'
    )
    assert detect_widget_will_render(shell) is True


def test_detect_will_render_matches_nestiostatic_asset() -> None:
    """Asset host reference is also a will-render signal — some pages
    don't directly include the widget script but pull a logo from
    ``assets.nestiostatic.com``."""
    html = (
        '<html><body>'
        '<img src="https://assets.nestiostatic.com/community_logos/x.jpg">'
        '</body></html>'
    )
    assert detect_widget_will_render(html) is True


def test_detect_will_render_rejects_unrelated() -> None:
    assert detect_widget_will_render("<html><body>nothing</body></html>") is False
    assert detect_widget_will_render("") is False


# ─────────────────────────────────────────────────────────────────────
# Parser — against real rendered DOM
# ─────────────────────────────────────────────────────────────────────


def test_parse_dermot_11c_extracts_all_fields() -> None:
    """The 11C fixture matches the live Chrome MCP probe — every field
    must populate."""
    units = parse_widget_dom(
        _load("dermot_11c_rendered.html"),
        source_url="https://www.dermotcompany.com/properties/building/availability/apartment",
    )
    assert len(units) == 1, f"expected 1 unit; got {len(units)}"
    u = units[0]
    assert u["unit_number"] == "11C"
    assert u["building"] == "101 West End Avenue"
    assert u["address"].startswith("101 West End Avenue,")
    assert u["bedrooms"] == "0", f"studio should normalise to '0'; got {u['bedrooms']!r}"
    assert u["bathrooms"] == "1"
    assert u["sqft"] == "561"
    assert u["market_rent_low"] == 4212
    assert u["market_rent_high"] == 4212
    assert u["rent_range"] == "$4,212"
    assert u["availability_date"] == "2026-05-05"
    assert u["availability_status"] == "AVAILABLE"
    assert u["fee_label"] == "No Fee"
    assert u["source"] == "nestio_widget_dom"


def test_parse_dermot_11c_carries_concession_blurb() -> None:
    """The widget renders the net-effective-rent disclosure into
    ``apt-description``. The adapter passes it through as ``concession``
    so downstream concession-parsing can extract the months-free."""
    units = parse_widget_dom(_load("dermot_11c_rendered.html"))
    assert units
    c = units[0]["concession"]
    assert "0.5 Months Free" in c
    assert "$4395.00" in c


def test_parse_returns_empty_on_l1_shell() -> None:
    """Defensive — if the orchestrator accidentally calls us on the L1
    shell (pre-render), return empty so we don't emit a zero-value
    unit record."""
    shell = (
        '<html><body>'
        '<div id="apt-results"></div>'
        '<script src="https://integrations.nestio.com/contact-widget/v1/integration.js"></script>'
        '</body></html>'
    )
    assert parse_widget_dom(shell) == []


def test_parse_returns_empty_on_unrelated_html() -> None:
    assert parse_widget_dom("") == []
    assert parse_widget_dom("<html><body><h1>Unrelated</h1></body></html>") == []


def test_parse_studio_dash_normalises_to_zero() -> None:
    """The widget renders studio bedrooms as ``-``. Parser normalises
    to ``"0"`` so downstream code treats it like every other adapter's
    bedrooms (numeric string)."""
    html = (
        '<html><body>'
        '<div id="apt-results">'
        '<span id="apt-number-02">A101</span>'
        '<span id="apt-value-bedroom">-</span>'
        '<span id="apt-value-bathroom">1</span>'
        '<span id="apt-value-sf">500</span>'
        '<div id="apt-price">$1,200</div>'
        '<span id="apt-date-available">06 / 01 / 2026</span>'
        '</div>'
        '</body></html>'
    )
    units = parse_widget_dom(html)
    assert len(units) == 1
    assert units[0]["bedrooms"] == "0"


def test_parse_handles_one_bedroom_explicit() -> None:
    """Non-studio bedrooms render as the integer."""
    html = (
        '<html><body><div id="apt-results">'
        '<span id="apt-number-02">2B</span>'
        '<span id="apt-value-bedroom">2</span>'
        '<span id="apt-value-bathroom">1.5</span>'
        '<span id="apt-value-sf">980</span>'
        '<div id="apt-price">$3,500</div>'
        '<span id="apt-date-available">07 / 15 / 2026</span>'
        '</div></body></html>'
    )
    units = parse_widget_dom(html)
    assert len(units) == 1
    assert units[0]["bedrooms"] == "2"
    assert units[0]["bathrooms"] == "1.5"
    assert units[0]["sqft"] == "980"
    assert units[0]["market_rent_low"] == 3500


def test_parse_iso_date_format() -> None:
    """``MM / DD / YYYY`` → ``YYYY-MM-DD`` so dates align with the rest
    of the adapter family (which uses ISO)."""
    units = parse_widget_dom(_load("dermot_11c_rendered.html"))
    assert units[0]["availability_date"] == "2026-05-05"
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", units[0]["availability_date"])


def test_parse_emits_available_units_count_when_known() -> None:
    """One unit per Nestio-widget page (the URL is per-apartment), so
    ``available_units`` is ``"1"`` when the unit has a date or rent —
    helps downstream merge logic."""
    units = parse_widget_dom(_load("dermot_11c_rendered.html"))
    assert units[0]["available_units"] == "1"
