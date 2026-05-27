"""2026-05-27 chip: SightMap iframe-fallback standalone-drill.

Verifies that when an operator site embeds ``sightmap.com/embed/{id}``,
``_try_sightmap_iframe_fallback`` fetches the embed URL with a ``Referer``
header pointing at the operator origin and a Chrome 120 ``User-Agent``,
and that units parsed from the embed API JSON are returned.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from ma_poc.pms.adapters import sightmap as _sm
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import detect_pms


@dataclass
class _StubFetchResult:
    body: bytes | str | None


# Operator page embeds the sightmap iframe — non-sightmap host. This
# mirrors the 4 RECOVERABLE sightmap_embed:api props from the 2026-05-27
# 612-failure-grind.
_OPERATOR_HOST = "https://www.exampleoperator.com"
_OPERATOR_HTML = """
<html><head><title>Example Apartments</title></head>
<body>
  <h1>Live at Example</h1>
  <iframe src="https://sightmap.com/embed/abc123xy" width="900" height="700">
  </iframe>
</body></html>
"""

# Embed page contains __APP_CONFIG__ with the API URL the JS would XHR.
_API_URL = "https://sightmap.com/app/api/v1/CLIENT/sightmaps/9999"
_EMBED_HTML = (
    "<html><body><script>window.__APP_CONFIG__ = "
    '{"sightmaps":[{"href":"'
    + _API_URL.replace("/", "\\/")
    + '"}]};</script></body></html>'
)

# Minimal SightMap API payload — 2 units, 1 floor plan.
_API_PAYLOAD = {
    "data": {
        "sightmap_id": 9999,
        "floor_plans": [
            {
                "id": 1,
                "name": "A1",
                "bedroom_count": 1,
                "bathroom_count": 1,
                "filter_label": "1BR",
            }
        ],
        "units": [
            {
                "floor_plan_id": 1,
                "unit_number": "201",
                "price": 1850,
                "area": 720,
                "available_on": "2026-06-15",
            },
            {
                "floor_plan_id": 1,
                "unit_number": "202",
                "price": 1925,
                "area": 720,
                "available_on": "2026-07-01",
            },
        ],
    }
}


def _make_ctx() -> AdapterContext:
    return AdapterContext(
        base_url=_OPERATOR_HOST + "/",
        detected=detect_pms(_OPERATOR_HOST + "/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST_EMBED",
        fetch_result=_StubFetchResult(body=_OPERATOR_HTML.encode("utf-8")),
    )


def _install_mock_httpx(monkeypatch: pytest.MonkeyPatch, recorded: list[httpx.Request]):
    """Replace httpx.AsyncClient with one backed by a MockTransport that
    records every outbound Request and serves canned responses."""

    def _handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        url = str(request.url)
        if url.endswith("/embed/abc123xy"):
            return httpx.Response(200, text=_EMBED_HTML)
        if url == _API_URL:
            return httpx.Response(200, json=_API_PAYLOAD)
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(_handler)
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_async_client)


@pytest.mark.asyncio
async def test_iframe_fallback_sends_referer_and_chrome120_ua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[httpx.Request] = []
    _install_mock_httpx(monkeypatch, recorded)

    ctx = _make_ctx()
    result = AdapterResult()
    units = await _sm._try_sightmap_iframe_fallback(ctx, result)

    # Embed + API must have both been fetched.
    urls = [str(r.url) for r in recorded]
    assert "https://sightmap.com/embed/abc123xy" in urls, urls
    assert _API_URL in urls, urls

    # Referer on the embed page request must point at the operator origin.
    embed_req = next(r for r in recorded if str(r.url).endswith("/embed/abc123xy"))
    assert embed_req.headers.get("Referer") == _OPERATOR_HOST + "/", (
        embed_req.headers
    )
    # UA bumped to Chrome 120 (chip explicitly calls this out).
    assert "Chrome/120" in embed_req.headers.get("User-Agent", "")

    # API request Referer should be the embed URL itself (mirrors the
    # browser's iframe → XHR chain), and Origin should be the operator.
    api_req = next(r for r in recorded if str(r.url) == _API_URL)
    assert api_req.headers.get("Referer") == "https://sightmap.com/embed/abc123xy"
    assert api_req.headers.get("Origin") == _OPERATOR_HOST

    # Units returned + plumbed onto result.
    assert len(units) == 2
    assert {u["unit_number"] for u in units} == {"201", "202"}
    assert result.winning_url == _API_URL
    assert any(r.get("via") == "iframe_fallback" for r in result.api_responses)


@pytest.mark.asyncio
async def test_iframe_fallback_skips_referer_when_operator_is_sightmap_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the property is hosted on *.sightmap.com itself (synthetic
    test pages, not real customer embeds), we must NOT spoof a same-host
    Referer — fall back to no Referer rather than a misleading one."""
    recorded: list[httpx.Request] = []
    _install_mock_httpx(monkeypatch, recorded)

    ctx = AdapterContext(
        base_url="https://tour.sightmap.com/embed/abc123xy",
        detected=detect_pms("https://tour.sightmap.com/embed/abc123xy"),
        profile=None,
        expected_total_units=None,
        property_id="TEST_SELF",
        fetch_result=_StubFetchResult(body=_OPERATOR_HTML.encode("utf-8")),
    )
    await _sm._try_sightmap_iframe_fallback(ctx, AdapterResult())

    embed_req = next(r for r in recorded if str(r.url).endswith("/embed/abc123xy"))
    # No spoofed Referer — operator host suppression engaged.
    assert "Referer" not in {k.title() for k in embed_req.headers.keys()}


@pytest.mark.asyncio
async def test_iframe_fallback_empty_when_no_iframe_in_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[httpx.Request] = []
    _install_mock_httpx(monkeypatch, recorded)

    ctx = AdapterContext(
        base_url=_OPERATOR_HOST + "/",
        detected=detect_pms(_OPERATOR_HOST + "/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST_NO_IFRAME",
        fetch_result=_StubFetchResult(
            body=b"<html><body>no sightmap here</body></html>"
        ),
    )
    units = await _sm._try_sightmap_iframe_fallback(ctx, AdapterResult())
    assert units == []
    assert recorded == []  # never reached the network
