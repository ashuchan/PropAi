"""LeaseLabs `.floorplan-block` .aspx recovery via the CWS GetUnits fallback (#85).

These properties are DETECTED realpage_oll but expose no OLL/units API in the
captured responses, so realpage_oll returned empty (FAILED_NO_DATA). Their unit
roster is served by the property-hosted CWS `GetUnits` proxy — the same static
JSON endpoint + parser realpage_cws already uses. `_try_cws_getunits` is the
additive fallback that reaches it; live-verified on Sierra Verde (15 units) and
Meadowcrest (19), whose real GetUnits JSON is captured under
ma_poc/tests/fixtures/realpage_cws_getunits/.

The load-bearing property is that the fallback is ADDITIVE: it only runs when the
OLL API path found nothing, and returns None on any failure, so it can never
remove a row a working OLL property already had.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from ma_poc.pms.adapters.realpage_cws import parse_realpage_cws_getunits
from ma_poc.pms.adapters.realpage_oll import RealPageOllAdapter

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "realpage_cws_getunits"


class _Resp:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status


def _ctx(base: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(base_url=base, fetch_result=None, property_id="P1")


class TestParserOnRealFixtures:
    """Pin the parser to the two real GetUnits bodies."""

    @pytest.mark.parametrize(("stem", "n_units"), [("sierra_verde", 15), ("meadowcrest", 19)])
    def test_unit_count(self, stem: str, n_units: int) -> None:
        body = (_FIX / f"{stem}.json").read_text(encoding="utf-8")
        rows = parse_realpage_cws_getunits(body, "https://x/CmsSiteManager/callback.aspx")
        assert len(rows) == n_units

    def test_rows_are_unit_level_with_rent(self) -> None:
        body = (_FIX / "sierra_verde.json").read_text(encoding="utf-8")
        rows = parse_realpage_cws_getunits(body, "https://x")
        assert all(r.get("unit_number") for r in rows)
        assert any(r.get("market_rent_low") for r in rows)


class TestOllFallbackRecovers:
    def test_fallback_returns_units_on_a_leaselabs_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.config.feature_flags as ff
        import ma_poc.pms.adapters._probe as probe_mod

        body = (_FIX / "sierra_verde.json").read_text(encoding="utf-8")
        monkeypatch.setattr(probe_mod, "probe_get", lambda url, **k: _Resp(body))
        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: True)

        res = asyncio.run(
            RealPageOllAdapter()._try_cws_getunits(_ctx("https://www.sierraverdeapts.com/"))
        )
        assert res is not None
        assert len(res.units) >= 10
        assert res.tier_used == "TIER_1_API_REALPAGE_CWS_UNITS"
        assert res.winning_url and "callback.aspx" in res.winning_url

    def test_uses_final_url_over_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The post-redirect host is authoritative for the GetUnits path."""
        import ma_poc.config.feature_flags as ff
        import ma_poc.pms.adapters._probe as probe_mod

        seen: dict[str, str] = {}

        def _spy(url: str, **k: object) -> _Resp:
            seen["url"] = url
            return _Resp((_FIX / "meadowcrest.json").read_text(encoding="utf-8"))

        monkeypatch.setattr(probe_mod, "probe_get", _spy)
        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: True)
        ctx = types.SimpleNamespace(
            base_url="https://old-host.com/",
            fetch_result=types.SimpleNamespace(final_url="https://www.meadowcrestapartments.com/"),
            property_id="P1",
        )
        res = asyncio.run(RealPageOllAdapter()._try_cws_getunits(ctx))
        assert res is not None
        assert "meadowcrestapartments.com" in seen["url"]


class TestFallbackIsSafe:
    def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.config.feature_flags as ff

        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: False)
        assert asyncio.run(RealPageOllAdapter()._try_cws_getunits(_ctx("https://x.com/"))) is None

    def test_zero_available_units_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.config.feature_flags as ff
        import ma_poc.pms.adapters._probe as probe_mod

        monkeypatch.setattr(probe_mod, "probe_get", lambda url, **k: _Resp("{}"))
        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: True)
        assert asyncio.run(RealPageOllAdapter()._try_cws_getunits(_ctx("https://x.com/"))) is None

    def test_no_base_url_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.config.feature_flags as ff

        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: True)
        assert asyncio.run(RealPageOllAdapter()._try_cws_getunits(_ctx(""))) is None

    def test_probe_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.config.feature_flags as ff
        import ma_poc.pms.adapters._probe as probe_mod

        def _boom(url: str, **k: object) -> _Resp:
            raise RuntimeError("network down")

        monkeypatch.setattr(probe_mod, "probe_get", _boom)
        monkeypatch.setattr(ff, "enable_cws_getunits", lambda: True)
        assert asyncio.run(RealPageOllAdapter()._try_cws_getunits(_ctx("https://x.com/"))) is None
