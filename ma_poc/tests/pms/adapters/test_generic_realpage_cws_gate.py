"""RealPage CWS generic-fallback probe gate (#22, 2026-07-16).

The generic sub-tier 4b credential probe (`_probe_realpage_cws`) reads a
public ``propertyId`` + UUID ``apiKey`` from the page and fires the RealPage
units API directly, yielding UNIT-LEVEL data. It used to gate solely on the
literal ``rpfp_config`` — but legacy / newer CWS themes (floorplan-V3 / Lyon)
carry the same credentials without that wrapper literal, so those pages were
skipped. The gate now also fires on the credential signature itself
(propertyId + apiKey present), which is exactly the probe's precondition.
"""

from __future__ import annotations

import httpx
import pytest

from ma_poc.pms.adapters.generic import (
    _probe_realpage_cws,
    _should_probe_realpage_cws,
)

# Legacy CWS shape: real creds embedded, but NO ``rpfp_config`` wrapper literal.
_LEGACY_HTML = """
<html><head><script src="https://cs-cdn.realpage.com/CWS/2383573/floorplan-V3.js"></script>
<script>
  var propertyId = '7824624';
  var config = { apiKey: 'c87bbe01-ee2b-44cd-a6f7-628881fb790e' };
</script></head><body></body></html>
"""

# Classic shape: the ``rpfp_config`` literal is present (already handled).
_RPFP_CONFIG_HTML = """
<html><body><script>
  var RPFP_config = { propertyId: 4471234, apiKey: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" };
</script></body></html>
"""


# ── gate helper ──────────────────────────────────────────────────────────────

def test_gate_fires_on_rpfp_config_literal():
    assert _should_probe_realpage_cws(_RPFP_CONFIG_HTML) is True


def test_gate_fires_on_credential_signature_without_literal():
    # The whole point of the fix: legacy theme, no rpfp_config literal, but the
    # probe's creds (propertyId + apiKey UUID) ARE present.
    assert "rpfp_config" not in _LEGACY_HTML.lower()
    assert _should_probe_realpage_cws(_LEGACY_HTML) is True


def test_gate_does_not_fire_without_credentials():
    html = "<html><body><h1>Floor Plans</h1><p>Call for pricing.</p></body></html>"
    assert _should_probe_realpage_cws(html) is False


def test_gate_requires_both_credentials_not_just_property_id():
    # propertyId alone (no apiKey UUID) must not fire — the probe would fail.
    html = "<html><script>var propertyId = '7824624';</script></html>"
    assert _should_probe_realpage_cws(html) is False


def test_gate_requires_both_credentials_not_just_api_key():
    html = '<html><script>var config={apiKey:"c87bbe01-ee2b-44cd-a6f7-628881fb790e"};</script></html>'
    assert _should_probe_realpage_cws(html) is False


def test_gate_empty_html():
    assert _should_probe_realpage_cws("") is False


# ── probe end-to-end (monkeypatched httpx) ────────────────────────────────────

class _FakeResp:
    status_code = 200

    @staticmethod
    def json() -> dict:
        return {
            "response": {
                "units": [
                    {
                        "unitNumber": "101",
                        "numberOfBeds": 1,
                        "numberOfBaths": 1,
                        "squareFeet": 700,
                        "rent": 1500,
                        "leaseStatus": "Available",
                        "internalAvailableDate": "2026-06-01T00:00:00-05:00",
                    },
                    {
                        "unitNumber": "205",
                        "numberOfBeds": 2,
                        "numberOfBaths": 2,
                        "squareFeet": 1050,
                        "rent": 2100,
                        "leaseStatus": "Available",
                        "internalAvailableDate": "2026-07-15T00:00:00-05:00",
                    },
                ]
            }
        }


class _FakeClient:
    """Minimal async httpx.AsyncClient stand-in; records the auth header."""

    captured_headers: dict[str, str] = {}

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
        _FakeClient.captured_headers = dict(headers or {})
        return _FakeResp()


@pytest.mark.asyncio
async def test_probe_extracts_units_from_legacy_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy CWS (creds present, no rpfp_config) yields unit-level rows and
    sends the extracted apiKey as x-ws-authkey."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    units = await _probe_realpage_cws(_LEGACY_HTML)
    assert len(units) == 2
    assert [u["unit_number"] for u in units] == ["101", "205"]
    assert units[0]["market_rent_low"] == 1500
    assert units[0]["available_date"] == "2026-06-01"  # time/TZ stripped
    assert units[0]["availability_status"] == "AVAILABLE"
    # the public apiKey extracted from HTML is sent as the auth header
    assert (
        _FakeClient.captured_headers.get("x-ws-authkey")
        == "c87bbe01-ee2b-44cd-a6f7-628881fb790e"
    )


@pytest.mark.asyncio
async def test_probe_returns_empty_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No creds → probe self-guards and never calls the API."""
    called = {"n": 0}

    class _NoCall(_FakeClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
            called["n"] += 1
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _NoCall)
    units = await _probe_realpage_cws("<html><body>no creds here</body></html>")
    assert units == []
    assert called["n"] == 0
