"""AppFolio-embed recovery for Wix/Squarespace shells (2026-05-19).

Verified live on brooksidejohnsoncreek.com (Squarespace) → /listings →
iframe https://illumepm.appfolio.com/listings (69 data-listing-id blocks).
The recovery scans the live page / probes known sub-paths, finds the
AppFolio iframe, fetches it in-session, and reuses parse_appfolio_listings_ssr.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._appfolio_embed import recover_appfolio_embed
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter
from ma_poc.pms.detector import detect_pms

# Production-shape AppFolio listings SSR (same js-listing-* shape the live
# illumepm.appfolio.com/listings page emits).
_APPFOLIO_SSR = """
<html><body>
<article class="listing-item js-listing-item" data-listing-id="1386">
  <div class="js-listing-blurb-rent">$1,695</div>
  <div class="js-listing-blurb-bed-bath">2 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 900</div>
  <div class="js-listing-available">6/01/26</div>
  <div class="js-listing-address"><span>10 Creek Rd</span></div>
</article>
<article class="listing-item js-listing-item" data-listing-id="1037">
  <div class="js-listing-blurb-rent">$1,350</div>
  <div class="js-listing-blurb-bed-bath">Studio / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 540</div>
  <div class="js-listing-available">5/22/26</div>
  <div class="js-listing-address"><span>12 Creek Rd</span></div>
