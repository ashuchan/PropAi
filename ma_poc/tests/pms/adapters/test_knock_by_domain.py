"""Knock-by-domain resolver fallback (2026-05-20).

When a Knock-managed property doesn't expose ``knockDoorway.init()`` in
static HTML (common on Aspen Square / brand portfolio sites where the
init call loads via dynamic JS), the by-domain resolver queries Knock's
public ``/v1/profile?code=w&domain={URL}`` endpoint to map the
marketing-site URL directly to a numeric property_id, then fetches
``/v1/property/{pid}/units``.

Per the JSON-LD recovery memo
(``project_jsonld_recovery_2026-05-20.md``) — verified live against
Adley at 72nd: 15 unit rows with full data via this 2-call resolver,
no auth required.

Tests cover:
* ``_should_try_knock_by_domain`` signal detection — including the
  ``utm_knock=`` red-herring rule (must NOT fire on RentCafe-hosted sites)
* ``_fetch_knock_units_by_domain`` happy-path + 4 error paths
* ``KnockAdapter.extract()`` falls back to by-domain when init() not
  present in HTML
* ``KnockAdapter.extract()`` prefers the init() path when present
* Empty html still permits by-domain when base_url is Aspen Square
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from ma_poc.pms.adapters import knock as knock_mod
from ma_poc.pms.adapters.base import AdapterContext, DetectedPMS
from ma_poc.pms.adapters.knock import (
    KnockAdapter,
    _fetch_knock_units_by_domain,
    _should_try_knock_by_domain,
)

# ── _should_try_knock_by_domain signal detection ────────────────────────────


class TestShouldTryKnockByDomain:
    def test_aspen_square_url_always_qualifies(self) -> None:
        """Aspen Square portfolio always uses Knock — match URL directly."""
        assert _should_try_knock_by_domain(
            "", "https://www.aspensquare.com/apartments/nebraska/papillion/adley-at-72nd"
        )
        # Case-insensitive
        assert _should_try_knock_by_domain(
            "", "https://aspensquare.com/apartments/Arkansas/cabot/the-avenue"
        )

    def test_doorway_api_in_html_qualifies(self) -> None:
        """``doorway-api.knockrentals.com`` reference in HTML → Knock signal."""
        html = '<script>fetch("https://doorway-api.knockrentals.com/v1/...")</script>'
        assert _should_try_knock_by_domain(html, "https://x.com")

    def test_knockrentals_widget_url_qualifies(self) -> None:
        html = '<script src="https://knockrentals.com/widget/loader.js"></script>'
        assert _should_try_knock_by_domain(html, "https://x.com")

    def test_doorway_api_with_rentcafe_cdn_disqualified(self) -> None:
        """When BOTH Knock AND RentCafe-CDN are present, RentCafe is the real
        inventory backend — Knock is just lead tracking. Don't fire Knock-
        by-domain (would resolve nothing or stale data)."""
        html = (
            '<img src="https://resource.rentcafe.com/x.png">'
            '<script>doorway-api.knockrentals.com</script>'
        )
        assert not _should_try_knock_by_domain(html, "https://x.com")

    def test_utm_knock_alone_qualifies_when_no_rentcafe(self) -> None:
        """``utm_knock=`` on a non-RentCafe site is a Knock signal."""
        html = '<a href="https://x.com/apply?utm_knock=gmb">Apply</a>'
        assert _should_try_knock_by_domain(html, "https://x.com")

    def test_utm_knock_with_rentcafe_is_red_herring(self) -> None:
        """The 2026-05-20 probe confirmed 10X Iona Lakes and Main Street
        Square both have ``utm_knock=gmb`` URLs but inventory in RentCafe.
        Must NOT fire Knock-by-domain on those — would return wrong data."""
        html = (
            '<img src="https://resource.rentcafe.com/x.png">'
            '<a href="https://x.com/apply?utm_knock=gmb">Apply</a>'
        )
        assert not _should_try_knock_by_domain(html, "https://x.com")

    def test_no_signals_does_not_qualify(self) -> None:
        assert not _should_try_knock_by_domain(
            "<html><body>plain page</body></html>",
            "https://plain-property.com",
        )

    def test_empty_html_does_not_qualify_when_no_aspen(self) -> None:
        assert not _should_try_knock_by_domain("", "https://x.com")


# ── _fetch_knock_units_by_domain — httpx mocking ────────────────────────────


@dataclass
class _FakeResp:
    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        return self.payload


class _FakeClient:
    """Replaces ``httpx.AsyncClient`` for tests. ``responses`` is a
    URL-prefix → ``_FakeResp`` mapping; the longest matching prefix wins."""

    def __init__(self, responses: dict[str, _FakeResp]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def get(self, url: str, **_kw: object) -> _FakeResp:
        self.calls.append(url)
        # Longest-prefix match
        best = None
        for prefix, resp in self._responses.items():
            if url.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, resp)
        if best:
            return best[1]
        return _FakeResp(404, {})


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)


def _aspen_units_payload() -> dict[str, Any]:
    """Mimics the actual Adley at 72nd /units response shape from the HAR."""
    return {
        "units_data": {
            "buildings": [],
            "layouts": [
                {
                    "id": "lay-1",
                    "name": "1x1 MKT",
                    "bedrooms": 1,
                    "bathrooms": 1,
                    "area": 753,
                }
            ],
            "units": [
                {
                    "id": "u1",
                    "name": "F306",
                    "displayPrice": "1544",
                    "price": "1544",
                    "available": True,
                    "availableOn": "2026-05-06",
                    "area": 925,
                    "bedrooms": 2,
                    "bathrooms": 2,
                    "layoutId": "lay-1",
                    "buildingName": None,
                    "hidden": False,
                    "leased": False,
                    "occupied": False,
                    "reserved": False,
                },
                {
                    "id": "u2",
                    "name": "D119",
                    "displayPrice": "1325",
                    "price": "1325",
                    "available": True,
                    "availableOn": "2026-06-01",
                    "area": 753,
                    "bedrooms": 1,
                    "bathrooms": 1,
                    "layoutId": "lay-1",
                    "hidden": False,
                    "leased": False,
                    "occupied": False,
                    "reserved": False,
                },
            ],
        }
    }


@pytest.mark.asyncio
async def test_by_domain_resolver_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/v1/profile`` returns property_id → ``/v1/property/{id}/units``
    returns 2 units. Both calls 200, returns parsed unit dicts."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(
                200, {"profile": {"property": "2007584", "type": "prospect"}}
            ),
            "https://doorway-api.knockrentals.com/v1/property/2007584/units": _FakeResp(
                200, _aspen_units_payload()
            ),
        }
    )
    _patch_async_client(monkeypatch, fake)

    pid, units = await _fetch_knock_units_by_domain(
        "https://www.aspensquare.com/apartments/nebraska/papillion/adley-at-72nd"
    )
    assert pid == "2007584"
    assert len(units) == 2
    # Verify shape: real unit numbers, real rents, AVAILABLE status
    names = sorted(u["unit_number"] for u in units)
    assert names == ["D119", "F306"]
    rents = sorted(u["market_rent_low"] for u in units)
    assert rents == [1325, 1544]
    assert all(u["availability_status"] == "AVAILABLE" for u in units)
    assert all(u["extraction_tier"] == "TIER_1_KNOCK_API" for u in units)


@pytest.mark.asyncio
async def test_by_domain_resolver_profile_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile endpoint returns 404 → ``(None, [])``."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(404, {}),
        }
    )
    _patch_async_client(monkeypatch, fake)

    pid, units = await _fetch_knock_units_by_domain("https://x.com")
    assert pid is None
    assert units == []


@pytest.mark.asyncio
async def test_by_domain_resolver_profile_returns_no_property_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile endpoint returns 200 but missing ``property`` field →
    ``(None, [])``."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(
                200, {"profile": {"type": "prospect"}}  # no "property"
            ),
        }
    )
    _patch_async_client(monkeypatch, fake)

    pid, units = await _fetch_knock_units_by_domain("https://x.com")
    assert pid is None
    assert units == []


@pytest.mark.asyncio
async def test_by_domain_resolver_units_endpoint_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile resolves property_id but /units returns 500 →
    ``(pid_str, [])``. We surface the pid so the caller can record
    ``knock-by-domain: property_id=X /units returned no units``."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(
                200, {"profile": {"property": "999999"}}
            ),
            "https://doorway-api.knockrentals.com/v1/property/999999/units": _FakeResp(
                500, {}
            ),
        }
    )
    _patch_async_client(monkeypatch, fake)

    pid, units = await _fetch_knock_units_by_domain("https://x.com")
    assert pid == "999999"
    assert units == []


@pytest.mark.asyncio
async def test_by_domain_resolver_handles_network_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network exception during fetch → ``(None, [])`` (never raises)."""

    class _RaisingClient:
        async def __aenter__(self) -> _RaisingClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def get(self, url: str, **_kw: object) -> object:
            raise RuntimeError("simulated network error")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _RaisingClient())

    pid, units = await _fetch_knock_units_by_domain("https://x.com")
    assert pid is None
    assert units == []


