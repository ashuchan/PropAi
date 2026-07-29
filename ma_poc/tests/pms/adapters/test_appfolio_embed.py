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

    async def evaluate(self, _js: str, *args: object) -> object:
        if not args:
            if 'tenant-scan' in (_js or ''):
                return list(self._tenants)
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
    """
    showings = "https://yourmetropolitan.appfolio.com/listings/showings/new?listable_uid=abc123&source=Website"
    canonical = "https://yourmetropolitan.appfolio.com/listings"
    page = _FakePage(
        url="https://www.yourmetropolitan.com/properties/bala/",
        live=[showings],            # the regex matched this URL
        responses={canonical: _APPFOLIO_SSR},  # fetch must canonicalize to here
    )
    units = await recover_appfolio_embed(page, _ctx("https://www.yourmetropolitan.com/"))  # type: ignore[arg-type]
    assert len(units) == 2
    # 2026-07-28: the SSR address lands in unit_name; floor_plan_name is
    # empty because AppFolio SSR cards publish no plan name. Asserting on
    # unit_name still proves the fetch canonicalized to the listings index
    # (the actual subject of this test) rather than the showings form.
    assert units[0]["unit_name"] == "10 Creek Rd"
    assert units[0]["floor_plan_name"] == ""


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
    https://bendermanagement.appfolio.com/listings."""
    login_url = "https://bendermanagement.appfolio.com/connect/users/sign_in"
    canonical = "https://bendermanagement.appfolio.com/listings"
    page = _FakePage(
        url="https://www.aptsedenprairie.com/",
        live=[],                        # no /listings URL on page
        tenants=[login_url],            # but tenant URL is there
        responses={canonical: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(
        page, _ctx("https://www.aptsedenprairie.com/")  # type: ignore[arg-type]
    )
    assert len(units) == 2, "expected 2 units parsed from the canonical /listings page"
    assert units[0]["extraction_tier"] == "TIER_1_DOM_APPFOLIO_SSR"


@pytest.mark.asyncio
async def test_recover_via_tenant_only_request_access_url() -> None:
    """Same fallback fires on /connect/users/request_access (rentdwp pattern)."""
    req_url = "https://dougwettonproperties.appfolio.com/connect/users/request_access"
    canonical = "https://dougwettonproperties.appfolio.com/listings"
    page = _FakePage(
        url="https://www.rentdwp.com/parkviewpalms",
        live=[],
        tenants=[req_url],
        responses={canonical: _APPFOLIO_SSR},
    )
    units = await recover_appfolio_embed(
        page, _ctx("https://www.rentdwp.com/")  # type: ignore[arg-type]
    )
    assert len(units) == 2


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
