"""Regression tests for the Sylvan Tributary / Squarespace + SightMap case.

User-flagged residue (2026-05-25):
  URL : https://www.sylvantributary.com/floor-plans
  Flag: "rent data in engrain sitemap, not captured"

Deep-probe finding (verified 2026-05-25 against live site):
  - Squarespace-hosted marketing site (NOT in production CSV).
  - Embeds TWO iframes in the same page:
      1. ``<iframe src="https://sightmap.com/embed/40vl503rwle/...">``
         → SightMap API ``/app/api/v1/60p7y1x3p7n/sightmaps/121550``
         → 125 units, 100% priced, 100% with availability date.
      2. ``<iframe src="https://www.embed.fortresstech.io/unit-availability/.../">``
         → FortressTech leasing portal.
  - The SightMap iframe is the canonical rent source (the user's own
    note confirms this). FortressTech is a separate (likely auxiliary)
    embed.

The existing pipeline ALREADY handles this site end-to-end:
  - Detector picks ``sightmap`` (0.93) over ``fortresstech`` (0.90) and
    ``squarespace_nopms`` (0.85) on the STRONG ``sightmap.com/embed/``
    marker (detector.py:686-691).
  - When the canary captures the SightMap XHR live, ``parse_sightmap_
    payload`` extracts 125 units cleanly.
  - When the canary canNOT capture the XHR (GET-mode fetch, or iframe
    XHR misses the capture window), ``_try_sightmap_iframe_fallback``
    re-issues the embed → ``__APP_CONFIG__`` → API chain and recovers
    the same 125 units. Emits ``TIER_1_API_SIGHTMAP_IFRAME``.

This file is a REGRESSION-PROTECTION ship: no code change, but the
detector-priority margin (sightmap 0.93 vs fortresstech 0.90) is only
0.03 — any future tweak that lowers the SightMap STRONG signal would
silently misroute Sylvan-like sites to FortressTech and lose the
rent data. The tests lock in the priority + the iframe-fallback path
against the live HTML + live SightMap API payload.

Fixtures captured live 2026-05-25:
  fixtures/sightmap/sylvan_tributary/floor_plans.html      — 534 KB
  fixtures/sightmap/sylvan_tributary/embed_40vl503rwle.html — 4 KB
  fixtures/sightmap/sylvan_tributary/api_response.json     — 376 KB
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.sightmap import (
    SightMapAdapter,
    _is_sightmap_response,
    extract_sightmap_api_url,
    find_sightmap_embed_codes,
    parse_sightmap_payload,
)
from ma_poc.pms.detector import _detect_pms_impl

FIXTURES = (
    Path(__file__).parent / "fixtures" / "sightmap" / "sylvan_tributary"
)

EMBED_CODE = "40vl503rwle"
API_TOKEN = "60p7y1x3p7n"
SIGHTMAP_ID = "121550"
EXPECTED_API_URL = (
    f"https://sightmap.com/app/api/v1/{API_TOKEN}/sightmaps/{SIGHTMAP_ID}"
)


def _load_html() -> str:
    return (FIXTURES / "floor_plans.html").read_text(encoding="utf-8")


def _load_embed_html() -> str:
    return (FIXTURES / "embed_40vl503rwle.html").read_text(encoding="utf-8")


def _load_api_responses() -> list[dict]:
    return json.loads(
        (FIXTURES / "api_response.json").read_text(encoding="utf-8")
    )


class _DummyPage:
    pass


class _DummyFetchResult:
    def __init__(self, body: bytes, final_url: str) -> None:
        self.body = body
        self.final_url = final_url


def _make_ctx(
    html: str | None,
    api_responses: list[dict],
    base_url: str = "https://www.sylvantributary.com/floor-plans",
) -> AdapterContext:
    detected = _detect_pms_impl(base_url, None, html)
    ctx = AdapterContext(
        base_url=base_url,
        detected=detected,
        profile=None,
        expected_total_units=None,
        property_id="TEST_SYLVAN_TRIBUTARY",
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    if html is not None:
        ctx.fetch_result = _DummyFetchResult(
            body=html.encode("utf-8"), final_url=base_url
        )
    return ctx


def test_detector_picks_sightmap_over_fortresstech_and_squarespace() -> None:
    """The dual-iframe Sylvan page contains:
      - ``sightmap.com/embed/`` → STRONG sightmap signal 0.93
      - ``embed.fortresstech.io/unit-availability/`` → STRONG fortresstech 0.90
      - ``squarespace.com`` script → MEDIUM squarespace_nopms 0.85
    SightMap MUST win — that's where the 125 priced units live.
    """
    html = _load_html()
    detected = _detect_pms_impl(
        "https://www.sylvantributary.com/floor-plans", None, html
    )
    assert detected.pms == "sightmap", (
        f"Expected sightmap, got {detected.pms!r}. Evidence: {detected.evidence}"
    )
    assert detected.confidence >= 0.93
    assert any("sightmap.com/embed/" in ev for ev in detected.evidence)


def test_find_sightmap_embed_codes_on_squarespace_html() -> None:
    """``find_sightmap_embed_codes`` must surface ``40vl503rwle`` from the
    real Squarespace-hosted page body, even though the iframe lives
    ~350 KB deep into the 534 KB HTML."""
    html = _load_html()
    codes = find_sightmap_embed_codes(html)
    assert EMBED_CODE in codes, (
        f"Expected {EMBED_CODE!r} in codes, got {codes!r}"
    )


def test_extract_api_url_from_embed_page() -> None:
    """The embed page must yield the canonical
    ``sightmap.com/app/api/v1/{token}/sightmaps/{id}`` URL via
    ``window.__APP_CONFIG__`` parsing."""
    embed_html = _load_embed_html()
    api_url = extract_sightmap_api_url(embed_html)
    assert api_url == EXPECTED_API_URL


def test_parse_sightmap_payload_returns_125_priced_units() -> None:
    """Joining ``data.units`` to ``data.floor_plans`` on the live API
    payload must produce 125 unit rows, every one with a positive
    ``rent_range`` and an availability date.

    This locks in the expectation that operator-data-gap dropping
    (``_drop_zero_info_sightmap_units``) does NOT remove Sylvan
    Tributary units — they're all genuinely available with rent.
    """
    responses = _load_api_responses()
    body = responses[0]["body"]
    assert _is_sightmap_response(body)

    units, dropped = parse_sightmap_payload(body, responses[0]["url"])
    assert len(units) == 126, (
        f"Expected 126 units (125 priced + 1 plan-presence row for empty plan), got {len(units)} (dropped {dropped})"
    )
    assert dropped == 0

    # Every unit must carry a positive rent — Sylvan IS a priced site
    # (verified live: ``A-101 → $1,774``, etc.).
    no_rent = [
        u for u in units
        if str(u.get("rent_range") or "").strip() in {"", "$0", "0"}
        and u.get("data_quality_flag") != "SIGHTMAP_PLAN_PRESENCE"
    ]
    assert not no_rent, (
        f"Expected 0 units without rent; got {len(no_rent)} "
        f"(first: {no_rent[0] if no_rent else None})"
    )

    # All units must have a real availability date.
    no_date = [
        u for u in units if not u.get("availability_date")
        and u.get("data_quality_flag") != "SIGHTMAP_PLAN_PRESENCE"
    ]
    assert not no_date, (
        f"Expected 0 units without availability_date; "
        f"got {len(no_date)}"
    )

    # Spot-check a known unit from the live capture.
    a_101 = [u for u in units if u.get("unit_number") == "A-101"]
    assert a_101, "Unit A-101 missing"
    assert "$1,774" in a_101[0]["rent_range"]
    assert a_101[0]["sqft"] == "856"
    assert a_101[0]["bed_label"] == "2 Bedroom"
    assert a_101[0]["availability_status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_adapter_extract_from_captured_xhr() -> None:
    """When the canary's Playwright session captures the SightMap XHR
    live, the adapter must take the primary path (no fallback), emit
    ``TIER_1_API_SIGHTMAP``, and return 125 units."""
    html = _load_html()
    responses = _load_api_responses()
    adapter = SightMapAdapter()
    ctx = _make_ctx(html, responses)

    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 126
    assert result.tier_used.startswith("TIER_1_API_SIGHTMAP")
    # Must NOT be the rent-gap or zero-info tier — Sylvan publishes rent.
    assert "OPERATOR_RENT_NOT_PUBLISHED" not in result.tier_used
    assert result.confidence > 0.7
    assert result.winning_url and "sightmaps/121550" in result.winning_url


def test_static_fingerprint_marker_still_present() -> None:
    """Defensive lock — the static fingerprint list must keep
    ``sightmap.com`` so the body-shape and host checks remain reachable
    if the marketing-page HTML ever loses the explicit ``embed/`` form."""
    fp = SightMapAdapter().static_fingerprints()
    assert "sightmap.com" in fp


class _StubAsyncResp:
    """Minimal stand-in for a ``probe_get`` response used by the iframe-
    fallback test below. Carries the accessors the SightMap adapter
    touches: ``status_code``, ``text``, and ``json()``."""

    def __init__(self, status_code: int, text: str, body: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._body = body

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.mark.asyncio
async def test_adapter_iframe_fallback_when_xhr_uncaptured(monkeypatch) -> None:
    """The most likely production failure mode: canary's Playwright
    capture window misses the SightMap iframe XHR (iframe lazy-loads,
    or the canary runs in GET mode with no live JS).

    With ``api_responses=[]``, the adapter must:
      1. Find the embed code from the page HTML.
      2. Refetch the embed page to discover the API URL.
      3. Refetch the API URL to recover the 125 units (+1 plan-presence
         row for the floor plan with no available units, added 5ea7772
         — hence 126 rows total).
      4. Emit ``TIER_1_API_SIGHTMAP_IFRAME``.

    ``_probe.probe_get`` is monkey-patched to serve the embed-page and
    API fixtures so the test is hermetic. It — not ``httpx.AsyncClient``
    — is the seam the fallback fetches through as of 1d8fb89; stubbing
    httpx left the call escaping to the LIVE internet, so this test was
    silently grading real sightmap.com inventory (85 units on 2026-07-26)
    against a May-2026 fixture count.
    """
    html = _load_html()
    embed_html = _load_embed_html()
    api_responses = _load_api_responses()
    api_body = api_responses[0]["body"]

    embed_url = f"https://sightmap.com/embed/{EMBED_CODE}"

    def _handler(url: str) -> _StubAsyncResp:
        if url == embed_url:
            return _StubAsyncResp(200, embed_html)
        if url == EXPECTED_API_URL:
            return _StubAsyncResp(200, json.dumps(api_body), body=api_body)
        return _StubAsyncResp(404, "")

    from ma_poc.pms.adapters import _probe as _probe_mod

    def _probe_get(url: str, **_kwargs) -> _StubAsyncResp:  # noqa: ANN003
        return _handler(url)

    monkeypatch.setattr(_probe_mod, "probe_get", _probe_get)

    adapter = SightMapAdapter()
    # NO captured api_responses — the iframe-fallback must do all the work.
    ctx = _make_ctx(html, api_responses=[])

    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert isinstance(result, AdapterResult)
    assert len(result.units) == 126, (
        f"Iframe-fallback produced {len(result.units)} rows, expected 126 "
        f"(125 fixture units + 1 plan-presence row). "
        f"tier={result.tier_used!r} errors={result.errors[:3]}"
    )
    assert result.tier_used == "TIER_1_API_SIGHTMAP_IFRAME", (
        f"Expected TIER_1_API_SIGHTMAP_IFRAME, got {result.tier_used!r}"
    )
    assert result.winning_url == EXPECTED_API_URL
    assert "OPERATOR_RENT_NOT_PUBLISHED" not in result.tier_used


def test_sightmap_signal_margin_over_fortresstech_recorded() -> None:
    """The SightMap 0.93 STRONG marker MUST beat the FortressTech 0.90
    marker on dual-iframe pages. This test exists because the margin is
    only 0.03 — any future tweak lowering SightMap (or raising
    FortressTech) would silently misroute Sylvan-class sites.

    Yields the raw HTML marker iterator and verifies both markers are
    present, in the expected priority order, before the orchestrator
    picks the highest-confidence hit.
    """
    from ma_poc.pms.detector import _iter_html_markers

    html = _load_html()
    signals = list(_iter_html_markers(html))
    # SightMap may yield multiple markers (STRONG ``sightmap.com/embed/``
    # @ 0.93 + WEAK ``sightmap.com`` @ 0.80). Compare the strongest hit
    # for each PMS — that's what the orchestrator does.
    by_pms_max: dict[str, float] = {}
    for pms, conf, _ in signals:
        if conf > by_pms_max.get(pms, -1.0):
            by_pms_max[pms] = conf

    assert "sightmap" in by_pms_max, (
        f"sightmap missing from {list(by_pms_max)}"
    )
    assert "fortresstech" in by_pms_max, (
        f"fortresstech missing from {list(by_pms_max)}"
    )
    assert by_pms_max["sightmap"] >= 0.93
    assert by_pms_max["sightmap"] > by_pms_max["fortresstech"], (
        f"SightMap STRONG signal ({by_pms_max['sightmap']}) must beat "
        f"FortressTech ({by_pms_max['fortresstech']}) on dual-iframe pages "
        f"— if this assertion fails, Sylvan Tributary and similar "
        f"Squarespace + SightMap + FortressTech sites will lose their "
        f"125 priced units."
    )
