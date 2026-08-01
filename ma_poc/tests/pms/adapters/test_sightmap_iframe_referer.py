"""2026-05-27 chip: SightMap iframe-fallback standalone-drill.

Verifies that when an operator site embeds ``sightmap.com/embed/{id}``,
``_try_sightmap_iframe_fallback`` fetches the embed URL with a ``Referer``
header pointing at the operator origin and a Chrome 120 ``User-Agent``,
and that units parsed from the embed API JSON are returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from ma_poc.pms.adapters import _probe as _probe_mod
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
_LINDLEY_HTML = (
    '<script>window.propertyConfig = {"spaces_asset_name":"The Lindley",'
    '"sightmap_url":"https:\\/\\/sightmap.com\\/embed\\/60p7x5j3v7n"};'
    "</script>"
)

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


@dataclass
class _RecordedRequest:
    """One outbound probe call — mirrors the ``httpx.Request`` surface these
    assertions used before the seam moved (``.url`` / ``.headers``)."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _StubProbeResponse:
    """Minimal ``probe_get`` return shape: ``.status_code`` + ``.text``."""

    status_code: int
    text: str


def _install_mock_probe(
    monkeypatch: pytest.MonkeyPatch, recorded: list[_RecordedRequest]
):
    """Replace ``_probe.probe_get`` with a recorder serving canned responses.

    ``probe_get`` — NOT ``httpx.AsyncClient`` — is the seam the iframe
    fallback fetches through as of 1d8fb89 ("route the page=None fallback API
    fetch through probe_get"), which moved it onto the residential/Web-Unlocker
    path because sightmap.com's CDN CF-blocks the bare GCP runner IP. Stubbing
    ``httpx`` here recorded nothing and let the call escape to the LIVE
    internet, so these assertions silently graded real sightmap.com traffic.

    The adapter imports ``probe_get`` *inside* the function, so patching the
    module attribute is what takes effect.
    """

    def _probe_get(url: str, headers: dict[str, str] | None = None, **_kwargs):  # type: ignore[no-untyped-def]
        recorded.append(_RecordedRequest(url=url, headers=dict(headers or {})))
        if url.endswith(("/embed/abc123xy", "/embed/60p7x5j3v7n")):
            return _StubProbeResponse(200, _EMBED_HTML)
        if url == _API_URL:
            return _StubProbeResponse(200, json.dumps(_API_PAYLOAD))
        return _StubProbeResponse(404, "not found")

    monkeypatch.setattr(_probe_mod, "probe_get", _probe_get)


@pytest.mark.asyncio
async def test_iframe_fallback_sends_referer_and_chrome120_ua(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[_RecordedRequest] = []
    _install_mock_probe(monkeypatch, recorded)

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
async def test_iframe_fallback_follows_lindley_sightmap_url_live_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[_RecordedRequest] = []
    _install_mock_probe(monkeypatch, recorded)
    ctx = AdapterContext(
        base_url="https://www.livethelindley.com/",
        detected=detect_pms("https://www.livethelindley.com/floor-plans/"),
        profile=None,
        expected_total_units=None,
        property_id="253393",
        fetch_result=_StubFetchResult(body=_LINDLEY_HTML),
    )
    result = AdapterResult()

    units = await _sm._try_sightmap_iframe_fallback(ctx, result)

    assert [request.url for request in recorded] == [
        "https://sightmap.com/embed/60p7x5j3v7n",
        _API_URL,
    ]
    assert {unit["unit_number"] for unit in units} == {"201", "202"}
    assert result.winning_url == _API_URL


@pytest.mark.asyncio
async def test_iframe_fallback_skips_referer_when_operator_is_sightmap_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the property is hosted on *.sightmap.com itself (synthetic
    test pages, not real customer embeds), we must NOT spoof a same-host
    Referer — fall back to no Referer rather than a misleading one."""
    recorded: list[_RecordedRequest] = []
    _install_mock_probe(monkeypatch, recorded)

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
    recorded: list[_RecordedRequest] = []
    _install_mock_probe(monkeypatch, recorded)

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
