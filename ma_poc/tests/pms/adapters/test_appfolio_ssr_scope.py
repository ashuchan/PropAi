"""2026-07-28 defect ``ssr-scope-and-filter`` — the main AppFolio
adapter's SSR branch shipped whole account rosters as one property.

REPRODUCED on run 0d54ca7 (100/100 shards, full census, 4,982 props):
``TIER_1_DOM_APPFOLIO_SSR*`` carried 118 properties / 11,250 rows, and
11,130 of those rows carry a parseable ZIP of which 10,416 (93.6%) sat
at a ZIP other than the property's own.

115 of those properties (11,033 rows) are the embed-recovery path, which
imports ``parse_appfolio_listings_ssr`` directly and never enters
``AppFolioAdapter.extract`` — a different defect, fixed elsewhere. The
residual on THIS branch is 5 properties / 220 rows, worst case pid
260505 Onyx Uptown PHX (Phoenix AZ 85013), which shipped 211 rows of
which 204 were at another ZIP entirely (Tempe 85283, Mesa 85210, Tucson
85710, even Las Vegas NV 89113) because its saved-profile replay landed
on ``chamberlin.appfolio.com/listings`` — the operator's whole roster.

The branch had ``ctx.address`` and ``ctx.zip_code`` populated and never
read them, and never asked for the server-side ``filters[property_list]``
scope, even though both levers already lived in the same module.

This file pins:
  1. ``appfolio_urls.is_account_index_url`` — the evidence distinction. An
     account-wide roster is weak evidence; a surface the operator scoped
     is strong. Includes the URLs it must NOT claim.
  2. ``_listing_address_is_judgeable`` — a card whose address span the
     parser could not read is UNJUDGEABLE. Dropping it under lenient
     scope would manufacture an absence out of a parse gap.
  3. ``filter_listings_by_property_address(evidence=...)`` —
     ``PUBLISHED_INDEX`` is the pre-existing vanity/DUDA behaviour,
     unchanged, and the default.
  4. The branch end-to-end: roster body gets scoped, operator-scoped
     body is left alone, and NEITHER is ever zeroed.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest

from ma_poc.pms.adapters._parsing import is_street_address
from ma_poc.pms.adapters.appfolio import (
    AppFolioAdapter,
    ScopeEvidence,
    _listing_address_is_judgeable,
    _listing_address_of,
    filter_listings_by_property_address,
    parse_appfolio_listings_ssr,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.appfolio_urls import is_account_index_url, scoped_listings_url
from ma_poc.pms.detector import detect_pms


@dataclass
class _StubFetchResult:
    body: bytes | str | None
    final_url: str = ""


class _DummyPage:
    """The SSR branch never touches the page when a body is present."""

    async def content(self) -> str:  # pragma: no cover — defensive
        return ""


def _ctx(base_url: str, body: str, address: str = "", zip_code: str = "",
         final_url: str = "") -> AdapterContext:
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
        fetch_result=_StubFetchResult(body=body.encode("utf-8"),
                                      final_url=final_url or base_url),
        address=address,
        zip_code=zip_code,
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    return ctx


def _card(listing_id: str, address: str, rent: str = "$1,500") -> str:
    return f"""
    <article class="listing-item js-listing-item" data-listing-id="{listing_id}">
      <div class="js-listing-blurb-rent">{rent}</div>
      <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
      <div class="js-listing-square-feet">Square Feet: 900</div>
      <span class="js-listing-address">{address}</span>
    </article>
    """


# ─────────────────────────────────────────────────────────────────────
# 1. The evidence distinction
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        # MUST be treated as an account-wide roster.
        ("https://chamberlin.appfolio.com/listings", True),
        ("https://olympicmanagement.appfolio.com/listings", True),
        ("http://illumepm.appfolio.com/listings/", True),
        ("https://ILLUMEPM.AppFolio.COM/LISTINGS", True),
        ("https://www.chamberlin.appfolio.com/listings", True),
        ("https://chamberlin.appfolio.com/listings#cards", True),
        # ``portfolio_uuid`` is NOT a property scope — live-tested
        # 2026-07-28, byte-identical response with and without it.
        ("https://illumepm.appfolio.com/listings?portfolio_uuid=abc", True),
        # MUST NOT — already carries the server-side per-property scope.
        ("https://illumepm.appfolio.com/listings"
         "?filters%5Bproperty_list%5D=brookside", False),
        ("https://illumepm.appfolio.com/listings"
         "?filters[property_list]=brookside", False),
        # MUST NOT — deeper paths are a single listing, not a roster.
        ("https://yourmetropolitan.appfolio.com/listings/detail/46495a3b", False),
        ("https://yourmetropolitan.appfolio.com/listings/showings/new"
         "?listable_uid=x", False),
        # MUST NOT — the operator's own marketing pages (live 2026-07-28:
        # /properties/tareyton-estates/ and /properties/bala/ serve
        # different card sets out of one AppFolio account).
        ("https://www.yourmetropolitan.com/properties/tareyton-estates/", False),
        ("https://www.bamliving.com/nt", False),
        # MUST NOT — no tenant slug / reserved slug / lookalike host.
        ("https://www.appfolio.com/terms/listings", False),
        ("https://appfolio.com/listings", False),
        ("https://secure.appfolio.com/listings", False),
        ("https://chamberlin.appfolio.com.evil.example/listings", False),
        ("https://evil-appfolio.com/listings", False),
        # MUST NOT — not the index path, not http(s), not a URL.
        ("https://chamberlin.appfolio.com/", False),
        ("https://chamberlin.appfolio.com/listingsx", False),
        ("ftp://chamberlin.appfolio.com/listings", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_account_index_url_predicate(url: str, expected: bool) -> None:
    assert is_account_index_url(url) is expected


def test_scoped_listings_url_encodes_the_group() -> None:
    """The narrow URL is the live-verified shape: illumepm's roster is
    metro-wide, the scoped one returns 12 cards all in 97222."""
    assert scoped_listings_url(
        "https://olympicmanagement.appfolio.com/listings", "COLLEGE PARK"
    ) == ("https://olympicmanagement.appfolio.com/listings"
          "?filters%5Bproperty_list%5D=COLLEGE%20PARK")
    # Group names carry punctuation; it must survive percent-encoding.
    assert scoped_listings_url(
        "https://illumepm.appfolio.com/listings/", "Foo - Bar & Baz #1"
    ) == ("https://illumepm.appfolio.com/listings"
          "?filters%5Bproperty_list%5D=Foo%20-%20Bar%20%26%20Baz%20%231")


# ─────────────────────────────────────────────────────────────────────
# 2. Judgeability — the guard against manufacturing an absence
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address,expected",
    [
        ("3140 N Clybourn Ave Unit 205, Chicago, IL 60618", True),
        ("999 E Baseline Rd, 2406, Tempe, AZ 85283", True),
        ("7440 N. Sepulveda Blvd., 120, Van Nuys, CA 91405", True),
        ("1420 Oak St", True),
        ("  88 Main Street Apt 3", True),
        # The SSR parser's fallback when it cannot read an address span.
        # Real rows from pids 295964 / 18634 / 292955 in run 0d54ca7.
        ("AppFolio listing 8545", False),
        ("AppFolio listing 4475", False),
        ("Non-Resident Parking 05", False),
        ("The Willows", False),
        ("", False),
    ],
)
def test_listing_address_is_judgeable(address: str, expected: bool) -> None:
    assert _listing_address_is_judgeable(address) is expected


# ─────────────────────────────────────────────────────────────────────
# 3. ScopeEvidence on the shared filter
# ─────────────────────────────────────────────────────────────────────


def _unit(address: str, uid: str) -> dict[str, str]:
    return {"floor_plan_name": address, "unit_number": uid,
            "rent_range": "$1,500"}


def test_published_index_is_the_default_and_unchanged() -> None:
    """The vanity / DUDA callers pass no ``evidence``; their
    demote-to-empty behaviour must be byte-for-byte what it was."""
    units = [_unit("4801 Meadowview Dr., Erie, PA 16509", "a"),
             _unit("1106 Hammocks Drive, Canandaigua, NY 14424", "b")]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="11 W 3rd St", ctx_zip="14830"
    )
    assert filtered == []
    assert tel["reason"] == "filter_rejected_all_demote"
    assert tel["unscopeable"] is True


def test_lenient_keeps_unjudgeable_rows() -> None:
    """pid 292955 (3140 Clybourn, Chicago 60618) shape: one card the
    parser could read, one it could not. Lenient scope keeps BOTH —
    the unreadable card is a parse gap, not evidence of contamination."""
    units = [_unit("3140 N Clybourn Ave Unit 205, Chicago, IL 60618", "a"),
             _unit("AppFolio listing 4475", "b")]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="3140 N Clybourn Ave", ctx_zip="60618",
        evidence=ScopeEvidence.OPERATOR_SCOPED,
    )
    assert {u["unit_number"] for u in filtered} == {"a", "b"}
    assert tel["dropped"] == 0


def test_lenient_still_drops_a_judgeable_mismatch() -> None:
    """pid 17555 (North Tower, Van Nuys 91405) shape: the operator's own
    page renders the whole roster and hides the rest client-side. Rows
    that DO carry an address and disagree are still dropped."""
    units = [_unit("7440 N. Sepulveda Blvd., 120, Van Nuys, CA 91405", "keep"),
             _unit("2069 N. Argyle Ave., 304, Hollywood Hills, CA 90068", "d1"),
             _unit("10741 Camarillo St., 116, Toluca Lake, CA 91602", "d2"),
             _unit("14242 Roscoe Blvd., 102, Panorama City, CA 91402", "d3")]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="7440 North Sepulveda Blvd", ctx_zip="91405",
        evidence=ScopeEvidence.OPERATOR_SCOPED,
    )
    assert [u["unit_number"] for u in filtered] == ["keep"]
    assert tel["dropped"] == 3


def test_lenient_never_returns_empty() -> None:
    """Every row judgeable, none matching, on an operator-scoped surface
    → keep the rows and let validation flag the mismatch. Emitting zero
    here would be an absence manufactured from a CSV disagreement."""
    units = [_unit("1 A St, Nowhere, TX 75001", "a"),
             _unit("2 B St, Nowhere, TX 75001", "b")]
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="99 Elsewhere Ave", ctx_zip="19047",
        evidence=ScopeEvidence.OPERATOR_SCOPED,
    )
    assert filtered == units
    assert tel["reason"] == "filter_rejected_all_lenient_keep"
    assert tel["dropped"] == 0


# ─────────────────────────────────────────────────────────────────────
# 4. The branch, end to end
# ─────────────────────────────────────────────────────────────────────


_ROSTER = "<html><body>" + "".join([
    _card("14638", "999 E Baseline Rd, 2406, Tempe, AZ 85283"),
    _card("12291", "1750 S Alma School Rd, Mesa, AZ 85210"),
    _card("11663", "7017 S Buffalo Dr., Las Vegas, NV 89113"),
    _card("9001", "500 W Camelback Rd, 210, Phoenix, AZ 85013"),
]) + "</body></html>"


def _container_card(listing_id: str, address: str) -> str:
    """The other live AppFolio card shape: a ``js-listing-item`` DIV whose
    id is ``listing_N``, with ``data-listing-id`` on a map-link anchor in
    the MIDDLE of the card. Verified against the captured body for pid
    17555 (bamliving) in run-2026-07-27-full-0d54ca7.
    """
    return f"""
    <div class="listing-item result js-listing-item" id="listing_{listing_id}">
      <div class="js-listing-blurb-rent">$1,500</div>
      <div class="js-listing-blurb-bed-bath">2 bd / 2 ba</div>
      <span class="js-listing-address">{address}</span>
      <a href="#" class="js-listing-map-view-link"
         data-listing-id="{listing_id}"></a>
      <span class="js-listing-square-feet">Square Feet: 900</span>
    </div>
    """


_CONTAINER_ROSTER = "<html><body>" + "".join([
    _container_card("14638", "999 E Baseline Rd, 2406, Tempe, AZ 85283"),
    _container_card("12291", "1750 S Alma School Rd, Mesa, AZ 85210"),
    _container_card("11663", "7017 S Buffalo Dr., Las Vegas, NV 89113"),
    _container_card("9001", "500 W Camelback Rd, 210, Phoenix, AZ 85013"),
]) + "</body></html>"


# ─────────────────────────────────────────────────────────────────────
# 3b. PARSER -> FILTER CONTRACT — the invariant that silently broke.
#
# 2026-07-29. Two individually-correct changes collided with ZERO merge
# conflicts and reopened the contamination defect this file exists to
# close:
#
#   (A) the scope work filters the account roster on a named field,
#       defaulting to ``floor_plan_name`` — where the SSR parser used to
#       put the street address;
#   (B) "a street address is not a floor plan name" moved that string to
#       ``unit_name`` and blanked ``floor_plan_name``.
#
# Merged, the filter's input became the empty string on every row, so
# every row was unjudgeable and EVERY account-roster listing passed. No
# conflict, no failing test on either branch.
#
# Neither branch could catch it because every existing test of the filter
# feeds it HAND-BUILT dicts (``_unit()`` above, which hard-codes
# ``floor_plan_name=address``). A hand-built dict cannot disagree with the
# parser. These tests close that gap: they run the REAL parser and assert
# the filter can still read what it produced.
# ─────────────────────────────────────────────────────────────────────


def _default_address_field() -> str:
    """The field the address filter actually reads, taken from the
    signature rather than restated — so renaming the default here cannot
    drift away from the production default."""
    param = inspect.signature(
        filter_listings_by_property_address
    ).parameters["address_field"]
    assert isinstance(param.default, str)
    return param.default


@pytest.mark.parametrize("roster", [_ROSTER, _CONTAINER_ROSTER],
                         ids=["anchor-card", "container-card"])
def test_ssr_parser_fills_the_field_the_address_filter_reads(roster: str) -> None:
    """THE GUARD. Whatever field the filter is configured to read, the SSR
    parser must actually put a street address there — on BOTH live card
    shapes. Move the address without teaching the filter where it went and
    this goes red immediately, instead of silently passing the whole
    account roster through.
    """
    units = parse_appfolio_listings_ssr(
        roster, "https://chamberlin.appfolio.com/listings"
    )
    assert len(units) == 4, f"parser lost cards: {units}"
    field = _default_address_field()
    for u in units:
        seen = _listing_address_of(u, field)
        assert seen, (
            f"the address filter reads {field!r} and got NOTHING for {u!r}. "
            "Every row is now unjudgeable and the whole account roster "
            "ships as one property — this is the collision, not a typo."
        )
        assert is_street_address(seen), (
            f"{seen!r} is not address-shaped; the filter cannot scope on it"
        )


@pytest.mark.parametrize("roster", [_ROSTER, _CONTAINER_ROSTER],
                         ids=["anchor-card", "container-card"])
def test_filter_scopes_a_roster_that_came_from_the_real_parser(roster: str) -> None:
    """Same invariant, stated as the outcome that matters: a four-property
    account roster fed straight from the parser into the filter must come
    out as this property's ONE row. This is the assertion that would have
    failed on the merged tree before the fix (it returned all four).
    """
    units = parse_appfolio_listings_ssr(
        roster, "https://chamberlin.appfolio.com/listings"
    )
    filtered, tel = filter_listings_by_property_address(
        units, ctx_address="500 W Camelback Rd", ctx_zip="85013"
    )
    assert len(filtered) == 1, (
        f"account roster not scoped: kept {[u['unit_name'] for u in filtered]}"
    )
    assert "85013" in filtered[0]["unit_name"]
    assert tel["filter_activated"] is True
    assert tel["dropped"] == 3


def test_address_fallback_does_not_mistake_a_unit_number_for_an_address() -> None:
    """The other half of the guard. ``_listing_address_of`` falls back to
    ``unit_name`` only when it is address-SHAPED. On a normal multifamily
    property unit_name is a bare unit number; treating "101" as an address
    would make the row judgeable and then unmatchable, and the filter would
    drop real inventory.
    """
    for bare in ("101", "A1", "2B", ""):
        assert _listing_address_of(
            {"floor_plan_name": "", "unit_name": bare}, "floor_plan_name"
        ) == "", f"{bare!r} must not be judged as a street address"
    # ...and the configured field always wins when it is populated.
    # 2026-07-29: the note that used to sit here — "the DUDA path genuinely
    # maps full_address -> floor_plan_name" — was stale and is why the DUDA
    # call site kept riding the default. Since 2026-07-28 that path maps
    # ``unit_template_name`` -> floor_plan_name and ``full_address`` ->
    # unit_name, so it now names ``address_field="unit_name"`` explicitly
    # (see test_appfolio_address_gate_parity.py). The precedence asserted
    # below is unchanged and still correct.
    assert _listing_address_of(
        {"floor_plan_name": "500 W Camelback Rd, Phoenix, AZ 85013",
         "unit_name": "210"},
        "floor_plan_name",
    ) == "500 W Camelback Rd, Phoenix, AZ 85013"


@pytest.mark.asyncio
async def test_account_roster_is_scoped_to_the_property() -> None:
    """THE DEFECT. pid 260505 shape: the body is an account-wide roster
    at ``<tenant>.appfolio.com/listings``. Before the fix all four cards
    shipped as Onyx Uptown PHX; after it, only the 85013 one does."""
    ctx = _ctx(
        "https://chamberlin.appfolio.com/listings",
        _ROSTER,
        address="500 W Camelback Rd",
        zip_code="85013",
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 1
    # 2026-07-29: the SSR parser now writes the listing address to
    # ``unit_name`` (it is an address, not a plan name). The behavioural
    # claim is unchanged — the surviving row must be the 85013 one, not
    # Tempe/Mesa/Las Vegas — only the column it is read from moved.
    assert "85013" in result.units[0]["unit_name"]
    assert any("appfolio-ssr-address-filter" in e
               and "evidence=published_index" in e
               for e in result.errors)


@pytest.mark.asyncio
async def test_operator_scoped_page_keeps_unreadable_cards() -> None:
    """pid 295964 / 18634 shape (live-verified 2026-07-28): the operator
    SSRs the cards into their OWN per-property page and our parser can
    read no address out of them. The branch must not zero the property."""
    body = ("<html><body>"
            + _card("8925", "")
            + _card("8545", "")
            + "</body></html>")
    ctx = _ctx(
        "https://www.yourmetropolitan.com/properties/tareyton-estates/",
        body,
        address="100 Barclay Ct",
        zip_code="19047",
    )
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_APPFOLIO_SSR"
    assert len(result.units) == 2, "unreadable cards must not be dropped"


@pytest.mark.asyncio
async def test_roster_without_ctx_address_is_left_alone() -> None:
    """No CSV address AND no CSV ZIP → the filter cannot run. Pass the
    rows through rather than inventing a zero."""
    ctx = _ctx("https://chamberlin.appfolio.com/listings", _ROSTER)
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 4


@pytest.mark.asyncio
async def test_server_side_scope_is_preferred_over_the_address_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A roster body carrying a ``propertyGroup`` gets the narrow URL
    refetched, and the scoped response is what ships — including cards
    whose address our post-hoc filter could not have matched."""
    scoped_body = ("<html><body>"
                   + _card("9001", "500 W Camelback Rd, 210, Phoenix, AZ 85013")
                   + _card("9002", "")
                   + "</body></html>")
    seen: list[str] = []

    async def _fake_fetch(url: str, result: object) -> str:
        seen.append(url)
        return scoped_body

    monkeypatch.setattr(
        "ma_poc.pms.adapters.appfolio._fetch_appfolio_listings_html",
        _fake_fetch,
    )
    body = _ROSTER.replace(
        "<body>", "<body><script>Appfolio.Listing({propertyGroup: 'Onyx Uptown'})</script>"
    )
    ctx = _ctx("https://chamberlin.appfolio.com/listings", body,
               address="500 W Camelback Rd", zip_code="85013")
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert seen == ["https://chamberlin.appfolio.com/listings"
                    "?filters%5Bproperty_list%5D=Onyx%20Uptown"]
    # 2 rows: the scoped response is authoritative, so the address-less
    # card survives where the post-hoc filter would have dropped it.
    assert len(result.units) == 2
    assert any("appfolio-ssr-server-scope" in e for e in result.errors)


@pytest.mark.asyncio
async def test_empty_scoped_refetch_falls_back_never_zeroes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale or placeholder group name matches no server-side list and
    returns nothing. "Could not look" is not "nothing there" — fall back
    to the address filter on the body we already have."""

    async def _fake_fetch(url: str, result: object) -> str:
        return ""

    monkeypatch.setattr(
        "ma_poc.pms.adapters.appfolio._fetch_appfolio_listings_html",
        _fake_fetch,
    )
    body = _ROSTER.replace(
        "<body>", "<body><script>Appfolio.Listing({propertyGroup: 'Stale'})</script>"
    )
    ctx = _ctx("https://chamberlin.appfolio.com/listings", body,
               address="500 W Camelback Rd", zip_code="85013")
    result = await AppFolioAdapter().extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    # See above — address column moved to ``unit_name``; the claim (the
    # fallback filter kept THIS property's row, not the other three) is
    # asserted unchanged.
    assert "85013" in result.units[0]["unit_name"]
