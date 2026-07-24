"""Direct-GET shortcut for WARM Knock profiles (task #21, warm=fast).

Pins: (1) the endpoint helper finds the doorway /units URL and ignores non-Knock
endpoints; (2) fires only under the flag + WARM/HOT + a stored endpoint + a
non-empty roster; (3) on success returns a drop-in FetchResult whose network_log
carries the JSON (RenderMode.GET, OK, DIRECT tier) so the render is skipped;
(4) never-raise — flag-off / COLD / no-endpoint / non-200 / empty / unparseable /
no-units all fall through to None (→ normal render path). No network: probe_get
is patched.
"""

from __future__ import annotations

import json

import pytest

from ma_poc.pms.knock_direct import knock_units_endpoint, try_knock_direct

_EP = "https://doorway-api.knockrentals.com/v1/property/2021296/units"
_UNITS_JSON = json.dumps(
    {
        "units_data": {
            "units": [
                {"name": "101", "price": "1500", "bedrooms": 1, "area": 700, "available": True},
                {"name": "102", "price": "1600", "bedrooms": 2, "area": 900, "available": True},
            ]
        }
    }
)


class _Ep:
    def __init__(self, url: str) -> None:
        self.url_pattern = url


class _Api:
    def __init__(self, eps: list) -> None:
        self.known_endpoints = eps


class _Conf:
    def __init__(self, m: str) -> None:
        self.maturity = m


class _Profile:
    def __init__(self, maturity: str = "WARM", eps: list | None = None) -> None:
        self.api_hints = _Api(eps if eps is not None else [_Ep(_EP)])
        self.confidence = _Conf(maturity)


class _Task:
    property_id = "281928"
    url = "https://www.mosbycitrusridge.com/"


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


def _patch_probe(monkeypatch, resp=None, raises=None) -> None:
    def _pg(url, *, unlocker=True, **kw):
        if raises:
            raise raises
        return resp

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _pg)


def test_endpoint_helper_finds_units_url() -> None:
    assert knock_units_endpoint(_Profile()) == _EP
    assert knock_units_endpoint(_Profile(eps=[])) is None
    assert knock_units_endpoint(None) is None
    # a non-Knock endpoint is ignored (host + /units both required)
    assert knock_units_endpoint(_Profile(eps=[_Ep("https://x.com/api/units")])) is None
    # dict-shaped endpoint entry also works
    assert knock_units_endpoint(_Profile(eps=[{"url_pattern": _EP}])) == _EP


@pytest.mark.asyncio
async def test_flag_off_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", False)
    _patch_probe(monkeypatch, _Resp(200, _UNITS_JSON))
    assert await try_knock_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_cold_profile_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(200, _UNITS_JSON))
    assert await try_knock_direct(_Task(), _Profile(maturity="COLD"), None) is None


@pytest.mark.asyncio
async def test_no_endpoint_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(200, _UNITS_JSON))
    assert await try_knock_direct(_Task(), _Profile(eps=[]), None) is None


@pytest.mark.asyncio
async def test_happy_path_builds_result_with_real_unit_ids(monkeypatch) -> None:
    from ma_poc.fetch.contracts import FetchOutcome, RenderMode
    from ma_poc.models.fetch_tier import FetchTier

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(200, _UNITS_JSON))
    kd = await try_knock_direct(_Task(), _Profile(), None)
    assert kd is not None and set(kd) == {"fetch_result", "result"}

    fr = kd["fetch_result"]
    assert fr.outcome == FetchOutcome.OK
    assert fr.render_mode == RenderMode.GET  # NOT a render
    assert fr.fetch_tier_used == int(FetchTier.DIRECT)
    assert fr.final_url == _EP

    result = kd["result"]
    # canonical parser → REAL unit numbers (name), NOT synthetic inferred_* ids
    assert result["extraction_tier_used"] == "TIER_1_KNOCK_API_DIRECT"
    unit_ids = [str(u.get("unit_number")) for u in result["units"]]
    assert unit_ids == ["101", "102"], f"expected real unit numbers, got {unit_ids}"
    assert not any(str(u.get("unit_number", "")).startswith("inferred") for u in result["units"])
    # rent carried through (gold = unit + rent)
    assert all(u.get("market_rent_low") for u in result["units"])
    er = result["_extract_result"]
    assert er.tier_used == "TIER_1_KNOCK_API_DIRECT" and er.adapter_name == "knock"


@pytest.mark.asyncio
async def test_200_no_units_falls_through(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(200, json.dumps({"units_data": {"units": []}})))
    assert await try_knock_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_non_200_falls_through(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(403, ""))
    assert await try_knock_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_unparseable_body_falls_through(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, _Resp(200, "<html>not json</html>"))
    assert await try_knock_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_probe_raises_falls_through(monkeypatch) -> None:
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_KNOCK_DIRECT_GET", True)
    _patch_probe(monkeypatch, raises=RuntimeError("network died"))
    assert await try_knock_direct(_Task(), _Profile(), None) is None
