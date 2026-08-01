"""PMS-portal-hop recovery (2026-05-19).

Verified-live ground-truth from the same-day deep probe of the
bootstrap79 0%-uid cohort:

  - cobblestonephx.com → /floorplans-detail.php?u=... → ResMan iframe
    ``https://<tenant>.myresman.com/Portal/Applicants/Availability?a=N&p=G``
    (page embeds ``var unitTypes = [...]`` per the existing ResMan adapter).
  - forge65.com → ``Check Availability`` anchor →
    ``https://<sub>.securecafe.com/onlineleasing/.../availableunits.aspx``
    (page emits ``<tr class='AvailUnitRow'>`` per real apartment).

The recovery is a pure routing fix — both per-PMS SSR parsers
(``parse_resman_unittypes`` / ``parse_securecafe_availableunits``)
already work; this helper just gets them the portal HTML they were
never being handed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters._pms_portal_hop import (
    _canonical_rentcafe_availableunits,
    _direct_public_html,
    get_portal_hints,
    recover_pms_portal,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import detect_pms

# --- Production-shape portal HTMLs ----------------------------------------

# ResMan ``Portal/Applicants/Availability`` page. The page is a chrome shell
# wrapping a ``<script>var unitTypes = [...]`` JSON blob — exactly the
# bracket-matched shape ``_extract_unittypes`` walks.
_RESMAN_PORTAL_URL = (
    "https://acmepm.myresman.com/Portal/Applicants/Availability"
    "?a=12345&p=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
_RESMAN_HTML = """
<html><head><title>Availability</title></head>
<body><script>
var unitTypes = [
  {
    "Bedrooms": 1,
    "Bathrooms": 1.0,
    "MinSquareFootage": 615,
    "MaxSquareFootage": 615,
    "MarketRent": 999,
    "UnitTypeID": "A1-1X1-615",
    "Units": [
      {"Number": "101", "UnitType": "A1 1x1",
       "Floor": "1", "SquareFootage": 615,
       "AvailableDate": "/Date(1779408000000)/",
       "Pricing": [{"Rent": 1100, "Term": "12"}]},
      {"Number": "202", "UnitType": "A1 1x1",
       "Floor": "2", "SquareFootage": 615,
       "AvailableDate": "/Date(1782000000000)/",
       "Pricing": [{"Rent": 1150, "Term": "12"}]}
    ]
  }
];
</script></body></html>
"""

# RentCafe SecureCafe ``availableunits.aspx`` page. The parser keys on:
#   "Floor Plan: <name> - <N Bedroom(s)>, <X.X Bathroom>" header text
#   followed by ``<tr class='AvailUnitRow'>`` rows. Each row carries
#   ``data-label='Apartment' | 'Sq.Ft.' | 'Rent' | 'Date Available'`` cells.
_SECURECAFE_PORTAL_URL = "https://abc.securecafe.com/onlineleasing/forge-65/availableunits.aspx"

# 2026-07-31 plan-level cohort probes. Plain browser navigation reached each
# canonical page and exposed real apartment identifiers plus numeric rents:
# Summerlin at Winter Park (10 rows), Timber Ridge (12), Hawks Creek (22).
_LIVE_SECURECAFE_HANDOFFS = (
    (
        "225785",
        "http://www.summerlinatwinterpark.com/",
        "https://summerlinatwinterpark.securecafe.com/onlineleasing/"
        "summerlin-at-winter-park-apartments/guestlogin.aspx",
        "https://summerlinatwinterpark.securecafe.com/onlineleasing/"
        "summerlin-at-winter-park-apartments/availableunits.aspx",
    ),
    (
        "284917",
        "https://alaska.weidner.com/apartments/ak/eagle-river/timber-ridge-4/index.aspx",
        "https://alaska-weidner.securecafe.com/onlineleasing/timber-ridge-4/availableunits.aspx",
        "https://alaska-weidner.securecafe.com/onlineleasing/timber-ridge-4/availableunits.aspx",
    ),
    (
        "33785",
        "https://www.villageofhawkscreek.com/",
        "https://villageofhawkscreek.securecafe.com/onlineleasing/village-of-hawks-creek/availableunits.aspx",
        "https://villageofhawkscreek.securecafe.com/onlineleasing/village-of-hawks-creek/availableunits.aspx",
    ),
)

_LIVE_SECURECAFE_ENTRY_FAMILIES = (
    (
        "clearwater-onlineleasing",
        "https://clearwatercreekapartments.securecafe.com/onlineleasing/"
        "clearwater-creek/guestlogin.aspx",
        "https://clearwatercreekapartments.securecafe.com/onlineleasing/"
        "clearwater-creek/availableunits.aspx",
    ),
    (
        "woods-residentservices",
        "https://thewoodsatalderwood.securecafe.com/residentservices/"
        "the-woods-at-alderwood/userlogin.aspx",
        "https://thewoodsatalderwood.securecafe.com/onlineleasing/"
        "the-woods-at-alderwood/availableunits.aspx",
    ),
    (
        "bremerton-applicant",
        "https://bremertonparkapts.securecafeapplicant.com/onlineleasing/"
        "content3/access/bremerton-park/login",
        "https://bremertonparkapts.securecafe.com/onlineleasing/"
        "bremerton-park/availableunits.aspx",
    ),
    (
        "clearwater-resident-app",
        "https://clearwatercreekapartments.securecaferesident.com/"
        "residentservices/content3/access/clearwater-creek/login",
        "https://clearwatercreekapartments.securecafe.com/onlineleasing/"
        "clearwater-creek/availableunits.aspx",
    ),
)

_SECURECAFE_HTML = """
<html><body>
<h3>Floor Plan: A1 - 1 Bedroom, 1.0 Bathroom</h3>
<table>
<tr class='AvailUnitRow'>
  <td data-label='Apartment'>#101</td>
  <td data-label='Sq.Ft.'>650</td>
  <td data-label='Rent'>$1,200</td>
  <td data-label='Date Available'><span>5/20/26</span></td>
