"""RentManager (iLoveLeasing) adapter — parser + detector wiring tests.

Acceptance (2026-05-18, server-side curl verified on high.ua.rentmanager.com)
- The real unit feed is the no-auth, no-bot-wall
  ``<eid>.ua.rentmanager.com/Search_Result`` endpoint (NOT the JS-only
  iLoveLeasing widget). The full URL is verbatim in the static HTML.
- Response is a sequence of ``document.write("{ ... }")`` chunks, each a
  backtick-delimited pseudo-JSON record with ``unitid`` / ``unit`` /
  ``marketrent`` / ``availabilitydateresult``.
- Verified: high.ua.rentmanager.com → unit 2600 @ $4,971.00.
- Detector routes the rentmanager/iloveleasing HTML marker → "rentmanager".
- Adapter registered.
"""
from __future__ import annotations

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters.registry import get_adapter
from ma_poc.pms.adapters.rentmanager import (
    RentManagerAdapter,
    _rm_iso,
    _rm_money,
    find_rentmanager_search_url,
    parse_rentmanager_search,
    parse_rentmanager_unitlist_cards,
)
from ma_poc.pms.detector import detect_pms

# Faithful trim of the real Search_Result body (2 of 99 records, structure
# verbatim — backtick-delimited, document.write-wrapped).
_RM_BODY = """
document.write("{ `ppropertyname`:`Highland Apartments`, `pid`:`49`,
 `unitid`:`18302`, `unit`:`2600`, `availabilitydateresult`:`5/31/2023`,
 `marketrent`:`$4,971.00` }");
document.write("{ `ppropertyname`:`Highland Apartments`, `pid`:`49`,
 `unitid`:`18311`, `unit`:`2601`, `availabilitydateresult`:`2023-06-15`,
 `marketrent`:`$3,200.00` }");
document.write("Page 1 of 11");
"""


def test_rm_money() -> None:
    assert _rm_money("$4,971.00") == 4971
    assert _rm_money("3200") == 3200
    assert _rm_money("$1,250.50") == 1250
    assert _rm_money("") is None
    assert _rm_money("n/a") is None


def test_rm_iso() -> None:
    assert _rm_iso("5/31/2023") == "2023-05-31"
    assert _rm_iso("2023-06-15") == "2023-06-15"
    assert _rm_iso("2023-6-5") == "2023-06-05"
    assert _rm_iso("") == ""
    assert _rm_iso("nope") == ""


def test_find_search_url_verbatim_and_maxperpage_bump() -> None:
    html = (
        '<html><body><a href="https://high.ua.rentmanager.com/Search_Result'
        "?command=Search_Result&#038;template=highlandUnit&amp;locations="
        'default&maxperpage=99">units</a></body></html>'
    )
    url = find_rentmanager_search_url(html)
    assert url.startswith("https://high.ua.rentmanager.com/Search_Result?")
    assert "&#038;" not in url and "&amp;" not in url
    assert "template=highlandUnit" in url
    assert "maxperpage=9999" in url
    assert "maxperpage=99&" not in url and not url.endswith("maxperpage=99")


def test_find_search_url_appends_maxperpage_when_absent() -> None:
    html = (
        '<a href="https://high.ua.rentmanager.com/Search_Result'
        '?command=Search_Result&template=highlandUnit&locations=default">x</a>'
    )
    url = find_rentmanager_search_url(html)
    assert "maxperpage=9999" in url


def test_find_search_url_preserves_operator_published_property_name_spaces() -> None:
    html = (
        '<script src="https://lewiston.ua.rentmanager.com/Search_Result?'
        "template=unitList&amp;maxperpage=999&amp;"
        'propertynameeq=Lafayette Townhomes"></script>'
    )
    url = find_rentmanager_search_url(html)
    assert "propertynameeq=Lafayette Townhomes" in url
    assert "maxperpage=9999" in url


def test_find_search_url_absent() -> None:
    assert find_rentmanager_search_url("<html>no rentmanager here</html>") == ""
    # A bare twa host link is not a Search_Result URL.
    assert find_rentmanager_search_url(
        '<a href="https://high.twa.rentmanager.com/">portal</a>'
    ) == ""


def test_parse_search_unit_level() -> None:
    units = parse_rentmanager_search(_RM_BODY, "https://high.ua.rentmanager.com/x")
    assert len(units) == 2
    u0 = units[0]
    assert u0["unit_number"] == "2600"
    assert u0["market_rent_low"] == 4971
    assert u0["market_rent_high"] == 4971
    assert u0["availability_date"] == "2023-05-31"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_API_RENTMANAGER"
    assert units[1]["unit_number"] == "2601"
    assert units[1]["market_rent_low"] == 3200
    assert units[1]["availability_date"] == "2023-06-15"


def test_parse_search_dedup_by_unitid() -> None:
    dup = _RM_BODY + (
        'document.write("{ `unitid`:`18302`, `unit`:`2600`, '
        '`marketrent`:`$4,971.00` }");'
    )
    units = parse_rentmanager_search(dup, "x")
    assert len(units) == 2  # 18302 not double-counted


def test_parse_search_empty_and_non_rm() -> None:
    assert parse_rentmanager_search("", "x") == []
    assert parse_rentmanager_search("<html><body>no units</body></html>", "x") == []


