from __future__ import annotations

import json

from ma_poc.pms.adapters.entrata import (
    _beans_map_url,
    _extract_beans_map_pairs,
    _fetch_published_grid_and_beans_sync,
    parse_entrata_beans_map,
)

GRID_URL = "https://www.example-apts.com/city/example/conventional/"
BEANS_URL = (
    "https://www.example-apts.com/Apartments/module/property_info/"
    "action/view_beans_map/property%5Bid%5D/1182838/"
    "?occupancy_type=conventional&analytics=1"
)


def _grid(*iframe_urls: str) -> str:
    frames = "".join(f'<iframe id="beans-maps-iframe" data-src="{url}"></iframe>' for url in iframe_urls)
    return f"<html><body><h1>Example Hills Apartments</h1>{frames}</body></html>"


def _card(
    *,
    unit: str = "A-101",
    uid: str = "4870030",
    listing_id: str = "4598003",
    plan: str = "A1",
    fpid: str = "1041356",
    rent: str = "$1,425 - $1,675",
    availability: str = "Available Sep 13, 2026",
) -> str:
    return f"""
    <div class="beans-map-unit-listing-container" data-unit-id="{listing_id}">
      <div class="beans-map-preview-content-header-title">{unit}</div>
      <span class="beans-map-preview-content-detail-text">{plan}</span>
      <span class="beans-map-preview-content-detail-text">1 Bed</span>
      <span class="beans-map-preview-content-detail-text">1 Bath</span>
      <span class="beans-map-preview-content-detail-text">725 SqFt</span>
      <span class="beans-map-preview-content-detail-text">Floor 2</span>
      <span class="beans-map-preview-content-availability-text">
        {availability}
      </span>
      <span class="beans-map-preview-content-pricing-price">
        <span class="fee-transparency-text">{rent}</span>
      </span>
      <span class="lease-term-name">6-15mo lease</span>
      <button data-unit-number="{unit}" data-unit-id="{uid}"
              data-floorplan-id="{fpid}" data-floorplan-name="{plan}">
        Calculate
      </button>
    </div>
    """


def _item(
    *,
    address: str = "715 Ash Lane, San Marcos, CA 92069, US",
    **card_kwargs: str,
) -> dict[str, object]:
    unit = card_kwargs.get("unit", "A-101")
    availability = card_kwargs.get("availability", "Available Sep 13, 2026")
    return {
        "address": address,
        "unit": unit,
        "options": {
            "onPreviewData": [{"value": availability}],
            "onCardContent": _card(**card_kwargs),
        },
    }


def _map_body(*arrays: list[dict[str, object]]) -> str:
    return "\n".join(
        f'<script>var be = new BeansMap(); be.render("map-{i}", "token", {json.dumps(array)});</script>'
        for i, array in enumerate(arrays)
    )


def test_beans_map_url_requires_one_exact_same_origin_published_iframe() -> None:
    assert _beans_map_url(_grid(BEANS_URL), GRID_URL) == (BEANS_URL, "1182838")

    cross_origin = BEANS_URL.replace("www.example-apts.com", "other.example")
    assert _beans_map_url(_grid(cross_origin), GRID_URL) == ("", "")
    second = BEANS_URL.replace("1182838", "1182839")
    assert _beans_map_url(_grid(BEANS_URL, second), GRID_URL) == ("", "")


def test_extract_pairs_requires_visible_configured_property_name() -> None:
    assert _extract_beans_map_pairs(
        [(GRID_URL, _grid(BEANS_URL))],
        "https://www.example-apts.com",
        "Example Hills",
    ) == [(GRID_URL, BEANS_URL, "1182838")]
    assert not _extract_beans_map_pairs(
        [(GRID_URL, _grid(BEANS_URL))],
        "https://www.example-apts.com",
        "Different Property",
    )


def test_parse_beans_map_collapses_duplicate_2d_3d_rows_with_full_fields() -> None:
    item = _item()
    rows = parse_entrata_beans_map(
        _map_body([item], [item]),
        BEANS_URL,
        expected_address="715 Ash Ln",
        expected_zip="92069",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == "A-101"
    assert row["floor_plan_name"] == "A1"
    assert row["bedrooms"] == "1"
    assert row["bathrooms"] == "1"
    assert row["sqft"] == "725"
    assert row["floor"] == "2"
    assert row["market_rent_low"] == 1425
    assert row["market_rent_high"] == 1675
    assert row["availability_date"] == "2026-09-13"
    assert row["lease_term"] == "6-15mo lease"
    assert row["extraction_tier"] == "TIER_1_DOM_ENTRATA_BEANS_MAP"
    assert row["source_ids"] == {
        "entrata_uid": "4870030",
        "entrata_fpid": "1041356",
        "entrata_property_id": "1182838",
        "entrata_beans_listing_id": "4598003",
    }


def test_parse_beans_map_fails_closed_on_mixed_address_or_conflict() -> None:
    matching = _item()
    wrong_address = _item(
        unit="B-202",
        uid="4870031",
        listing_id="4598004",
        address="999 Other Road, San Marcos, CA 92069, US",
    )
    assert not parse_entrata_beans_map(
        _map_body([matching, wrong_address]),
        BEANS_URL,
        expected_address="715 Ash Ln",
        expected_zip="92069",
    )

    conflicting_duplicate = _item(rent="$1,900 - $2,100")
    assert not parse_entrata_beans_map(
        _map_body([matching], [conflicting_duplicate]),
        BEANS_URL,
        expected_address="715 Ash Ln",
        expected_zip="92069",
    )


def test_parse_beans_map_rejects_missing_positive_rent_or_native_identity() -> None:
    assert not parse_entrata_beans_map(
        _map_body([_item(rent="Call for Rent")]),
        BEANS_URL,
        expected_address="715 Ash Ln",
        expected_zip="92069",
    )
    assert not parse_entrata_beans_map(
        _map_body([_item(uid="not-native")]),
        BEANS_URL,
        expected_address="715 Ash Ln",
        expected_zip="92069",
    )


def test_published_grid_and_beans_share_one_cookie_session(monkeypatch) -> None:
    map_body = _map_body([_item()])

    class Response:
        def __init__(self, status_code: int, text: str, url: str) -> None:
            self.status_code = status_code
            self.text = text
            self.url = url

    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []
            self.closed = False

        def get(
            self, url: str, *, timeout: int, headers: dict[str, str] | None = None
        ) -> Response:
            del timeout
            self.calls.append((url, headers or {}))
            if url == GRID_URL:
                return Response(200, _grid(BEANS_URL), GRID_URL)
            if url == BEANS_URL:
                return Response(200, map_body, BEANS_URL)
            raise AssertionError(f"unexpected URL {url}")

        def close(self) -> None:
            self.closed = True

    session = Session()
    monkeypatch.setattr(
        "curl_cffi.requests.Session",
        lambda *, impersonate: session,
    )

    grid_html, final_grid, rows, winning_url = (
        _fetch_published_grid_and_beans_sync(
            GRID_URL,
            property_name="Example Hills",
            expected_address="715 Ash Ln",
            expected_zip="92069",
        )
    )

    assert grid_html == _grid(BEANS_URL)
    assert final_grid == GRID_URL
    assert len(rows) == 1
    assert winning_url == BEANS_URL
    assert [call[0] for call in session.calls] == [GRID_URL, BEANS_URL]
    assert session.calls[1][1]["Referer"] == GRID_URL
    assert session.calls[1][1]["X-Requested-With"] == "XMLHttpRequest"
    assert session.closed
