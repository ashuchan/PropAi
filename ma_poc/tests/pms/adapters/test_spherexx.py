"""Spherexx adapter tests.

Fixture captured live from henryonthepark.com/interactive-site-map/ via
Playwright on 2026-05-13. Documents the real Spherexx /api/unit and
/api/floorplan response shapes.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.spherexx import (
    SpherexxAdapter,
    _extract_spherexx_embed_key,
    _is_spherexx_floorplan_response,
    _is_spherexx_unit_response,
    _recover_spherexx_presentation_units,
    _spherexx_marketing_boundary_matches,
    _spherexx_operator_inventory_routes,
    _strict_spherexx_presentation_units,
    parse_spherexx_units,
    parse_zrs_availability_v2,
    parse_zrs_unit_list,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "spherexx"


class _FakeProbeResponse:
    """Minimal curl_cffi-response shim (``.status_code`` / ``.text``)."""

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ``_probe`` seam for every test in this module.

    When no ``/api/unit`` response is admitted, ``SpherexxAdapter.extract``
    falls through to the ZRS crawl: ``_zrs_fetch`` GETs
    ``{origin}/floorplans/`` and then each detail link it finds. Both
    negative-path tests below want the adapter to reach its
    ``_NO_RESPONSE`` / ``_SHAPE_REJECTED`` label, which means the ZRS
    crawl must find nothing. A 404 yields ``""`` from ``_zrs_fetch``, so
    ``find_zrs_detail_links`` returns no links and the cascade lands on
    the asserted label — without touching henryonthepark.com.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


def _load_fixture() -> list[dict]:
    return json.loads((FIXTURES / "henryonthepark.json").read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.henryonthepark.com/interactive-site-map/",
        detected=detect_pms("https://www.henryonthepark.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


def _embed_key(feed: str = "feed123") -> str:
    return base64.b64encode(f"fpaw:{feed}".encode()).decode()


def _presentation_ctx() -> AdapterContext:
    return AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="PRESENTATION_TEST",
        property_name="Example Ridge East Apartments",
        address="123 Main Avenue",
        city="Exampleville",
        state="TX",
        zip_code="75001",
    )


def _presentation_marketing_html(config: str = "") -> str:
    return f"""
    <html><head><title>Example Ridge East Apartments</title></head>
    <body><address>123 Main Avenue, Exampleville, TX 75001</address>
    {config}
    </body></html>
    """


def _presentation_unit(unit_id: int = 101, name: str = "A101") -> dict:
    return {
        "ID": unit_id,
        "Name": name,
        "Number": "101",
        "Sqft": 750,
        "Bed": 1.0,
        "Bath": 1.0,
        "Price": 1_500.0,
        "PriceMin": 1_450.0,
        "PriceMax": 1_800.0,
        "FloorplanID": 10,
        "FloorplanName": "The Elm",
        "AvailableDate": "2026-09-15T00:00:00",
        "Building": "A",
        "Floor": "1",
    }


# ── Body-shape detection ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "config",
    [
        lambda key: (
            f"<script>window.sspcfg={{'key':'{key}','opts':{{'hash':true}}}}"
            "</script><script src=\"https://presentation.spherexx.app/"
            "js/ssploader.js\"></script>"
        ),
        lambda _key: (
            "<script>var widgetKey = window.btoa('fpaw:' + 'feed123')\n"
            "window.sspcfg={'key':widgetKey,'opts':{'inline':true}}</script>"
            "<script src=\"https://presentation.spherexx.app/js/ssploader.js\""
            "></script>"
        ),
        lambda key: (
            "<iframe src=\"https://presentation.spherexx.app/convert.asp?key="
            + base64.b64encode(
                ("{'key':'" + key + "','opts':{'inline':true}}").encode()
            ).decode()
            + "\"></iframe>"
        ),
    ],
)
def test_extract_spherexx_embed_key_accepts_three_observed_forms(config) -> None:
    key = _embed_key()
    assert _extract_spherexx_embed_key(config(key)) == key


@pytest.mark.parametrize(
    "source_html",
    [
        "<script>window.sspcfg={'key':'ZnBhdzpmZWVkMTIz','opts':{}}</script>",
        "<div>ZnBhdzpmZWVkMTIz</div>",
        (
            "<script>window.sspcfg={'key':'d3Jvbmc6ZmVlZA==','opts':{}}</script>"
            "<script src='https://presentation.spherexx.app/js/ssploader.js'>"
            "</script>"
        ),
        (
            "<iframe src='https://presentation.spherexx.app.example.com/"
            "convert.asp?key=ZnBhdzpmZWVkMTIz'></iframe>"
        ),
        (
            "<iframe src='https://presentation.spherexx.app/convert.asp?"
            "key=ZnBhdzpmZWVkMTIz&amp;next=https://example.net'></iframe>"
        ),
    ],
)
def test_extract_spherexx_embed_key_rejects_unscoped_or_malformed_forms(
    source_html: str,
) -> None:
    assert _extract_spherexx_embed_key(source_html) == ""


def test_extract_spherexx_embed_key_rejects_conflicting_configs() -> None:
    first = _embed_key("feed123")
    second = _embed_key("feed456")
    source_html = (
        f"<script>window.sspcfg={{'key':'{first}','opts':{{}}}}</script>"
        f"<script>window.sspcfg={{'key':'{second}','opts':{{}}}}</script>"
        "<script src='https://presentation.spherexx.app/js/ssploader.js'>"
        "</script>"
    )
    assert _extract_spherexx_embed_key(source_html) == ""


def test_spherexx_operator_route_is_exact_and_same_origin() -> None:
    html = """
    <a href="/interactive-site-map/">Availability</a>
    <a href="https://evil.example/interactive-site-map/">Wrong host</a>
    <a href="/interactive-site-map/?next=evil">Wrong query</a>
    <a href="/floorplans/">Unrelated</a>
    """
    assert _spherexx_operator_inventory_routes(
        html,
        "https://example.com/",
    ) == ["https://example.com/interactive-site-map/"]


def test_spherexx_marketing_boundary_requires_name_and_location() -> None:
    ctx = _presentation_ctx()
    assert _spherexx_marketing_boundary_matches(
        ctx,
        _presentation_marketing_html(),
    )
    assert not _spherexx_marketing_boundary_matches(
        ctx,
        "Example Ridge East Apartments, Exampleville TX 99999",
    )
    assert not _spherexx_marketing_boundary_matches(
        ctx,
        "Different Property, 123 Main Avenue, Exampleville TX 75001",
    )


def test_strict_spherexx_presentation_units_rejects_ambiguous_rows() -> None:
    api_url = "https://presentation.spherexx.app/api/unit"
    assert len(
        _strict_spherexx_presentation_units([_presentation_unit()], api_url)
    ) == 1

    duplicate = _presentation_unit(102, "A101")
    assert not _strict_spherexx_presentation_units(
        [_presentation_unit(), duplicate],
        api_url,
    )

    missing_date = _presentation_unit()
    missing_date["AvailableDate"] = ""
    assert not _strict_spherexx_presentation_units([missing_date], api_url)


@pytest.mark.asyncio
async def test_recover_spherexx_presentation_units_is_property_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = "a" * 24 + "." + "b" * 24 + "." + "c" * 24
    calls: list[tuple[str, str, str]] = []

    async def fake_request(
        method: str,
        path: str,
        authorization: str,
    ) -> tuple[int, object]:
        calls.append((method, path, authorization.split(" ", 1)[0]))
        if path == "/api/authenticate":
            return 200, [jwt]
        if path == "/api/community":
            return 200, [{"ID": 1, "Name": "Example Ridge"}]
        if path == "/api/unit":
            return 200, [_presentation_unit()]
        raise AssertionError(path)

    monkeypatch.setattr(
        "ma_poc.pms.adapters.spherexx._spherexx_api_request",
        fake_request,
    )
    units, error = await _recover_spherexx_presentation_units(
        _embed_key(),
        _presentation_marketing_html(),
        _presentation_ctx(),
    )
    assert error == ""
    assert len(units) == 1
    assert units[0]["unit_number"] == "A101"
    assert units[0]["availability_date"] == "2026-09-15"
    assert units[0]["extraction_tier"].endswith("PRESENTATION_DIRECT")
    assert [(method, path) for method, path, _kind in calls] == [
        ("POST", "/api/authenticate"),
        ("GET", "/api/community"),
        ("GET", "/api/unit"),
    ]


@pytest.mark.asyncio
async def test_recover_spherexx_community_mismatch_aborts_before_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwt = "a" * 24 + "." + "b" * 24 + "." + "c" * 24
    paths: list[str] = []

    async def fake_request(
        method: str,
        path: str,
        authorization: str,
    ) -> tuple[int, object]:
        del method, authorization
        paths.append(path)
        if path == "/api/authenticate":
            return 200, [jwt]
        if path == "/api/community":
            return 200, [{"ID": 2, "Name": "Different Community"}]
        raise AssertionError("unit endpoint must not be called on mismatch")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.spherexx._spherexx_api_request",
        fake_request,
    )
    units, error = await _recover_spherexx_presentation_units(
        _embed_key(),
        _presentation_marketing_html(),
        _presentation_ctx(),
    )
    assert units == []
    assert error == "community_property_boundary_mismatch"
    assert paths == ["/api/authenticate", "/api/community"]


@pytest.mark.asyncio
async def test_recover_spherexx_marketing_mismatch_makes_no_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("API must not be called before marketing boundary")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.spherexx._spherexx_api_request",
        unexpected_request,
    )
    units, error = await _recover_spherexx_presentation_units(
        _embed_key(),
        "Different Property, Other City TX 99999",
        _presentation_ctx(),
    )
    assert units == []
    assert error == "marketing_property_boundary_mismatch"


def test_is_spherexx_unit_response_matches_real_shape() -> None:
    """Live-captured /api/unit body should pass the shape check."""
    fix = _load_fixture()
    unit_resp = next(r for r in fix if r["url"].endswith("/api/unit"))
    assert _is_spherexx_unit_response(unit_resp["body"])


def test_is_spherexx_unit_response_rejects_floorplan_shape() -> None:
    """/api/floorplan has Bed/Bath but uses MinSqFt/MaxSqFt instead of Sqft —
    must be rejected by the /api/unit shape check.
    """
    fix = _load_fixture()
    fp_resp = next(r for r in fix if r["url"].endswith("/api/floorplan"))
    assert not _is_spherexx_unit_response(fp_resp["body"])


def test_is_spherexx_unit_response_rejects_empty_list() -> None:
    assert not _is_spherexx_unit_response([])
    assert not _is_spherexx_unit_response(None)
    assert not _is_spherexx_unit_response({"data": []})


def test_is_spherexx_unit_response_rejects_unrelated_json() -> None:
    """SightMap/RentCafe/AppFolio response shapes must not falsely match."""
    sightmap_like = {"data": {"units": [], "floor_plans": []}}
    rentcafe_like = [{"propertyId": 1, "floorplanId": 2}]
    appfolio_like = [{"unit_id": "x", "rent": 1500, "available_date": "2026-01"}]
    assert not _is_spherexx_unit_response(sightmap_like)
    assert not _is_spherexx_unit_response(rentcafe_like)
    assert not _is_spherexx_unit_response(appfolio_like)


def test_is_spherexx_floorplan_response_matches_real_shape() -> None:
    fix = _load_fixture()
    fp_resp = next(r for r in fix if r["url"].endswith("/api/floorplan"))
    assert _is_spherexx_floorplan_response(fp_resp["body"])


# ── Unit parsing ──────────────────────────────────────────────


def test_parse_spherexx_units_real_fixture() -> None:
    """Parse the live henryonthepark /api/unit fixture into our schema."""
    fix = _load_fixture()
    unit_resp = next(r for r in fix if r["url"].endswith("/api/unit"))
    units = parse_spherexx_units(unit_resp["body"], unit_resp["url"])
    assert units, "expected ≥1 unit parsed from live fixture"
    u0 = units[0]
    # Schema spot-check
    assert u0["unit_number"]  # e.g. "B102"
    assert u0["bedrooms"]  # numeric string
    assert u0["bathrooms"]
    assert u0["sqft"]
    assert u0["rent_range"].startswith("$")
    assert u0["market_rent_low"]
    assert u0["floor_plan_name"]
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_API_SPHEREXX"


def test_parse_spherexx_units_skips_empty_placeholders() -> None:
    """Units with no Price AND no Sqft (unbuilt buildings) are skipped."""
    body = [
        {"ID": 1, "Name": "X", "Sqft": 0, "Bed": 0, "Bath": 0,
         "Price": 0, "PriceMin": 0, "PriceMax": 0, "FloorplanID": 99,
         "FloorplanName": "TBD", "AvailableDate": None},
    ]
    assert parse_spherexx_units(body, "test") == []


def test_parse_spherexx_units_handles_price_range() -> None:
    """PriceMin/PriceMax both present + different → rent_range shows both."""
    body = [{
        "ID": 1, "Name": "A101", "Sqft": 750, "Bed": 1.0, "Bath": 1.0,
        "Price": 1500.0, "PriceMin": 1500.0, "PriceMax": 1800.0,
        "FloorplanID": 1, "FloorplanName": "Olive",
        "AvailableDate": "2026-06-29T00:00:00", "Building": "A", "Floor": "1",
    }]
    units = parse_spherexx_units(body, "test")
    assert len(units) == 1
    u = units[0]
    assert u["market_rent_low"] == 1500
    assert u["market_rent_high"] == 1800
    assert "$1,500" in u["rent_range"]
    assert "$1,800" in u["rent_range"]
    assert u["availability_date"] == "2026-06-29"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"


def test_parse_spherexx_units_handles_half_bath() -> None:
    """Bath=2.5 emits "2.5", not "2.50"."""
    body = [{
        "ID": 1, "Name": "X", "Sqft": 1000, "Bed": 2, "Bath": 2.5,
        "Price": 2000, "FloorplanID": 1, "FloorplanName": "Y",
    }]
    units = parse_spherexx_units(body, "test")
    assert units[0]["bathrooms"] == "2.5"


# ── Adapter extract ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spherexx_adapter_extracts_from_fixture() -> None:
    fix = _load_fixture()
    adapter = SpherexxAdapter()
    ctx = _make_ctx(fix)
    result = await adapter.extract(_DummyPage(), ctx)
    assert isinstance(result, AdapterResult)
    assert result.units, "expected units from fixture"
    assert result.tier_used == "TIER_1_API_SPHEREXX"
    assert result.confidence >= 0.7


@pytest.mark.asyncio
async def test_spherexx_adapter_replays_public_presentation_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _embed_key()
    config = (
        f"<script>window.sspcfg={{'key':'{key}','opts':{{'hash':true}}}}"
        "</script><script src='https://presentation.spherexx.app/"
        "js/ssploader.js'></script>"
    )
    jwt = "a" * 24 + "." + "b" * 24 + "." + "c" * 24

    async def fake_request(
        method: str,
        path: str,
        authorization: str,
    ) -> tuple[int, object]:
        del method, authorization
        if path == "/api/authenticate":
            return 200, [jwt]
        if path == "/api/community":
            return 200, [{"ID": 1, "Name": "Example Ridge"}]
        if path == "/api/unit":
            return 200, [_presentation_unit()]
        raise AssertionError(path)

    monkeypatch.setattr(
        "ma_poc.pms.adapters.spherexx._spherexx_api_request",
        fake_request,
    )
    ctx = _presentation_ctx()
    ctx.fetch_result = SimpleNamespace(
        body=_presentation_marketing_html(config),
        final_url="https://example.com/",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await SpherexxAdapter().extract(None, ctx)
    assert len(result.units) == 1
    assert result.units[0]["unit_number"] == "A101"
    assert result.tier_used == "TIER_1_API_SPHEREXX_PRESENTATION_DIRECT"
    assert result.winning_url == "https://presentation.spherexx.app/api/unit"
    assert result.api_responses[0]["body"] == (
        "<spherexx-presentation-unit-json>"
    )


@pytest.mark.asyncio
async def test_spherexx_adapter_no_response_path() -> None:
    """No spherexx responses in api_responses → NO_RESPONSE tier."""
    adapter = SpherexxAdapter()
    ctx = _make_ctx([
        {"url": "https://example.com/api/units", "body": [{"foo": "bar"}]},
    ])
    result = await adapter.extract(_DummyPage(), ctx)
    assert result.units == []
    assert result.tier_used == "TIER_1_API_SPHEREXX_NO_RESPONSE"


@pytest.mark.asyncio
async def test_spherexx_adapter_shape_rejected_path() -> None:
    """Has spherexx responses but none match /api/unit shape."""
    adapter = SpherexxAdapter()
    ctx = _make_ctx([
        {"url": "https://presentation.spherexx.app/api/community",
         "body": [{"ID": 5671, "Name": "Henry on the Park"}]},
        {"url": "https://presentation.spherexx.app/api/configuration",
         "body": [{"TileUrl": "", "ElevationUrl": ""}]},
    ])
    result = await adapter.extract(_DummyPage(), ctx)
    assert result.units == []
    assert result.tier_used == "TIER_1_API_SPHEREXX_SHAPE_REJECTED"
    # Diagnostic must list which endpoints WERE seen
    assert any("community" in e for e in result.errors)


# ── Detector integration ──────────────────────────────────────────────


def test_detector_recognizes_spherexx_marker_in_html() -> None:
    detection = detect_pms(
        "https://www.henryonthepark.com/interactive-site-map/",
        page_html='<html><script>window.sspcfg={"key":"x"}</script></html>',
    )
    assert detection.pms == "spherexx"
    assert detection.confidence >= 0.85
    assert detection.recommended_strategy == "api_first"


def test_detector_recognizes_spherexx_via_ssploader_script() -> None:
    detection = detect_pms(
        "https://example.com/",
        page_html=(
            '<html><script src="https://presentation.spherexx.app/js/ssploader.js" '
            'defer></script></html>'
        ),
    )
    assert detection.pms == "spherexx"


def test_detector_recognizes_spherexx_via_iframe_src() -> None:
    detection = detect_pms(
        "https://example.com/",
        page_html=(
            '<html><iframe src="https://presentation.spherexx.app/"></iframe>'
            '</html>'
        ),
    )
    assert detection.pms == "spherexx"

ZRS_FIXTURE = '''<table><tbody>
<tr><td style="display:none;">
<input type="hidden" data-type="uid" value="1341177" />
<input type="hidden" data-type="unitNumber" value="101" />
<input type="hidden" data-type="bid" value="05" />
<input type="hidden" data-type="priceDisplayType" value="lowest" /></td>
<td class="floorplan-detail__units__number"><span><a href="05-101/">05 - 101</a></span></td>
<td class="floorplan-detail__units__price" data-base-unit-price="2389.0"><span><span style="order:1;">$2510.00</span></span></td></tr>
<tr><td style="display:none;">
<input type="hidden" data-type="uid" value="1341190" />
<input type="hidden" data-type="unitNumber" value="104" />
<input type="hidden" data-type="bid" value="06" /></td>
<td class="floorplan-detail__units__number"><a href="06-104/">06 - 104</a></td>
<td class="floorplan-detail__units__price" data-base-unit-price="2439.0"><span>$2560.00</span></td></tr>
</tbody></table>'''


def test_zrs_floorplan_detail_parses_unit_level():
    from ma_poc.pms.adapters.spherexx import parse_zrs_floorplan_detail
    rows = parse_zrs_floorplan_detail(
        ZRS_FIXTURE, "https://x.com/floorplans/4bedroom/d1/"
    )
    assert len(rows) == 2
    by = {r["unit_number"]: r for r in rows}
    assert "05-101" in by and "06-104" in by
    u = by["05-101"]
    assert u["bedrooms"] == "4"
    assert u["floor_plan_name"] == "D1"
    assert u["market_rent_low"] == 2389  # data-base-unit-price
    assert u["availability_status"] == "AVAILABLE"
    # never inferred_ — real bid-unitNumber identity
    assert not u["unit_number"].startswith("inferred_")


def test_zrs_detail_links_variants():
    from ma_poc.pms.adapters.spherexx import find_zrs_detail_links
    html = ('<a href="/floorplans/4bedroom/d1/">x</a>'
            '<a href="/floorplans-and-pricing/1-bed/11649">y</a>'
            '<a href="/floor-plans/2-bed/a2/">z</a><a href="/about/">no</a>')
    links = find_zrs_detail_links(html, "https://x.com")
    assert "https://x.com/floorplans/4bedroom/d1/" in links
    assert "https://x.com/floorplans-and-pricing/1-bed/11649/" in links
    assert "https://x.com/floor-plans/2-bed/a2/" in links
    assert len(links) == 3


def test_zrs_no_markup_returns_empty():
    from ma_poc.pms.adapters.spherexx import parse_zrs_floorplan_detail
    assert parse_zrs_floorplan_detail("<html>no units here</html>", "u") == []


ZRS_UNIT_LIST_FIXTURE = '''
<html><head>
  <script src="/Content/js/core/unit-list.js"></script>
  <link rel="copyright" href="https://www.spherexx.com/copyright/">
  <meta property="og:image" content="https://sxxweb8cdn.cachefly.net/p/x.jpg">
</head><body>
<div class="floorplan-overview">
  <h2 class="floorplan-overview__name">The Birch</h2>
  <div class="floorplan-overview__info">
    <span>2 Bed</span><span>1.5 Bath</span><span>945 SF</span>
  </div>
</div>
<table class="unit-list__table"><tbody class="unit-list__body">
  <tr class="unit-list__unit">
    <th data-label="Apt #"><a href="/floorplans/birch/3201-04/"
      title="View details for unit 3201-04.">3201-04</a></th>
    <td data-label="Price" data-og-display-price="$1,270">$1,270</td>
    <td data-label="Lease"><a href="/apply-now/?MoveInDate=7/31/2026&amp;unitID=122&amp;siteid=5309889&amp;type=other"
      title="Lease unit 3201-04 Now">Lease Now</a></td>
  </tr>
  <tr class="unit-list__unit">
    <th data-label="Apt #"><a href="/floorplans/birch/3203-09/"
      aria-label="View details for unit 3203-09.">3203-09</a></th>
    <td data-label="Price" data-og-display-price="$1,324">
      <div class="unit-list__unit__tmlp">$1,387 /mo* | 12 months</div>
      <div class="unit-list__unit__base-price">$1,324 Base rent | 12 months</div>
    </td>
    <td data-label="Lease"><a href="/apply-now/?MoveInDate=8/4/2026&amp;unitID=129&amp;siteid=5309889&amp;type=other">Lease Now</a></td>
  </tr>
</tbody></table>
</body></html>
'''


def test_zrs_current_unit_list_parses_strict_physical_units() -> None:
    rows = parse_zrs_unit_list(
        ZRS_UNIT_LIST_FIXTURE,
        "https://www.avenliving.com/floorplans/2bedroom/birch/",
    )
    assert len(rows) == 2
    by_number = {row["unit_number"]: row for row in rows}
    first = by_number["3201-04"]
    assert first["floor_plan_name"] == "The Birch"
    assert first["bedrooms"] == "2"
    assert first["bathrooms"] == "1.5"
    assert first["sqft"] == "945"
    assert first["market_rent_low"] == 1270
    assert first["availability_date"] == "7/31/2026"
    assert first["source_ids"]["spherexx_unit_id"] == "122"
    assert by_number["3203-09"]["lease_term"] == "12 months"
    assert all(
        row["extraction_tier"] == "TIER_1_DOM_SPHEREXX_ZRS_UNIT_LIST"
        for row in rows
    )


def test_zrs_current_unit_list_requires_apply_identity_and_positive_rent() -> None:
    no_apply = ZRS_UNIT_LIST_FIXTURE.replace("unitID=122", "notUnitID=122")
    rows = parse_zrs_unit_list(
        no_apply,
        "https://www.avenliving.com/floorplans/2bedroom/birch/",
    )
    assert {row["unit_number"] for row in rows} == {"3203-09"}

    no_rent = ZRS_UNIT_LIST_FIXTURE.replace(
        'data-og-display-price="$1,270"', 'data-og-display-price="$0"'
    )
    rows = parse_zrs_unit_list(
        no_rent,
        "https://www.avenliving.com/floorplans/2bedroom/birch/",
    )
    assert {row["unit_number"] for row in rows} == {"3203-09"}


def test_zrs_current_unit_list_rejects_conflicting_identity() -> None:
    conflict = ZRS_UNIT_LIST_FIXTURE.replace("unitID=129", "unitID=122")
    assert parse_zrs_unit_list(
        conflict,
        "https://www.avenliving.com/floorplans/2bedroom/birch/",
    ) == []


def test_detector_recognizes_server_rendered_spherexx_zrs_template() -> None:
    detection = detect_pms(
        "https://www.avenliving.com/",
        page_html=ZRS_UNIT_LIST_FIXTURE,
    )
    assert detection.pms == "spherexx"
    assert detection.confidence == 0.92


def test_detector_does_not_promote_spherexx_copyright_link_alone() -> None:
    detection = detect_pms(
        "https://example.com/",
        page_html='<a href="https://www.spherexx.com/copyright/">copyright</a>',
    )
    assert detection.pms != "spherexx"


def test_detector_prefers_spherexx_fci_inventory_over_contact_widget() -> None:
    html = """
    <a href="https://www.spherexx.com/copyright/">copyright</a>
    <img src="https://sxxweb7cdn.cachefly.net/p/logo.svg">
    <a href="/floorplans/1-bedroom/action/">Action</a>
    <script src="https://www.iloveleasing.com/pub/widget/js/luv.js"></script>
    """
    detection = detect_pms("https://example.com/", page_html=html)
    assert detection.pms == "spherexx"
    assert detection.confidence == 0.92


def test_detector_recognizes_legacy_zrs_roster_over_chat_widget() -> None:
    html = (
        '<a href="https://www.spherexx.com/copyright/">copyright</a>'
        '<img src="https://sxxweb8cdn.cachefly.net/a.png">'
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        + ZRS_FIXTURE
    )
    detection = detect_pms(
        "https://example.com/floorplans/4bedroom/d1/",
        page_html=html,
    )
    assert detection.pms == "spherexx"
    assert detection.confidence == 0.92


@pytest.mark.asyncio
async def test_spherexx_adapter_uses_already_fetched_zrs_unit_list() -> None:
    url = "https://www.avenliving.com/floorplans/2bedroom/birch/"
    ctx = _make_ctx([])
    ctx.base_url = url
    ctx.fetch_result = SimpleNamespace(body=ZRS_UNIT_LIST_FIXTURE, final_url=url)

    result = await SpherexxAdapter().extract(_DummyPage(), ctx)

    assert len(result.units) == 2
    assert {row["unit_number"] for row in result.units} == {"3201-04", "3203-09"}
    assert result.tier_used == "TIER_1_DOM_SPHEREXX_ZRS_UNIT_LIST"
    assert result.winning_url == url


@pytest.mark.asyncio
async def test_spherexx_adapter_uses_already_fetched_legacy_zrs_roster() -> None:
    url = "https://example.com/floorplans/4bedroom/d1/"
    ctx = _make_ctx([])
    ctx.base_url = url
    ctx.fetch_result = SimpleNamespace(body=ZRS_FIXTURE, final_url=url)

    result = await SpherexxAdapter().extract(_DummyPage(), ctx)

    assert len(result.units) == 2
    assert {row["unit_number"] for row in result.units} == {"05-101", "06-104"}
    assert result.tier_used == "TIER_1_DOM_SPHEREXX_ZRS"
    assert result.winning_url == url


@pytest.mark.asyncio
async def test_zrs_secondary_fetch_is_plain_direct_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters.spherexx import _zrs_fetch

    seen: dict[str, object] = {}

    def _capture(url: str, **kwargs: object) -> _FakeProbeResponse:
        seen.update(kwargs)
        return _FakeProbeResponse(url, status_code=200, text="ok")

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _capture)
    assert await _zrs_fetch("https://example.com/floorplans/") == "ok"
    assert seen["unlocker"] is False
    assert seen["proxies"] == {}
    assert seen["retries"] == 1


ZRS_AVAILABILITY_INDEX = '''
<script src="/Content/js/core/floorplans.js"></script>
<ul class="floorplans">
  <li class="floorplans__item" data-fpid="17" data-bed="1" data-bath="1"
      data-name="Action" data-sqft="800" data-lease-term="12">
    <span class="floorplans__fpname">Action</span>
  </li>
  <li class="floorplans__item" data-fpid="20" data-bed="2" data-bath="2"
      data-name="Moves" data-sqft="1106" data-lease-term="12">
    <span class="floorplans__fpname">Moves</span>
  </li>
</ul>
'''

ZRS_AVAILABILITY_PLANS = [
    {"FloorplanID": 17, "FloorplanName": "Action"},
    {"FloorplanID": 20, "FloorplanName": "Moves"},
]

ZRS_AVAILABILITY_UNITS = [
    {
        "UnitID": "657262",
        "ApartmentNumber": "3-307",
        "FloorplanID": 17,
        "DateAvailable": "8/3/2026 12:00:00 AM",
        "MinPrice": 2310,
        "DisplayPrice": "$2,310",
        "SqFt": 800,
        "LeaseTerm": 12,
    },
    {
        "UnitID": "657359",
        "ApartmentNumber": "7-302",
        "FloorplanID": 20,
        "DateAvailable": "2026-09-14T00:00:00",
        "MinPrice": 2551,
        "DisplayPrice": "$2,551",
        # Unit-feed area can differ from the public plan-card area.  The
        # advertised floor-plan measurement is the correct output value.
        "SqFt": 1097,
        "LeaseTerm": 12,
    },
    {
        "UnitID": "999999",
        "ApartmentNumber": "X-1",
        "FloorplanID": None,
        "DateAvailable": "8/3/2026 12:00:00 AM",
        "MinPrice": 2000,
        "SqFt": 900,
    },
]


def test_zrs_availability_v2_parses_strict_joined_units() -> None:
    url = "https://example.com/ajax/availabilityv2/"
    rows = parse_zrs_availability_v2(
        ZRS_AVAILABILITY_UNITS,
        ZRS_AVAILABILITY_PLANS,
        ZRS_AVAILABILITY_INDEX,
        url,
    )

    assert {row["unit_number"] for row in rows} == {"3-307", "7-302"}
    by_number = {row["unit_number"]: row for row in rows}
    action = by_number["3-307"]
    assert action["floor_plan_name"] == "Action"
    assert action["bedrooms"] == "1"
    assert action["bathrooms"] == "1"
    assert action["sqft"] == "800"
    assert action["market_rent_low"] == 2310
    assert action["market_rent_high"] == 2310
    assert action["availability_date"] == "2026-08-03"
    assert action["source_ids"] == {
        "spherexx_unit_id": "657262",
        "spherexx_floorplan_id": "17",
    }
    assert by_number["7-302"]["sqft"] == "1106"
    assert by_number["7-302"]["availability_date"] == "2026-09-14"
    assert all(
        row["extraction_tier"] == "TIER_1_API_SPHEREXX_ZRS_AVAILABILITY_V2"
        for row in rows
    )


def test_zrs_availability_v2_keeps_plan_only_response_empty() -> None:
    assert parse_zrs_availability_v2(
        {}, ZRS_AVAILABILITY_PLANS, ZRS_AVAILABILITY_INDEX, "https://example.com"
    ) == []


def test_zrs_availability_v2_rejects_conflicting_identity() -> None:
    conflict = [
        dict(ZRS_AVAILABILITY_UNITS[0]),
        {**ZRS_AVAILABILITY_UNITS[0], "ApartmentNumber": "3-308"},
    ]
    assert parse_zrs_availability_v2(
        conflict,
        ZRS_AVAILABILITY_PLANS,
        ZRS_AVAILABILITY_INDEX,
        "https://example.com/ajax/availabilityv2/",
    ) == []


@pytest.mark.asyncio
async def test_spherexx_adapter_replays_same_origin_availability_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "https://example.com"

    async def _fake_zrs_fetch(url: str) -> str:
        if url == base + "/ajax/availabilityv2/":
            return json.dumps(ZRS_AVAILABILITY_UNITS)
        if url == base + "/ajax/api/plansandpricing/":
            return json.dumps(ZRS_AVAILABILITY_PLANS)
        return ""

    monkeypatch.setattr(
        "ma_poc.pms.adapters.spherexx._zrs_fetch", _fake_zrs_fetch
    )
    ctx = _make_ctx([])
    ctx.base_url = base + "/"
    ctx.fetch_result = SimpleNamespace(
        body=ZRS_AVAILABILITY_INDEX,
        final_url=base + "/",
    )

    result = await SpherexxAdapter().extract(_DummyPage(), ctx)

    assert {row["unit_number"] for row in result.units} == {"3-307", "7-302"}
    assert result.tier_used == "TIER_1_API_SPHEREXX_ZRS_AVAILABILITY_V2"
    assert result.winning_url == base + "/ajax/availabilityv2/"
