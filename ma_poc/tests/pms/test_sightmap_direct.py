"""SightMap direct raw-GET shortcut (task #21, warm=fast).

Pins: (1) endpoint helper reads the full URL from llm_field_mappings and ignores
non-sightmap; (2) fires only under the flag + WARM/HOT + a stored API URL;
(3) on a priced roster returns a complete result (TIER_1_API_SIGHTMAP_DIRECT, HB
tier, real unit#); (4) CONTENT GUARD — a null-price payload (Entrata-hint props)
falls through to None; (5) never-raise — flag-off / COLD / no-url / non-200 all
fall through. hb_raw_get is patched; the REAL parse_sightmap_payload runs.
"""

from __future__ import annotations

import json

import pytest

from ma_poc.pms.sightmap_direct import sightmap_api_url, try_sightmap_direct

_URL = "https://sightmap.com/app/api/v1/8epmg884p6d/sightmaps/41123"


def _payload(price):
    return json.dumps(
        {
            "data": {
                "floor_plans": [
                    {"id": "fp1", "name": "A1", "bedrooms": 1, "bathrooms": 1, "area": 700}
                ],
                "units": [
                    {"unit_number": "101", "floor_plan_id": "fp1", "price": price},
                    {"unit_number": "102", "floor_plan_id": "fp1", "price": price},
                ],
            }
        }
    )


class _Map:
    def __init__(self, url):
        self.api_url_pattern = url


class _Api:
    def __init__(self, url):
        self.llm_field_mappings = [_Map(url)] if url else []
        self.known_endpoints = []


class _Conf:
    def __init__(self, m):
        self.maturity = m


class _Profile:
    def __init__(self, maturity="WARM", url="sightmap.com/app/api/v1/8epmg884p6d/sightmaps/41123"):
        self.api_hints = _Api(url)
        self.confidence = _Conf(maturity)


class _Task:
    property_id = "77595"
    url = "https://ovationco.com/property/inspire/"


def _patch(monkeypatch, *, flag=True, raw=None):
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_SIGHTMAP_DIRECT_GET", flag)

    async def _fake_hb(url, pid="?", **kw):
        return raw if raw is not None else (200, _payload(1500))

    monkeypatch.setattr("ma_poc.fetch.hyperbrowser_backend.hb_raw_get", _fake_hb)


def test_endpoint_helper() -> None:
    assert sightmap_api_url(_Profile()) == _URL  # prepends https://
    assert sightmap_api_url(None) is None
    assert sightmap_api_url(_Profile(url="https://x.com/api/units")) is None
    assert sightmap_api_url(_Profile(url="")) is None


@pytest.mark.asyncio
async def test_flag_off(monkeypatch) -> None:
    _patch(monkeypatch, flag=False)
    assert await try_sightmap_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_cold(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_sightmap_direct(_Task(), _Profile(maturity="COLD"), None) is None


@pytest.mark.asyncio
async def test_no_url(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_sightmap_direct(_Task(), _Profile(url=""), None) is None


@pytest.mark.asyncio
async def test_happy_path_priced(monkeypatch) -> None:
    from ma_poc.fetch.contracts import FetchOutcome, RenderMode
    from ma_poc.models.fetch_tier import FetchTier

    _patch(monkeypatch, raw=(200, _payload(1500)))
    sm = await try_sightmap_direct(_Task(), _Profile(), None)
    assert sm is not None and set(sm) == {"fetch_result", "result"}
    fr, result = sm["fetch_result"], sm["result"]
    assert fr.outcome == FetchOutcome.OK and fr.render_mode == RenderMode.GET
    assert fr.fetch_tier_used == int(FetchTier.HYPERBROWSER)
    assert result["extraction_tier_used"] == "TIER_1_API_SIGHTMAP_DIRECT"
    assert len(result["units"]) == 2
    assert result["_extract_result"].adapter_name == "sightmap"


@pytest.mark.asyncio
async def test_content_guard_null_price_falls_through(monkeypatch) -> None:
    # Entrata-hint / operator-suppressed: units parse but 0 have rent → None.
    _patch(monkeypatch, raw=(200, _payload(None)))
    assert await try_sightmap_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_non_200_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, raw=(403, ""))
    assert await try_sightmap_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_unparseable_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, raw=(200, "<html>not json</html>"))
    assert await try_sightmap_direct(_Task(), _Profile(), None) is None
