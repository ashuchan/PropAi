"""Phase 3 — SightMap adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.sightmap import (
    SightMapAdapter,
    _is_sightmap_response,
    parse_sightmap_payload,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "sightmap"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://tour.sightmap.com/embed/12345",
        detected=detect_pms("https://tour.sightmap.com/embed/12345"),
        profile=None,
        expected_total_units=None,
        property_id="TEST",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    pass


@pytest.mark.asyncio
async def test_sightmap_extract_happy_path() -> None:
    responses = _load_fixture("synthetic_units.json")
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 4
    unit_101 = [u for u in result.units if u["unit_number"] == "101"][0]
    assert unit_101["floor_plan_name"] == "A1"
    assert unit_101["bedrooms"] == "1"
    assert "$1,500" in unit_101["rent_range"]
    assert unit_101["availability_status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_sightmap_extract_from_stored_fixture() -> None:
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = SightMapAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)


@pytest.mark.asyncio
async def test_sightmap_extract_real_fixture_268836() -> None:
    responses = _load_fixture("268836_amenities_only.json")
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) > 0
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_sightmap_extract_returns_empty_on_no_data() -> None:
    responses = [
        {"url": "https://sightmap.com/app/api/v1/x/sightmaps/1", "body": {"data": {"amenities": []}}}
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_parse_sightmap_handles_null_units() -> None:
    body = {"data": {"units": None, "floor_plans": []}}
    units, dropped = parse_sightmap_payload(body, "test")
    assert units == []
    assert dropped == 0


def test_parse_sightmap_handles_empty_units() -> None:
    body = {"data": {"units": [], "floor_plans": []}}
    units, dropped = parse_sightmap_payload(body, "test")
    assert units == []
    assert dropped == 0


def test_parse_sightmap_studio_detection() -> None:
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units, _ = parse_sightmap_payload(body, "test")
    studio = [u for u in units if u["unit_number"] == "301"][0]
    assert studio["bed_label"] == "Studio"


def test_static_fingerprints_nonempty() -> None:
    assert SightMapAdapter().static_fingerprints()


def test_tier_used_label_is_pms_specific() -> None:
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units, _ = parse_sightmap_payload(body, "test")
    assert all("SIGHTMAP" in u["extraction_tier"] for u in units)


def test_rent_within_sanity_range() -> None:
    responses = _load_fixture("synthetic_units.json")
    body = responses[0]["body"]
    units, _ = parse_sightmap_payload(body, "test")
    import re

    for u in units:
        if u["rent_range"]:
            nums = re.findall(r"\d[\d,]*", u["rent_range"])
            for n in nums:
                val = int(n.replace(",", ""))
                assert 200 <= val <= 50000


# ---------------------------------------------------------------------------
# 2026-05-06 — direct-fetch fallback path
# ---------------------------------------------------------------------------


def test_find_embed_hashid_handles_plain_url() -> None:
    """`https://sightmap.com/embed/{id}` substring is the simple case."""
    from ma_poc.pms.adapters.sightmap import _find_embed_hashid

    html = '<a href="https://sightmap.com/embed/1ywyjz3ywq0">Open Map</a>'
    assert _find_embed_hashid(html) == "1ywyjz3ywq0"


def test_find_embed_hashid_handles_escaped_url_in_json_blob() -> None:
    """ECS Spaces plugin emits the embed URL with escaped slashes inside JSON.

    The pattern is ``"sightmap_url":"https:\\/\\/sightmap.com\\/embed\\/<id>"``.
    The plain regex misses this — _EMBED_ESCAPED_RE picks it up.
    """
    from ma_poc.pms.adapters.sightmap import _find_embed_hashid

    html = (
        '<script>const spacesConfig = {"sightmap_url":"https:\\/\\/sightmap.com\\/embed\\/x1p8d9xovd6"}</script>'
    )
    assert _find_embed_hashid(html) == "x1p8d9xovd6"


def test_find_embed_hashid_returns_none_when_absent() -> None:
    from ma_poc.pms.adapters.sightmap import _find_embed_hashid

    assert _find_embed_hashid("") is None
    assert _find_embed_hashid("<html><body>nope</body></html>") is None


@pytest.mark.asyncio
async def test_sightmap_direct_fetch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: fingerprint embed substring → fetch embed → fetch API → parse units.

    Uses respx-style monkeypatching of httpx.AsyncClient.get to avoid actual
    network calls.
    """
    import httpx

    from ma_poc.pms.adapters import sightmap as sm_mod

    embed_html = (
        "<html><body><script>"
        'window.__APP_CONFIG__ = {"name":"Test","sightmaps":[{"href":"https://sightmap.com/app/api/v1/abc/sightmaps/123"}]}\n'
        "</script></body></html>"
    )
    api_body = {
        "data": {
            "id": "123",
            "sightmap_id": "123",
            "units": [
                {
                    "id": "u1",
                    "floor_plan_id": "fp1",
                    "unit_number": "101",
                    "price": 1500,
                    "area": 700,
                }
            ],
            "floor_plans": [
                {"id": "fp1", "name": "A1", "bedroom_count": 1, "bathroom_count": 1}
            ],
        }
    }

    class _Resp:
        def __init__(self, status_code: int, text: str = "", json_body: object = None) -> None:
            self.status_code = status_code
            self.text = text
            self._json = json_body

        def json(self) -> object:
            if isinstance(self._json, Exception):
                raise self._json
            return self._json

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _Resp:
            if url.endswith("/embed/abc123"):
                return _Resp(200, text=embed_html)
            if "/sightmaps/123" in url:
                return _Resp(200, json_body=api_body)
            return _Resp(404, text="not found")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    units, source_url, errors = await sm_mod.fetch_sightmap_units_direct("abc123")
    assert errors == []
    assert source_url.endswith("/sightmaps/123")
    assert len(units) == 1
    assert units[0]["unit_number"] == "101"
    assert units[0]["bedrooms"] == "1"
    assert "$1,500" in units[0]["rent_range"]


