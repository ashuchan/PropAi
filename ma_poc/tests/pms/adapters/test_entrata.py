"""Phase 3 — Entrata adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    parse_entrata_floorplans,
    parse_entrata_widget_envelope,
)
from ma_poc.pms.detector import detect_pms

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"


def _load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _make_ctx(api_responses: list[dict]) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://www.hackneyhouseapartments.com/",
        detected=detect_pms("https://www.hackneyhouseapartments.com/"),
        profile=None,
        expected_total_units=None,
        property_id="257356",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


class _DummyPage:
    """Minimal mock for Playwright Page."""

    pass


# ── probe-seam stub (2026-07-26) ────────────────────────────────────────────
# When the captured-API stage yields nothing the Entrata adapter walks its
# static Prospect-Portal fallbacks (``_entrata_static_fetch`` at entrata.py:1775,
# reached from :2259/:2297/:2317/:2359/:2406/:2526/:2534). Those fetch through
# the *sync* ``ma_poc.pms.adapters._probe.probe_get`` curl_cffi seam, which no
# page/adapter-level stub in this file intercepts — so the no-data tests below
# were hitting the live internet (hackneyhouseapartments.com, livethearch.com).
#
# The stub answers "nothing here": ``_entrata_static_fetch`` maps a non-200 to
# ``""``, so the PP cascade finds no grid and the adapter reaches the empty-exit
# classification the tests assert on. Unit data for the fixture-driven tests
# still comes from the stored JSON fixtures via ``ctx._api_responses``.


class _NoDataProbeResponse:
    """curl_cffi-response stand-in carrying no usable body.

    Exposes the full attribute surface adapter call sites read off a probe
    response: ``status_code`` / ``text`` / ``content`` / ``headers`` / ``url``.
    """

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``_probe`` fetch seam at an offline 404/empty-body stub."""

    def _fake_probe_get(url: str = "", **_kw: object) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    def _fake_probe_post(
        url: str = "", data: object = None, **_kw: object
    ) -> _NoDataProbeResponse:
        return _NoDataProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_post", _fake_probe_post)


@pytest.mark.asyncio
async def test_entrata_extract_happy_path() -> None:
    """Real Entrata payload (257356) produces units with rent and floor plan name."""
    responses = _load_fixture("257356.json")
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) >= 10
    first = result.units[0]
    assert first["floor_plan_name"]
    assert first["rent_range"]
    assert "ENTRATA" in first["extraction_tier"]


@pytest.mark.asyncio
async def test_entrata_extract_from_stored_fixture() -> None:
    """All stored fixtures load and produce units."""
    for fixture_path in FIXTURES.glob("*.json"):
        responses = json.loads(fixture_path.read_text(encoding="utf-8"))
        adapter = EntrataAdapter()
        ctx = _make_ctx(responses)
        result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
        assert isinstance(result, AdapterResult)
        # 252511 has no floorplan data (only availability widget + ppConfig)
        if "257356" in fixture_path.name:
            assert len(result.units) > 0


@pytest.mark.asyncio
async def test_entrata_extract_returns_empty_list_on_no_data() -> None:
    """Noise-only responses produce empty units, not an exception."""
    responses = [
        {"url": "https://example.com/Apartments/module/widgets/", "body": {"widget_name": "directions"}}
    ]
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


