"""SightMap subpage recovery (2026-05-24).

Pins the universal recovery that fires when no primary adapter found
units AND the homepage hides a SightMap embed behind a /floorplans/
link — the exact pattern that explains the TIER_1_API_SIGHTMAP P1
cohort (131 props in the prod-vs-canary gap report).

Strategy under test:
  1. Extract candidate /floorplans/ family URLs from homepage HTML.
  2. Probe each. Take the first whose body has a SightMap embed marker.
  3. Splice that body into ctx.fetch_result.
  4. Invoke SightMapAdapter — it discovers the embed code + canonical
     /sightmaps/{id} API.

Tests cover candidate selection, marker detection, no-op exits,
splicing correctness, and the negative path (no candidate matches).
"""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters._sightmap_subpage_recovery import (
    _candidate_subpages,
    _SIGHTMAP_MARKER_RE,
    recover_sightmap_subpage,
)


# ── small helpers ──────────────────────────────────────────────────────


def _make_ctx(
    *,
    body: bytes | str = b"<html>homepage</html>",
    final_url: str = "https://example.com/",
):
    """Mock AdapterContext with a frozen-dataclass-like fetch_result."""
    @dataclasses.dataclass
    class _FR:
        body: bytes | str | None
        final_url: str

    ctx = MagicMock()
    ctx.fetch_result = _FR(
        body=body if isinstance(body, bytes) else body.encode(),
        final_url=final_url,
    )
    ctx.base_url = final_url
    return ctx


def _fake_probe_response(status: int, text: str):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


# ── candidate URL discovery ───────────────────────────────────────────


def test_candidate_subpages_prefers_same_origin_body_links() -> None:
    """A homepage with an explicit ``/communities/foo/floorplans/`` link
    should be tried before the vanilla ``/floorplans/`` fallback —
    operators sometimes namespace under a community slug."""
    body = '''
    <html><body>
        <a href="/communities/parker-plano/floorplans/">View Floor Plans</a>
        <a href="/about">About us</a>
    </body></html>
    '''
    cand = _candidate_subpages(body, "https://www.parkerplano.com")
    # The body link should appear FIRST
    assert cand[0] == "https://www.parkerplano.com/communities/parker-plano/floorplans/"
    # The vanilla fallbacks still follow
    assert "https://www.parkerplano.com/floorplans/" in cand
    assert "https://www.parkerplano.com/availability/" in cand


def test_candidate_subpages_skips_off_origin_links() -> None:
    """A homepage link pointing at e.g. rentcafe.com/floorplans/ is the
    CTA the primary adapter already failed on — don't re-probe it."""
    body = '''
    <html><body>
        <a href="https://www.rentcafe.com/some/floorplans">Apply</a>
        <a href="https://other.com/availability">View</a>
    </body></html>
    '''
    cand = _candidate_subpages(body, "https://example.com")
    # None of the body's off-origin links should appear
    assert all("rentcafe.com" not in c for c in cand)
    assert all("other.com" not in c for c in cand)
    # The same-origin fallbacks should still be there
    assert any("example.com/floorplans/" in c for c in cand)


def test_candidate_subpages_dedupes_repeats() -> None:
    body = '''
    <a href="/floorplans/">1</a>
    <a href="/floorplans/">2</a>
    <a href="/availability/">3</a>
    '''
    cand = _candidate_subpages(body, "https://x.com")
    # Each URL should appear exactly once
    assert len(cand) == len(set(cand))
    # /floorplans/ from body shows up only once even with two anchors
    assert sum("x.com/floorplans/" in c for c in cand) == 1


def test_candidate_subpages_caps_at_six() -> None:
    """Cost-control: never probe more than 6 candidate subpages per
    property — the fallback list of vanilla paths + body-discovered
    same-origin links must collapse to ≤6."""
    body = "".join(
        f'<a href="/page{i}/floorplans/">x</a>' for i in range(20)
    )
    cand = _candidate_subpages(body, "https://x.com")
    assert len(cand) <= 6


def test_candidate_subpages_empty_body_falls_back_to_vanilla() -> None:
    cand = _candidate_subpages("", "https://x.com")
    assert cand, "must still return vanilla subpaths when body is empty"
    assert "https://x.com/floorplans/" in cand


def test_candidate_subpages_no_base_returns_empty() -> None:
    cand = _candidate_subpages("<body>hi</body>", "")
    assert cand == []


