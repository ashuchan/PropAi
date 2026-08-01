"""RentCafe anchor-walk fallback tests (2026-05-27, 612-grind row 2).

Pins:
  • ``_discover_rentcafe_anchors`` extracts /availability, /apartments,
    /floor-plans, /rentcafe-listings, /listings same-host anchors.
  • Cross-host anchors and /module/ / /applic* dead-ends are filtered out.
  • ``_try_rentcafe_anchor_walk`` reuses ``parse_rentcafe_hosted_table``
    when the candidate page emits the ``fp-unit`` markup, and
    ``parse_securecafe_availableunits`` when it emits ``AvailUnitRow``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.rentcafe import (
    _discover_rentcafe_anchors,
    _try_rentcafe_anchor_walk,
)
from ma_poc.pms.detector import detect_pms


def _make_ctx(base_url: str, html: str) -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="rc-aw-1",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    ctx.fetch_result = SimpleNamespace(  # type: ignore[attr-defined]
        body=html.encode("utf-8"), final_url=base_url
    )
    return ctx


def test_discover_finds_canonical_drill_anchors() -> None:
    html = """
    <html><body>
      <a href="/availability">Avail</a>
      <a href="/apartments/">Apts</a>
      <a href="/floor-plans">FP</a>
      <a href="/floorplans">FP2</a>
      <a href="/rentcafe-listings">RC</a>
      <a href="/listings">L</a>
    </body></html>
    """
    anchors = _discover_rentcafe_anchors(
        html, "https://example.com", "https://example.com/"
    )
    paths = sorted(a.rsplit("example.com", 1)[1].rstrip("/") for a in anchors)
    assert "/availability" in paths
    assert "/apartments" in paths
    assert "/floor-plans" in paths
    assert "/floorplans" in paths
    assert "/rentcafe-listings" in paths
    assert "/listings" in paths


def test_discover_filters_cross_host() -> None:
    html = (
        '<a href="https://other.com/availability">x</a>'
        '<a href="/availability">y</a>'
    )
    anchors = _discover_rentcafe_anchors(
        html, "https://example.com", "https://example.com/"
    )
    assert all("example.com" in a for a in anchors)
    assert not any("other.com" in a for a in anchors)


def test_discover_rejects_other_properties_on_shared_rentcafe_host() -> None:
    base = (
        "https://www.rentcafe.com/apartments/mi/kalamazoo/"
        "coopers-landing-apartments/default.aspx"
    )
    html = (
        f'<a href="{base}?view=units">own property</a>'
        '<a href="/apartments/mi/kalamazoo/winchell-way0/default.aspx">'
        "recommended property</a>"
        '<a href="/apartments-for-rent/mi/">regional search</a>'
    )
    assert _discover_rentcafe_anchors(
        html, "https://www.rentcafe.com", base
    ) == [f"{base}?view=units"]


def test_discover_skips_module_and_applic_paths() -> None:
    html = (
        '<a href="/module/floorplans">m</a>'
        '<a href="/application/availability">a</a>'
        '<a href="/availability">ok</a>'
    )
    anchors = _discover_rentcafe_anchors(
        html, "https://example.com", "https://example.com/"
    )
    assert len(anchors) == 1
    assert anchors[0].endswith("/availability")


def test_discover_empty_inputs() -> None:
    assert _discover_rentcafe_anchors("", "https://x.com", "") == []
    assert _discover_rentcafe_anchors("<a href='/availability'>x</a>", "", "") == []


@pytest.mark.asyncio
async def test_anchor_walk_uses_hosted_table_parser_on_fp_unit_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a candidate URL returns ``fp-unit`` markup, anchor-walk reuses
    ``parse_rentcafe_hosted_table`` and returns the parsed units."""
    homepage = (
        '<html><body><a href="/availability">Find availability</a>'
        "</body></html>"
    )
    base_url = "https://example.com/"
    ctx = _make_ctx(base_url, homepage)
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")

    sentinel_units = [{"unit_number": "101", "rent": "1500", "sqft": "750"}]

    fake_response_text = (
        "<html><body>"
        + "x" * 2000
        + "<tr class='fp-unit'>dummy fp-unit markup</tr>"
        + "</body></html>"
    )

    def fake_probe_get(url: str, **kw: Any) -> Any:
        assert url.endswith("/availability")
        return SimpleNamespace(status_code=200, text=fake_response_text)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", fake_probe_get
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.rentcafe.parse_rentcafe_hosted_table",
        lambda html, src: sentinel_units,
    )

    async def fake_get_page_html(page: Any, ctx: Any) -> str:
        return homepage

    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic._get_page_html", fake_get_page_html
    )

    units = await _try_rentcafe_anchor_walk(None, ctx, result)  # type: ignore[arg-type]
    assert units == sentinel_units
    assert result.winning_url and result.winning_url.endswith("/availability")