</article>
</body></html>
"""

_IFRAME_SRC = "https://illumepm.appfolio.com/listings?1234567890"
# After 2026-05-19 canonicalization (``_to_appfolio_listings_root``), any
# captured AppFolio URL — including showings/new anchor links — is stripped
# to the listings SSR root. Tests respond to BOTH variants so the
# canonicalization doesn't break the existing assertions.
_IFRAME_CANON = "https://illumepm.appfolio.com/listings"
_SUBPAGE_HTML = (
    '<html><body><div class="sqs-block">'
    f'<iframe src="{_IFRAME_SRC}" width="100%"></iframe>'
    "</div></body></html>"
)


class _FakePage:
    """evaluate() dispatches by JS body + arg shape:
        - no-args + JS containing 'tenant-scan' → live tenant scan (anchors/scripts)
        - no-args + any other JS              → live /listings iframe scan (legacy)
        - 1-arg                               → in-session fetch(url) → body string"""

    def __init__(
        self,
        url: str,
        live: list[str],
        responses: dict[str, str],
        tenants: list[str] | None = None,
    ) -> None:
        self.url = url
        self._live = live
        self._tenants = tenants or []
        self._responses = responses
        #: every URL the recovery actually fetched — lets a test assert that an
        #: account-wide roster was NOT requested, not merely not returned.
        self.fetched: list[str] = []

    async def evaluate(self, _js: str, *args: object) -> object:
        if not args:
            if 'tenant-scan' in (_js or ''):
                return list(self._tenants)
            return list(self._live)
        url = str(args[0])
        self.fetched.append(url)
        return self._responses.get(url, "")


def _ctx(base_url: str, address: str = "", zip_code: str = "") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        address=address,
        zip_code=zip_code,
    )


@pytest.mark.asyncio
async def test_recover_via_live_iframe_on_page() -> None:
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={_IFRAME_SRC: _APPFOLIO_SSR, _IFRAME_CANON: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    assert units[0]["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"
    assert units[0]["bedrooms"] == "2"
    assert units[1]["bedrooms"] == "0"  # Studio


@pytest.mark.asyncio
async def test_recover_via_subpath_probe() -> None:
    """No iframe on the current page → probe sub-paths, find it in /listings."""
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/",
        live=[],
        responses={
            "https://www.brooksidejohnsoncreek.com/listings": _SUBPAGE_HTML,
            _IFRAME_SRC: _APPFOLIO_SSR,
            _IFRAME_CANON: _APPFOLIO_SSR,
        },
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    assert units[0]["market_rent_low"] == 1695


@pytest.mark.asyncio
async def test_no_appfolio_returns_empty() -> None:
    """Genuine no-PMS shell: nothing matched → [] (no false positives)."""
    page = _FakePage(
        url="https://www.plainsquarespacesite.com/",
        live=[],
        responses={"https://www.plainsquarespacesite.com/listings": "<html><body>no widget</body></html>"},
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.plainsquarespacesite.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_recover_handles_pageless_stub() -> None:
    """A page stub without evaluate() degrades to [] (never raises)."""

    class _Bare:
        url = "https://x.com/"

    units = await recover_appfolio_embed(_Bare(), _ctx("https://x.com/"))  # type: ignore[arg-type]
    assert units == []


@pytest.mark.asyncio
async def test_squarespace_adapter_recovers_appfolio_embed() -> None:
    page = _FakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={_IFRAME_SRC: _APPFOLIO_SSR, _IFRAME_CANON: _APPFOLIO_SSR},
    )
    result = await SquarespaceNoPmsAdapter().extract(page, _ctx("https://www.brooksidejohnsoncreek.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 2
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_squarespace_adapter_still_deadends_when_no_embed() -> None:
    page = _FakePage(url="https://www.plain.com/", live=[], responses={})
    result = await SquarespaceNoPmsAdapter().extract(page, _ctx("https://www.plain.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "SYNDICATION_ONLY_SQUARESPACE"
    assert result.units == []


@pytest.mark.asyncio
async def test_wix_adapter_recovers_appfolio_embed() -> None:
    page = _FakePage(
        url="https://www.villasonrock.com/",
        live=[],
        responses={
            "https://www.villasonrock.com/availability": _SUBPAGE_HTML,
            _IFRAME_SRC: _APPFOLIO_SSR,
            _IFRAME_CANON: _APPFOLIO_SSR,
        },
    )
    result = await WixNoPmsAdapter().extract(page, _ctx("https://www.villasonrock.com/"))  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 2


def test_detector_appfolio_iframe_beats_squarespace_shell() -> None:
    """Squarespace marketing HTML that embeds an AppFolio listings iframe
    routes to appfolio (strong pass-1), not squarespace_nopms."""
    html = (
        '<html><head><script src="https://static1.squarespace.com/x.js"></script>'
        '</head><body><iframe src="https://illumepm.appfolio.com/listings?123">'
        "</iframe></body></html>"
    )
    det = detect_pms("https://www.brooksidejohnsoncreek.com/listings", page_html=html)
    assert det.pms == "appfolio"


def test_detector_plain_squarespace_still_nopms() -> None:
    html = '<html><head><script src="https://static1.squarespace.com/x.js"></script></head><body></body></html>'
    det = detect_pms("https://www.plain.com/", page_html=html)
    assert det.pms == "squarespace_nopms"


# ── 2026-05-19 bot-block telemetry: 403 on the AppFolio fetch ─────────────


class _StatusFakePage:
    """Like ``_FakePage`` but the responses dict maps URL → ``{status, body}``
    so the recovery's ``fetch_with_status`` JS-shim gets the new dict wire
    format and can record bot-blocks.
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


@pytest.mark.asyncio
async def test_recover_records_bot_block_when_appfolio_returns_403() -> None:
    """If the AppFolio listings fetch is 403'd (DataDome / etc.), the
    recovery returns ``[]`` BUT stamps a bot-block record on the ctx so
    triage can distinguish 'routing-correct, bot-walled' from 'no signal'.
    """
    from ma_poc.pms.adapters._universal_recovery import get_blocks

    page = _StatusFakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={
            _IFRAME_SRC: {"status": 403, "body": ""},
            _IFRAME_CANON: {"status": 403, "body": ""},
        },
    )
    ctx = _ctx("https://www.brooksidejohnsoncreek.com/")
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert units == []
    blocks = get_blocks(ctx)
    assert len(blocks) >= 1
    assert blocks[0]["recovery"] == "appfolio_embed"
    assert blocks[0]["status"] == 403