@pytest.mark.asyncio
async def test_sightmap_direct_fetch_surfaces_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the embed URL 404s, return empty + structured error code."""
    import httpx

    from ma_poc.pms.adapters import sightmap as sm_mod

    class _Resp:
        status_code = 404
        text = "not found"

        def json(self) -> object:
            return {}

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    units, _src, errors = await sm_mod.fetch_sightmap_units_direct("abc123")
    assert units == []
    assert any("SIGHTMAP_DIRECT_FETCH_EMBED_HTTP_404" in e for e in errors)


@pytest.mark.asyncio
async def test_sightmap_extract_uses_direct_fetch_when_no_xhr_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter's `extract` calls direct-fetch when api_responses is empty
    AND the page HTML mentions an embed URL. This is the actual gap surfaced
    by 2026-05-06 micro-dive: 9 of 81 failing properties had a real embed URL
    in the HTML but the iframe-load timing meant no XHR was captured.
    """
    from ma_poc.pms.adapters import sightmap as sm_mod

    class _StubFetchResult:
        body = (
            b"<html>... <iframe src='https://sightmap.com/embed/zz9abc'></iframe> ...</html>"
        )

    ctx = _make_ctx([])  # No XHR responses
    ctx.fetch_result = _StubFetchResult()  # type: ignore[assignment]

    async def _stub_direct(
        embed_hashid: str, *, timeout: float = 12.0
    ) -> tuple[list[dict[str, str]], str, list[str]]:
        assert embed_hashid == "zz9abc"
        return (
            [{"unit_number": "101", "rent_range": "$1,500", "extraction_tier": "TIER_1_API_SIGHTMAP"}],
            "https://sightmap.com/app/api/v1/x/sightmaps/1",
            [],
        )

    monkeypatch.setattr(sm_mod, "fetch_sightmap_units_direct", _stub_direct)

    adapter = SightMapAdapter()
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert result.tier_used == "TIER_1_API_SIGHTMAP_DIRECT_FETCH"
    assert result.confidence > 0.6


def test_parse_sightmap_display_price_fallback() -> None:
    body = {
        "data": {
            "units": [
                {
                    "id": "1",
                    "floor_plan_id": "1",
                    "unit_number": "X1",
                    "price": None,
                    "display_price": "$1,300",
                    "area": 600,
                }
            ],
            "floor_plans": [{"id": "1", "name": "Test", "bedroom_count": 1, "bathroom_count": 1}],
        }
    }
    units, _ = parse_sightmap_payload(body, "test")
    assert len(units) == 1
    assert "$1,300" in units[0]["rent_range"]


# --- 2026-04-19 fix tests (SM_T01 – SM_T08) ---------------------------------


