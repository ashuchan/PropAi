"""Tests for HTML-fragment XHR promotion in the generic adapter.

Some platforms (Jonah Digital, legacy ASP.NET, some headless WordPress)
deliver unit data via XHRs that return HTML fragments rather than JSON.
The L1 fetcher captures those bodies in network_log, but the JSON unit
gates (find_unit_list / has_unit_signals) reject them upstream because
the body is a string starting with ``<`` instead of a dict/list.

These tests cover the promotion helper that lets such bodies reach the
HTML-stage extractors (dom_scan / jsonld / embedded_json) alongside the
main page HTML, plus the end-to-end behaviour through GenericAdapter.extract.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.adapters.generic import (
    GenericAdapter,
    _CHUNK_BODY_CAP_BYTES,
    _MAX_CHUNK_BODIES,
    _collect_html_chunk_bodies,
)
from ma_poc.pms.detector import detect_pms


# ── Unit tests for the helper ────────────────────────────────────────────


def test_promotes_html_fragment_xhr_body() -> None:
    """A network_log entry with a chunk URL and an HTML body is promoted."""
    responses = [
        {
            "url": "https://example.com/floorplans/_fp-renderable/chunks",
            "body": '<div class="fp-card"><span>$1,500</span><span>750 sqft</span></div>',
            "status": 200,
            "content_type": "text/html",
        }
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert len(out) == 1
    url, body = out[0]
    assert "floorplans" in url
    assert "$1,500" in body and "750 sqft" in body


def test_skips_json_body() -> None:
    """A dict body (already parsed JSON) is not promoted — that's the API tier's job."""
    responses = [
        {
            "url": "https://example.com/api/units",
            "body": {"units": [{"rent": 1500, "sqft": 750}]},
            "content_type": "application/json",
        }
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


def test_skips_navigation_response() -> None:
    """The main page navigation response is also captured by ``page.on``;
    skip it so we don't double-count the HTML we already pull via
    ``_get_page_html`` / ``fetch_result.body``.
    """
    responses = [
        {
            "url": "https://example.com/",
            "body": "<html><body>main page</body></html>",
            "content_type": "text/html",
        }
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


def test_skips_body_that_parses_as_unit_list() -> None:
    """Belt-and-braces: if the body somehow parses through find_unit_list
    (e.g. an upstream change starts feeding JSON strings here), skip it so
    we don't double-feed the same data to the API and HTML tiers.

    Strings short-circuit inside find_unit_list today, so we approximate the
    contract by passing a list-of-dicts body that find_unit_list accepts.
    """
    responses = [
        {
            "url": "https://example.com/api/items",
            "body": [{"unit_id": "101", "rent": 1500, "sqft": 750}],
        }
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


def test_skips_empty_and_non_string_bodies() -> None:
    """Bodies that are None, empty, or whitespace-only are never promoted."""
    responses = [
        {"url": "https://example.com/a", "body": None},
        {"url": "https://example.com/b", "body": ""},
        {"url": "https://example.com/c", "body": "   \n\t  "},
        {"url": "https://example.com/d", "body": 12345},  # weird upstream
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


def test_skips_non_html_strings() -> None:
    """A string body that doesn't start with ``<`` (CSV, plain text, JSON
    string) is not an HTML fragment and should be skipped.
    """
    responses = [
        {"url": "https://example.com/data.csv", "body": "id,rent\n101,1500\n"},
        {"url": "https://example.com/text", "body": "this is plain text content"},
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


def test_caps_per_chunk_body_size() -> None:
    """Per-chunk bodies are capped at ``_CHUNK_BODY_CAP_BYTES``."""
    big_body = "<div>" + ("x" * (_CHUNK_BODY_CAP_BYTES + 50_000)) + "</div>"
    responses = [
        {"url": "https://example.com/big", "body": big_body, "content_type": "text/html"}
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert len(out) == 1
    _, body = out[0]
    assert len(body) == _CHUNK_BODY_CAP_BYTES


def test_caps_total_chunk_count() -> None:
    """No more than ``_MAX_CHUNK_BODIES`` fragments are promoted per property."""
    responses = [
        {
            "url": f"https://example.com/chunk/{i}",
            "body": f"<div>chunk {i}</div>",
            "content_type": "text/html",
        }
        for i in range(_MAX_CHUNK_BODIES + 5)
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert len(out) == _MAX_CHUNK_BODIES


def test_handles_empty_api_responses() -> None:
    """An empty input returns an empty list — no crash on edge cases."""
    assert _collect_html_chunk_bodies([], base_url="https://example.com/") == []
    assert _collect_html_chunk_bodies([], base_url=None) == []
    assert _collect_html_chunk_bodies([], base_url="") == []


def test_handles_missing_url() -> None:
    """Entries with no URL are skipped (can't dedupe against base_url)."""
    responses = [
        {"body": "<div>orphan</div>", "content_type": "text/html"},
        {"url": "", "body": "<div>empty-url</div>"},
    ]
    out = _collect_html_chunk_bodies(responses, base_url="https://example.com/")
    assert out == []


# ── Integration test through GenericAdapter.extract ─────────────────────


class _DummyPage:
    """Placeholder; GenericAdapter only calls page.content() when present.

    Setting page=None forces fall-back to ctx.fetch_result.body for the
    main page HTML; here we set neither, so ``_get_page_html`` returns
    None and the chunk-promoted body becomes the entire HTML pool.
    """

    pass


@pytest.mark.asyncio
async def test_extract_promotes_chunks_into_dom_scan_pool() -> None:
    """End-to-end: API tiers find no JSON units, but an HTML-fragment XHR
    body carrying rent + sqft signals is promoted into the HTML pool so
    dom_scan can pick them up.

    This is the Skyline-at-Kessler / Jonah Digital case: the
    ``/_fp-renderable/listing-chunks`` XHR returns HTML cards with rent
    inside, but ``find_unit_list`` short-circuits on strings.
    """
    chunk_html = """
    <div class="floorplan-card">
      <h3 class="plan-name">Studio</h3>
      <span class="rent">$1,450</span>
      <span class="sqft">377 sq ft</span>
      <span class="beds">Studio</span>
      <span class="baths">1 bath</span>
    </div>
    <div class="floorplan-card">
      <h3 class="plan-name">One Bedroom</h3>
      <span class="rent">$1,795</span>
      <span class="sqft">689 sq ft</span>
      <span class="beds">1 bed</span>
      <span class="baths">1 bath</span>
    </div>
    """
    responses = [
        {
            "url": "https://example.com/floorplans/_fp-renderable/chunks",
            "body": chunk_html,
            "content_type": "text/html",
            "status": 200,
        }
    ]

    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST-CHUNK",
    )
    ctx._api_responses = responses  # type: ignore[attr-defined]

    adapter = GenericAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    assert isinstance(result, AdapterResult)
    attempts = getattr(result, "_tier_attempts", []) or []
    promoted_attempts = [a for a in attempts if a.get("tier_key") == "generic:html_chunks_promoted"]
    assert promoted_attempts, (
        f"expected 'generic:html_chunks_promoted' attempt; got: {[a.get('tier_key') for a in attempts]}"
    )
    assert promoted_attempts[0]["outcome"] == "ran_promoted"
    assert "1" in str(promoted_attempts[0].get("reason", ""))


@pytest.mark.asyncio
async def test_extract_does_not_promote_when_api_tier_already_extracted() -> None:
    """If JSON API responses produce units, the chunk-promotion log line
    should not appear because there are no chunk bodies to promote. This
    guards against silent telemetry noise on the happy path.
    """
    responses = [
        {
            "url": "https://example.com/api/units",
            "body": {"units": [
                {"unit_id": "101", "rent": 1500, "sqft": 750, "beds": 1, "baths": 1},
                {"unit_id": "102", "rent": 1750, "sqft": 900, "beds": 2, "baths": 2},
            ]},
            "content_type": "application/json",
        }
    ]
    ctx = AdapterContext(
        base_url="https://example.com/",
        detected=detect_pms("https://example.com/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST-NO-CHUNK",
    )
    ctx._api_responses = responses  # type: ignore[attr-defined]

    adapter = GenericAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    attempts = getattr(result, "_tier_attempts", []) or []
    promoted = [a for a in attempts if a.get("tier_key") == "generic:html_chunks_promoted"]
    assert promoted == [], (
        f"chunk promotion should not log when there are no HTML chunks; got: {promoted}"
    )
