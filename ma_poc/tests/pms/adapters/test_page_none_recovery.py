"""page=None recovery helpers + body-capable embed/portal recoveries
(task #37 Track 1, 2026-07-19).

Production dispatches L3 with page=None, which hard-gated recover_appfolio_embed
and recover_pms_portal to return [] and never fired the sub-path probe with a
usable fetcher. These pin: (a) the shared probe_fetch_status / body_html_from_ctx
helpers, (b) that at page=None the recoveries scan the fetched RENDER body and
fetch the discovered URL via curl_cffi, (c) that the blanket sub-path probe stays
page-only (no curl_cffi storm on every 0-unit property).
"""

from __future__ import annotations

import types

import pytest

from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
from ma_poc.pms.adapters._probe import body_html_from_ctx, probe_fetch_status
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import detect_pms
from tests.pms.adapters.test_appfolio_embed import _APPFOLIO_SSR, _IFRAME_SRC


def _ctx_with_body(body: str | bytes | None, base_url: str = "https://x.com/") -> AdapterContext:
    fr = types.SimpleNamespace(body=body)
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        fetch_result=fr,
    )


# ── body_html_from_ctx ─────────────────────────────────────────────────────────


def test_body_html_from_ctx_bytes_str_none() -> None:
    assert body_html_from_ctx(_ctx_with_body(b"<html>hi</html>")) == "<html>hi</html>"
    assert body_html_from_ctx(_ctx_with_body("<html>hi</html>")) == "<html>hi</html>"
    assert body_html_from_ctx(_ctx_with_body(None)) == ""
    assert body_html_from_ctx(None) == ""


# ── probe_fetch_status (mock the sync probe_get) ───────────────────────────────


@pytest.mark.asyncio
async def test_probe_fetch_status_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: types.SimpleNamespace(status_code=200, text="BODY"),
    )
    assert await probe_fetch_status("https://x/") == (200, "BODY")


@pytest.mark.asyncio
async def test_probe_fetch_status_non_2xx_returns_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: types.SimpleNamespace(status_code=403, text="<html>blocked</html>"),
    )
    assert await probe_fetch_status("https://x/") == (403, "")


@pytest.mark.asyncio
async def test_probe_fetch_status_raise_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, **kw: object) -> object:
        raise RuntimeError("no curl_cffi")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _boom)
    assert await probe_fetch_status("https://x/") == (0, "")


# ── recover_appfolio_embed at page=None ────────────────────────────────────────


@pytest.mark.asyncio
async def test_appfolio_recover_page_none_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WIN: page=None, but the fetched body carries the AppFolio /listings
    iframe → body scan finds it, curl_cffi fetches it, SSR parses 2 units."""
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: types.SimpleNamespace(status_code=200, text=_APPFOLIO_SSR),
    )
    body = f"<html><body><iframe src='{_IFRAME_SRC}'></iframe></body></html>"
    units = await recover_appfolio_embed(None, _ctx_with_body(body))  # type: ignore[arg-type]
    assert len(units) == 2
    assert units[0]["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"


@pytest.mark.asyncio
async def test_appfolio_recover_page_none_protocol_relative_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Squarespace commonly serializes ``//tenant.appfolio.com`` URLs.
    They are still operator-published HTTPS inventory surfaces."""
    calls: list[str] = []

    def _get(url: str, **kw: object) -> object:
        calls.append(url)
        return types.SimpleNamespace(status_code=200, text=_APPFOLIO_SSR)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _get)
    body = "<html><body><iframe src='//illumepm.appfolio.com/listings'></iframe></body></html>"
    units = await recover_appfolio_embed(None, _ctx_with_body(body))  # type: ignore[arg-type]

    assert len(units) == 2
    assert calls.count("https://illumepm.appfolio.com/listings") == 1


@pytest.mark.asyncio
async def test_appfolio_recover_page_none_no_marker_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain body with no AppFolio marker must NOT fire any curl_cffi probe
    (the blanket sub-path probe is page-only) → clean [] with zero network."""
    calls: list[str] = []
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, **kw: (calls.append(url), types.SimpleNamespace(status_code=200, text=""))[1],
    )
    units = await recover_appfolio_embed(
        None, _ctx_with_body("<html><body>plain marketing site</body></html>")
    )  # type: ignore[arg-type]
    assert units == []
    assert calls == []  # no blanket probing at page=None


@pytest.mark.asyncio
async def test_appfolio_recover_page_none_no_body_is_empty() -> None:
    units = await recover_appfolio_embed(None, _ctx_with_body(None))  # type: ignore[arg-type]
    assert units == []
