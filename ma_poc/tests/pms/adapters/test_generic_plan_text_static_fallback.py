"""Static-body fallback for GenericPlanTextAdapter (2026-05-24).

Pins the fix that lifts the TIER_1_DOM_GENERIC_PLAN_TEXT cluster (67
props in focused-3886351 canary). Pre-fix the adapter hard-required
``page.evaluate`` to harvest ``document.body.innerText``; the Phase 6
orchestrator dispatched it with a stub page so 67/67 properties
bailed out with the single error "generic_plan_text: no live page"
without ever exercising the parser — even though their captured
fetch_result.body carried 6-23 rent signals each.

Post-fix the adapter derives bodyText from ctx.fetch_result.body via
``_bodytext_from_fetch_result`` (strip script/style, unescape JS
entities, drop HTML tags, collapse whitespace) and feeds the same
regex pipeline.
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic_plan_text import (
    GenericPlanTextAdapter,
    _bodytext_from_fetch_result,
)
from ma_poc.pms.detector import DetectedPMS


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

    The subject here is the static-body fallback: bodyText derived from
    ``ctx.fetch_result.body``. When that body yields no plan rows the
    adapter probes ``/floorplans/`` … ``/apartments/`` on
    ``www.example.com`` before giving up. A 404 keeps that sweep a
    deterministic no-op, so the "found nothing" assertion is about the
    body under test rather than whatever example.com happens to serve.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


def _make_ctx(body: bytes | str | None) -> AdapterContext:
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    body_bytes: bytes | None
    if isinstance(body, str):
        body_bytes = body.encode("utf-8", "replace")
    elif isinstance(body, bytes):
        body_bytes = body
    else:
        body_bytes = None

    fr = FetchResult(
        url="https://www.example.com/",
        outcome=FetchOutcome.OK,
        status=200,
        body=body_bytes,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://www.example.com/",
        attempts=1,
        elapsed_ms=1234,
    )
    return AdapterContext(
        base_url="https://www.example.com/",
        detected=DetectedPMS(pms="unknown", confidence=0.0, evidence=()),
        profile=None,
        expected_total_units=None,
        property_id="TEST-PID",
        fetch_result=fr,
    )


# ----- helper: _bodytext_from_fetch_result ---------------------------


def test_bodytext_strips_script_and_style_blocks() -> None:
    """innerText omits <script> and <style>; the fallback must match
    or it leaks JS string literals into the rent/plan regex."""
    html = b"""
    <html><body>
      <script>var plans = [{rent: '$9999'}];</script>
      <style>.x{color:red}</style>
      <p>1 Bedroom / 1 Bath  $1,800/month  650 sq ft</p>
    </body></html>
    """
    ctx = _make_ctx(html)
    body = _bodytext_from_fetch_result(ctx)
    assert "$9999" not in body, "script content leaked through"
    assert ".x{color:red}" not in body, "style content leaked through"
    assert "1 Bedroom" in body
    assert "$1,800" in body


def test_bodytext_collapses_whitespace_and_strips_tags() -> None:
    html = b"""
    <html><body>
      <div><span>2 Bed</span>  /  <span>2 Bath</span>
        <br><strong>$2,400</strong>     <em>900 sq ft</em>
      </div>
    </body></html>
    """
    body = _bodytext_from_fetch_result(_make_ctx(html))
    # No tags should remain
    assert "<" not in body and ">" not in body
    # Whitespace collapsed
    assert "  " not in body
    # Tokens preserved
    assert "2 Bed" in body
    assert "$2,400" in body
    assert "900 sq ft" in body


def test_bodytext_unescapes_js_entities() -> None:
    """Wix-style bodies embed JS-unicode entities in JSON blobs that
    survive the tag strip — the existing unescape pass converts them
    so the regex can see "$1,608"."""
    html = (
        b"<html><body>"
        b'<script>blob = "\\u003cstrong\\u003e$1,608\\u003c/strong\\u003e"</script>'
        b"<div>1 Bedroom 1 Bath \\u003cstrong\\u003e$1,608\\u003c/strong\\u003e 700 sq ft</div>"
        b"</body></html>"
    )
    body = _bodytext_from_fetch_result(_make_ctx(html))
    assert "$1,608" in body
    # The script-wrapped one was stripped; the inline one survived + got unescaped
    assert "1 Bedroom" in body


def test_bodytext_returns_empty_for_missing_body() -> None:
    assert _bodytext_from_fetch_result(_make_ctx(None)) == ""


def test_bodytext_accepts_str_body_too() -> None:
    """body field is typed bytes | None, but legacy paths sometimes
    store str — accept both rather than failing closed."""
    ctx = _make_ctx(None)
    # Manually swap a str into the frozen FetchResult
    ctx.fetch_result = dataclasses.replace(
        ctx.fetch_result, body=None
    )
    # Set body to str via dataclasses.replace bypass — not the prod path,
    # but exercises the isinstance(raw, str) branch
    object.__setattr__(
        ctx.fetch_result.__class__, "__match_args__", ()
    ) if False else None
    # Easier: use the actual path — ctx.fetch_result.body is bytes already
    # so just call directly with a str body via a stub ctx
    stub = MagicMock()
    stub.fetch_result = MagicMock()
    stub.fetch_result.body = "<html><body>1 Bed 1 Bath $1,400 600 sqft</body></html>"
    out = _bodytext_from_fetch_result(stub)
    assert "$1,400" in out


# ----- end-to-end: extract() with no live page -----------------------


def _run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio._get_running_loop() else asyncio.run(coro)


def test_extract_uses_static_fallback_when_page_has_no_evaluate() -> None:
    """End-to-end: dispatch the adapter with a stub page (no .evaluate)
    against a body that has >=2 plan rows + rent + sqft. Pre-fix it
    would have bailed with 'no live page'. Post-fix it admits units."""
    # Two distinct plans + sqft + rent — enough to clear the
    # >=2-distinct-rows anti-noise threshold.
    body = (
        b"<html><body>"
        b"<h2>Floor Plans</h2>"
        b"<div class='plan'>"
        b"<h3>The Maple - 1 Bedroom / 1 Bath</h3>"
        b"<p>From $1,450 per month</p>"
        b"<p>650 sq ft</p>"
        b"</div>"
        b"<div class='plan'>"
        b"<h3>The Oak - 2 Bedroom / 2 Bath</h3>"
        b"<p>From $1,950 per month</p>"
        b"<p>950 sq ft</p>"
        b"</div>"
        b"</body></html>"
    )
    ctx = _make_ctx(body)
    stub_page = MagicMock(spec=[])  # no .evaluate attribute

    adapter = GenericPlanTextAdapter()
    result = asyncio.run(adapter.extract(stub_page, ctx))

    # Post-fix: bodyText derived from fetch_result; adapter ran the parser
    assert any(
        "static-body fallback" in e for e in result.errors
    ), f"static-body fallback marker missing in errors: {result.errors}"
    # Pre-fix the only error was the exact-match early bailout
    # "generic_plan_text: no live page". Post-fix the marker reads
    # "no live page evaluate" (substring overlap is fine — we just want
    # to confirm the early-bailout sentinel isn't the SOLE error).
    early_bailout = "generic_plan_text: no live page"
    assert not any(
        e == early_bailout for e in result.errors
    ), f"old early-bailout error still fires verbatim: {result.errors}"


def test_extract_static_fallback_extracts_units_when_body_has_plans() -> None:
    """Confirm the static fallback admits units end-to-end (validity
    gate passes, strict-pass count expected)."""
    body = b"""
    <html><body>
      <div>The Maple Suite - 1 Bedroom / 1 Bath - $1,450/month - 650 sq ft</div>
      <div>The Oak - 2 Bedroom / 2 Bath - $1,950/month - 950 sq ft</div>
      <div>The Pine - 3 Bedroom / 2 Bath - $2,400/month - 1,200 sq ft</div>
    </body></html>
    """
    ctx = _make_ctx(body)
    stub_page = MagicMock(spec=[])

    adapter = GenericPlanTextAdapter()
    result = asyncio.run(adapter.extract(stub_page, ctx))

    assert len(result.units) >= 2, (
        f"expected ≥2 admitted units, got {len(result.units)} "
        f"(errors: {result.errors})"
    )
    # At least one unit should have both rent and sqft (strict pass)
    strict = [
        u for u in result.units
        if u.get("market_rent_low") and u.get("sqft")
    ]
    assert strict, (
        f"no strict-pass units extracted; units={result.units}"
    )


def test_extract_static_fallback_returns_empty_when_no_plans() -> None:
    """When the static fallback runs but finds no plan rows, the
    adapter should still emit the existing <2-row error rather than
    silently admitting nothing."""
    body = b"<html><body><p>Welcome to our luxury apartments!</p></body></html>"
    ctx = _make_ctx(body)
    stub_page = MagicMock(spec=[])

    adapter = GenericPlanTextAdapter()
    result = asyncio.run(adapter.extract(stub_page, ctx))

    assert result.units == []
    # Either the no-rows error or the empty-body error is fine —
    # both mean "we tried and found nothing"
    assert any(
        "distinct plan rows" in e or "empty body" in e
        for e in result.errors
    )


def test_extract_static_fallback_skipped_when_live_page_works() -> None:
    """Sanity check: when evaluate IS callable and returns a useful
    payload, the DOM-JS path stays the source — static fallback only
    fires as a backup."""
    # async-ready evaluate stub
    async def fake_eval(_js: str) -> dict[str, str]:
        return {"ok": "true", "bodyText": "1 Bedroom 1 Bath $1,500 650 sq ft / 2 Bedroom 2 Bath $2,000 950 sq ft"}

    page = MagicMock()
    page.evaluate = fake_eval
    page.url = "https://www.example.com/"

    ctx = _make_ctx(b"<html><body>different body</body></html>")
    adapter = GenericPlanTextAdapter()
    result = asyncio.run(adapter.extract(page, ctx))

    # No static-body marker since the live path produced bodyText
    assert not any(
        "static-body fallback" in e for e in result.errors
    ), f"static path fired when live path succeeded: {result.errors}"
