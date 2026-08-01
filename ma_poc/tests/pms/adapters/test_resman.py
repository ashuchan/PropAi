"""ResMan adapter tests — ``Portal/Applicants/Availability`` ``var unitTypes``.

History (read before "restoring" anything)
------------------------------------------
Two ResMan adapters were written independently and both landed as
``ma_poc/pms/adapters/resman.py``:

  * 78b298f (2026-05-17, canary iter10) — fetches the public
    ``Portal/Applicants/Availability`` page and bracket-matches the
    ``var unitTypes = [...]`` SSR JSON blob. Deterministic Tier-1.
  * e199a01 (2026-05-19, deep-probe) — a Playwright DOM adapter that
    appended ``&MoveInDate=`` to the Implicity iframe URL to un-gate the
    roster (``parse_resman_units`` / ``_move_in_date``).

The merge at 248b475 / d16f78f (2026-05-21) kept the iter10 *adapter* but
kept the deep-probe branch's *test file*, so this module spent two months
importing ``_move_in_date`` and ``parse_resman_units`` — symbols that the
surviving adapter never had. That ImportError aborted collection for the
whole of ``tests/pms/``.

The iter10 adapter is the live one and has since been extended past the
DOM adapter's reach: bc664a7 (2026-07-11) fixed HTML-entity-encoded hrefs
and took regaliabellaterra — the exact property the DOM adapter was built
for — from 0 to 27 units (vs. the DOM adapter's 15). So these tests were
rewritten against the surviving implementation rather than resurrecting
the clobbered one, which is unrouted and would be dead code.

Fixture shape is the real captured one documented in ``resman.py`` and
reused by ``test_pms_portal_hop.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.resman import (
    ResManAdapter,
    _extract_unittypes,
    _ms_to_iso,
    find_resman_applicant_url,
    find_resman_availability_url,
    parse_resman_unittypes,
)
from ma_poc.pms.detector import detect_pms

_AVAIL_URL = (
    "https://implicity.myresman.com/Portal/Applicants/Availability"
    "?a=1450&p=57495da9-baae-4ba3-98c0-e62612db16c3"
)

# Real ResMan Availability SSR shape: a chrome page wrapping a
# ``<script>var unitTypes = [...]`` floorplan-grouped JSON array. Group 1
# has two real units (the second with empty Pricing + the ASP.NET
# DateTime.MinValue sentinel); group 2 advertises a MarketRent with no
# available Units, which the parser degrades to a plan-level row.
_AVAILABILITY_HTML = """
<html><head><title>Availability</title></head><body>
<div id="chrome"></div>
<script>
var unitTypes = [
  {"Bedrooms":1,"Bathrooms":1.00,"MinSquareFootage":728,"MaxSquareFootage":728,
   "MarketRent":1299,"UnitTypeID":"A1",
   "Units":[
     {"Number":"21008","UnitType":"A1 1x1","Floor":"1","SquareFootage":728,
      "AvailableDate":"/Date(1784160000000)/","Pricing":[{"Rent":1299,"Term":12}]},
     {"Number":"21105","UnitType":"A1 1x1","Floor":"2","SquareFootage":728,
      "AvailableDate":"/Date(-62135596800000)/","Pricing":[]}
   ]},
  {"Bedrooms":2,"Bathrooms":2.00,"MinSquareFootage":1050,"MaxSquareFootage":1120,
   "MarketRent":1410,"UnitTypeID":"B1","Units":[]}
];
</script></body></html>
"""

# The marketing page links the portal. Regalia's CMS entity-encodes the
# href (``?a&#61;..&amp;p&#61;..``) — the bc664a7 audit fix.
_MARKETING_HTML = f'<html><body><a href="{_AVAIL_URL}">Check Availability</a></body></html>'
_MARKETING_HTML_ENTITY_ENCODED = (
    "<html><body><a href='https://implicity.myresman.com/Portal/Applicants/"
    "Availability?a&#61;1450&amp;p&#61;57495da9-baae-4ba3-98c0-e62612db16c3'>"
    "Check Availability</a></body></html>"
)


class _FetchResult:
    """Stub of the jugnu L1 ``FetchResult`` the adapter reads ``body`` from."""

    def __init__(self, body: str | bytes = "", final_url: str = "") -> None:
        self.body = body
        self.final_url = final_url


class _BarePage:
    """The adapter never touches the page — it works off ctx + probe fetch."""

    url = "https://www.regaliabellaterra.com/"


def _ctx(
    base_url: str = "https://www.regaliabellaterra.com/",
    fetch_result: _FetchResult | None = None,
) -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        fetch_result=fetch_result,
    )


# --- _ms_to_iso -----------------------------------------------------------


def test_ms_to_iso_converts_aspnet_date() -> None:
    assert _ms_to_iso("/Date(1784160000000)/") == "2026-07-16"


def test_ms_to_iso_rejects_minvalue_sentinel() -> None:
    # /Date(-62135596800000)/ is DateTime.MinValue — "no date", not 0001-01-01.
    assert _ms_to_iso("/Date(-62135596800000)/") == ""


def test_ms_to_iso_rejects_non_string_and_unparseable() -> None:
    assert _ms_to_iso(None) == ""
    assert _ms_to_iso(1784160000000) == ""
    assert _ms_to_iso("2026-07-16") == ""


# --- _extract_unittypes ---------------------------------------------------


def test_extract_unittypes_bracket_matches_blob() -> None:
    data = _extract_unittypes(_AVAILABILITY_HTML)
    assert data is not None
    assert len(data) == 2
    assert data[0]["UnitTypeID"] == "A1"


def test_extract_unittypes_returns_none_without_marker() -> None:
    assert _extract_unittypes("<html><body>no portal here</body></html>") is None


def test_extract_unittypes_returns_none_on_malformed_json() -> None:
    assert _extract_unittypes("<script>var unitTypes = [{'not': json},];</script>") is None


# --- parse_resman_unittypes -----------------------------------------------


def test_parse_unittypes_emits_unit_level_rows() -> None:
    data = _extract_unittypes(_AVAILABILITY_HTML)
    assert data is not None
    units = parse_resman_unittypes(data, _AVAIL_URL)
    assert len(units) == 3  # 2 real units + 1 plan-level fallback

    u1 = units[0]
    assert u1["unit_number"] == "21008"
    assert u1["bedrooms"] == "1"
    assert u1["bathrooms"] == "1.0"
    assert u1["sqft"] == "728"
    assert u1["floor"] == "1"
    assert u1["market_rent_low"] == 1299
    assert u1["availability_date"] == "2026-07-16"
    assert u1["availability_status"] == "AVAILABLE"
    assert u1["floor_plan_name"] == "A1 1x1"
    assert u1["extraction_tier"] == "TIER_1_API_RESMAN"
    assert u1["source_api_url"] == _AVAIL_URL


def test_parse_unittypes_falls_back_to_group_market_rent() -> None:
    # Unit 21105 has empty Pricing → inherits the group's MarketRent, and
    # its MinValue AvailableDate degrades to "" rather than a bogus date.
    data = _extract_unittypes(_AVAILABILITY_HTML)
    assert data is not None
    u2 = parse_resman_unittypes(data, _AVAIL_URL)[1]
    assert u2["unit_number"] == "21105"
    assert u2["market_rent_low"] == 1299
    assert u2["availability_date"] == ""


def test_parse_unittypes_prefers_12_month_price_over_first_short_term() -> None:
    """A seven-month ResMan rate cannot masquerade as the standard lease price."""
    data = [
        {
            "Bedrooms": 1,
            "Bathrooms": 1,
            "Units": [
                {
                    "Number": "216-306",
                    "UnitType": "One Bedroom Mini",
                    "Pricing": [
                        {"Rent": 1799, "Term": 7},
                        {"Rent": 1399, "Term": 12},
                        {"Rent": 1399, "Term": 13},
                    ],
                }
            ],
        }
    ]
    unit = parse_resman_unittypes(data, _AVAIL_URL)[0]
    assert unit["market_rent_low"] == 1399
    assert unit["lease_term"] == "12"


def test_parse_unittypes_degrades_empty_group_to_plan_level() -> None:
    # Group B1 has no Units but advertises MarketRent → one plan-level row
    # with no unit_number and UNKNOWN availability (flagged downstream).
    data = _extract_unittypes(_AVAILABILITY_HTML)
    assert data is not None
    plan = parse_resman_unittypes(data, _AVAIL_URL)[2]
    assert plan["unit_number"] == ""
    assert plan["bedrooms"] == "2"
    assert plan["sqft"] == "1120"  # MaxSquareFootage preferred over Min
    assert plan["market_rent_low"] == 1410
    assert plan["availability_status"] == "UNKNOWN"


def test_parse_unittypes_skips_malformed_entries() -> None:
    # Non-dict groups/units are skipped; a group with neither Units nor a
    # MarketRent emits nothing at all.
    data: list[Any] = ["junk", {"Bedrooms": 1, "Units": ["junk"]}, {"Bedrooms": 2, "Units": []}]
    assert parse_resman_unittypes(data, _AVAIL_URL) == []


# --- find_resman_availability_url -----------------------------------------


def test_find_availability_url_plain_href() -> None:
    assert find_resman_availability_url(_MARKETING_HTML) == _AVAIL_URL


def test_find_availability_url_html_entity_encoded() -> None:
    # bc664a7: CMS templates encode ``=``→``&#61;`` and ``&``→``&amp;``.
    assert find_resman_availability_url(_MARKETING_HTML_ENTITY_ENCODED) == _AVAIL_URL


def test_find_availability_url_absent_and_empty() -> None:
    assert find_resman_availability_url("") is None
    assert find_resman_availability_url("<html><body>nothing</body></html>") is None


def test_find_one_exact_property_scoped_applicant_url() -> None:
    applicant = "https://richmark.myresman.com/Portal/Applicants/New/GRAND?a=1054"
    assert find_resman_applicant_url(f'<a href="{applicant}">Apply</a>') == applicant


def test_applicant_discovery_rejects_account_login_and_ambiguity() -> None:
    login = "https://richmark.myresman.com/Portal/Access/SignIn/GRAND"
    assert find_resman_applicant_url(f'<a href="{login}">Residents</a>') is None
    assert find_resman_applicant_url(
        '<a href="https://richmark.myresman.com/Portal/Applicants/New/ONE?a=1054">One</a>'
        '<a href="https://richmark.myresman.com/Portal/Applicants/New/TWO?a=1054">Two</a>'
    ) is None


# --- ResManAdapter.extract ------------------------------------------------


@pytest.mark.asyncio
async def test_resman_adapter_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(url: str) -> str:
        assert url == _AVAIL_URL
        return _AVAILABILITY_HTML

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    ctx = _ctx(fetch_result=_FetchResult(body=_MARKETING_HTML))
    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RESMAN"
    assert len(result.units) == 3
    assert result.units[0]["unit_number"] == "21008"
    assert result.confidence > 0.7
    assert result.winning_url == _AVAIL_URL


@pytest.mark.asyncio
async def test_resman_adapter_accepts_bytes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(url: str) -> str:
        return _AVAILABILITY_HTML

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    ctx = _ctx(fetch_result=_FetchResult(body=_MARKETING_HTML.encode("utf-8")))
    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 3


@pytest.mark.asyncio
async def test_resman_adapter_uses_current_redirected_availability_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_fetch(url: str) -> str:
        raise AssertionError(f"must reuse fetched availability body: {url}")

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _unexpected_fetch)
    ctx = _ctx(
        base_url="https://richmark.myresman.com/Portal/Applicants/New/GRAND?a=1054",
        fetch_result=_FetchResult(body=_AVAILABILITY_HTML, final_url=_AVAIL_URL),
    )

    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RESMAN"
    assert len(result.units) == 3
    assert result.winning_url == _AVAIL_URL


@pytest.mark.asyncio
async def test_resman_adapter_follows_exact_published_applicant_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applicant = "https://richmark.myresman.com/Portal/Applicants/New/GRAND?a=1054"
    availability = (
        "https://richmark.myresman.com/Portal/Applicants/Availability"
        "?a=1054&p=57495da9-baae-4ba3-98c0-e62612db16c3"
    )

    async def _fake_fetch_page(url: str) -> tuple[str, str]:
        assert url == applicant
        return _AVAILABILITY_HTML, availability

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch_page", _fake_fetch_page)
    ctx = _ctx(
        fetch_result=_FetchResult(body=f'<a href="{applicant}">Apply</a>')
    )

    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RESMAN"
    assert len(result.units) == 3
    assert result.winning_url == availability


@pytest.mark.asyncio
async def test_resman_applicant_cross_tenant_redirect_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applicant = "https://richmark.myresman.com/Portal/Applicants/New/GRAND?a=1054"
    foreign = _AVAIL_URL.replace("implicity.myresman.com", "sibling.myresman.com")

    async def _fake_fetch_page(url: str) -> tuple[str, str]:
        return _AVAILABILITY_HTML, foreign

    async def _empty_fetch(url: str) -> str:
        return ""

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch_page", _fake_fetch_page)
    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _empty_fetch)
    ctx = _ctx(
        fetch_result=_FetchResult(body=f'<a href="{applicant}">Apply</a>')
    )

    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RESMAN_NO_PORTAL"
    assert result.units == []


@pytest.mark.asyncio
async def test_resman_adapter_recovers_portal_url_from_api_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch(url: str) -> str:
        return _AVAILABILITY_HTML if url == _AVAIL_URL else ""

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    ctx = _ctx(fetch_result=_FetchResult(body="<html>no link</html>"))
    # Captured network log carries the portal URL even when the body misses it.
    ctx._api_responses = [{"url": "https://cdn.example.com/x.js"}, {"url": _AVAIL_URL}]  # type: ignore[attr-defined]

    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RESMAN"
    assert len(result.units) == 3


@pytest.mark.asyncio
async def test_resman_adapter_no_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(url: str) -> str:
        return ""  # /floorplans/, /, /floor-plans/ all yield nothing

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    result = await ResManAdapter().extract(_BarePage(), _ctx())  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RESMAN_NO_PORTAL"
    assert result.units == []
    assert result.confidence == 0.0
    assert result.errors


@pytest.mark.asyncio
async def test_resman_adapter_shape_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch(url: str) -> str:
        # Portal reachable but no unitTypes blob (auth wall / redesign).
        return "<html><body>Availability</body></html>"

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    ctx = _ctx(fetch_result=_FetchResult(body=_MARKETING_HTML))
    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RESMAN_SHAPE_REJECTED"
    assert result.units == []


@pytest.mark.asyncio
async def test_resman_adapter_fetch_error_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch(url: str) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr("ma_poc.pms.adapters.resman._fetch", _fake_fetch)

    ctx = _ctx(fetch_result=_FetchResult(body=_MARKETING_HTML))
    result = await ResManAdapter().extract(_BarePage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_RESMAN_FETCH_ERROR"
    assert result.units == []
    assert any("resman-fetch-error" in e for e in result.errors)


# --- routing --------------------------------------------------------------


def test_detector_routes_resman_iframe_marker() -> None:
    html = (
        '<html><body><iframe src="https://implicity.myresman.com/Portal/'
        'Applicants/Availability?a=1450&p=57495da9-baae-4ba3-98c0-e62612db16c3">'
        "</iframe></body></html>"
    )
    det = detect_pms("https://www.regaliabellaterra.com/floorplans/", page_html=html)
    assert det.pms == "resman"
    # The surviving adapter fetches the portal JSON blob, so the detector
    # routes api_first (the clobbered DOM adapter wanted dom_first).
    assert det.recommended_strategy == "api_first"


def test_resman_adapter_registered() -> None:
    adapter = get_adapter("resman")
    assert isinstance(adapter, ResManAdapter)
    assert adapter.pms_name == "resman"


def test_resman_matches_response_body() -> None:
    adapter = ResManAdapter()
    # Needs BOTH markers: the blob alone could be any ASP.NET portal.
    portal_body = f"<!-- {_AVAIL_URL} -->{_AVAILABILITY_HTML}"
    assert adapter.matches_response_body(portal_body) is True
    assert adapter.matches_response_body(_AVAILABILITY_HTML) is False  # no myresman host
    assert adapter.matches_response_body("<html>unrelated</html>") is False
    assert adapter.matches_response_body(None) is False
