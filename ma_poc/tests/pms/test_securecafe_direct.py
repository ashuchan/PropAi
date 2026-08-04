"""RentCafe/SecureCafe direct raw-GET shortcut (task #21, warm=fast).

Pins: (1) the URL helper finds the securecafe availableunits.aspx winning_url and
ignores non-securecafe; (2) fires only under the flag + WARM/HOT + a stored URL +
a non-empty roster; (3) on success returns a complete result dict (real units,
TIER_1_RENTCAFE_SECURECAFE_DIRECT, HB fetch tier) used in place of scrape_jugnu;
(4) never-raise — flag-off / COLD / no-url / non-200 / empty / 0-units all fall
through to None (→ render). No network: hb_raw_get + the parser are patched.
"""

from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from ma_poc.pms.securecafe_direct import securecafe_availableunits_url, try_rentcafe_direct

_URL = "https://grantparkvillage.securecafe.com/onlineleasing/grant-park-village/availableunits.aspx"
_UNITS = [
    {"unit_number": "0411", "market_rent_low": 1361, "rent_range": "$1,361 - $3,180"},
    {"unit_number": "0511", "market_rent_low": 1400, "rent_range": "$1,400 - $3,200"},
]


class _Nav:
    def __init__(self, url):
        self.winning_page_url = url


class _Conf:
    def __init__(self, m):
        self.maturity = m


class _Profile:
    def __init__(self, maturity="WARM", url=_URL):
        self.navigation = _Nav(url)
        self.confidence = _Conf(maturity)


class _Task:
    property_id = "61377"
    url = "https://www.grantparkvillage.com/"


class _TimberTask:
    property_id = "98191"
    url = "https://www.thebeverlycollectionapts.com/timber/"


def _patch(monkeypatch, *, flag=True, raw=(200, "<html>rows</html>"), units=None):
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_RENTCAFE_DIRECT_GET", flag)

    async def _fake_hb(url, pid="?", **kw):
        return raw

    monkeypatch.setattr("ma_poc.fetch.hyperbrowser_backend.hb_raw_get", _fake_hb)
    monkeypatch.setattr(
        "ma_poc.pms.adapters.rentcafe.parse_securecafe_availableunits",
        lambda html, url: _UNITS if units is None else units,
    )


def test_url_helper() -> None:
    assert securecafe_availableunits_url(_Profile()) == _URL
    assert securecafe_availableunits_url(None) is None
    assert securecafe_availableunits_url(_Profile(url="https://x.com/floorplans")) is None
    # securecafe but not availableunits → not the unit surface
    assert (
        securecafe_availableunits_url(
            _Profile(url="https://x.securecafe.com/onlineleasing/y/floorplans.aspx")
        )
        is None
    )