# ── KnockAdapter.extract() — fallback wiring ────────────────────────────────


class _FakeFetchResult:
    def __init__(self, body: str = "") -> None:
        self.body = body


def _ctx(base_url: str, html: str = "") -> AdapterContext:
    """Build a minimal AdapterContext for adapter.extract() tests."""
    return AdapterContext(
        base_url=base_url,
        detected=DetectedPMS(pms="knock", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        fetch_result=_FakeFetchResult(html),
    )


@pytest.mark.asyncio
async def test_adapter_falls_back_to_by_domain_when_no_init_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aspen Square site: HTML has no knockDoorway.init() but URL pattern
    qualifies for the by-domain resolver. Adapter must dispatch and emit
    units with the BY_DOMAIN tier label."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(
                200, {"profile": {"property": "2007584"}}
            ),
            "https://doorway-api.knockrentals.com/v1/property/2007584/units": _FakeResp(
                200, _aspen_units_payload()
            ),
        }
    )
    _patch_async_client(monkeypatch, fake)

    ctx = _ctx(
        "https://www.aspensquare.com/apartments/nebraska/papillion/adley-at-72nd",
        html="<html><body>no init call here</body></html>",
    )
    adapter = KnockAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_KNOCK_API_BY_DOMAIN"
    assert len(result.units) == 2
    assert result.winning_url == (
        "https://doorway-api.knockrentals.com/v1/property/2007584/units"
    )
    assert result.confidence > 0.6


