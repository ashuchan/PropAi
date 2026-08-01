"""Current AppFolio detail-template parsing from visible semantic fields."""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._parsing import address_unit_id
from ma_poc.pms.adapters.appfolio import parse_appfolio_detail_page


def _detail_html(
    *,
    address: str,
    summary: str,
    rent: str,
    listing_title: str = "Promotional listing title",
) -> str:
    return f"""
    <html><body><main>
      <style>.grid-17-bed {{ display: block }}</style>
      <div class="header">
        <h1 class="fw-normal js-show-title">
          {address}
          <a class="header__title__map-link" href="https://maps.example/">MAP</a>
        </h1>
        <p class="header__summary js-show-summary">{summary}</p>
      </div>
      <h2 class="listing-detail__title">{listing_title}</h2>
      <ul class="list fw-light js-show-rental-terms">
        <li class="list__item">Rent: {rent}</li>
        <li class="list__item">Application Fee: $45</li>
        <li class="list__item">Security Deposit: {rent}</li>
        <li class="list__item">Available Now</li>
      </ul>
    </main></body></html>
    """


@pytest.mark.parametrize(
    (
        "uid",
        "address",
        "summary",
        "rent",
        "expected_unit",
        "expected_beds",
        "expected_baths",
        "expected_sqft",
        "expected_date",
    ),
    [
        (
            "13c06338-c373-42ae-a236-8687d24c30ad",
            "1251 Aster Drive - 108, 108, Tiffin, IA 52340",
            "1 bd, 1 ba, 821 Sq. Ft. | Available 10/10/26",
            "$1,365",
            "108",
            "1",
            "1",
            "821",
            "2026-10-10",
        ),
        (
            "501d3938-dce7-4938-b43b-55bed06c2dd2",
            "17037 Loring Ln, Lindale, TX 75771",
            "3 bd, 2 ba, 1,180 Sq. Ft. | Available 8/10/26",
            "$1,375",
            "",
            "3",
            "2",
            "1180",
            "2026-08-10",
        ),
        (
            "9424e2e3-c0f9-46e4-82cd-6f284b64ea62",
            "5402 E. 30th Street, E01, Tucson, AZ 85711",
            "4 bd, 1.5 ba, 1,400 Sq. Ft. | Available Now",
            "$1,695",
            "E01",
            "4",
            "1.5",
            "1400",
            "",
        ),
    ],
)
def test_current_template_reads_visible_summary_not_embedded_noise(
    uid: str,
    address: str,
    summary: str,
    rent: str,
    expected_unit: str,
    expected_beds: str,
    expected_baths: str,
    expected_sqft: str,
    expected_date: str,
) -> None:
    url = f"https://tenant.appfolio.com/listings/detail/{uid}"
    rows = parse_appfolio_detail_page(
        _detail_html(
            address=address,
            summary=summary,
            rent=rent,
            listing_title="Tour today! Promotional marketing copy",
        ),
        url,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == expected_unit
    assert row["unit_name"] == address
    assert row["floor_plan_name"] == ""
    assert row["bedrooms"] == expected_beds
    assert row["bathrooms"] == expected_baths
    assert row["sqft"] == expected_sqft
    assert row["availability_status"] == "AVAILABLE"
    assert row["availability_date"] == expected_date
    assert row["market_rent_low"] == int(rent[1:].replace(",", ""))
    assert row["source_ids"] == {"appfolio_listable_uid": uid}
    assert row["unit_id"] == address_unit_id(address)
    assert "data_gaps" not in row


def test_current_template_does_not_promote_marketing_h2_to_floorplan() -> None:
    rows = parse_appfolio_detail_page(
        _detail_html(
            address="17037 Loring Ln, Lindale, TX 75771",
            summary="3 bd, 2 ba, 1,180 Sq. Ft. | Available 8/10/26",
            rent="$1,375",
            listing_title="3 Bedroom Duplex for Rent in Lindale! Tour Today!",
        ),
        (
            "https://cross.appfolio.com/listings/detail/"
            "501d3938-dce7-4938-b43b-55bed06c2dd2"
        ),
    )

    assert rows[0]["floor_plan_name"] == ""
    assert rows[0]["unit_name"] == "17037 Loring Ln, Lindale, TX 75771"