</tr>
<tr class='AvailUnitRow'>
  <td data-label='Apartment'>#202</td>
  <td data-label='Sq.Ft.'>650</td>
  <td data-label='Rent'>$1,250</td>
  <td data-label='Date Available'><span>7/01/26</span></td>
</tr>
</table>
<h3>Floor Plan: B1 - 2 Bedroom, 2.0 Bathroom</h3>
<table>
<tr class='AvailUnitRow'>
  <td data-label='Apartment'>#310</td>
  <td data-label='Sq.Ft.'>950</td>
  <td data-label='Rent'>$1,800</td>
  <td data-label='Date Available'><span>Available</span></td>
</tr>
</table>
</body></html>
"""

_SECURECAFE_RENTLESS_HTML = """
<html><body>
<h3>Floor Plan: A1 - 1 Bedroom, 1.0 Bathroom</h3>
<table><tr class='AvailUnitRow'>
  <td data-label='Apartment'>#404</td>
  <td data-label='Sq.Ft.'>650</td>
  <td data-label='Rent'>Call for pricing</td>
  <td data-label='Date Available'><span>Available</span></td>
</tr></table>
</body></html>
"""

_RESMAN_PLAN_ONLY_HTML = """
<html><body><script>
var unitTypes = [
  {
    "Bedrooms": 1,
    "Bathrooms": 1.0,
    "MinSquareFootage": 615,
    "MaxSquareFootage": 615,
    "MarketRent": 999,
    "UnitTypeID": "A1-1X1-615",
    "Units": []
  }
];
</script></body></html>
"""

# Marketing-site sub-page that links/embeds the portal (the "one nav-hop
# deep" we're probing for).
_RESMAN_SUBPAGE_HTML = f"""
<html><body><div class="cta">
<iframe src="{_RESMAN_PORTAL_URL}" width="100%"></iframe>
</div></body></html>
"""
_SECURECAFE_SUBPAGE_HTML = f"""
<html><body>
<a class="check-avail" href="{_SECURECAFE_PORTAL_URL}">Check Availability</a>
</body></html>
"""


class _FakePage:
    """``evaluate()`` dispatches: no-arg call = live portal-src scan;
    1-arg call = in-session fetch(url) → body string."""

    def __init__(self, url: str, live: list[str], responses: dict[str, str]) -> None:
        self.url = url
        self._live = live
        self._responses = responses

    async def evaluate(self, _js: str, *args: object) -> object:
        if not args:
            return list(self._live)
        url = str(args[0])
        return self._responses.get(url, "")


def _ctx(base_url: str) -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


# --- ResMan ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_resman_recover_via_live_iframe_on_page() -> None:
    """Marketing-site page already embeds the ResMan portal iframe."""
    page = _FakePage(
        url="https://www.cobblestonephx.com/floorplans-detail.php?u=A1-1X1-615",
        live=[_RESMAN_PORTAL_URL],
        responses={_RESMAN_PORTAL_URL: _RESMAN_HTML},
    )
    units = await recover_pms_portal(page, _ctx("https://www.cobblestonephx.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    nums = sorted(u.get("unit_number") for u in units)
    assert nums == ["101", "202"]


@pytest.mark.asyncio
async def test_resman_recover_via_subpath_probe() -> None:
    """No iframe on current page → probe ``/floorplans``; find portal there."""
    page = _FakePage(
        url="https://www.cobblestonephx.com/",
        live=[],
        responses={
            "https://www.cobblestonephx.com/floorplans": _RESMAN_SUBPAGE_HTML,
            _RESMAN_PORTAL_URL: _RESMAN_HTML,
        },
    )
    units = await recover_pms_portal(page, _ctx("https://www.cobblestonephx.com/"))  # type: ignore[arg-type]
    assert len(units) == 2


@pytest.mark.asyncio
async def test_resman_applicants_new_resolves_hidden_target_ids() -> None:
    """A property-code entry route resolves to one target-bound roster."""
    property_id = "39a922f0-1111-4222-8333-444444444444"
    entry = "https://acmepm.myresman.com/Portal/Applicants/New/STONE?a=1054"
    availability = (
        "https://acmepm.myresman.com/Portal/Applicants/Availability"
        f"?a=1054&p={property_id}"
    )
    entry_html = f"""
    <form>
      <input VALUE="1054" NAME="AccountID" type="hidden">
      <input type="hidden" value="{property_id}" id="PropertyID">
    </form>
    """
    scoped_roster = _RESMAN_HTML.replace(
        '"Bedrooms": 1,',
        f'"PropertyID": "{property_id}", "Bedrooms": 1,',
        1,
    )
    page = _FakePage(
        url="https://www.stoneridge.example/floorplans",
        live=[entry],
        responses={entry: entry_html, availability: scoped_roster},
    )

    rows = await recover_pms_portal(page, _ctx("https://www.stoneridge.example/"))  # type: ignore[arg-type]

    assert [row["unit_number"] for row in rows] == ["101", "202"]
    assert all(row["market_rent_low"] > 0 for row in rows)


@pytest.mark.asyncio
async def test_resman_current_winning_signin_url_is_resolved() -> None:
    """A fetched profile WPU is itself a resolver candidate."""
    property_id = "7eb1afa2-1111-4222-8333-444444444444"
    entry = "https://acmepm.myresman.com/Portal/Access/SignIn/RB"
    availability = (
        "https://acmepm.myresman.com/Portal/Applicants/Availability"
        f"?a=1219&p={property_id}"
    )
    entry_html = (
        '<input name="AccountID" value="1219">'
        f'<input name="PropertyID" value="{property_id}">'
    )
    scoped_roster = _RESMAN_HTML.replace(
        '"Bedrooms": 1,',
        f'"PropertyID": "{property_id}", "Bedrooms": 1,',
        1,
    )
    page = _FakePage(
        url=entry,
        live=[],
        responses={entry: entry_html, availability: scoped_roster},
    )

    rows = await recover_pms_portal(page, _ctx(entry))  # type: ignore[arg-type]

    assert [row["unit_number"] for row in rows] == ["101", "202"]


@pytest.mark.asyncio
async def test_resman_entry_rejects_cross_property_roster() -> None:
    """The hidden target UUID cannot authorize another property's groups."""
    target = "39a922f0-1111-4222-8333-444444444444"
    other = "f7aea92f-aaaa-4bbb-8ccc-555555555555"
    entry = "https://acmepm.myresman.com/Portal/Access/SignIn/STONE"
    availability = (
        "https://acmepm.myresman.com/Portal/Applicants/Availability"
        f"?a=1054&p={target}"
    )
    entry_html = (
        '<input name="AccountID" value="1054">'
        f'<input name="PropertyID" value="{target}">'
    )
    wrong_roster = _RESMAN_HTML.replace(
        '"Bedrooms": 1,',
        f'"PropertyID": "{other}", "Bedrooms": 1,',
        1,
    )
    page = _FakePage(
        url="https://www.stoneridge.example/",
        live=[entry],
        responses={entry: entry_html, availability: wrong_roster},
    )

    rows = await recover_pms_portal(page, _ctx("https://www.stoneridge.example/"))  # type: ignore[arg-type]

    assert rows == []