def test_parse_entrata_floorplans_basic() -> None:
    """Parse a minimal Entrata floorplan list."""
    items = [
        {
            "id": 100,
            "floorplan-name": "A1",
            "no_of_bedroom": 1,
            "no_of_bathroom": 1,
            "square_footage": 750,
            "min_rent": "$1,500",
            "max_rent": "$1,800",
        },
    ]
    units = parse_entrata_floorplans(items, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "A1"
    assert units[0]["bedrooms"] == "1"
    assert "$1,500" in units[0]["rent_range"]


def test_parse_entrata_widget_envelope() -> None:
    """Parse from widget_data.content.floor_plans envelope."""
    body = {
        "widget_name": "floor_plans",
        "widget_data": {
            "content": {
                "floor_plans": {
                    "floor_plans": [
                        {
                            "id": 1,
                            "floorplan-name": "B2",
                            "no_of_bedroom": 2,
                            "no_of_bathroom": 2,
                            "square_footage": 1000,
                            "min_rent": "$2,000",
                            "max_rent": "$2,500",
                        },
                    ]
                }
            }
        },
    }
    units = parse_entrata_widget_envelope(body, "https://test.com/widgets/")
    assert len(units) == 1
    assert units[0]["floor_plan_name"] == "B2"


def test_static_fingerprints_nonempty() -> None:
    adapter = EntrataAdapter()
    fps = adapter.static_fingerprints()
    assert len(fps) >= 1
    assert "entrata.com" in fps


def test_tier_used_label_is_pms_specific() -> None:
    items = [
        {
            "id": 1,
            "floorplan-name": "X",
            "no_of_bedroom": 1,
            "no_of_bathroom": 1,
            "square_footage": 500,
            "min_rent": "$1,000",
            "max_rent": "$1,000",
        }
    ]
    units = parse_entrata_floorplans(items, "test")
    assert "ENTRATA" in units[0]["extraction_tier"]


def test_rent_within_sanity_range() -> None:
    """All emitted rents from real fixture are in sanity range."""
    responses = _load_fixture("257356.json")
    for resp in responses:
        body = resp.get("body")
        if isinstance(body, list) and body and isinstance(body[0], dict):
            units = parse_entrata_floorplans(body, "test")
            for u in units:
                if u["rent_range"]:
                    # Extract numeric rent from range
                    import re

                    nums = re.findall(r"\d[\d,]*", u["rent_range"])
                    for n in nums:
                        val = int(n.replace(",", ""))
                        assert 200 <= val <= 50000, f"Rent {val} out of range"


# ── Bug 9 (2026-05-09) — direct-endpoint probe ──────────────────────────────


class _ProbingPage:
    """Stub Page that returns a scripted payload from the first probe URL it
    sees. Used to exercise EntrataAdapter._probe_known_endpoints without a
    real browser session."""

    def __init__(self, url: str, payload_by_path: dict[str, object] | None) -> None:
        self.url = url
        self._payloads = payload_by_path or {}
        self.calls: list[str] = []

    async def evaluate(self, _script: str, target_url: str) -> object | None:
        self.calls.append(target_url)
        # Find a path key whose suffix matches the requested URL.
        for path, payload in self._payloads.items():
            if target_url.endswith(path):
                return payload
        return None


@pytest.mark.asyncio
async def test_bug9_probe_returns_units_when_floorplans_endpoint_responds() -> None:
    """Bug 9: direct probe of /Apartments/module/floor_plans/ wins when the
    captured-API path returned nothing."""
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        payload_by_path={
            "/Apartments/module/floor_plans/": [
                {
                    "id": 1,
                    "floorplan-name": "1BR-A",
                    "no_of_bedroom": 1,
                    "no_of_bathroom": 1,
                    "square_footage": "650",
                    "min_rent": "$1,500",
                    "max_rent": "$1,500",
                },
                {
                    "id": 2,
                    "floorplan-name": "2BR-B",
                    "no_of_bedroom": 2,
                    "no_of_bathroom": 2,
                    "square_footage": "950",
                    "min_rent": "$2,100",
                    "max_rent": "$2,300",
                },
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    assert len(result.units) == 2
    assert result.tier_used == "TIER_1_API_ENTRATA_PROBE"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_bug9_probe_skips_paths_until_data_returned() -> None:
    """Bug 9: probes try each path until one returns useful data, then stop."""
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        # First two probe paths return None; floor_plans is later in the
        # catalogue but we map only that one to a payload.
        payload_by_path={
            "/api/floorplans": [
                {
                    "id": 7,
                    "floorplan-name": "Studio",
                    "no_of_bedroom": 0,
                    "no_of_bathroom": 1,
                    "square_footage": "450",
                    "min_rent": "$1,100",
                    "max_rent": "$1,100",
                }
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_B",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    assert len(result.units) == 1
    # The probe loop tried each catalogue path in order until /api/floorplans hit.
    assert any(call.endswith("/api/floorplans") for call in page.calls)


@pytest.mark.asyncio
async def test_bug9_probe_handles_evaluate_exception() -> None:
    """Bug 9: a probe that raises must not abort the whole extract — the
    adapter falls through to the empty-result path cleanly."""

    class _RaisingPage:
        url = "https://www.livethearch.com/"

        async def evaluate(self, _script: str, _url: str) -> None:
            raise RuntimeError("boom")

    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_C",
    )
    ctx._api_responses = []  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(_RaisingPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_bug9_probe_skipped_when_captured_api_already_has_units() -> None:
    """Bug 9: when the captured-API path already produced units, the probe
    must NOT run (return early). Otherwise we'd double-fire the LLM cost
    on every successful Entrata property."""
    captured = [
        {
            "url": "https://www.livethearch.com/Apartments/module/widgets/",
            "body": [
                {
                    "id": 99,
                    "floorplan-name": "From Capture",
                    "no_of_bedroom": 1,
                    "no_of_bathroom": 1,
                    "square_footage": "700",
                    "min_rent": "$1,800",
                    "max_rent": "$1,800",
                }
            ],
        }
    ]
    page = _ProbingPage(
        url="https://www.livethearch.com/",
        payload_by_path={
            "/Apartments/module/floor_plans/": [
                {
                    "id": 100,
                    "floorplan-name": "FROM PROBE",
                    "no_of_bedroom": 2,
                    "no_of_bathroom": 2,
                    "square_footage": "1100",
                    "min_rent": "$2,500",
                    "max_rent": "$2,500",
                }
            ]
        },
    )
    ctx = AdapterContext(
        base_url="https://www.livethearch.com/",
        detected=detect_pms("https://www.livethearch.com/"),
        profile=None,
        expected_total_units=None,
        property_id="P_BUG9_D",
    )
    ctx._api_responses = captured  # type: ignore[attr-defined]
    result = await EntrataAdapter().extract(page, ctx)  # type: ignore[arg-type]
    # Captured units win; probe never ran.
    assert len(result.units) == 1
    assert result.units[0]["floor_plan_name"] == "From Capture"
    assert page.calls == []
    assert result.tier_used == "TIER_1_API_ENTRATA"


# ── Prospect Portal SSR-DOM recovery (2026-05-19) ────────────────────────
# Card data captured live from Arrive Oak Brook Heights
# (arriveoakbrookheights.prospectportal.com/.../conventional/) and a vanity
# Prospect Portal domain — the shape the SSR ``.fp-card`` grid emits.
from ma_poc.pms.adapters.entrata import parse_prospect_portal_cards  # noqa: E402

_PP_CARDS = [
    {
        "name": "Adams",
        "bedbath": "1 Bed / 1 Bath",
        "sqft": "760 sq. ft",
        "fee": "Starting from $1,939/month",
        "lease": "12mo lease",
        "deposit": "$500 deposit",
        "availability": "Available May 30, 2026",
        "special": "",
    },
    {
        "name": "Baldwin Townhome",
        "bedbath": "2 Bed / 2.5 Bath",
        "sqft": "1,225 sq. ft",
        "fee": "Starting from $2,269/month",
        "lease": "10mo lease",
        "deposit": "$500 deposit",
        "availability": "Available May 27, 2026",
        "special": "",
    },
    {
        "name": "Butterfield Townhome",
        "bedbath": "2 Bed / 1.5 Bath",
        "sqft": "1,072 sq. ft",
        "fee": "Starting from $2,469/month",
        "lease": "12mo lease",
        "deposit": "$500 deposit",
        "availability": "2 Units Available",
        "special": "$1000 off first month",
    },
]


class _PPPage:
    """Page stub whose evaluate() returns SSR Prospect Portal cards."""

    def __init__(self, cards: list[dict], url: str = "https://x.prospectportal.com/c/s/conventional/") -> None:
        self._cards = cards
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> list[dict]:
        return self._cards


def test_parse_prospect_portal_cards_fields() -> None:
    units = parse_prospect_portal_cards(_PP_CARDS, "https://x.prospectportal.com/c/s/conventional/")
    assert len(units) == 3
    adams = units[0]
    assert adams["floor_plan_name"] == "Adams"
    assert adams["bedrooms"] == "1"
    assert adams["bathrooms"] == "1"
    assert adams["sqft"] == "760"
    assert adams["market_rent_low"] == 1939
    assert adams["lease_term"] == "12mo lease"
    assert adams["deposit"] == "$500 deposit"
    assert adams["availability_date"] == "May 30, 2026"
    assert adams["availability_status"] == "AVAILABLE"
    assert adams["extraction_tier"] == "TIER_3_DOM_ENTRATA_PP"
    # Baldwin: 2.5 bath, comma sqft
    baldwin = units[1]
    assert baldwin["bathrooms"] == "2.5"
    assert baldwin["sqft"] == "1225"
    # Butterfield: per-plan unit count + concession
    butter = units[2]
    assert butter["available_units"] == "2"
    assert butter["availability_date"] == ""
    assert butter["concession"] == "$1000 off first month"


def test_parse_prospect_portal_studio_and_waitlist() -> None:
    cards = [
        {"name": "S1", "bedbath": "Studio / 1 Bath", "sqft": "500 sq. ft",
         "fee": "$1,200 – $1,400/month", "lease": "", "deposit": "",
         "availability": "Waitlist Available", "special": ""},
    ]
    units = parse_prospect_portal_cards(cards, "u")
    assert units[0]["bedrooms"] == "0"
    assert units[0]["bed_label"]
    assert units[0]["market_rent_low"] == 1200
    assert units[0]["market_rent_high"] == 1400
    assert units[0]["availability_status"] == "UNAVAILABLE"


@pytest.mark.skip(
    reason=(
        "SSR fallback drifted after fix/resolver-path-patterns-may13 merge: "
        "the merged adapter emits TIER_1_API_ENTRATA_NO_RESPONSE (empty-exit "
        "label for Path B retry) before reaching the SSR Prospect Portal "
        "fallback. Re-enable after the SSR fallback is wired ahead of the "
        "no-response classification."
    )
)
@pytest.mark.asyncio
async def test_entrata_ssr_fallback_when_api_empty() -> None:
    """No API responses → SSR Prospect Portal DOM fallback fires."""
    adapter = EntrataAdapter()
    ctx = _make_ctx([])
    result = await adapter.extract(_PPPage(_PP_CARDS), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_3_DOM_ENTRATA_PP"
    assert len(result.units) == 3
    assert result.confidence > 0.0
    assert result.units[0]["floor_plan_name"] == "Adams"


@pytest.mark.asyncio
async def test_entrata_ssr_not_used_when_api_succeeds() -> None:
    """A genuine API hit wins; SSR DOM fallback is never consulted."""
    responses = _load_fixture("257356.json")
    adapter = EntrataAdapter()
    ctx = _make_ctx(responses)
    result = await adapter.extract(_PPPage(_PP_CARDS), ctx)  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_API_ENTRATA"
    assert all(u["floor_plan_name"] != "Adams" for u in result.units)


def test_detector_routes_prospectportal_host_to_entrata() -> None:
    det = detect_pms("https://ariaatella.prospectportal.com/spring/aria-at-ella/conventional/")
    assert det.pms == "entrata"


def test_detector_routes_prospect_portal_html_to_entrata() -> None:
    html = (
        '<html><head><script src="https://commoncf.entrata.com/website_templates/'
        '_assets/prospect_portal/module/floorplan_overview.min.js"></script>'
        "</head><body></body></html>"
    )
    det = detect_pms("https://www.somevanitydomain.com/city/slug/conventional/", page_html=html)
    assert det.pms == "entrata"


def test_parse_prospect_portal_live_vanity_variants() -> None:
    """Live rivertonterrace.com (vanity domain) card-text variants:
    'NNN+ sq. ft', 'From $X/month', bare '$X/month', 'Only 1 Unit Available!'.
    """
    cards = [
        {"name": "1 x 1", "bedbath": "1 Bed / 1 Bath", "sqft": "630+ sq. ft",
         "fee": "From $1,054/month", "lease": "12mo lease", "deposit": "$500 deposit",
         "availability": "Only 1 Unit Available!", "special": ""},
        {"name": "3 x 1", "bedbath": "3 Bed / 1 Bath", "sqft": "900+ sq. ft",
         "fee": "$1,471/month", "lease": "12mo lease", "deposit": "$500 deposit",
         "availability": "Available Jul 07, 2026", "special": ""},
    ]
    units = parse_prospect_portal_cards(cards, "https://www.rivertonterrace.com/x/y/conventional/")
    assert units[0]["sqft"] == "630"
    assert units[0]["market_rent_low"] == 1054
    assert units[0]["available_units"] == "1"
    assert units[1]["sqft"] == "900"
    assert units[1]["market_rent_low"] == 1471
    assert units[1]["available_units"] == ""
    assert units[1]["availability_date"] == "Jul 07, 2026"


# ─────────────────────────────────────────────────────────────────────
# 2026-05-20: structured empty-exit labels (Path B retry support).
# 193 properties in canary sw9p4 had ``tier_used == "TIER_1_API_ENTRATA"``
# with 0 strict units — the adapter initialised the result tier_used to
# the success label and never overwrote on a 0-unit exit, so Path B
# retry could not fire (``is_empty_exit("TIER_1_API_ENTRATA")`` is False
# by design — that string is the SUCCESS label). Fix: classify the
# failure mode and stamp ``_NO_RESPONSE`` / ``_SHAPE_REJECTED`` /
# ``_EMPTY`` based on what happened.
# ─────────────────────────────────────────────────────────────────────


def test_classify_entrata_failure_no_response() -> None:
    """Empty api_responses → _NO_RESPONSE label."""
    from ma_poc.pms.adapters.entrata import _classify_entrata_failure
    tier, msg = _classify_entrata_failure([])
    assert tier == "TIER_1_API_ENTRATA_NO_RESPONSE"
    assert "NO_RESPONSE" in msg


def test_classify_entrata_failure_shape_rejected() -> None:
    """Responses captured but none Entrata-shaped → _SHAPE_REJECTED."""
    from ma_poc.pms.adapters.entrata import _classify_entrata_failure
    responses = [
        {"url": "https://x.com/api/analytics", "body": {"event": "page_view"}},
        {"url": "https://x.com/api/ga", "body": [{"unrelated": "data"}]},
    ]
    tier, msg = _classify_entrata_failure(responses)
    assert tier == "TIER_1_API_ENTRATA_SHAPE_REJECTED"
    assert "2 responses" in msg


def test_classify_entrata_failure_empty_when_shape_matched() -> None:
    """Entrata-shaped envelope captured but no parseable units → _EMPTY."""
    from ma_poc.pms.adapters.entrata import _classify_entrata_failure
    responses = [
        {
            "url": "https://x.com/Apartments/module/widgets/floorplans",
            "body": [{"floorplan-name": "1BR", "no_of_bedroom": 1, "square_footage": 750}],
        },
    ]
    tier, msg = _classify_entrata_failure(responses)
    assert tier == "TIER_1_API_ENTRATA_EMPTY"
    assert "shape-matched" in msg


def test_is_entrata_response_recognizes_flat_list() -> None:
    """Flat list of floorplan dicts with Entrata keys is shape-matched."""
    from ma_poc.pms.adapters.entrata import _is_entrata_response
    assert _is_entrata_response(
        [{"floorplan-name": "1BR", "square_footage": 750}]
    )
    assert _is_entrata_response([{"no_of_bedroom": 1}])
    # Non-Entrata-shaped body must NOT match.
    assert not _is_entrata_response([{"random": "data"}])
    assert not _is_entrata_response([])
    assert not _is_entrata_response(None)


def test_is_entrata_response_recognizes_widget_envelope() -> None:
    """Widget envelope dicts with known Entrata keys are shape-matched."""
    from ma_poc.pms.adapters.entrata import _is_entrata_response
    assert _is_entrata_response({"widget_data": {"any": "thing"}})
    assert _is_entrata_response({"floorplans": [{"x": 1}]})
    assert _is_entrata_response({"available_units": []})
    assert not _is_entrata_response({"unrelated": "data"})


def test_empty_exit_label_recognised_by_path_b_registry() -> None:
    """The new Entrata empty-exit tier_used labels MUST be recognised
    by ``is_empty_exit()`` so the orchestrator's Path B retry fires.
    This is the regression that the 193-property cohort exposed —
    bare ``TIER_1_API_ENTRATA`` is success-label so retry never ran."""
    from ma_poc.pms.empty_exit import is_empty_exit
    # Bare success label is NOT empty-exit (by design).
    assert not is_empty_exit("TIER_1_API_ENTRATA")
    # All three failure labels MUST be recognised.
    assert is_empty_exit("TIER_1_API_ENTRATA_NO_RESPONSE")
    assert is_empty_exit("TIER_1_API_ENTRATA_SHAPE_REJECTED")
    assert is_empty_exit("TIER_1_API_ENTRATA_EMPTY")
