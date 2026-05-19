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

import pytest

from ma_poc.pms.adapters._pms_portal_hop import recover_pms_portal
from ma_poc.pms.adapters.base import AdapterContext
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
_SECURECAFE_PORTAL_URL = (
    "https://abc.securecafe.com/onlineleasing/forge-65/availableunits.aspx"
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


# --- RentCafe / SecureCafe ------------------------------------------------


@pytest.mark.asyncio
async def test_securecafe_recover_via_live_anchor_on_page() -> None:
    """Marketing-site page already carries the SecureCafe ``a.href``."""
    page = _FakePage(
        url="https://www.forge65.com/floorplans",
        live=[_SECURECAFE_PORTAL_URL],
        responses={_SECURECAFE_PORTAL_URL: _SECURECAFE_HTML},
    )
    units = await recover_pms_portal(page, _ctx("https://www.forge65.com/"))  # type: ignore[arg-type]
    # 2 A1 + 1 B1 = 3 real apartments
    assert len(units) == 3
    nums = sorted(u.get("unit_number") for u in units)
    assert nums == ["101", "202", "310"]


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