@pytest.mark.asyncio
async def test_resman_registration_query_resolves_without_entry_fetch() -> None:
    """Long-form query keys are sufficient when both are unambiguous."""
    property_id = "7eb1afa2-1111-4222-8333-444444444444"
    entry = (
        "https://acmepm.myresman.com/Portal/Access/ApplicantRegistration"
        f"?accountID=1219&propertyID={property_id}"
    )
    availability = (
        "https://acmepm.myresman.com/Portal/Applicants/Availability"
        f"?a=1219&p={property_id}"
    )
    scoped_roster = _RESMAN_HTML.replace(
        '"Bedrooms": 1,',
        f'"PropertyID": "{property_id}", "Bedrooms": 1,',
        1,
    )
    page = _FakePage(
        url="https://www.bellfort.example/",
        live=[entry],
        responses={availability: scoped_roster},
    )

    rows = await recover_pms_portal(page, _ctx("https://www.bellfort.example/"))  # type: ignore[arg-type]

    assert len(rows) == 2


# --- RentCafe / SecureCafe ------------------------------------------------


@pytest.mark.parametrize(
    ("case_name", "entry_url", "canonical_url"),
    _LIVE_SECURECAFE_ENTRY_FAMILIES,
    ids=[case[0] for case in _LIVE_SECURECAFE_ENTRY_FAMILIES],
)
def test_securecafe_entry_families_canonicalize_to_unit_roster(
    case_name: str,
    entry_url: str,
    canonical_url: str,
) -> None:
    del case_name
    assert _canonical_rentcafe_availableunits(entry_url) == canonical_url