@pytest.mark.asyncio
async def test_flag_off_returns_none(monkeypatch) -> None:
    _patch(monkeypatch, flag=False)
    assert await try_rentcafe_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_cold_returns_none(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_rentcafe_direct(_Task(), _Profile(maturity="COLD"), None) is None


@pytest.mark.asyncio
async def test_no_url_returns_none(monkeypatch) -> None:
    _patch(monkeypatch)
    assert await try_rentcafe_direct(_Task(), _Profile(url="https://x.com/"), None) is None


@pytest.mark.asyncio
async def test_happy_path(monkeypatch, tmp_path) -> None:
    from ma_poc.fetch.contracts import FetchOutcome, RenderMode
    from ma_poc.models.fetch_tier import FetchTier

    _patch(monkeypatch)
    rc = await try_rentcafe_direct(_Task(), _Profile(), None)
    assert rc is not None and set(rc) == {"fetch_result", "result"}

    fr = rc["fetch_result"]
    assert fr.outcome == FetchOutcome.OK
    assert fr.render_mode == RenderMode.GET  # NOT a render
    assert fr.fetch_tier_used == int(FetchTier.HYPERBROWSER)
    assert fr.response_sha256 == hashlib.sha256(b"<html>rows</html>").hexdigest()

    result = rc["result"]
    assert result["extraction_tier_used"] == "TIER_1_RENTCAFE_SECURECAFE_DIRECT"
    assert [u["unit_number"] for u in result["units"]] == ["0411", "0511"]
    body_hash = hashlib.sha256(b"<html>rows</html>").hexdigest()
    assert result["_raw_html_responses"] == [
        {
            "url": _URL,
            "status": 200,
            "body": "<html>rows</html>",
            "content_type": "text/html",
            "response_kind": "unit_roster",
            "via": "securecafe_direct",
            "identity": {
                "status": "CONFIGURED_WARM_ROUTE",
                "configured_property_id": "61377",
                "requested_url": _URL,
                "final_url": _URL,
            },
        }
    ]
    assert result["_unit_source_provenance"] == [
        {
            "provider": "rentcafe",
            "response_kind": "unit_roster",
            "source_url": _URL,
            "response_status": 200,
            "response_sha256": body_hash,
            "unit_count": 2,
            "identity": {
                "status": "CONFIGURED_WARM_ROUTE",
                "configured_property_id": "61377",
                "requested_url": _URL,
                "final_url": _URL,
            },
        }
    ]
    assert all(unit["source_response_sha256"] == body_hash for unit in result["units"])
    assert all(unit["source_response_url"] == _URL for unit in result["units"])
    assert result["units"][0]["source_record_locator"] == "availableunits.aspx[unit_number=0411]"

    # Exercise the actual runner archive boundary, not just the shortcut's
    # in-memory fields.  The manifest hash and decompressed body must both map
    # back to the exact unit-producing SecureCafe response.
    from ma_poc.scripts.runners.jugnu import _archive_raw_source_responses

    archive = _archive_raw_source_responses(
        result,
        tmp_path,
        "61377",
        result["extraction_tier_used"],
    )
    assert archive is not None
    assert archive["source_count"] == 1
    assert archive["source_kind_counts"] == {"api": 0, "html": 1, "asset": 0}
    manifest_path = tmp_path / archive["manifest_path"]
    with gzip.open(manifest_path, "rt", encoding="utf-8") as handle:
        manifest = json.load(handle)
    response = manifest["responses"][0]
    assert response["source_response_sha256"] == body_hash
    assert response["source_url"] == _URL
    archived_body_path = tmp_path / response["archive_body_path"]
    with gzip.open(archived_body_path, "rb") as handle:
        archived_body = handle.read()
    assert archived_body == b"<html>rows</html>"
    assert hashlib.sha256(archived_body).hexdigest() == body_hash
    er = result["_extract_result"]
    assert er.tier_used == "TIER_1_RENTCAFE_SECURECAFE_DIRECT" and er.adapter_name == "rentcafe"


@pytest.mark.asyncio
async def test_non_200_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, raw=(403, ""))
    assert await try_rentcafe_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_zero_units_falls_through(monkeypatch) -> None:
    _patch(monkeypatch, units=[])
    assert await try_rentcafe_direct(_Task(), _Profile(), None) is None


@pytest.mark.asyncio
async def test_timber_direct_path_filters_sibling_communities(monkeypatch) -> None:
    _patch(
        monkeypatch,
        units=[
            {"unit_id": "324", "unit_number": "324", "floor_plan_name": "Timber | A01"},
            {"unit_id": "240", "unit_number": "240", "floor_plan_name": "Meredith House | B2"},
            {"unit_id": "321", "unit_number": "321", "floor_plan_name": "Platform | A + Den"},
        ],
    )

    direct = await try_rentcafe_direct(_TimberTask(), _Profile(), None)

    assert direct is not None
    assert [row["unit_number"] for row in direct["result"]["units"]] == ["324"]
    assert direct["result"]["_extract_result"].records is direct["result"]["units"]
    assert any("COLLECTION_PROPERTY_SCOPE_FINAL_APPLIED" in error for error in direct["result"]["errors"])


@pytest.mark.asyncio
async def test_timber_direct_path_falls_through_when_only_siblings_remain(monkeypatch) -> None:
    _patch(
        monkeypatch,
        units=[
            {"unit_id": "240", "unit_number": "240", "floor_plan_name": "Meredith House | B2"},
        ],
    )

    assert await try_rentcafe_direct(_TimberTask(), _Profile(), None) is None