@pytest.mark.asyncio
async def test_recover_no_bot_block_recorded_on_success() -> None:
    """200 with parseable SSR markup must not stamp a bot-block."""
    from ma_poc.pms.adapters._universal_recovery import get_blocks

    page = _StatusFakePage(
        url="https://www.brooksidejohnsoncreek.com/listings",
        live=[_IFRAME_SRC],
        responses={
            _IFRAME_SRC: {"status": 200, "body": _APPFOLIO_SSR},
            _IFRAME_CANON: {"status": 200, "body": _APPFOLIO_SSR},
        },
    )
    ctx = _ctx("https://www.brooksidejohnsoncreek.com/")
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert len(units) == 2
    assert get_blocks(ctx) == []


# ── 2026-05-19 regression: "schedule a showing" anchor → don't waste a fetch ──


@pytest.mark.asyncio
async def test_recover_canonicalizes_showings_new_anchor_to_listings_root() -> None:
    """The 100-sample validation surfaced 3 sites with anchor links of the form
    ``{tenant}.appfolio.com/listings/showings/new?listable_uid=...`` (a
    "request a tour" form, NOT the listings SSR index). The greedy
    ``/listings[^\\s"'<>]*`` regex captures the whole URL — the recovery
    must canonicalize to ``{tenant}.appfolio.com/listings`` before fetching
    so we hit the data-bearing index, not the form.

    2026-07-28 (QC): canonicalizing the anchor is still required, but reaching
    the index off a per-unit deep link is NOT evidence that the account roster
    is this property's inventory — a showings/tour form is exactly as weak as a
    ``/connect/users/sign_in`` link. So the fetch must still go to the index,
    and the roster must then clear the strict address bar. With no address to
    check against, an unscopeable roster is not data.
    """
    showings = "https://yourmetropolitan.appfolio.com/listings/showings/new?listable_uid=abc123&source=Website"
    canonical = "https://yourmetropolitan.appfolio.com/listings"
    page = _FakePage(
        url="https://www.yourmetropolitan.com/properties/bala/",
        live=[showings],            # the regex matched this URL
        responses={canonical: _APPFOLIO_SSR},  # fetch must canonicalize to here
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.yourmetropolitan.com/"))  # type: ignore[arg-type]
    # Canonicalization still happened: the form URL was never requested.
    assert canonical in page.fetched
    assert showings not in page.fetched
    # ...but weak evidence + no address to corroborate = decline, not ship.
    assert units == []


@pytest.mark.asyncio
async def test_showings_anchor_still_yields_units_when_address_matches() -> None:
    """The strict bar must not turn into a blanket refusal: the same deep-link
    anchor still reaches the index and still emits THIS property's listing when
    the CSV address corroborates it. Guards against 'fixed' meaning 'emits
    nothing'.
    """
    showings = "https://yourmetropolitan.appfolio.com/listings/showings/new?listable_uid=abc123"
    canonical = "https://yourmetropolitan.appfolio.com/listings"
    page = _FakePage(
        url="https://www.yourmetropolitan.com/properties/bala/",
        live=[showings],
        responses={canonical: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(  # type: ignore[arg-type]
        page, _ctx("https://www.yourmetropolitan.com/", address="10 Creek Rd")
    )
    assert [u["floor_plan_name"] for u in units] == ["10 Creek Rd"]


# ─────────────────────────────────────────────────────────────────────
# Tenant-only fallback (2026-05-20 — feature-fail-1429 probe finding):
# Wix shells often have a *.appfolio.com/connect/users/sign_in (or
# /request_access) link in the footer — the tenant subdomain is the
# canonical one, but the path is auth not /listings. Pre-fix recovery
# missed it. New behavior: extract tenant from ANY appfolio.com URL
# and construct https://{tenant}.appfolio.com/listings.
# Verified-live on aptsedenprairie (pid 30796, 298 strict),
# aptslindenpark (32502, 297 strict), rentdwp (26772, 117 strict).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recover_via_tenant_only_login_url() -> None:
    """Wix shell footer has *.appfolio.com/connect/users/sign_in link
    (tenant subdomain, auth path). Pre-fix recovery missed it because
    no /listings URL was present anywhere. The tenant fallback should
    extract 'bendermanagement' from the auth URL and fetch the canonical
    https://bendermanagement.appfolio.com/listings.

    2026-07-28 — the tenant-discovery half of this is unchanged and still
    asserted: the sign_in link IS legitimate evidence of which AppFolio
    account manages this property. What is now also asserted is the half
    that was missing, and whose absence shipped 11,033 rows of other
    people's inventory: the fetched roster is an ACCOUNT roster, so only
    the listings that match this property's own address are emitted.
    """
    login_url = "https://bendermanagement.appfolio.com/connect/users/sign_in"
    canonical = "https://bendermanagement.appfolio.com/listings"
    page = _FakePage(
        url="https://www.aptsedenprairie.com/",
        live=[],                        # no /listings URL on page
        tenants=[login_url],            # but tenant URL is there
        responses={canonical: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://www.aptsedenprairie.com/", "10 Creek Rd"),  # type: ignore[arg-type]
    )
    assert len(units) == 1, "tenant discovered, roster scoped to this property"
    assert units[0]["floor_plan_name"] == "10 Creek Rd"
    assert units[0]["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"


@pytest.mark.asyncio
async def test_recover_via_tenant_only_request_access_url() -> None:
    """Same fallback fires on /connect/users/request_access (rentdwp pattern).

    Same 2026-07-28 addition as above: tenant discovery still works off the
    auth URL; the account roster it reaches is scoped to the CSV address.
    """
    req_url = "https://dougwettonproperties.appfolio.com/connect/users/request_access"
    canonical = "https://dougwettonproperties.appfolio.com/listings"
    page = _FakePage(
        url="https://www.rentdwp.com/parkviewpalms",
        live=[],
        tenants=[req_url],
        responses={canonical: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://www.rentdwp.com/", "12 Creek Rd"),  # type: ignore[arg-type]
    )
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "12 Creek Rd"


@pytest.mark.asyncio
async def test_tenant_fallback_does_not_fire_when_no_appfolio_on_page() -> None:
    """Genuine no-PMS shell: no /listings, no tenant URL → still []
    (regression guard — the fallback must not invent tenants)."""
    page = _FakePage(
        url="https://www.plainshell.com/",
        live=[],
        tenants=[],
        responses={},
    )
    units = await recover_appfolio_embed(
        page, _ctx("https://www.plainshell.com/")  # type: ignore[arg-type]
    )
    assert units == []


# ─────────────────────────────────────────────────────────────────────
# 2026-07-28 — SCOPE OR DECLINE.
#
# Reference run 2026-07-27-full-0d54ca7: 115 properties / 11,033 rows
# (10.5% of the run) came out of this function, and every one of them was
# an unscoped MANAGEMENT-COMPANY roster. Seven Washington communities each
# emitted the same 294-unit olympicmanagement roster; College Park (Lacey
# WA) shipped "10309-92ND SW, 07, TACOMA, WA 98498" at $1,725 as its own
# unit, verdict SUCCESS. Live ground truth for College Park is 4 listings.
#
# The discriminator is not the sign_in link (that correctly identifies the
# tenant) — it is whether the roster was SCOPED.
# ─────────────────────────────────────────────────────────────────────

# Account roster: three listings, two of them a different property in the
# same AppFolio account. Shape copied from the real
# olympicmanagement.appfolio.com/listings response.
_ACCOUNT_ROSTER_SSR = """
<html><body>
<article class="js-listing-item" data-listing-id="7218">
  <div class="js-listing-blurb-rent">$1,725</div>
  <div class="js-listing-blurb-bed-bath">2 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 989</div>
  <div class="js-listing-address"><span>10309-92ND SW, 07, TACOMA, WA 98498</span></div>
</article>
<article class="js-listing-item" data-listing-id="7219">
  <div class="js-listing-blurb-rent">$1,540</div>
  <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 700</div>
  <div class="js-listing-address"><span>9805 PACIFIC AVE, 12, SUMNER, WA 98390</span></div>
</article>
<article class="js-listing-item" data-listing-id="7300">
  <div class="js-listing-blurb-rent">$1,395</div>
  <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
  <div class="js-listing-square-feet">Square Feet: 650</div>
  <div class="js-listing-address"><span>3307 COLLEGE STREET SE, A101, LACEY, WA 98503</span></div>
</article>
</body></html>
"""

# AppFolio's real empty-scope response — Cherry Tree's honest answer.
_NO_VACANCIES_SSR = (
    "<html><body><div class='js-listing-list'>"
    "No vacancies found matching your search criteria."
    "</div></body></html>"
)

_SIGN_IN = "https://olympicmanagement.appfolio.com/connect/users/sign_in"
_ACCOUNT_URL = "https://olympicmanagement.appfolio.com/listings"
_SCOPED_URL = (
    "https://olympicmanagement.appfolio.com/listings"
    "?filters%5Bproperty_list%5D=COLLEGE%20PARK"
)


def _embed_js(host: str, group: str) -> str:
    return (
        "<html><body><script>Appfolio.Listing({"
        f"hostUrl: '{host}', propertyGroup: '{group}', defaultOrder: 'rent_asc'"
        "});</script></body></html>"
    )


@pytest.mark.asyncio
async def test_scoped_widget_url_keeps_its_property_list_filter() -> None:
    """brooksidejohnsoncreek.com (pid 19955) embeds the widget URL already
    carrying ``filters[property_list]=brookside``. ``_to_appfolio_listings_root``
    stripped the whole query string, so the run fetched illumepm's 78-card
    ACCOUNT roster and emitted 70 units for a 3-unit Milwaukie property.
    The scope must survive canonicalization, and the account URL must never
    be fetched.
    """
    scoped_src = (
        "https://illumepm.appfolio.com/listings?1785172489213"
        "&filters%5Bproperty_list%5D=brookside&theme_color=%2361C7C9"
        "&filters%5Border_by%5D=rent_asc"
    )
    scoped_canon = (
        "https://illumepm.appfolio.com/listings?filters%5Bproperty_list%5D=brookside"
    )
    page = _FakePage(
        url="https://brooksidejohnsoncreek.com/",
        live=[scoped_src],
        responses={scoped_canon: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://brooksidejohnsoncreek.com/", "3000 SE Brookside Dr", "97222"),  # type: ignore[arg-type]
    )
    assert len(units) == 2, "scoped URL was fetched (only it has a response)"
    assert units[0]["source_api_url"] == scoped_canon
    assert "https://illumepm.appfolio.com/listings" not in page.fetched, (
        "the unscoped account roster must never be fetched once a scope exists"
    )


@pytest.mark.asyncio
async def test_property_group_is_read_off_the_availability_subpage() -> None:
    """collegeparklacey.com's entry body has ONLY the sign_in link — the
    ``propertyGroup: 'COLLEGE PARK'`` lives one hop away. Of the 32 cohort
    properties whose scope was found by hand, 21 had it on a sub-page and
    only 11 on the entry page; the pipeline never looked past the entry
    body, which is why ``find_appfolio_property_group()`` kept returning
    None and the account roster kept winning.
    """
    page = _FakePage(
        url="https://collegeparklacey.com/",
        live=[],
        tenants=[_SIGN_IN],
        responses={
            "https://collegeparklacey.com/availability": _embed_js(
                "olympicmanagement.appfolio.com", "COLLEGE PARK"
            ),
            _SCOPED_URL: _APPFOLIO_SSR,
            _ACCOUNT_URL: _ACCOUNT_ROSTER_SSR,
        },
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://collegeparklacey.com/", "3307 College St SE", "98503"),  # type: ignore[arg-type]
    )
    assert len(units) == 2
    assert _SCOPED_URL in page.fetched
    assert _ACCOUNT_URL not in page.fetched


@pytest.mark.asyncio
async def test_scoped_response_with_no_listings_is_a_real_answer() -> None:
    """Cherry Tree's scoped query returns "No vacancies found matching your
    search criteria" — that is SUCCESS_NO_AVAILABILITY, not permission to
    re-ask the account-wide question. The run answered it with 294 units.
    """
    from ma_poc.pms.adapters._universal_recovery import get_notes

    page = _FakePage(
        url="https://cherrytreetacoma.com/",
        live=[],
        tenants=[_SIGN_IN],
        responses={
            "https://cherrytreetacoma.com/availability": _embed_js(
                "olympicmanagement.appfolio.com", "CHERRY TREE"
            ),
            "https://olympicmanagement.appfolio.com/listings"
            "?filters%5Bproperty_list%5D=CHERRY%20TREE": _NO_VACANCIES_SSR,
            _ACCOUNT_URL: _ACCOUNT_ROSTER_SSR,
        },
    )
    ctx = _ctx("https://cherrytreetacoma.com/", "8801 S Hosmer St", "98444")
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert units == []
    assert _ACCOUNT_URL not in page.fetched, (
        "an empty scoped answer must not reopen the account-wide query"
    )
    assert [n["reason"] for n in get_notes(ctx)] == ["scoped_no_availability"]


@pytest.mark.asyncio
async def test_unscopeable_account_roster_emits_only_this_property() -> None:
    """No propertyGroup anywhere — the roster is admitted only through the
    address filter, exactly as the VANITY path already does it.
    """
    page = _FakePage(
        url="https://collegeparklacey.com/",
        live=[],
        tenants=[_SIGN_IN],
        responses={_ACCOUNT_URL: _ACCOUNT_ROSTER_SSR},
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://collegeparklacey.com/", "3307 College St SE", "98503"),  # type: ignore[arg-type]
    )
    assert len(units) == 1
    assert units[0]["floor_plan_name"].startswith("3307 COLLEGE STREET SE")


@pytest.mark.asyncio
async def test_account_roster_that_is_not_ours_emits_nothing_and_says_why() -> None:
    """pid 237787 (Heritage Amity Commons, Douglassville PA) emitted 5 units
    that were all in Perkasie PA, 40 miles away. Nothing in that roster is
    this property's, so nothing is emitted — and the property must land
    VISIBLY unresolved, not silently empty.
    """
    from ma_poc.pms.adapters._universal_recovery import get_notes

    page = _FakePage(
        url="https://heritageamitycommons.com/",
        live=[],
        tenants=["https://heritagepropertyrentals.appfolio.com/connect/users/sign_in"],
        responses={
            "https://heritagepropertyrentals.appfolio.com/listings": _ACCOUNT_ROSTER_SSR
        },
    )
    ctx = _ctx("https://heritageamitycommons.com/", "606A Lake Dr", "19518")
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert units == []
    notes = get_notes(ctx)
    assert [n["reason"] for n in notes] == ["filter_rejected_all_demote"]
    assert notes[0]["recovery"] == "appfolio_embed"


@pytest.mark.asyncio
async def test_account_roster_declined_when_ctx_has_no_address_to_check() -> None:
    """A synthesized tenant URL plus no CSV address is nothing to verify
    against. "Could not look" is not "it is ours" — decline and record it.
    """
    from ma_poc.pms.adapters._universal_recovery import get_notes

    page = _FakePage(
        url="https://collegeparklacey.com/",
        live=[],
        tenants=[_SIGN_IN],
        responses={_ACCOUNT_URL: _ACCOUNT_ROSTER_SSR},
    )
    ctx = _ctx("https://collegeparklacey.com/")  # no address, no zip
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert units == []
    assert [n["reason"] for n in get_notes(ctx)] == ["no_ctx_address_or_zip_demote"]


@pytest.mark.asyncio
async def test_single_property_account_still_recovers_in_full() -> None:
    """MUST NOT BREAK. 34 properties / 296 rows in the reference run reached
    their roster off nothing but a portal link, and the roster really was
    theirs (pid 261381: 65 units, all 614 CENTRAL PKWY). A single-property
    AppFolio account must still come back whole.
    """
    roster = """
    <html><body>
    <article class="js-listing-item" data-listing-id="9001">
      <div class="js-listing-blurb-rent">$1,100</div>
      <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
      <div class="js-listing-square-feet">Square Feet: 600</div>
      <div class="js-listing-address"><span>614 CENTRAL PKWY, 101, CINCINNATI, OH 45202</span></div>
    </article>
    <article class="js-listing-item" data-listing-id="9002">
      <div class="js-listing-blurb-rent">$1,250</div>
      <div class="js-listing-blurb-bed-bath">2 bd / 1 ba</div>
      <div class="js-listing-square-feet">Square Feet: 820</div>
      <div class="js-listing-address"><span>614 CENTRAL PKWY, 205, CINCINNATI, OH 45202</span></div>
    </article>
    </body></html>
    """
    page = _FakePage(
        url="https://614central.com/",
        live=[],
        tenants=["https://onesixfourteen.appfolio.com/connect/users/sign_in"],
        responses={"https://onesixfourteen.appfolio.com/listings": roster},
    )
    units = await recover_appfolio_embed(
        page,
        _ctx("https://614central.com/", "614 Central Pkwy", "45202"),  # type: ignore[arg-type]
    )
    assert len(units) == 2, "the whole roster is this property's inventory"


# ─────────────────────────────────────────────────────────────────────
# 2026-07-28 (reconciliation) — the PUBLISHED-INDEX grade must not drift
# into the OPERATOR_SCOPED one.
#
# This is the silent flip that a naive merge of the embed-scope branch and
# the SSR-scope branch produced: both added a boolean named ``strict_scope``
# with opposite defaults and opposite meanings, so this caller — which
# passes the NON-strict value because a published widget index is not weak
# evidence — silently inherited "never return empty" and shipped a foreign
# account roster. Reproduced on a scratch worktree at ba68279 + both
# branches (commit 6c8cfa7):
#
#   AssertionError: contamination reopened:
#     ['10309-92ND SW, 07, TACOMA, WA 98498', '9805 PACIFIC AVE, 12, SUMNER, WA 98390']
#
# Nothing on either branch caught it. It is caught here.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_published_index_roster_that_is_not_ours_still_demotes() -> None:
    """A ``/listings`` INDEX the operator embedded on the property's own page
    is PUBLISHED_INDEX, not OPERATOR_SCOPED: the roster it returns is still
    the whole account. Nothing in it matches this property, so the 2026-07-18
    demote applies — emit nothing, and say why.
    """
    from ma_poc.pms.adapters._universal_recovery import get_notes

    index = "https://heritagepropertyrentals.appfolio.com/listings"
    page = _FakePage(
        url="https://heritageamitycommons.com/",
        live=[index],
        responses={index: _ACCOUNT_ROSTER_SSR},
    )
    ctx = _ctx("https://heritageamitycommons.com/", "606A Lake Dr", "19518")
    units = await recover_appfolio_embed(page, ctx)  # type: ignore[arg-type]
    assert units == [], (
        f"contamination reopened: {[u['floor_plan_name'] for u in units]}"
    )
    notes = get_notes(ctx)
    assert [n["reason"] for n in notes] == ["filter_rejected_all_demote"]
    assert "evidence=published_index" in str(notes[0]["detail"])


@pytest.mark.asyncio
async def test_published_index_keeps_its_single_address_passthrough() -> None:
    """...and PUBLISHED_INDEX is NOT the weak grade either. A single-address
    roster reached from an index the operator published is a single-property
    account and still passes through — that pass-through is load-bearing for
    the 34 properties / 296 rows that legitimately recover this way.
    """
    roster = """
    <html><body>
    <article class="js-listing-item" data-listing-id="9001">
      <div class="js-listing-blurb-rent">$1,100</div>
      <div class="js-listing-blurb-bed-bath">1 bd / 1 ba</div>
      <div class="js-listing-square-feet">Square Feet: 600</div>
      <div class="js-listing-address"><span>614 CENTRAL PKWY, 101, CINCINNATI, OH 45202</span></div>
    </article>
    </body></html>
    """
    index = "https://onesixfourteen.appfolio.com/listings"
    page = _FakePage(
        url="https://614central.com/", live=[index], responses={index: roster}
    )
    # ctx address deliberately DISAGREES; the pass-through is what is pinned.
    units = await recover_appfolio_embed(
        page,
        _ctx("https://614central.com/", "1 Nowhere St", "99999"),  # type: ignore[arg-type]
    )
    assert len(units) == 1