@pytest.mark.asyncio
async def test_adapter_prefers_init_call_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If knockDoorway.init() IS in the HTML, the existing community-
    keyed path runs and the by-domain fallback is never invoked."""
    # Mock the community-keyed path to return one unit.
    async def _fake_fetch_units(comm_id: str, kind: str = "community") -> list[dict[str, Any]]:
        return [
            {
                "unit_number": "101",
                "floor_plan_name": "A1",
                "bedrooms": "1",
                "bathrooms": "1",
                "sqft": "750",
                "market_rent_low": 1500,
                "market_rent_high": 1500,
                "availability_status": "AVAILABLE",
                "availability_date": "2026-06-01",
                "extraction_tier": "TIER_1_KNOCK_API",
            }
        ]

    monkeypatch.setattr(knock_mod, "_fetch_knock_units", _fake_fetch_units)

    # By-domain path must NOT fire — fail if it does.
    by_domain_calls: list[str] = []

    async def _no_by_domain(base_url: str) -> tuple[str | None, list[dict[str, Any]]]:
        by_domain_calls.append(base_url)
        return None, []

    monkeypatch.setattr(knock_mod, "_fetch_knock_units_by_domain", _no_by_domain)

    html = (
        '<script>knockDoorway.init('
        '"a8e311e98aee0ee4545fea9e01b06ac6","community","69e936e6567a11ef");'
        "</script>"
    )
    ctx = _ctx("https://flatiron.com/", html=html)
    adapter = KnockAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_KNOCK_API"  # init-path tier, not BY_DOMAIN
    assert len(result.units) == 1
    assert by_domain_calls == []  # confirmed by-domain was skipped


@pytest.mark.asyncio
async def test_adapter_no_init_no_signal_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No init call + no Knock signal + non-Aspen URL → no recovery
    fires, adapter returns empty + error message."""
    by_domain_calls: list[str] = []

    async def _no_call(base_url: str) -> tuple[str | None, list[dict[str, Any]]]:
        by_domain_calls.append(base_url)
        return None, []

    monkeypatch.setattr(knock_mod, "_fetch_knock_units_by_domain", _no_call)

    ctx = _ctx(
        "https://plain-property.com",
        html="<html><body>nothing knock-related here</body></html>",
    )
    adapter = KnockAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    assert result.units == []
    assert by_domain_calls == []  # signal gate prevented the call


@pytest.mark.asyncio
async def test_adapter_aspen_square_with_empty_html_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aspen Square URL pattern qualifies even when L1 fetch returned
    empty HTML (e.g. CF challenge shell). The by-domain resolver works
    on URL alone."""
    fake = _FakeClient(
        {
            "https://doorway-api.knockrentals.com/v1/profile": _FakeResp(
                200, {"profile": {"property": "2007584"}}
            ),
            "https://doorway-api.knockrentals.com/v1/property/2007584/units": _FakeResp(
                200, _aspen_units_payload()
            ),
        }
    )
    _patch_async_client(monkeypatch, fake)

    ctx = _ctx(
        "https://www.aspensquare.com/apartments/massachusetts/chicopee/edgewood-court",
        html="",  # empty body — CF challenge shell shape
    )
    adapter = KnockAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_KNOCK_API_BY_DOMAIN"
    assert len(result.units) == 2
