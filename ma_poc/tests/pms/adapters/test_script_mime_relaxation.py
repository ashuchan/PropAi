"""Phase 6.5 — broaden the <script type="..."> accept-set.

2026-05-21: a handful of HARs in the actionable_html_extractor bucket
ship unit data inside non-standard ``<script>`` MIME types — Vue.js
SSR uses ``text/x-template``, some CMSes ship raw inventory inside
``application/x-json``. The legacy filter (text/javascript /
application/javascript / empty) silently dropped them. Strategy A
(pure-JSON bodies) was anchored on the exact ``application/json``
MIME, missing the same patterns.

This module pins:
  • ``_SCRIPT_TYPE_ACCEPT`` covers JS-template MIMEs for Strategy B.
  • ``_JSON_SCRIPT_TYPE_ACCEPT`` covers JSON-bodied MIMEs for Strategy A.
  • ``application/ld+json`` is deliberately NOT in either set — JSON-LD
    has its own dedicated extractor and we don't want to double-process
    Schema.org @graph nodes as raw inventory.
"""

from __future__ import annotations

from ma_poc.pms.adapters._html_extract import (
    _JSON_SCRIPT_TYPE_ACCEPT,
    _SCRIPT_TYPE_ACCEPT,
    extract_embedded_blobs_from_html,
)

# ─────────────────────────────────────────────────────────────────────
# Constants — pin the contract
# ─────────────────────────────────────────────────────────────────────


def test_strategy_b_accepts_default_and_legacy_js_mimes() -> None:
    """The legacy accept-set must still be a subset of the new one —
    otherwise pre-existing properties regress."""
    for legacy in ("", "text/javascript", "application/javascript"):
        assert legacy in _SCRIPT_TYPE_ACCEPT, (
            f"{legacy!r} dropped from _SCRIPT_TYPE_ACCEPT — would break "
            "every property currently extracted via Strategy B."
        )


def test_strategy_b_accepts_vue_template_mimes() -> None:
    """Vue.js SSR uses ``text/x-template`` to ship the inventory shell
    inline; some hand-rolled CMSes use ``text/template`` or the bare
    ``x-template`` legacy name."""
    for tpl in ("text/x-template", "x-template", "text/template"):
        assert tpl in _SCRIPT_TYPE_ACCEPT, (
            f"{tpl!r} missing — Vue SSR / template CMSes will fall "
            "back to Tier-4 LLM unnecessarily."
        )


def test_strategy_a_does_not_double_process_jsonld() -> None:
    """``application/ld+json`` is owned by ``extract_jsonld_from_html``
    — putting it in the embedded-JSON accept-set would re-process
    Schema.org @graph nodes as raw inventory."""
    assert "application/ld+json" not in _JSON_SCRIPT_TYPE_ACCEPT, (
        "application/ld+json belongs to extract_jsonld_from_html; "
        "double-processing risks misinterpreting @graph nodes."
    )
    assert "application/ld+json" not in _SCRIPT_TYPE_ACCEPT, (
        "Same as above — Schema.org is not a JS-assignment payload."
    )


def test_strategy_a_accepts_x_json() -> None:
    """``application/x-json`` is the documented mis-MIME for raw
    inventory in several captured HARs."""
    assert "application/x-json" in _JSON_SCRIPT_TYPE_ACCEPT


# ─────────────────────────────────────────────────────────────────────
# End-to-end behavior
# ─────────────────────────────────────────────────────────────────────


# Synthetic inventory blob — generous size to clear both Strategy A's
# 200-char floor AND Strategy B's 300-char floor on the wrapped JS body.
_UNIT_LIST_JSON = (
    '['
    '{"id":1,"name":"Aspen","floorPlanName":"A1","bedrooms":1,'
    '"bathrooms":1,"sqft":720,"rent":1450,"availability":"AVAILABLE"},'
    '{"id":2,"name":"Birch","floorPlanName":"B1","bedrooms":2,'
    '"bathrooms":2,"sqft":980,"rent":1850,"availability":"AVAILABLE"},'
    '{"id":3,"name":"Cedar","floorPlanName":"C1","bedrooms":3,'
    '"bathrooms":2,"sqft":1240,"rent":2250,"availability":"AVAILABLE"}'
    ']'
)
_UNIT_OBJ_JSON = '{"floorPlans":' + _UNIT_LIST_JSON + '}'


def test_extract_picks_up_x_template_body() -> None:
    """A ``<script type="text/x-template">`` with a JS assignment
    containing the unit-keyword should be extracted via Strategy B."""
    html = (
        '<html><body><script type="text/x-template">'
        'var floorPlans = ' + _UNIT_LIST_JSON + ';'
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    assert any(b["url"].startswith("embedded:script-var:") for b in blobs), (
        f"expected at least one script-var blob; got: "
        f"{[b['url'] for b in blobs]}"
    )


def test_extract_picks_up_x_json_body() -> None:
    """A ``<script type="application/x-json">`` with a JSON object
    payload should be extracted via Strategy A."""
    html = (
        '<html><body><script type="application/x-json" id="fp-data">'
        + _UNIT_OBJ_JSON +
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    json_blocks = [b for b in blobs if b["url"].startswith("embedded:json-block:")]
    assert json_blocks, (
        f"expected at least one json-block blob; got: "
        f"{[b['url'] for b in blobs]}"
    )
    # Confirm the id propagates so the synthetic URL is debuggable
    assert any(b["url"] == "embedded:json-block:fp-data" for b in json_blocks)


def test_extract_rejects_unknown_mime() -> None:
    """An unrelated MIME (e.g. ``text/html``) must still be filtered out
    — broadening doesn't mean accepting everything."""
    html = (
        '<html><body><script type="text/html">'
        + _UNIT_OBJ_JSON +
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    assert blobs == [], (
        f"text/html scripts must not be processed; got: "
        f"{[b['url'] for b in blobs]}"
    )


def test_extract_still_finds_legacy_application_json_block() -> None:
    """The pre-Phase-6.5 Strategy A path (the literal
    ``application/json`` MIME, often Next.js ``__NEXT_DATA__``) must
    still work. Smoke test against a synthetic Next-like envelope."""
    html = (
        '<html><body><script type="application/json" id="__NEXT_DATA__">'
        + _UNIT_OBJ_JSON +
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    assert any(
        b["url"] == "embedded:json-block:__NEXT_DATA__" for b in blobs
    ), f"Next.js block lost from Strategy A; got: {[b['url'] for b in blobs]}"