def test_generic_content3_shell_is_not_a_property_roster() -> None:
    assert (
        _canonical_rentcafe_availableunits(
            "https://tenant.securecafe.com/onlineleasing/content3/floorplans.aspx"
        )
        == ""
    )


@pytest.mark.asyncio
async def test_securecafe_recover_via_live_anchor_on_page() -> None:
    """Marketing-site page already carries the SecureCafe ``a.href``."""
    page = _FakePage(
        url="https://www.forge65.com/floorplans",
        live=[_SECURECAFE_PORTAL_URL],
        responses={_SECURECAFE_PORTAL_URL: _SECURECAFE_HTML},
    )
    ctx = _ctx("https://www.forge65.com/")
    units = await recover_pms_portal(page, ctx)  # type: ignore[arg-type]
    # 2 A1 + 1 B1 = 3 real apartments
    assert len(units) == 3
    nums = sorted(u.get("unit_number") for u in units)
    assert nums == ["101", "202", "310"]
    assert all(unit_has_real_anchor(unit) for unit in units)
    assert all(isinstance(unit.get("market_rent_low"), (int, float)) for unit in units)
    # A native roster already satisfied the strict contract; do not queue a
    # second render that could displace or duplicate those apartments.
    assert get_portal_hints(ctx) == []


@pytest.mark.asyncio
async def test_securecafe_recover_via_subpath_probe() -> None:
    """No anchor on current page → probe; find SecureCafe link in /floorplans/."""
    page = _FakePage(
        url="https://www.forge65.com/",
        live=[],
        responses={
            "https://www.forge65.com/floorplans": _SECURECAFE_SUBPAGE_HTML,
            _SECURECAFE_PORTAL_URL: _SECURECAFE_HTML,
        },
    )
    units = await recover_pms_portal(page, _ctx("https://www.forge65.com/"))  # type: ignore[arg-type]
    assert len(units) == 3