@pytest.mark.asyncio
async def test_anchor_walk_rejects_hosted_property_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homepage = (
        '<a href="/availability">Find availability</a>'
        '<button onclick="go(\'x?myOlePropertyId=480033\')">Apply</button>'
    )
    ctx = _make_ctx("https://example.com/", homepage)
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")
    candidate = "<html>" + "x" * 2000 + "<tr class='fp-unit'>x</tr></html>"

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: SimpleNamespace(status_code=200, text=candidate),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.rentcafe.parse_rentcafe_hosted_table",
        lambda html, src: [
            {
                "unit_number": "04-P308",
                "market_rent_low": 1183,
                "source_property_id": "1682177",
            }
        ],
    )

    async def fake_get_page_html(page: Any, ctx: Any) -> str:
        return homepage

    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic._get_page_html", fake_get_page_html
    )

    units = await _try_rentcafe_anchor_walk(None, ctx, result)  # type: ignore[arg-type]
    assert units == []
    assert any("property-mismatch" in error for error in result.errors)


@pytest.mark.asyncio
async def test_anchor_walk_uses_securecafe_parser_on_availunitrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a candidate URL returns ``AvailUnitRow`` markup, anchor-walk
    reuses ``parse_securecafe_availableunits``."""
    homepage = '<a href="/apartments">Apts</a>'
    base_url = "https://example.com/"
    ctx = _make_ctx(base_url, homepage)
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")

    sentinel_units = [{"unit_number": "A1", "rent": "1200"}]

    fake_text = "<html>" + "z" * 2000 + "<tr class='AvailUnitRow'>r</tr></html>"

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: SimpleNamespace(status_code=200, text=fake_text),
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.rentcafe.parse_securecafe_availableunits",
        lambda html, src: sentinel_units,
    )

    async def fake_get_page_html(page: Any, ctx: Any) -> str:
        return homepage

    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic._get_page_html", fake_get_page_html
    )

    units = await _try_rentcafe_anchor_walk(None, ctx, result)  # type: ignore[arg-type]
    assert units == sentinel_units


@pytest.mark.asyncio
async def test_anchor_walk_returns_empty_when_no_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _make_ctx("https://example.com/", "<html><body>nothing</body></html>")
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")

    async def fake_get_page_html(page: Any, ctx: Any) -> str:
        return "<html><body>nothing</body></html>"

    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic._get_page_html", fake_get_page_html
    )
    units = await _try_rentcafe_anchor_walk(None, ctx, result)  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_anchor_walk_swallows_probe_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe_get raise must not propagate; helper returns [] and logs."""
    homepage = '<a href="/availability">x</a>'
    ctx = _make_ctx("https://example.com/", homepage)
    result = AdapterResult(tier_used="TIER_1_API_RENTCAFE")

    def boom(url: str, **kw: Any) -> Any:
        raise RuntimeError("network down")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", boom)

    async def fake_get_page_html(page: Any, ctx: Any) -> str:
        return homepage

    monkeypatch.setattr(
        "ma_poc.pms.adapters.generic._get_page_html", fake_get_page_html
    )
    units = await _try_rentcafe_anchor_walk(None, ctx, result)  # type: ignore[arg-type]
    assert units == []
    assert any("rentcafe-anchor-walk-fetch-error" in e for e in result.errors)
