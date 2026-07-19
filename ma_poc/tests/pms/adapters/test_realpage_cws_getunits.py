"""RealPage CWS GetUnits unit-level path tests (2026-07-19, gap #3).

Pins the flag-gated ``/CmsSiteManager/callback.aspx?act=Proxy/GetUnits`` path
that upgrades CWS from plan-level DOM to unit-level — refuting the adapter's
original "CWS doesn't publish a per-unit roster publicly" assumption.

Fixtures are REAL ``available=true`` GetUnits bodies captured 2026-07-19:
  huntingtonwoods (4 available units) · capitalplace (4 available units)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.realpage_cws import (
    RealPageCwsAdapter,
    _cws_avail_date,
    cws_getunits_url,
    parse_realpage_cws_getunits,
)
from ma_poc.pms.detector import detect_pms

FIX = Path(__file__).parent / "fixtures" / "realpage_cws"


def _body(name: str) -> str:
    return (FIX / f"getunits_{name}.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_realpage_cws_getunits — real fixtures
# ---------------------------------------------------------------------------


def test_parse_huntingtonwoods_4_units() -> None:
    rows = parse_realpage_cws_getunits(_body("huntingtonwoods_avail"), "u")
    assert len(rows) == 4
    assert all(r["unit_number"] for r in rows)
    assert all(r["market_rent_low"] for r in rows)
    assert all(r["sqft"] for r in rows)
    first = next(r for r in rows if r["unit_number"] == "0204")
    assert first["market_rent_low"] == 1360
    assert first["sqft"] == "698"
    assert first["floor_plan_name"] == "A"
    assert first["availability_status"] == "AVAILABLE"
    assert first["availability_date"] == "2026-06-02"
    assert first["extraction_tier"] == "TIER_1_API_REALPAGE_CWS_UNITS"
    assert first["source_ids"]["realpage_cws_unit_id"] == 3239983


def test_parse_capitalplace_4_units() -> None:
    rows = parse_realpage_cws_getunits(_body("capitalplace_avail"), "u")
    assert len(rows) == 4
    assert all(r["availability_status"] == "AVAILABLE" for r in rows)


def test_parse_non_json_returns_empty() -> None:
    assert parse_realpage_cws_getunits("<html>not json</html>", "u") == []


def test_parse_no_units_key_returns_empty() -> None:
    assert parse_realpage_cws_getunits('{"other": 1}', "u") == []


def test_parse_skips_units_without_number() -> None:
    body = '{"units":[{"rent":1000,"squareFeet":500},{"unitNumber":"12A","rent":1200,"squareFeet":600}]}'
    rows = parse_realpage_cws_getunits(body, "u")
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "12A"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_cws_getunits_url_builds_property_hosted_endpoint() -> None:
    u = cws_getunits_url("https://www.foo.com/Floor-Plans.aspx")
    assert u == (
        "https://www.foo.com/CmsSiteManager/callback.aspx"
        "?act=Proxy/GetUnits&available=true&honordisplayorder=true"
    )


@pytest.mark.parametrize("bad", ["", "not a url", "ftp://x", "/relative/path"])
def test_cws_getunits_url_none_on_bad_input(bad: str) -> None:
    assert cws_getunits_url(bad) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-02 00:00 -0500", "2026-06-02"),
        ("2026-06-02", "2026-06-02"),
        ("", ""),
        ("garbage", ""),
        (None, ""),
        (12345, ""),
    ],
)
def test_cws_avail_date(raw: object, expected: str) -> None:
    assert _cws_avail_date(raw) == expected


# ---------------------------------------------------------------------------
# adapter integration (flag-gated)
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, payload: object, url: str = "https://x.test/Floor-Plans.aspx") -> None:
        self._payload = payload
        self.url = url

    async def evaluate(self, _js: str) -> object:
        return self._payload


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200


def _ctx(base: str = "https://www.huntingtonwoodsapts.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base,
        detected=detect_pms(base),
        profile=None,
        expected_total_units=None,
        property_id="P_CWS",
    )


@pytest.mark.asyncio
async def test_adapter_getunits_unit_level_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CWS_GETUNITS", "true")
    body = _body("huntingtonwoods_avail")
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _Resp(body),
    )
    # DOM payload would be a plan-level bail — GetUnits must win first.
    result = await RealPageCwsAdapter().extract(
        _FakePage({"ok": False, "reason": "n/a"}), _ctx()
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_REALPAGE_CWS_UNITS"
    assert len(result.units) == 4
    assert result.confidence >= 0.7
    assert result.winning_url.endswith("GetUnits&available=true&honordisplayorder=true")


@pytest.mark.asyncio
async def test_adapter_falls_through_when_getunits_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CWS_GETUNITS", "true")
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        lambda url, timeout=None, unlocker=None: _Resp('{"units":[]}'),
    )
    # 0 available units → fall through to the DOM path (here a plan-level bail).
    result = await RealPageCwsAdapter().extract(
        _FakePage({"ok": False, "reason": "no cards"}), _ctx()
    )  # type: ignore[arg-type]
    assert result.tier_used != "TIER_1_API_REALPAGE_CWS_UNITS"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_adapter_skips_getunits_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_CWS_GETUNITS", "false")

    def _boom(url, timeout=None, unlocker=None):  # noqa: ANN001
        raise AssertionError("probe_get must not be called when flag is off")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _boom)
    # Flag off → GetUnits never attempted → existing DOM path runs unchanged.
    result = await RealPageCwsAdapter().extract(
        _FakePage({"ok": False, "reason": "no cards"}), _ctx()
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_REALPAGE_CWS"
    assert result.confidence == 0.0
