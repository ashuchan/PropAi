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
    ctx.property_name = "Test Property"
    return ctx


def _avail_html(n_rows: int = 3) -> str:
    """Produce a minimal AvailUnitRow HTML body so the drill thinks
    it found unit rows."""
    rows = "".join(
        f"<tr class='AvailUnitRow' data-unit='{i}'>"
        f"<td>{1000 + i}</td><td>1 Bed</td><td>1 Bath</td>"
        f"<td>750</td><td>$1,500</td><td>Now</td></tr>"
        for i in range(n_rows)
    )
    return f"<html><body><table>{rows}</table></body></html>"


def _parseable_avail_html() -> str:
    """Minimal modern SecureCafe row with a nested advertised rent range."""
    return """
    <h3>Apartment Details and Selection for Floor Plan: Capewood -
        2 Bedrooms, 2 Bathrooms</h3>
    <table><tr class='AvailUnitRow'>
      <td data-label='Apartment'>#1896</td>
      <td data-label='Sq.Ft.'>1,620</td>
      <td data-label='Rent'><span>$2,817</span>-<span>$3,383</span></td>
      <td data-label='Date Available'><span>9/17/2026</span></td>
    </tr></table>
    """


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

    html = '<a href="https://x.securecafe.com/onlineleasing/foo/availableunits.aspx">apply</a>'
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
        f"First SC drill call must be direct (proxies={{}}), got proxies={captured[0]['proxies']}"
    )


def test_securecafe_drill_falls_through_to_proxy_when_direct_misses() -> None:
    """When direct doesn't yield AvailUnitRow AND PROBE_PROXY_URL is
    set (Yardi tenant that blocks GCP IPs), retry through the proxy."""
    import asyncio
    import os

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = '<a href="https://y.securecafe.com/onlineleasing/bar/availableunits.aspx">apply</a>'
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

    html = '<a href="https://z.securecafe.com/onlineleasing/baz/availableunits.aspx">apply</a>'
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

    # One legacy direct request plus one first-party Applicant V2 theme lookup;
    # neither is a proxy retry and both explicitly disable proxy use.
    assert len(calls) == 2
    assert calls[0]["url"].endswith("/availableunits.aspx")
    assert "getcustomcolorsfilename" in calls[1]["url"]
    assert all(call["proxies"] == {} for call in calls)
    assert calls[0]["proxies"] == {}


def test_securecafe_drill_uses_bounded_hb_raw_fallback() -> None:
    """A CF-blocked direct probe may use one solver-off HB raw session."""
    import asyncio
    import os

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = '<a href="https://redwood.securecafe.com/onlineleasing/redwood/availableunits.aspx">apply</a>'
    ctx = _make_ctx(html)
    result = AdapterResult()
    hb_calls: list[tuple[str, str]] = []

    def fake_probe(url, **kw):
        return _make_probe_response(403, "blocked")

    async def fake_hb(url: str, pid: str = "?"):
        hb_calls.append((url, pid))
        return 200, _parseable_avail_html()

    env = {k: v for k, v in os.environ.items() if k not in ("PROBE_PROXY_URL", "WEB_UNLOCKER_KEY")}
    env["FETCH_BACKEND"] = "hyperbrowser"
    env["COMPLIANCE_MODE"] = "1"
    with (
        patch.dict(os.environ, env, clear=True),
        patch("ma_poc.pms.adapters._probe.probe_get", side_effect=fake_probe),
        patch(
            "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
            side_effect=fake_hb,
        ),
    ):
        units = asyncio.run(_try_rentcafe_securecafe_probe(ctx, result))

    assert len(hb_calls) == 1
    assert hb_calls[0][1] == "TEST-001"
    assert len(units) == 1
    assert units[0]["unit_number"] == "1896"
    assert units[0]["market_rent_low"] == 2817
    assert units[0]["market_rent_high"] == 3383
    assert units[0]["availability_date"] == "9/17/2026"


def test_securecafe_fast_direct_path_does_not_open_hb() -> None:
    """The explicitly code-only fast path remains direct-only and bounded."""
    import asyncio
    import os

    from ma_poc.pms.adapters.rentcafe import _try_rentcafe_securecafe_probe

    html = '<a href="https://x.securecafe.com/onlineleasing/foo/availableunits.aspx">apply</a>'
    ctx = _make_ctx(html)
    result = AdapterResult()

    async def forbidden_hb(*args, **kwargs):
        raise AssertionError("fast_direct_only must not spend an HB session")

    env = {k: v for k, v in os.environ.items() if k != "PROBE_PROXY_URL"}
    env["FETCH_BACKEND"] = "hyperbrowser"
    with (
        patch.dict(os.environ, env, clear=True),
        patch(
            "ma_poc.pms.adapters._probe.probe_get",
            return_value=_make_probe_response(403, "blocked"),
        ),
        patch(
            "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
            side_effect=forbidden_hb,
        ),
    ):
        units = asyncio.run(_try_rentcafe_securecafe_probe(ctx, result, fast_direct_only=True))

    assert units == []