@pytest.mark.asyncio
async def test_securecafe_empty_legacy_roster_routes_to_applicant_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published legacy portal is also the key to its migrated API."""
    from ma_poc.pms.adapters import rentcafe

    candidate = (
        "https://cooperslanding.securecafe.com/onlineleasing/coopers-landing-apartments/guestlogin.aspx"
    )
    canonical = (
        "https://cooperslanding.securecafe.com/onlineleasing/coopers-landing-apartments/availableunits.aspx"
    )
    calls: list[tuple[str, bool]] = []

    async def _applicant(
        candidate_url: str,
        _ctx: AdapterContext,
        result: AdapterResult,
        *,
        allow_hyperbrowser: bool,
    ) -> list[dict[str, object]]:
        calls.append((candidate_url, allow_hyperbrowser))
        result.tier_used = "TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT"
        return [
            {
                "floor_plan_name": "B1",
                "bedrooms": "2",
                "bathrooms": "1",
                "sqft": "900",
                "unit_number": "C-204",
                "market_rent_low": 1645,
                "market_rent_high": 1645,
            }
        ]

    monkeypatch.setattr(
        rentcafe,
        "_try_securecafe_applicant_candidate",
        _applicant,
    )
    page = _FakePage(
        url="https://www.example.com/floorplans",
        live=[candidate],
        responses={canonical: "<html><body>Applicant Portal</body></html>"},
    )

    units = await recover_pms_portal(page, _ctx("https://www.example.com/"))  # type: ignore[arg-type]

    assert [row["unit_number"] for row in units] == ["C-204"]
    assert units[0]["extraction_tier"] == ("TIER_1_API_RENTCAFE_APPLICANT_FLOORPLANS_V2_DIRECT")
    assert calls == [(candidate, False)]


@pytest.mark.asyncio
async def test_securecafe_applicant_fallback_rejects_plan_only_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portal-hop contract cannot promote a plan label as an apartment."""
    from ma_poc.pms.adapters import rentcafe

    candidate = "https://abc.securecafe.com/onlineleasing/acme/guestlogin.aspx"
    canonical = "https://abc.securecafe.com/onlineleasing/acme/availableunits.aspx"

    async def _plan_only(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "floor_plan_name": "A1",
                "unit_number": "",
                "market_rent_low": 1500,
            }
        ]

    monkeypatch.setattr(
        rentcafe,
        "_try_securecafe_applicant_candidate",
        _plan_only,
    )
    page = _FakePage(
        url="https://www.example.com/",
        live=[candidate],
        responses={canonical: "<html><body>no legacy rows</body></html>"},
    )

    assert await recover_pms_portal(page, _ctx("https://www.example.com/")) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_securecafe_legacy_units_win_without_applicant_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the established SSR roster as the first, cheapest winner."""
    from ma_poc.pms.adapters import rentcafe

    async def _unexpected(*_args: object, **_kwargs: object) -> list[dict]:
        raise AssertionError("Applicant fallback should not run")

    monkeypatch.setattr(
        rentcafe,
        "_try_securecafe_applicant_candidate",
        _unexpected,
    )
    page = _FakePage(
        url="https://www.forge65.com/floorplans",
        live=[_SECURECAFE_PORTAL_URL],
        responses={_SECURECAFE_PORTAL_URL: _SECURECAFE_HTML},
    )

    units = await recover_pms_portal(page, _ctx("https://www.forge65.com/"))  # type: ignore[arg-type]

    assert len(units) == 3


# --- Safety / no-false-positive paths -------------------------------------


@pytest.mark.asyncio
async def test_no_portal_returns_empty() -> None:
    """Genuinely no-portal marketing site: nothing matched → ``[]``."""
    page = _FakePage(
        url="https://www.plainsite.com/",
        live=[],
        responses={
            "https://www.plainsite.com/floorplans": "<html><body>no portal</body></html>",
        },
    )
    units = await recover_pms_portal(page, _ctx("https://www.plainsite.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_handles_pageless_stub() -> None:
    """Page stub without ``evaluate()`` degrades to ``[]`` (never raises)."""

    class _Bare:
        url = "https://x.com/"

    units = await recover_pms_portal(_Bare(), _ctx("https://x.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_unrecognised_portal_host_ignored() -> None:
    """A look-alike URL that doesn't match the strict host regexes → ``[]``."""
    page = _FakePage(
        url="https://www.lookalike.com/",
        live=["https://impostor.example.com/availability"],
        responses={"https://impostor.example.com/availability": _RESMAN_HTML},
    )
    units = await recover_pms_portal(page, _ctx("https://www.lookalike.com/"))  # type: ignore[arg-type]
    assert units == []