def _sm_valid_body() -> dict:
    return {
        "data": {
            "units": [
                {
                    "floor_plan_id": 1,
                    "price": 1800,
                    "unit_number": "101",
                    "area": 720,
                    "available_on": "2026-05-01",
                }
            ],
            "floor_plans": [
                {
                    "id": 1,
                    "name": "1BR",
                    "bedroom_count": 1,
                    "bathroom_count": 1,
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_sm_t01_sightmap_url_still_works() -> None:
    body = _sm_valid_body()
    assert _is_sightmap_response(body) is True
    responses = [
        {
            "url": "https://sightmap.com/app/api/v1/abc/sightmaps/123",
            "body": body,
        }
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    u = result.units[0]
    assert u["bedrooms"] == "1"
    assert u["rent_range"]
    assert u["unit_number"] == "101"


@pytest.mark.asyncio
async def test_sm_t02_proxied_url_is_matched() -> None:
    body = _sm_valid_body()
    assert _is_sightmap_response(body) is True
    responses = [
        {
            "url": "https://lasvegasliving.com/api/properties/123/availability",
            "body": body,
        }
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1


@pytest.mark.asyncio
async def test_sm_t03_amenities_only_error() -> None:
    responses = [
        {
            "url": "https://sightmap.com/app/api/v1/abc/sightmaps/456",
            "body": {"data": {"amenities": [{"id": 1, "name": "Pool"}], "floor_plans": [], "units": []}},
        }
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_AMENITIES_ONLY") for e in result.errors)


@pytest.mark.asyncio
async def test_sm_t04_no_sightmap_response_error() -> None:
    """SM_T04: empty network log produces SIGHTMAP_NO_RESPONSE.

    Updated 2026-04-20: the prior test sent a non-empty response that didn't
    shape-match. Under the structured-error refactor that path is now
    SIGHTMAP_SHAPE_REJECTED. To keep this test's *intent* — verifying the
    NO_RESPONSE branch fires — we now pass an empty api_responses list.
    """
    responses: list[dict] = []
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_NO_RESPONSE") for e in result.errors)


@pytest.mark.asyncio
async def test_sm_t05_units_but_empty_fps_parse_failed() -> None:
    responses = [
        {
            "url": "https://sightmap.com/app/api/v1/x/sightmaps/1",
            "body": {"data": {"units": [{"floor_plan_id": 99, "price": 1500}], "floor_plans": []}},
        }
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_PARSE_FAILED") for e in result.errors)


def test_sm_t06_is_sightmap_response_rejects_non_sightmap_body() -> None:
    assert _is_sightmap_response({"floorplanName": "1BR", "minimumRent": "1800"}) is False


def test_sm_t07_is_sightmap_response_matches_sightmap_id_alone() -> None:
    assert _is_sightmap_response({"data": {"sightmap_id": 80671, "other_stuff": []}}) is True


@pytest.mark.asyncio
async def test_sm_t08_unmatched_floor_plan_join_fails() -> None:
    responses = [
        {
            "url": "https://sightmap.com/app/api/v1/x/sightmaps/9",
            "body": {
                "data": {
                    "units": [{"floor_plan_id": 999, "price": 2000, "unit_number": "A1", "area": 800}],
                    "floor_plans": [],
                }
            },
        }
    ]
    adapter = SightMapAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert any(e.startswith("SIGHTMAP_PARSE_FAILED") for e in result.errors)


# --- 2026-04-20 fix tests (structured failure tier codes) -------------------


@pytest.mark.asyncio
async def test_sightmap_tier_re_stamped_on_no_response() -> None:
    """Empty network log → ``TIER_1_API_SIGHTMAP_NO_RESPONSE``."""
    adapter = SightMapAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_SIGHTMAP_NO_RESPONSE"


def test_sightmap_amenities_only_no_false_positive() -> None:
    """``data.amenities`` alone must not match (was a false-positive pre-fix)."""
    body = {"data": {"amenities": [{"id": 1, "name": "Pool"}]}}
    assert _is_sightmap_response(body) is False


def test_sightmap_shape_check_requires_fp_keys() -> None:
    """A bare ``floor_plans`` array without SightMap-specific keys is rejected."""
    body = {"data": {"floor_plans": [{"id": 1, "name": "X"}]}}
    assert _is_sightmap_response(body) is False


def test_sightmap_shape_check_accepts_real_fp_envelope() -> None:
    """``floor_plans`` with SightMap-specific keys (bedroom_count) matches."""
    body = {"data": {"floor_plans": [{"id": 1, "name": "X", "bedroom_count": 1}]}}
    assert _is_sightmap_response(body) is True


@pytest.mark.asyncio
async def test_sightmap_tier_re_stamped_on_partial_parse() -> None:
    """SightMap success with >20% join drops emits SIGHTMAP_PARTIAL_JOIN warning."""
    body = {
        "data": {
            "units": [
                {"floor_plan_id": 1, "price": 1500, "unit_number": "101", "area": 700},
                {"floor_plan_id": 999, "price": 1500, "unit_number": "102", "area": 700},
                {"floor_plan_id": 999, "price": 1500, "unit_number": "103", "area": 700},
                {"floor_plan_id": 999, "price": 1500, "unit_number": "104", "area": 700},
                {"floor_plan_id": 999, "price": 1500, "unit_number": "105", "area": 700},
            ],
            "floor_plans": [{"id": 1, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1}],
        }
    }
    adapter = SightMapAdapter()
    ctx = _make_ctx([{"url": "https://sightmap.com/api", "body": body}])
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    assert any(e.startswith("SIGHTMAP_PARTIAL_JOIN") for e in result.errors)


def test_sightmap_matches_response_body_protocol() -> None:
    """``matches_response_body`` is implemented and reuses the predicate."""
    adapter = SightMapAdapter()
    assert (
        adapter.matches_response_body(
            {"data": {"units": [{"id": "1"}], "floor_plans": [{"id": 1, "bedroom_count": 1}]}}
        )
        is True
    )
    assert adapter.matches_response_body({"random": "not-sightmap"}) is False
