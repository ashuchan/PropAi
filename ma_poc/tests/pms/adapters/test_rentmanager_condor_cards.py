"""RentManager Condor-family SSR unit-card recovery.

The two card shapes below are trimmed from the 2026-07-31 archived bodies for
Eagan Heights (pid 261458) and Promenade Oaks (pid 35901). Both canary reports
won ``/availability/`` but emitted zero units because generic extraction read
``data-name`` as a plan name and ignored the real RentManager unit id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.extraction.post_process import post_process
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentmanager import (
    RentManagerAdapter,
    parse_rentmanager_condor_cards,
)
from ma_poc.pms.detector import detect_pms

_CONDOR_HTML = """
<html><body>
  <div id="availabilityListing" class="available-units listing-wrapper">
    <!-- Eagan Heights archived row. -->
    <div class="unit-details featured-listing"
         data-name="138" data-unitfloorplan="Basset"
         data-unitid="1961" data-propid="28"
         data-bed="1" data-bath="1.0" data-price="1955"
         data-availability="1778817600000" data-floor="1">
      <img src="https://rm12filereader.rentmanager.com/files/get/?EID=conco">
      <p class="availability-date nowAvailable">
        Available <span data-available="5/15/2026">Now</span>
      </p>
      <h2>Apt #138 – Floor 1</h2>
    </div>

    <!-- Promenade Oaks shape; a published unit may omit the plan name. -->
    <div class="unit-details not-featured"
         data-name="1133" data-unitfloorplan=""
         data-unitid="1328" data-propid="31"
         data-bed="3" data-bath="1.7" data-price="2150"
         data-availability="1789444800000" data-floor="">
      <p class="availability-date">
        Available <span data-available="9/15/2026">9/15/2026</span>
      </p>
      <h2>Apt #1133</h2>
    </div>

    <!-- Duplicate render of uid=1961 must not duplicate the apartment. -->
    <div class="unit-details modal-copy"
         data-name="138" data-unitfloorplan="Basset"
         data-unitid="1961" data-propid="28"
         data-bed="1" data-bath="1.0" data-price="1955"
         data-availability="1778817600000" data-floor="1">
      <p class="availability-date">
        Available <span data-available="5/15/2026">Now</span>
      </p>
    </div>
  </div>
  <footer><a href="https://www.rentmanager.com/">
    Website created by Rent Manager
  </a></footer>
</body></html>
"""


def _without_file_reader(html: str) -> str:
    return html.replace(
        "https://rm12filereader.rentmanager.com/files/get/?EID=conco",
        "/images/unit-138.jpg",
    )


def _without_attribution(html: str) -> str:
    return html.replace(
        '<footer><a href="https://www.rentmanager.com/">\n'
        "    Website created by Rent Manager\n"
        "  </a></footer>",
        "",
    )


def test_parse_condor_cards_maps_unit_fields_and_dedupes_uid() -> None:
    units = parse_rentmanager_condor_cards(
        _CONDOR_HTML,
        "https://eaganheights.com/availability/",
    )

    assert len(units) == 2
    eagan, promenade = units
    assert eagan["unit_number"] == "138"
    assert eagan["floor_plan_name"] == "Basset"
    assert eagan["bedrooms"] == "1"
    assert eagan["bathrooms"] == "1.0"
    assert eagan["floor"] == "1"
    assert eagan["market_rent_low"] == 1955
    assert eagan["market_rent_high"] == 1955
    assert eagan["availability_status"] == "AVAILABLE"
    assert eagan["availability_date"] == "2026-05-15"
    assert eagan["source_ids"] == {"rentmanager_uid": "1961"}
    assert eagan["extraction_tier"] == "TIER_1_DOM_RENTMANAGER_CONDOR_CARDS"

    assert promenade["unit_number"] == "1133"
    assert not promenade["floor_plan_name"]
    assert promenade["bedrooms"] == "3"
    assert promenade["bathrooms"] == "1.7"
    assert promenade["market_rent_low"] == 2150
    assert promenade["availability_date"] == "2026-09-15"
    assert promenade["source_ids"] == {"rentmanager_uid": "1328"}


def test_condor_cards_stay_in_unit_channel_after_post_process() -> None:
    parsed = parse_rentmanager_condor_cards(
        _CONDOR_HTML,
        "https://promenadeoaks-apartments.com/availability/",
    )
    processed = post_process(parsed, property_id="35901")

    assert processed.n_admitted == 2
    assert processed.plan_summaries == []
    assert {row["unit_number"] for row in processed.admitted} == {"138", "1133"}


@pytest.mark.parametrize(
    "html",
    [
        # Strong file-reader host, no footer attribution.
        _without_attribution(_CONDOR_HTML),
        # Strong explicit attribution, no file-reader asset.
        _without_file_reader(_CONDOR_HTML),
    ],
)
def test_detector_routes_only_composite_attributed_condor_cards(html: str) -> None:
    detected = detect_pms("https://example.test/availability/", page_html=html)

    assert detected.pms == "rentmanager"
    assert detected.confidence == 0.92


@pytest.mark.parametrize(
    "html",
    [
        # Attribution alone must not route a property-management brochure.
        "<footer>Website created by Rent Manager</footer>",
        # Generic data attributes plus a vendor asset are not the Condor shape.
        """
        <div data-unit="A-101" data-beds="1" data-baths="1"
             data-rent="1500">1 bedroom $1500</div>
        <img src="https://rm12filereader.rentmanager.com/files/get/x">
        """,
        # Even the card vocabulary is insufficient without vendor attribution.
        _without_attribution(_without_file_reader(_CONDOR_HTML)),
    ],
)
def test_detector_rejects_partial_or_unattributed_signals(html: str) -> None:
    detected = detect_pms("https://example.test/availability/", page_html=html)

    assert detected.pms != "rentmanager"


def test_parser_requires_the_same_composite_attribution_guard() -> None:
    unattributed = _without_attribution(_without_file_reader(_CONDOR_HTML))

    assert parse_rentmanager_condor_cards(unattributed, "https://example.test/") == []
    assert parse_rentmanager_condor_cards("", "https://example.test/") == []


@pytest.mark.asyncio
async def test_adapter_admits_condor_cards_as_units() -> None:
    url = "https://eaganheights.com/availability/"
    detected = detect_pms(url, page_html=_CONDOR_HTML)
    ctx = AdapterContext(
        base_url=url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id="261458",
        fetch_result=SimpleNamespace(body=_CONDOR_HTML),
    )

    result = await RentManagerAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_RENTMANAGER_CONDOR_CARDS"
    assert result.winning_url == url
    assert result.plan_summaries == []
    assert {row["unit_number"] for row in result.units} == {"138", "1133"}
    assert result.errors == []


def test_condor_html_matches_adapter_body_shape() -> None:
    adapter = RentManagerAdapter()

    assert adapter.matches_response_body(_CONDOR_HTML)
    assert not adapter.matches_response_body(_without_attribution(_without_file_reader(_CONDOR_HTML)))