_UNITLIST_BODY = """
<script src="https://lewiston.ua.rentmanager.com/Search_Result?
template=unitList&amp;propertynameeq=Lafayette Townhomes"></script>
<div class="list-item unit">
  <div class="list-details-wrapper">
    <div class="list-details unit">
      <h2 class="interior-item-title">Lafayette Townhomes</h2>
      <h2 class="interior-item-title">4817</h2>
      <div class="address-wrapper">
        <p class="list-csz">4812 University Ct.</p>
        <p class="list-csz">Niagara Falls, NY 14305</p>
      </div>
    </div>
    <div class="list-details-center">
      <div class="top-specs">
        <p><strong>Rent: </strong>$1,200.00 / m</p>
        <p><strong>Beds: </strong>2</p>
        <p><strong>Baths: </strong>1</p>
      </div>
      <script>var fullDate = new Date('7/30/2026');</script>
      <p class="pre-lease">Pre-Leasing</p>
    </div>
    <a href="https://niagara.findigs.com/apply/unitid=610187ae-7b09-4099-9ea7-ff064b495743">
      apply now
    </a>
  </div>
</div>
<div class="list-item unit">
  <div class="list-details-wrapper">
    <div class="list-details unit">
      <h2 class="interior-item-title">Lafayette Townhomes</h2>
      <h2 class="interior-item-title">4822</h2>
      <div class="address-wrapper">
        <p class="list-csz">4812 University Ct.</p>
        <p class="list-csz">Niagara Falls, NY 14305</p>
      </div>
    </div>
    <div class="list-details-center"><div class="top-specs">
      <p><strong>Rent: </strong>$1,225.00 / m</p>
      <p><strong>Beds: </strong>2</p><p><strong>Baths: </strong>1</p>
    </div></div>
    <a href="https://niagara.findigs.com/apply/unitid=610187ae-7b09-4099-9ea7-ff064b495743">
      apply now
    </a>
  </div>
</div>
"""


def test_parse_unitlist_cards_keeps_native_units_and_ignores_constant_apply_id() -> None:
    rows = parse_rentmanager_unitlist_cards(
        _UNITLIST_BODY,
        "https://lewiston.ua.rentmanager.com/Search_Result?template=unitList",
        property_name="Lafayette Townhomes",
        address="4812 University Ct",
        zip_code="14305",
    )

    assert [row["unit_number"] for row in rows] == ["4817", "4822"]
    assert [row["market_rent_low"] for row in rows] == [1200, 1225]
    assert rows[0]["availability_date"] == "2026-07-30"
    assert rows[0]["source_ids"] == {}
    assert rows[0]["extraction_tier"] == "TIER_1_DOM_RENTMANAGER_UNITLIST_CARDS"


def test_parse_unitlist_cards_rejects_wrong_property_or_address_boundary() -> None:
    assert parse_rentmanager_unitlist_cards(
        _UNITLIST_BODY.replace(
            '<h2 class="interior-item-title">Lafayette Townhomes</h2>',
            '<h2 class="interior-item-title">Foreign Property</h2>',
            1,
        ),
        "u",
        property_name="Lafayette Townhomes",
        address="4812 University Ct",
        zip_code="14305",
    ) == []
    assert parse_rentmanager_unitlist_cards(
        _UNITLIST_BODY.replace("4812 University Ct.", "99 Other St.", 1),
        "u",
        property_name="Lafayette Townhomes",
        address="4812 University Ct",
        zip_code="14305",
    ) == []


def test_parse_unitlist_cards_reads_bath_literal_before_browser_write() -> None:
    raw_response_shape = _UNITLIST_BODY.replace(
        '<p><strong>Baths: </strong>1</p>',
        "<script>var bath = '1.0'; document.write('Baths');</script>",
    )
    rows = parse_rentmanager_unitlist_cards(
        raw_response_shape,
        "u",
        property_name="Lafayette Townhomes",
        address="4812 University Ct",
        zip_code="14305",
    )
    assert len(rows) == 2
    assert {row["bathrooms"] for row in rows} == {"1.0"}


def test_detector_routes_rentmanager_marker() -> None:
    for marker in (
        '<script src="https://www.iloveleasing.com/pub/widget/js/luv.js">'
        "</script>",
        '<a href="https://high.ua.rentmanager.com/Search_Result?x">units</a>',
        '<iframe src="https://high.twa.rentmanager.com/"></iframe>',
        '<link href="https://cdn.rentmanager.com/x.css">',
    ):
        html = f"<html><body>{marker}</body></html>"
        d = detect_pms("https://www.highlandapts.com/", page_html=html)
        assert d.pms == "rentmanager", (marker, d.pms)


def test_adapter_registered() -> None:
    a = get_adapter("rentmanager")
    assert isinstance(a, RentManagerAdapter)
    assert a.pms_name == "rentmanager"
    assert "ua.rentmanager.com" in a.static_fingerprints()


def test_matches_response_body() -> None:
    a = RentManagerAdapter()
    assert a.matches_response_body(
        'document.write("{ `unitid`:`1`, }"); rentmanager'
    )
    assert not a.matches_response_body("`unitid`:`1` but no vendor token")
    assert not a.matches_response_body("unrelated html")
    assert not a.matches_response_body({"not": "a string"})
