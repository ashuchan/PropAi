"""Rently scattered-site portal recovery (#89).

A property whose site redirects to ``u{ID}.rently.com`` exposes its roster at
``secure.rently.com/api/properties/searchQuery?managerID={ID}`` — a code-only
JSON endpoint. These tests pin the parser to the real captured Jodeco Landing
body (5 homes) and cover the recovery net's host detection + safety.

Each entry is a scattered single-family home, so the street ADDRESS is the
identity (``unit_name``, per #29), not a unit number.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from ma_poc.pms.adapters.rently import (
    _ready_date_iso,
    parse_rently_search,
    recover_rently,
    rently_manager_id,
    rently_search_url,
)

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "rently" / "jodeco_searchQuery.json"


def _body() -> str:
    return _FIX.read_text(encoding="utf-8")


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200


class TestParser:
    def test_all_homes_parsed(self) -> None:
        rows = parse_rently_search(_body(), "https://x")
        assert len(rows) == 5

    def test_address_is_the_identity(self) -> None:
        rows = parse_rently_search(_body(), "https://x")
        r = next(x for x in rows if "Fiery Warbler" in str(x.get("unit_name")))
        assert r["unit_name"] == "509 Fiery Warbler St, Mcdonough, Georgia 30253"
        assert r["unit_number"] == ""  # scattered-site: no unit number
        assert (r.get("source_ids") or {}).get("rently_id") == "4185592"
        assert (r.get("source_ids") or {}).get("rently_full_address")

    def test_floorplan_fields(self) -> None:
        r = next(x for x in parse_rently_search(_body(), "x") if "Fiery Warbler" in str(x.get("unit_name")))
        assert str(r["bedrooms"]) == "4"        # 4.0 -> "4"
        assert str(r["bathrooms"]) == "2.5"
        assert str(r["sqft"]) == "2228"
        assert r["market_rent_low"] == 2975
        assert r["availability_date"] == "2026-08-18"

    def test_now_yields_no_invented_date(self) -> None:
        rows = parse_rently_search(_body(), "x")
        avail_now = [r for r in rows if "Verdant Crane" in str(r.get("unit_name"))]
        assert avail_now and avail_now[0]["availability_date"] == ""

    # ---- negatives ----
    def test_no_property_data_returns_empty(self) -> None:
        assert parse_rently_search('{"other": []}', "x") == []

    def test_malformed_json_returns_empty(self) -> None:
        assert parse_rently_search('{"property_data": [broken', "x") == []

    def test_empty_returns_empty(self) -> None:
        assert parse_rently_search("", "x") == []


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "mid"),
        [
            ("https://u62564.rently.com/propertiesSearch2", "62564"),
            ("http://U12.rently.com/x", "12"),
            ("https://www.jodecolandingga.com/", None),
            ("https://secure.rently.com/api", None),  # not a u{id} host
        ],
    )
    def test_manager_id(self, url: str, mid: str | None) -> None:
        assert rently_manager_id(url) == mid

    def test_search_url(self) -> None:
        assert rently_search_url("62564") == (
            "https://secure.rently.com/api/properties/searchQuery?pc=1&managerID=62564"
        )

    @pytest.mark.parametrize(
        ("raw", "iso"),
        [("Aug 18, 2026", "2026-08-18"), ("Jul 31, 2026", "2026-07-31"),
         ("Now", ""), ("Available Now", ""), ("", ""), ("garbage", "")],
    )
    def test_ready_date_iso(self, raw: str, iso: str) -> None:
        assert _ready_date_iso(raw) == iso


class TestRecoveryNet:
    def _ctx(self, final_url: str = "", base_url: str = "", body: object = None) -> types.SimpleNamespace:
        fr = types.SimpleNamespace(final_url=final_url, body=body)
        return types.SimpleNamespace(fetch_result=fr, base_url=base_url)

    def test_recovers_from_final_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.pms.adapters._probe as probe_mod

        monkeypatch.setattr(probe_mod, "probe_get", lambda url, **k: _Resp(_body()))
        units = asyncio.run(recover_rently(self._ctx(final_url="https://u62564.rently.com/propertiesSearch2")))
        assert len(units) == 5

    def test_recovers_from_body_when_redirect_is_client_side(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.pms.adapters._probe as probe_mod

        monkeypatch.setattr(probe_mod, "probe_get", lambda url, **k: _Resp(_body()))
        ctx = self._ctx(
            final_url="https://www.jodecolandingga.com/",
            body='<meta http-equiv="refresh" content="0;url=https://u62564.rently.com/propertiesSearch2">',
        )
        units = asyncio.run(recover_rently(ctx))
        assert len(units) == 5

    def test_non_rently_property_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.pms.adapters._probe as probe_mod

        # probe should never be called; if it is, it would still be a non-rently host
        monkeypatch.setattr(probe_mod, "probe_get", lambda url, **k: _Resp("{}"))
        units = asyncio.run(recover_rently(self._ctx(final_url="https://www.example.com/")))
        assert units == []

    def test_probe_error_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ma_poc.pms.adapters._probe as probe_mod

        def _boom(url: str, **k: object) -> _Resp:
            raise RuntimeError("down")

        monkeypatch.setattr(probe_mod, "probe_get", _boom)
        units = asyncio.run(recover_rently(self._ctx(final_url="https://u62564.rently.com/")))
        assert units == []
