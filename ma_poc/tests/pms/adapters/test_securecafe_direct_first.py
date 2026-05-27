"""SecureCafe drill direct-first probe test (2026-05-24).

Pins the post-canary fix to _try_rentcafe_securecafe_probe: each
candidate base must try a DIRECT curl_cffi probe first (proxies={}),
and only fall back to the PROBE_PROXY_URL proxied probe if direct
didn't yield AvailUnitRow content.

Background — focused canary 2026-05-23-focused-3886351 showed 105
TIER_1_API_RENTCAFE_SHAPE_REJECTED. Probe of 5 sample drill URLs:
  • Direct curl_cffi: 3/5 returned 200 + AvailUnitRow
  • Proxied via BrightData: only 2/5 (40% LESS than direct)

BrightData IP pool gets 403'd on some Yardi SecureCafe subdomains.
Direct works on most because the GCP worker IP isn't on the operator's
per-IP SC blocklist (separate from the marketing-vanity-host
blocklist).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from ma_poc.pms.adapters.base import AdapterResult


def _make_ctx(html: str = ""):
    """Minimal AdapterContext stub with a body that links to a SC base."""
    ctx = MagicMock()
    ctx.fetch_result = MagicMock()
    ctx.fetch_result.body = html.encode("utf-8")
    ctx.fetch_result.final_url = "https://example.com/"
    ctx.base_url = "https://example.com/"
    ctx.property_id = "TEST-001"
    return ctx


def _avail_html(n_rows: int = 3) -> str:
    """Produce a minimal AvailUnitRow HTML body so the drill thinks
    it found unit rows."""
    rows = "".join(
        f"<tr class='AvailUnitRow' data-unit='{i}'>"
        f"<td>{1000+i}</td><td>1 Bed</td><td>1 Bath</td>"
        f"<td>750</td><td>$1,500</td><td>Now</td></tr>"
        for i in range(n_rows)
    )
    return f"<html><body><table>{rows}</table></body></html>"


def _make_probe_response(status: int, text: str):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


def test_securecafe_drill_tries_direct_first() -> None:
    """The drill must call probe_get with proxies={} as the FIRST
    attempt on each candidate base."""
    import asyncio

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = (
        '<a href="https://x.securecafe.com/onlineleasing/foo/availableunits.aspx">'
        'apply</a>'
    )
    ctx = _make_ctx(html)
    result = AdapterResult()

    captured = []

    def fake_probe(url, **kw):
        captured.append({"url": url, "proxies": kw.get("proxies")})
        # Return success on the first DIRECT call
        return _make_probe_response(200, _avail_html(3))

    with patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe):
        # Parsing the synthetic HTML may emit 0 units; that's fine.
        # The test verifies the CALL ORDERING (direct first), not parse output.
        asyncio.run(_try_rentcafe_securecafe_probe(ctx, result))

    assert captured, "probe_get must be called"
    assert captured[0]["proxies"] == {}, (
        f"First SC drill call must be direct (proxies={{}}), got "
        f"proxies={captured[0]['proxies']}"
    )


def test_securecafe_drill_falls_through_to_proxy_when_direct_misses() -> None:
    """When direct doesn't yield AvailUnitRow AND PROBE_PROXY_URL is
    set (Yardi tenant that blocks GCP IPs), retry through the proxy."""
    import asyncio
    import os

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = (
        '<a href="https://y.securecafe.com/onlineleasing/bar/availableunits.aspx">'
        'apply</a>'
    )
    ctx = _make_ctx(html)
    result = AdapterResult()

    calls = []

    def fake_probe(url, **kw):
        calls.append({"url": url, "proxies": kw.get("proxies")})
        # First call (direct): 403
        if kw.get("proxies") == {}:
            return _make_probe_response(403, "blocked")
        # Second call (proxied): success
        return _make_probe_response(200, _avail_html(2))

    with (
        patch.dict(os.environ, {"PROBE_PROXY_URL": "http://user:pass@host:port"}),
        patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe),
    ):
        asyncio.run(_try_rentcafe_securecafe_probe(ctx, result))

    assert len(calls) == 2, f"expected direct+proxied = 2 calls, got {len(calls)}"
    assert calls[0]["proxies"] == {}, "first call must be direct"
    assert calls[1]["proxies"] is None, "second call must use default proxy"


def test_securecafe_drill_no_retry_when_no_proxy_env() -> None:
    """When direct yields nothing AND PROBE_PROXY_URL is unset, don't
    pointlessly retry through a non-existent proxy."""
    import asyncio
    import os

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = (
        '<a href="https://z.securecafe.com/onlineleasing/baz/availableunits.aspx">'
        'apply</a>'
    )
    ctx = _make_ctx(html)
    result = AdapterResult()

    calls = []

    def fake_probe(url, **kw):
        calls.append({"url": url, "proxies": kw.get("proxies")})
        return _make_probe_response(403, "blocked")

    env = {k: v for k, v in os.environ.items() if k != "PROBE_PROXY_URL"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe),
    ):
        asyncio.run(_try_rentcafe_securecafe_probe(ctx, result))

    # 1 base in HTML × 1 direct attempt only (no proxy fallback) = 1 call
    assert len(calls) == 1
    assert calls[0]["proxies"] == {}