# --- 2026-05-19 bot-block telemetry --------------------------------------


class _StatusFakePage:
    """Same shape as ``_FakePage`` but responses are ``{status, body}`` dicts
    — exercises the new ``fetch_with_status`` wire format used to detect
    SecureCafe DataDome 403s.
    """

    def __init__(
        self,
        url: str,
        live: list[str],
        responses: dict[str, dict[str, object]],
    ) -> None:
        self.url = url
        self._live = live
        self._responses = responses

    async def evaluate(self, _js: str, *args: object) -> object:
        if not args:
            return list(self._live)
        u = str(args[0])
        return self._responses.get(u, {"status": 0, "body": ""})


@pytest.mark.parametrize(
    ("property_id", "marketing_url", "discovered_url", "canonical_url"),
    _LIVE_SECURECAFE_HANDOFFS,
)
@pytest.mark.asyncio
async def test_live_probed_securecafe_block_emits_canonical_render_handoff(
    property_id: str,
    marketing_url: str,
    discovered_url: str,
    canonical_url: str,
) -> None:
    """Three live-positive cohort portals remain reachable after a code-only 403.

    Recovery must not fabricate units from the blocked response. It records one
    strict URL for the caller's existing bounded render queue instead.
    """
    page = _StatusFakePage(
        url=marketing_url,
        live=[discovered_url],
        responses={canonical_url: {"status": 403, "body": ""}},
    )
    ctx = _ctx(marketing_url)
    ctx.property_id = property_id

    units = await recover_pms_portal(page, ctx)  # type: ignore[arg-type]

    assert units == []
    assert get_portal_hints(ctx) == [(canonical_url, "securecafe")]


@pytest.mark.asyncio
async def test_pageless_production_dispatch_emits_securecafe_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """page=None body discovery survives a blocked code-only portal probe."""
    _, marketing_url, discovered_url, canonical_url = _LIVE_SECURECAFE_HANDOFFS[0]
    ctx = _ctx(marketing_url)
    ctx.fetch_result = SimpleNamespace(body=f'<a href="{discovered_url}">Check availability</a>'.encode())

    async def _blocked_probe(url: str) -> tuple[int, str]:
        assert url == canonical_url
        return 403, ""

    monkeypatch.setattr(
        "ma_poc.pms.adapters._pms_portal_hop._direct_public_html",
        _blocked_probe,
    )

    units = await recover_pms_portal(None, ctx)  # type: ignore[arg-type]

    assert units == []
    assert get_portal_hints(ctx) == [(canonical_url, "securecafe")]


@pytest.mark.parametrize(
    ("case_name", "discovered_url", "canonical_url"),
    _LIVE_SECURECAFE_ENTRY_FAMILIES,
    ids=[case[0] for case in _LIVE_SECURECAFE_ENTRY_FAMILIES],
)
@pytest.mark.asyncio
async def test_pageless_securecafe_403_recovers_raw_html_via_hyperbrowser(
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    discovered_url: str,
    canonical_url: str,
) -> None:
    """Explicit HB mode feeds the canonical parser raw SSR, not mutated DOM."""
    marketing_url = f"https://marketing.example/{case_name}"
    ctx = _ctx(marketing_url)
    ctx.property_id = f"securecafe-hb-raw-{case_name}"
    ctx.fetch_result = SimpleNamespace(
        body=f'<a href="{discovered_url}">Check availability</a>'.encode()
    )

    async def _blocked_probe(url: str) -> tuple[int, str]:
        assert url == canonical_url
        return 403, ""

    hb_raw = AsyncMock(return_value=(200, _SECURECAFE_HTML))
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(
        "ma_poc.pms.adapters._pms_portal_hop._direct_public_html",
        _blocked_probe,
    )
    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        hb_raw,
    )

    units = await recover_pms_portal(None, ctx)  # type: ignore[arg-type]

    assert sorted(unit["unit_number"] for unit in units) == ["101", "202", "310"]
    assert get_portal_hints(ctx) == []
    hb_raw.assert_awaited_once_with(
        canonical_url,
        f"securecafe-hb-raw-{case_name}",
    )