def test_candidate_subpages_skips_mailto_and_javascript() -> None:
    body = '''
    <a href="mailto:floorplans@example.com">email</a>
    <a href="javascript:showFloorplans()">js</a>
    <a href="tel:555-0100-floorplans">tel</a>
    '''
    cand = _candidate_subpages(body, "https://x.com")
    assert all(not c.startswith(("mailto:", "javascript:", "tel:")) for c in cand)
    # But vanilla still works
    assert any("x.com/floorplans/" in c for c in cand)


# ── marker regex ──────────────────────────────────────────────────────


def test_sightmap_marker_matches_embed() -> None:
    body = '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'
    assert _SIGHTMAP_MARKER_RE.search(body)


def test_sightmap_marker_matches_api_url() -> None:
    body = '"url":"https://sightmap.com/app/api/v1/abc/sightmaps/42"'
    assert _SIGHTMAP_MARKER_RE.search(body)


def test_sightmap_marker_matches_escaped_slashes() -> None:
    """Embed URLs inside JSON strings come escaped as ``sightmap.com\\/embed\\/``."""
    body = '"href":"https:\\/\\/sightmap.com\\/embed\\/abc123"'
    assert _SIGHTMAP_MARKER_RE.search(body)


def test_sightmap_marker_ignores_unrelated_text() -> None:
    body = "<p>A real-estate sightmap is not what we sell</p>"
    assert not _SIGHTMAP_MARKER_RE.search(body)


# ── recovery short-circuits ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_noop_when_homepage_already_has_sightmap() -> None:
    """If the homepage already shows a SightMap marker, the primary
    adapter (or Step 7b) would have caught it — don't double-fire."""
    body = '<html><iframe src="https://sightmap.com/embed/xyz"></iframe></html>'
    ctx = _make_ctx(body=body)
    with patch(
        "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get"
    ) as probe_mock:
        units = await recover_sightmap_subpage(None, ctx)
    assert units == []
    probe_mock.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_noop_when_no_fetch_result_body() -> None:
    ctx = MagicMock()
    ctx.fetch_result = None
    units = await recover_sightmap_subpage(None, ctx)
    assert units == []


@pytest.mark.asyncio
async def test_recovery_noop_when_no_base_url() -> None:
    ctx = _make_ctx(body="<html></html>", final_url="")
    ctx.base_url = ""
    units = await recover_sightmap_subpage(None, ctx)
    assert units == []


# ── happy path: subpage has SightMap → adapter extracts units ─────────


@pytest.mark.asyncio
async def test_recovery_probes_subpages_until_marker_found() -> None:
    """First /floorplans/ probe returns no SightMap; second probe
    (/availability/) returns one. Recovery picks the second and runs
    SightMapAdapter against it."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    fp_body = "<html>no sightmap here, plain HTML</html>"
    av_body = '<iframe src="https://sightmap.com/embed/abc123"></iframe>'

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    responses = {
        "https://example.com/floorplans/": _fake_probe_response(200, fp_body),
        "https://example.com/availability/": _fake_probe_response(200, av_body),
    }

    def fake_probe(url, **kw):
        return responses.get(
            url, _fake_probe_response(404, "")
        )

    # Capture what URL ended up spliced
    spliced_bodies = []

    fake_sm_result = MagicMock()
    fake_sm_result.units = [
        {"unit_number": "101", "market_rent_low": "1500", "sqft": "650"},
    ]

    class FakeSMAdapter:
        async def extract(self, page, ctx):
            spliced_bodies.append(
                ctx.fetch_result.body.decode("utf-8")
                if isinstance(ctx.fetch_result.body, bytes)
                else ctx.fetch_result.body
            )
            return fake_sm_result

    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            side_effect=fake_probe,
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            FakeSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert len(units) == 1
    assert units[0]["unit_number"] == "101"
    # The spliced body must be the SightMap-bearing /availability/ HTML
    assert spliced_bodies, "SightMapAdapter should have been invoked"
    assert "sightmap.com/embed" in spliced_bodies[0]


@pytest.mark.asyncio
async def test_recovery_stamps_extraction_tier_on_units() -> None:
    """Recovered units must carry a SUBPAGE_RECOVERY tier label so
    reporting can distinguish this path from direct SightMap detection."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    fp_body = '<iframe src="https://sightmap.com/embed/abc123"></iframe>'

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    fake_sm_result = MagicMock()
    # Note: NO extraction_tier on the unit dict — recovery must add one
    fake_sm_result.units = [{"unit_number": "1"}]

    class FakeSMAdapter:
        async def extract(self, page, ctx):
            return fake_sm_result

    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            return_value=_fake_probe_response(200, fp_body),
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            FakeSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units[0]["extraction_tier"] == "TIER_1_API_SIGHTMAP_SUBPAGE_RECOVERY"


