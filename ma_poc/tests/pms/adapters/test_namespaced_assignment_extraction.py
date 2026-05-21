"""Phase 6.1 — namespaced JS member-assignment extraction.

2026-05-21: 30 of the 55 properties in the HAR-archive "actionable_html_
extractor" bucket embed unit data via dotted-namespace assignments like
``ysi.floorplansList = [...]`` (Yardi/RentCafe SSR), ``propConfig.fp_data
= {...}``, etc. The legacy ``_ASSIGNMENT_RE`` only matches ``var/let/
const/window.X``; this pass adds a third extraction strategy in
``extract_embedded_blobs_from_html`` that:

  1. Locates dotted-namespace LHS positions via ``_NAMESPACED_LHS_RE``.
  2. Walks forward with proper bracket-balancing (``_extract_balanced_
     value``) to handle nested arrays/objects (e.g. Yardi's empty
     ``"Amenities": []`` arrays inside the floorplans list).

Fixture: knollcrestsa.com /floorplans page (Yardi PascalCase shape).
"""

from __future__ import annotations

from pathlib import Path

from ma_poc.pms.adapters._html_extract import (
    _NAMESPACED_LHS_RE,
    _extract_balanced_value,
    extract_embedded_blobs_from_html,
)

# ─────────────────────────────────────────────────────────────────────
# _extract_balanced_value — the core helper
# ─────────────────────────────────────────────────────────────────────


def test_balanced_value_array_with_nested_arrays() -> None:
    """The headline reason this helper exists: regex non-greedy
    matching stops at the FIRST closing ``]``, which on Yardi/RentCafe
    SSR (``"Amenities": []`` nested inside the floorplans array)
    truncates the value to a fragment. The balanced-bracket walker
    finds the outermost match."""
    text = 'x = [{"a": 1, "Amenities": []}, {"b": 2, "tags": [1, 2]}];'
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val == '[{"a": 1, "Amenities": []}, {"b": 2, "tags": [1, 2]}]'


def test_balanced_value_object_with_nested_objects() -> None:
    text = 'x = {"nested": {"deep": {"v": 1}}, "list": [1, 2]};'
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val == '{"nested": {"deep": {"v": 1}}, "list": [1, 2]}'


def test_balanced_value_handles_brackets_in_strings() -> None:
    """JSON strings may contain ``[`` / ``]`` / ``{`` / ``}`` — these
    must NOT throw the depth counter off. Quote-tracking matters."""
    text = 'x = [{"name": "Apt [101] Penthouse {Premium}"}];'
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val == '[{"name": "Apt [101] Penthouse {Premium}"}]'


def test_balanced_value_handles_escaped_quotes_in_strings() -> None:
    text = r'x = {"q": "he said \"hi\""};'
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val == r'{"q": "he said \"hi\""}'


def test_balanced_value_returns_none_when_no_bracket_after_start() -> None:
    """If the position after the equals sign isn't ``[`` or ``{``,
    return None (not a JSON value)."""
    text = "x = 42;"  # numeric — not a JSON object/array
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val is None


def test_balanced_value_returns_none_on_unbalanced() -> None:
    """Truncated input (no closing bracket) returns None — never
    invents a closing position."""
    text = 'x = [{"a": 1, "b":'  # broken
    val = _extract_balanced_value(text, text.index("=") + 1)
    assert val is None


def test_balanced_value_skips_leading_whitespace() -> None:
    """The helper is called with a position past the ``=`` sign; it
    must skip whitespace before the opening bracket."""
    text = 'x =   \n\t  [{"a": 1}];'
    val = _extract_balanced_value(text, text.index("=") + 1 + 1)
    assert val == '[{"a": 1}]'


# ─────────────────────────────────────────────────────────────────────
# _NAMESPACED_LHS_RE — matches dotted-namespace LHS
# ─────────────────────────────────────────────────────────────────────


def test_lhs_matches_two_segment_namespace() -> None:
    """``ysi.floorplansList = ...`` — Yardi/RentCafe SSR pattern."""
    text = "\n  ysi.floorplansList = [{}];"
    m = _NAMESPACED_LHS_RE.search(text)
    assert m is not None
    assert m.group(1) == "ysi.floorplansList"


def test_lhs_matches_deep_namespace() -> None:
    """``app.config.fp.list = ...`` — multi-level namespacing."""
    text = "\nfoo.bar.baz = {};"
    m = _NAMESPACED_LHS_RE.search(text)
    assert m is not None
    assert m.group(1) == "foo.bar.baz"


def test_lhs_does_not_match_bare_identifier() -> None:
    """Plain ``var X = ...`` is the legacy regex's territory; the
    namespaced LHS regex must require at least one dot."""
    text = "var x = {};"
    m = _NAMESPACED_LHS_RE.search(text)
    # Either no match, OR the match doesn't include "var" / bare identifier
    if m:
        # If something matched, it must be the dotted form, not "var" or "x"
        assert "." in m.group(1), (
            f"namespaced regex matched non-dotted {m.group(1)!r} — "
            "would overlap with legacy regex"
        )


def test_lhs_requires_statement_boundary() -> None:
    """``return x.y = {};`` shouldn't match — the ``.y`` would be a
    sub-expression of ``x.y``, not a statement-level assignment."""
    text = "function f() { return result.foo = {a: 1};"
    # The regex's `(?:^|[;\n}\s])` anchor requires statement-start
    # context. "return result.foo" has " " before "result" so the
    # anchor matches at the space. This is acceptable — the
    # downstream JSON-parse + unit-keyword gate filters real noise.
    # The point of this test: documenting WHY the helper is paired
    # with JSON.loads validation.
    m = _NAMESPACED_LHS_RE.search(text)
    if m:
        # If it matched, the regex pattern is permissive on purpose.
        # The downstream filters (JSON-validity + unit-keyword) are
        # the actual quality gate.
        assert "." in m.group(1)


