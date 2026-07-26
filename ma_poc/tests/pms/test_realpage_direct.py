"""RealPage CWS direct raw-GET shortcut (task #21, warm=fast).

Pins: (1) is_realpage_cws detects an api.ws.realpage.com endpoint and EXCLUDES
OLL; (2) fires only under the flag + WARM/HOT + CWS signal; (3) on success returns
a complete result (TIER_1_REALPAGE_CWS_DIRECT, HB tier); (4) never-raise —
flag-off / COLD / OLL / non-200 / no-units all fall through. hb_raw_get +
_probe_realpage_cws are patched.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.realpage_direct import is_realpage_cws, try_realpage_direct

_CWS_EP = "https://api.ws.realpage.com/v2/property/6053984/floorplans"
_OLL_EP = "https://myleasestar.onlineleasing.realpage.com/..."
_UNITS = [{"unit_number": "1525", "market_rent_low": 1229, "availability_status": "AVAILABLE"}]


class _Ep:
    def __init__(self, url):
        self.url_pattern = url


class _Api:
    def __init__(self, url):
        self.known_endpoints = [_Ep(url)] if url else []
        self.llm_field_mappings = []


class _Nav:
    def __init__(self, winning=_CWS_EP):
        self.entry_url = "https://crestlavalencia.net/"
        self.winning_page_url = winning


class _Conf:
    def __init__(self, m):
        self.maturity = m


class _Profile:
    def __init__(self, maturity="WARM", ep=_CWS_EP):
        self.api_hints = _Api(ep)
        # keep winning_url consistent with the endpoint so a non-CWS ep is a
        # genuinely non-CWS profile (the helper also checks winning_page_url).
        self.navigation = _Nav(winning=ep)
        self.confidence = _Conf(maturity)


class _OLLProfile:
    def __init__(self):
        self.api_hints = _Api(_OLL_EP)
        self.navigation = _Nav(winning=_OLL_EP)
        self.confidence = _Conf("WARM")


class _Task:
    property_id = "12914"
    url = "https://crestlavalencia.net/"


def _patch(monkeypatch, *, flag=True, status=200, html="<html>RPFP_config apiKey</html>", units=None):
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_REALPAGE_DIRECT_GET", flag)

    async def _fake_hb(url, pid="?", **kw):
        return (status, html)

    async def _fake_probe(h):
        return _UNITS if units is None else units

    monkeypatch.setattr("ma_poc.fetch.hyperbrowser_backend.hb_raw_get", _fake_hb)
    monkeypatch.setattr("ma_poc.pms.adapters.generic._probe_realpage_cws", _fake_probe)


def test_is_realpage_cws() -> None:
    assert is_realpage_cws(_Profile()) is True
    assert is_realpage_cws(None) is False
    # OLL is NOT CWS (winning_url + endpoint are onlineleasing.realpage.com)
    assert is_realpage_cws(_OLLProfile()) is False
    assert is_realpage_cws(_Profile(ep="https://x.com/api")) is False


@pytest.mark.asyncio
async def test_flag_off(monkeypatch) -> None:
    _patch(monkeypatch, flag=False)
    assert await try_realpage_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_cold(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_realpage_direct(_Task(), _Profile(maturity="COLD"), None) is None


@pytest.mark.asyncio
async def test_oll_excluded(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_realpage_direct(_Task(), _OLLProfile(), None) is None


@pytest.mark.asyncio
async def test_happy_path(monkeypatch) -> None:
    from ma_poc.fetch.contracts import FetchOutcome, RenderMode
    from ma_poc.models.fetch_tier import FetchTier

    _patch(monkeypatch)
    rp = await try_realpage_direct(_Task(), _Profile(), None)
    assert rp is not None and set(rp) == {"fetch_result", "result"}
    fr, result = rp["fetch_result"], rp["result"]
    assert fr.outcome == FetchOutcome.OK and fr.render_mode == RenderMode.GET
    assert fr.fetch_tier_used == int(FetchTier.HYPERBROWSER)
    assert result["extraction_tier_used"] == "TIER_1_REALPAGE_CWS_DIRECT"
    assert result["units"][0]["unit_number"] == "1525"
    assert result["_extract_result"].adapter_name == "realpage"


@pytest.mark.asyncio
async def test_page_non_200_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, status=403, html="")
    assert await try_realpage_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_no_units_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, units=[])
    assert await try_realpage_direct(_Task(), _Profile(), None) is None