@pytest.mark.asyncio
async def test_recovery_respects_existing_extraction_tier() -> None:
    """If SightMapAdapter set its own extraction_tier (e.g.
    TIER_1_API_SIGHTMAP_DIRECT from the direct-API probe), don't
    overwrite — the more-specific label wins for reporting."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    fp_body = '<iframe src="https://sightmap.com/embed/abc"></iframe>'

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    fake_sm_result = MagicMock()
    fake_sm_result.units = [
        {"unit_number": "1", "extraction_tier": "TIER_1_API_SIGHTMAP_DIRECT"},
    ]

    class FakeSMAdapter:
        async def extract(self, page, ctx):
            return fake_sm_result

    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            return_value=_fake_probe_response(200, fp_body),
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            FakeSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units[0]["extraction_tier"] == "TIER_1_API_SIGHTMAP_DIRECT"


# ── negative path: no candidate matches ───────────────────────────────


@pytest.mark.asyncio
async def test_recovery_returns_empty_when_no_subpage_has_sightmap() -> None:
    """Every probed subpage returns a body without SightMap markers —
    recovery cleanly returns [] (no false positives)."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    plain_body = "<html>no sightmap anywhere here</html>"

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    with patch(
        "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
        return_value=_fake_probe_response(200, plain_body),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units == []


@pytest.mark.asyncio
async def test_recovery_skips_subpages_with_non_200_status() -> None:
    """403/404 responses shouldn't be examined for markers — skip and
    move to the next candidate."""
    homepage = '<html><a href="/floorplans/">x</a></html>'

    responses = {
        "https://example.com/floorplans/": _fake_probe_response(403, "blocked"),
        "https://example.com/floor-plans/": _fake_probe_response(
            200, '<iframe src="sightmap.com/embed/x"></iframe>'
        ),
    }

    def fake_probe(url, **kw):
        return responses.get(url, _fake_probe_response(404, ""))

    fake_sm_result = MagicMock()
    fake_sm_result.units = [{"unit_number": "9"}]

    class FakeSMAdapter:
        async def extract(self, page, ctx):
            return fake_sm_result

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")
    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            side_effect=fake_probe,
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            FakeSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    # Recovery found the embed on /floor-plans/ despite /floorplans/ 403
    assert len(units) == 1


@pytest.mark.asyncio
async def test_recovery_returns_empty_when_sightmap_adapter_yields_no_units() -> None:
    """The subpage HAS a SightMap marker, the adapter is invoked, but
    the adapter itself yields 0 units (rare — e.g. the SightMap API
    is down or returns an unexpected shape). Recovery returns []."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    fp_body = '<iframe src="https://sightmap.com/embed/abc"></iframe>'

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    fake_sm_result = MagicMock()
    fake_sm_result.units = []  # adapter failed silently

    class FakeSMAdapter:
        async def extract(self, page, ctx):
            return fake_sm_result

    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            return_value=_fake_probe_response(200, fp_body),
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            FakeSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units == []


@pytest.mark.asyncio
async def test_recovery_swallows_sightmap_adapter_exceptions() -> None:
    """SightMapAdapter raising must not propagate — recovery is best-
    effort and returns [] on internal failures."""
    homepage = '<html><a href="/floorplans/">x</a></html>'
    fp_body = '<iframe src="https://sightmap.com/embed/abc"></iframe>'

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")

    class BrokenSMAdapter:
        async def extract(self, page, ctx):
            raise RuntimeError("simulated adapter blowup")

    with (
        patch(
            "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
            return_value=_fake_probe_response(200, fp_body),
        ),
        patch(
            "ma_poc.pms.adapters.sightmap.SightMapAdapter",
            BrokenSMAdapter,
        ),
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units == []


@pytest.mark.asyncio
async def test_recovery_swallows_probe_get_exceptions() -> None:
    """A network exception on probe_get must not propagate. Recovery
    moves on to the next candidate and ultimately returns [] if no
    candidate yields a marker."""
    homepage = '<html><a href="/floorplans/">x</a></html>'

    def fake_probe(url, **kw):
        raise ConnectionError("simulated network failure")

    ctx = _make_ctx(body=homepage, final_url="https://example.com/")
    with patch(
        "ma_poc.pms.adapters._sightmap_subpage_recovery.probe_get",
        side_effect=fake_probe,
    ):
        units = await recover_sightmap_subpage(None, ctx)

    assert units == []