@pytest.mark.asyncio
async def test_duplicate_securecafe_entries_spend_one_hb_raw_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest/floorplan/available variants canonicalise before HB spending."""
    guest = _SECURECAFE_PORTAL_URL.replace(
        "availableunits.aspx",
        "guestlogin.aspx?source=apply",
    )
    floorplans = _SECURECAFE_PORTAL_URL.replace(
        "availableunits.aspx",
        "floorplans.aspx",
    )
    page = _StatusFakePage(
        url="https://www.forge65.com/",
        live=[guest, floorplans, _SECURECAFE_PORTAL_URL],
        responses={
            _SECURECAFE_PORTAL_URL: {"status": 403, "body": ""},
        },
    )
    hb_raw = AsyncMock(return_value=(403, ""))
    monkeypatch.setenv("FETCH_BACKEND", "hyperbrowser")
    monkeypatch.setattr(
        "ma_poc.fetch.hyperbrowser_backend.hb_raw_get",
        hb_raw,
    )
    ctx = _ctx("https://www.forge65.com/")
    ctx.property_id = "securecafe-hb-dedup"

    assert await recover_pms_portal(page, ctx) == []  # type: ignore[arg-type]
    hb_raw.assert_awaited_once_with(
        _SECURECAFE_PORTAL_URL,
        "securecafe-hb-dedup",
    )
    assert get_portal_hints(ctx) == [(_SECURECAFE_PORTAL_URL, "securecafe")]


@pytest.mark.asyncio
async def test_pageless_portal_fetch_is_direct_bounded_and_same_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code-only lane cannot inherit a proxy or follow a foreign redirect."""
    real_client = httpx.AsyncClient
    client_kwargs: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/redirect"):
            return httpx.Response(
                302,
                headers={"location": "https://unrelated.example/inventory"},
            )
        return httpx.Response(200, content=b"<html>bounded portal</html>")

    def client(**kwargs: object) -> httpx.AsyncClient:
        client_kwargs.update(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    status, body = await _direct_public_html(
        "https://tenant.securecafe.com/onlineleasing/x/availableunits.aspx"
    )
    redirect_status, redirect_body = await _direct_public_html("https://tenant.securecafe.com/redirect")

    assert (status, body) == (200, "<html>bounded portal</html>")
    assert (redirect_status, redirect_body) == (302, "")
    assert client_kwargs["trust_env"] is False
    assert client_kwargs["follow_redirects"] is False
    assert "User-Agent" not in client_kwargs["headers"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_securecafe_403_records_bot_block() -> None:
    """A 403 remains visible in triage while render handoff is queued.

    The handoff uses the existing render route; this recovery does not add a
    solver, proxy, or fingerprint behavior.
    """
    from ma_poc.pms.adapters._universal_recovery import get_blocks

    canonical = "https://abc.securecafe.com/onlineleasing/forge-65/availableunits.aspx"
    page = _StatusFakePage(
        url="https://www.forge65.com/floorplans",
        live=[canonical],
        responses={canonical: {"status": 403, "body": ""}},
    )
    ctx = _ctx("https://www.forge65.com/")
    units = await recover_pms_portal(page, ctx)  # type: ignore[arg-type]
    assert units == []
    blocks = get_blocks(ctx)
    assert len(blocks) >= 1
    assert blocks[0]["recovery"] == "pms_portal_hop:rentcafe"
    assert blocks[0]["status"] == 403
    assert get_portal_hints(ctx) == [(canonical, "securecafe")]


@pytest.mark.asyncio
async def test_securecafe_handoff_is_normalized_and_deduplicated() -> None:
    """Repeated entry-point variants collapse to one canonical HTTPS hint."""
    guest = (
        "http://SUMMERLINATWINTERPARK.securecafe.com/onlineleasing/"
        "summerlin-at-winter-park-apartments/guestlogin.aspx?source=apply"
    )
    floorplans = (
        "https://summerlinatwinterpark.securecafe.com/onlineleasing/"
        "summerlin-at-winter-park-apartments/floorplans.aspx"
    )
    canonical = _LIVE_SECURECAFE_HANDOFFS[0][3]
    page = _StatusFakePage(
        url=_LIVE_SECURECAFE_HANDOFFS[0][1],
        live=[guest, floorplans, canonical],
        responses={canonical: {"status": 403, "body": ""}},
    )
    ctx = _ctx(_LIVE_SECURECAFE_HANDOFFS[0][1])

    assert await recover_pms_portal(page, ctx) == []  # type: ignore[arg-type]
    assert get_portal_hints(ctx) == [(canonical, "securecafe")]


@pytest.mark.asyncio
async def test_securecafe_zero_roster_never_fabricates_units() -> None:
    """A real portal with a zero-availability body remains zero unit rows."""
    page = _StatusFakePage(
        url="https://www.forge65.com/floorplans",
        live=[_SECURECAFE_PORTAL_URL],
        responses={
            _SECURECAFE_PORTAL_URL: {
                "status": 200,
                "body": "<html><body>No apartments are currently available.</body></html>",
            }
        },
    )
    ctx = _ctx("https://www.forge65.com/")

    assert await recover_pms_portal(page, ctx) == []  # type: ignore[arg-type]
    assert get_portal_hints(ctx) == [(_SECURECAFE_PORTAL_URL, "securecafe")]


@pytest.mark.asyncio
async def test_securecafe_rentless_apartment_is_not_accepted_as_unit() -> None:
    """Apartment identity alone cannot satisfy the recovered-unit contract."""
    page = _StatusFakePage(
        url="https://www.forge65.com/floorplans",
        live=[_SECURECAFE_PORTAL_URL],
        responses={
            _SECURECAFE_PORTAL_URL: {
                "status": 200,
                "body": _SECURECAFE_RENTLESS_HTML,
            }
        },
    )
    ctx = _ctx("https://www.forge65.com/")

    assert await recover_pms_portal(page, ctx) == []  # type: ignore[arg-type]
    assert get_portal_hints(ctx) == [(_SECURECAFE_PORTAL_URL, "securecafe")]


@pytest.mark.asyncio
async def test_resman_plan_catalogue_is_preserved_without_unit_promotion() -> None:
    """Unanchored plan evidence survives strict apartment validation."""
    page = _FakePage(
        url="https://www.cobblestonephx.com/",
        live=[_RESMAN_PORTAL_URL],
        responses={_RESMAN_PORTAL_URL: _RESMAN_PLAN_ONLY_HTML},
    )
    ctx = _ctx("https://www.cobblestonephx.com/")

    rows = await recover_pms_portal(page, ctx)  # type: ignore[arg-type]

    assert len(rows) == 1
    assert rows[0]["market_rent_low"] == 999
    assert not unit_has_real_anchor(rows[0])
    assert get_portal_hints(ctx) == []


@pytest.mark.asyncio
async def test_resman_500_not_recorded_as_bot_block() -> None:
    """A 500 is an upstream server fault, not a bot-wall — must NOT
    pollute the bot-block triage signal.
    """
    from ma_poc.pms.adapters._universal_recovery import get_blocks

    page = _StatusFakePage(
        url="https://www.cobblestonephx.com/",
        live=[_RESMAN_PORTAL_URL],
        responses={_RESMAN_PORTAL_URL: {"status": 500, "body": ""}},
    )
    ctx = _ctx("https://www.cobblestonephx.com/")
    units = await recover_pms_portal(page, ctx)  # type: ignore[arg-type]
    assert units == []
    assert get_blocks(ctx) == []


@pytest.mark.asyncio
async def test_stale_portal_url_does_not_short_circuit() -> None:
    """First candidate returns empty parse → walker tries the next.

    Real-world failure mode: a marketing site has a stale anchor pointing
    at a portal whose ``p=<guid>`` no longer resolves (zero ``unitTypes``).
    Recovery must keep walking the candidate list, not bail.
    """
    stale = (
        "https://acmepm.myresman.com/Portal/Applicants/Availability"
        "?a=1&p=11111111-2222-3333-4444-555555555555"
    )
    page = _FakePage(
        url="https://www.cobblestonephx.com/",
        live=[stale, _RESMAN_PORTAL_URL],
        responses={
            stale: "<html><body>portal moved</body></html>",
            _RESMAN_PORTAL_URL: _RESMAN_HTML,
        },
    )
    units = await recover_pms_portal(page, _ctx("https://www.cobblestonephx.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