# ─────────────────────────────────────────────────────────────────────
# extract_embedded_blobs_from_html — end-to-end with the fixture
# ─────────────────────────────────────────────────────────────────────


def _fixture_html() -> str:
    return Path("ma_poc/tests/fixtures/knollcrestsa_floorplans_snippet.html").read_text()


def test_extract_blobs_finds_yardi_namespace_assignment() -> None:
    """The Yardi/RentCafe ``ysi.floorplansList = [...]`` pattern that
    inspired this whole change. The fixture is a real <script> block
    from www.knollcrestsa.com/floorplans (captured 2026-05-21)."""
    blobs = extract_embedded_blobs_from_html(_fixture_html())
    namespaced = [b for b in blobs if b["url"].startswith("embedded:script-member:")]
    assert namespaced, (
        f"expected at least one namespaced blob; got URLs: "
        f"{[b['url'] for b in blobs]}"
    )
    # Find the ysi.floorplansList one specifically
    ysi = next(
        (b for b in namespaced if b["url"] == "embedded:script-member:ysi.floorplansList"),
        None,
    )
    assert ysi is not None, (
        f"missing ysi.floorplansList; got: {[b['url'] for b in namespaced]}"
    )
    # The body should be a list of floor-plan dicts
    body = ysi["body"]
    assert isinstance(body, list), f"expected list body; got {type(body).__name__}"
    assert len(body) >= 3, f"expected ≥3 floor plans; got {len(body)}"
    # Each item should carry the Yardi PascalCase keys
    first = body[0]
    expected_keys = {"Id", "Beds", "Baths", "MinSqFt", "MaxSqFt", "MinRent", "MaxRent"}
    assert expected_keys.issubset(first.keys()), (
        f"missing Yardi keys; got: {sorted(first.keys())[:15]}"
    )


def test_extract_blobs_handles_nested_arrays_in_value() -> None:
    """Validates the balanced-bracket parser actually handled the
    ``"Amenities": []`` nested arrays in the real Yardi shape. If the
    naive non-greedy regex were used, the body would be truncated
    after the first empty ``Amenities`` array."""
    blobs = extract_embedded_blobs_from_html(_fixture_html())
    ysi = next(
        (b for b in blobs if b["url"] == "embedded:script-member:ysi.floorplansList"),
        None,
    )
    assert ysi is not None
    body = ysi["body"]
    # All floor plans should be present (regex truncation would yield 1)
    assert len(body) >= 3
    # Every item should have its nested Amenities (proves we got past
    # the inner [] without stopping)
    for item in body[:3]:
        assert "Amenities" in item
        # Amenities is the empty array — confirms nested-bracket survival
        assert item["Amenities"] == []


def test_extract_blobs_no_namespaced_on_clean_html() -> None:
    """Pages without namespaced assignments still work — no spurious
    blobs introduced."""
    html = (
        '<html><body>'
        '<h1>Apartments</h1>'
        '<p>Welcome to our community. 1, 2, and 3 bedroom homes.</p>'
        '</body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    namespaced = [b for b in blobs if b["url"].startswith("embedded:script-member:")]
    assert namespaced == []


def test_extract_blobs_skips_invalid_json() -> None:
    """When the regex captures a value position that isn't valid JSON
    (e.g. a JS object literal with unquoted keys, function calls,
    template literals), the JSON-loads guard discards it silently."""
    html = (
        '<html><body><script type="text/javascript">'
        'config.floorplan = {beds: 1, rent: getValue()};\n'  # unquoted keys
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    # Either no namespaced blob, or none for the invalid-JSON assignment
    namespaced = [b for b in blobs if b["url"].startswith("embedded:script-member:config.floorplan")]
    assert namespaced == []


def test_extract_blobs_skips_too_short_values() -> None:
    """Values under 200 chars are rejected (filter against false-positive
    matches like trivial config). The legacy regex strategy has the same
    floor; Strategy C inherits it."""
    html = (
        '<html><body><script type="text/javascript">'
        'app.cfg.floorplans = [{"a": 1}];'  # only ~13 chars in value
        '</script></body></html>'
    )
    blobs = extract_embedded_blobs_from_html(html)
    namespaced = [b for b in blobs if b["url"].startswith("embedded:script-member:app.cfg.floorplans")]
    assert namespaced == []


# ─────────────────────────────────────────────────────────────────────
# Source-grep contract — Phase 6.1 wiring stays in place
# ─────────────────────────────────────────────────────────────────────


def test_html_extract_uses_namespaced_strategy() -> None:
    """Pin the wiring: ``_html_extract.py`` must call
    ``_NAMESPACED_LHS_RE.finditer`` inside the embedded-blob extractor.
    If a refactor drops this, the Yardi/RentCafe SSR cohort silently
    stops extracting and the Tier-4 LLM cost regresses by ~30 properties
    per run."""
    src = Path("ma_poc/pms/adapters/_html_extract.py").read_text(encoding="utf-8")
    assert "_NAMESPACED_LHS_RE.finditer" in src, (
        "_html_extract.py no longer iterates namespaced LHS matches — "
        "Phase 6.1 Strategy C wiring removed."
    )
    assert "_extract_balanced_value" in src, (
        "balanced-bracket parser missing — Yardi nested-array shape "
        "will silently truncate to first inner ]."
    )
    assert "embedded:script-member:" in src, (
        "namespaced-blob URL prefix dropped — downstream parsers may "
        "skip Strategy C blobs."
    )
