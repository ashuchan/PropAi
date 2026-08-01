"""Property-scoped ManageBuilding route promotion.

The fixtures model the three exact 549-cohort tenants live-probed on
2026-08-01: two safe recoveries and one portfolio-contamination control.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters import _managebuilding_recovery as recovery
from ma_poc.pms.adapters._managebuilding_recovery import (
    discover_managebuilding_route,
    recover_managebuilding,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS


def _card(
    listing_id: str,
    *,
    title: str,
    location: str,
    beds: str = "2",
    baths: str = "1",
    sqft: str = "900",
    rent: str = "1200.00",
) -> str:
    return f"""
    <a class="featured-listing" href="/Resident/public/rentals/{listing_id}"
       data-location="{location}" data-bedrooms="{beds}"
       data-bathrooms="{baths}" data-square-feet="{sqft}"
       data-rent="{rent}">
      <h3 class="featured-listing__title">{title}</h3>
      <p class="featured-listing__address">{location}</p>
      <p class="featured-listing__availability">available December 31, 2099</p>
    </a>
    """


def _index(account_label: str, cards: list[str]) -> str:
    return (
        "<html><body><header>"
        f'<img alt="{account_label}" src="/logo.png">'
        "</header>"
        + "".join(cards)
        + f"<footer><h2>{account_label}</h2></footer></body></html>"
    )


def _ctx(
    source_body: str,
    *,
    property_id: str,
    property_name: str,
    address: str,
    city: str,
    state: str,
    zip_code: str,
) -> AdapterContext:
    return AdapterContext(
        base_url="https://marketing.example/",
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id=property_id,
        fetch_result=SimpleNamespace(
            body=source_body.encode("utf-8"),
            final_url="https://marketing.example/",
        ),
        property_name=property_name,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
    )


def test_discovers_only_one_operator_authored_tenant_and_listing_ids() -> None:
    body = """
      <a href="https://allstatepropertymanagement.managebuilding.com/Resident/
      rental-application/new?unitId=308479&amp;buildingId=95082&amp;listingId=31630">
      Apply</a>
      <script>"https://ignored.managebuilding.com/Resident/public/rentals"</script>
    """.replace("/Resident/\n      rental", "/Resident/rental")

    route = discover_managebuilding_route(body)

    assert route is not None
    assert route.index_url == (
        "https://allstatepropertymanagement.managebuilding.com/Resident/public/rentals"
    )
    assert route.listing_ids == frozenset({"31630"})


def test_ambiguous_or_deceptive_tenant_links_fail_closed() -> None:
    assert (
        discover_managebuilding_route(
            """
            <a href="https://one.managebuilding.com/Resident/portal/login">One</a>
            <a href="https://two.managebuilding.com/Resident/portal/login">Two</a>
            """
        )
        is None
    )
    assert (
        discover_managebuilding_route(
            '<a href="https://tenant.managebuilding.com.evil.test/Resident/portal/login">x</a>'
        )
        is None
    )


def test_eagles_pointe_listing_whitelist_recovers_six_and_drops_portfolio_spill(
    monkeypatch,
) -> None:
    listing_ids = ("31433", "31630", "31631", "31632", "31633", "31635")
    source = "".join(
        (
            "<a href=\"https://allstatepropertymanagement.managebuilding.com/"
            "Resident/rental-application/new?unitId=308479&amp;buildingId=95082"
            f"&amp;listingId={listing_id}\">Apply</a>"
        )
        for listing_id in listing_ids
    )
    titles = (
        "39023 Edwards Court",
        "1005 N Lincoln St",
        "1029 N Lincoln St",
        "1703 South Lincoln Street",
        "901 W Main Road",
        "42 Foxwood Lane",
    )
    cards = [
        _card(listing_id, title=title, location="Peru,IN|46970")
        for listing_id, title in zip(listing_ids, titles, strict=True)
    ]
    # Same account and same target location, but not operator-whitelisted.
    cards.append(_card("99999", title="1 Foreign Street", location="Peru,IN|46970"))
    # Account-wide foreign inventory is also present and must never leak.
    cards.append(
        _card("88888", title="1 Other Road", location="Indianapolis,IN|46237")
    )
    html = _index("Allstate Property Management LLC", cards)

    async def fake_fetch(url: str) -> tuple[str, str]:
        return html, url

    monkeypatch.setattr(recovery, "_fetch_index", fake_fetch)
    ctx = _ctx(
        source,
        property_id="14943",
        property_name="Estates at Eagle's Pointe",
        address="2002 Shaw Ave",
        city="Peru",
        state="IN",
        zip_code="46970",
    )

    rows = asyncio.run(recover_managebuilding(ctx))

    assert len(rows) == 6
    assert {row["source_ids"]["managebuilding_listing_id"] for row in rows} == set(
        listing_ids
    )
    assert {row["unit_number"] for row in rows} == set(titles)
    assert all(unit_has_real_anchor(row) for row in rows)
    assert all(row["availability_date"] == "2099-12-31" for row in rows)


def test_town_center_exact_account_and_location_recovers_seventeen(
    monkeypatch,
) -> None:
    source = (
        '<a href="https://mhtowncenter.managebuilding.com/Resident/'
        'rental-application/new">Apply now</a>'
    )
    titles = [
        "115 Town Center Parkway - 1001",
        "115 Town Center Parkway - 1105 - ADA",
    ] + [f"131 Town Center Parkway - {1200 + i}" for i in range(15)]
    html = _index(
        "Town Center Apartments",
        [
            _card(str(1566 + i), title=title, location="Madison Heights,VA|24572")
            for i, title in enumerate(titles)
        ],
    )

    async def fake_fetch(url: str) -> tuple[str, str]:
        return html, url

    monkeypatch.setattr(recovery, "_fetch_index", fake_fetch)
    ctx = _ctx(
        source,
        property_id="280355",
        property_name="Town Center Apartments",
        address="4653 S Amherst Hwy",
        city="Madison Heights",
        state="VA",
        zip_code="24572",
    )

    rows = asyncio.run(recover_managebuilding(ctx))

    assert len(rows) == 17
    assert {row["unit_number"] for row in rows}.issuperset({"1001", "1105-ADA"})
    assert all(unit_has_real_anchor(row) for row in rows)


def test_grand_oaks_rejects_wrong_city_portfolio_inventory(monkeypatch) -> None:
    source = (
        '<a href="https://landcoproperties.managebuilding.com/Resident/'
        'portal/login">Resident center</a>'
    )
    html = _index(
        "Landco Property Management",
        [
            _card(str(53986 + i), title=f"{1625 + i} Wichman Lane", location="Shelbyville,IN|46176")
            for i in range(4)
        ],
    )

    async def fake_fetch(url: str) -> tuple[str, str]:
        return html, url

    monkeypatch.setattr(recovery, "_fetch_index", fake_fetch)
    ctx = _ctx(
        source,
        property_id="74528",
        property_name="Grand Oaks",
        address="10230 John Jay Dr",
        city="Indianapolis",
        state="IN",
        zip_code="46237",
    )

    assert asyncio.run(recover_managebuilding(ctx)) == []
