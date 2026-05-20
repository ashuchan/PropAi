"""End-to-end pipeline tests for the 2026-05-20 concession-quality fixes.

Three fixes, one test file:

  * **Fix A — scraper.py**: strip ``<script>``/``<style>`` BODIES before
    the concession-window regex, so adjacent JS/CSS doesn't leak into
    the captured ``concessions_text``. Tested against the Woodland
    Creek pattern (real bytes from feature canary 2026-05-19).

  * **Fix B — schema_v2.py**: emit ``concession_text_clean`` +
    ``_concession_quality`` alongside the raw ``concession_text``.
    Per user constraint: "error on side of unclean rather than discard"
    — preserve raw AND surface a display-ready variant.

  * **Fix C — daily_failures.py xlsx export**: read the v2 field
    ``concession_text`` (with ``concession_text_clean`` priority) — the
    legacy key ``concessions`` is empty on v2 input, which produced a
    100% empty Concessions column in main 2026-05-20 xlsx.
"""

from __future__ import annotations

import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Fix A — scraper.py script/style strip
# ─────────────────────────────────────────────────────────────────────


_SCRAPER_PATH = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"


def test_scraper_strips_script_and_style_bodies_before_concession_capture() -> None:
    """Pin the source contract: the concession-window builder must strip
    ``<script>``/``<style>`` BODIES (not just tags) before flattening.
    The legacy ``re.sub(r"<[^>]+>", " ", page_html)`` alone is
    insufficient — it removes opening/closing tags but keeps the JS/CSS
    code inside them, which leaks into the concession capture window."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    # The fix introduces a script/style/noscript-block strip before the
    # tag-strip. Search for that specific pattern; if a future refactor
    # drops it, this test fails loudly.
    assert "<(script|style|noscript)" in src, (
        "scraper.py no longer strips <script>/<style> BODIES before the "
        "concession-window regex. The Woodland Creek pattern will start "
        "leaking JS code into concession_text again."
    )


def test_scraper_concession_capture_on_woodland_creek_shape() -> None:
    """Synthesized Woodland Creek shape: a <script> block containing a
    PropLeadSource JS function, immediately followed by a 'Limited Time
    Offer!' button. The post-fix flat text contains the offer text
    cleanly with no JS leak."""
    page_html = """<html><body>
<script>
  if (href.indexOf("PropLeadSource") === -1) {
    if (href.indexOf("?") == -1) { href = href + "?"; }
    else { href = href + "&"; }
    href = href + 'PropLeadSource_' + 2335253 + "=" + propleadsource;
    el.setAttribute('href', href);
  } }); });
</script>
<style>
  .nudgestrip { display: none !important; }
</style>
<div class="nudgestrip">
  <button type="button" class="btn-primary">Limited Time Offer!</button>
  Move in by 6/15 and get 1 month free rent on select 1-bedroom units!
</div>
</body></html>"""

    # Mirror the production flow (Fix A): strip code blocks first.
    no_code = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", no_code))

    # Capture window same as scraper.py
    PROPERTY_CONCESSION_RE = re.compile(
        r"\b(?:limited\s+time|free\s+rent|weeks?\s+free|months?\s+free|"
        r"move[- ]?in\s+special|\$?\d+\s*(?:off|free))",
        re.I,
    )
    m = PROPERTY_CONCESSION_RE.search(flat)
    assert m is not None, "expected concession-pattern match"
    start, end = m.span()
    win = flat[max(0, start - 200):end + 200].strip()[:300]

    # The leak markers MUST be absent from the captured window.
    assert "PropLeadSource" not in win, f"JS leak still present: {win[:200]!r}"
    assert "href.indexOf" not in win
    assert "setAttribute" not in win
    assert "!important" not in win, f"CSS leak still present: {win[:200]!r}"
    # The real offer text MUST be present.
    assert "Limited Time Offer" in win
    assert "month free" in win.lower()


def test_scraper_pre_fix_behavior_would_have_leaked_js() -> None:
    """Regression evidence — confirm that the OLD code (without the
    script/style strip) DID leak JS into the capture window. This is
    a fixed reproduction of the bug; if it ever starts producing
    clean output without the script-strip pass, the fix is no longer
    necessary (unlikely)."""
    page_html = (
        "<script>function() { x.setAttribute('href', 'a'); }</script>"
        "<div>Limited Time Offer! 1 month free rent</div>"
    )
    # Simulate the BUGGY behavior (no script-body strip)
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page_html))

    PROPERTY_CONCESSION_RE = re.compile(
        r"\blimited\s+time\b", re.I,
    )
    m = PROPERTY_CONCESSION_RE.search(flat)
    assert m is not None
    win = flat[max(0, m.start() - 200):m.end() + 200].strip()
    # OLD behavior: leak IS in the window
    assert "setAttribute" in win, (
        "expected the buggy path to leak JS — if it doesn't, the bug "
        "may have been fixed elsewhere and the script-strip pass is no "
        "longer needed (verify before removing)"
    )


# ─────────────────────────────────────────────────────────────────────
# Fix B — schema_v2.py preserve-and-flag wiring
# ─────────────────────────────────────────────────────────────────────


def test_schema_v2_emits_concession_clean_and_quality_fields() -> None:
    """v2 schema must surface the three concession fields together:
    ``concession_text`` (raw, preserved), ``concession_text_clean``
    (best-effort cleaned), ``_concession_quality`` (label).
    Per user constraint: preserve + flag, never discard."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit

    # Unit dict with a dirty concession (Woodland Creek pattern)
    dirty = (
        "}); }); Limited Time Offer! Move in by 6/15 and get "
        "1 month free rent on select units"
    )
    unit = {
        "unit_number": "201",
        "floor_plan_name": "1BR",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "750",
        "market_rent_low": 1500,
        "market_rent_high": 1500,
        "concession": dirty,
    }
    out = _format_v2_unit(unit, datetime(2026, 5, 20, 12, 0, 0), "test-prop")
    # Raw preserved
    assert out["concession_text"] == dirty, (
        f"raw concession_text must be preserved unmodified; got {out['concession_text']!r}"
    )
    # Clean variant emitted, non-empty, doesn't contain leak
    clean = out["concession_text_clean"]
    assert clean and isinstance(clean, str)
    assert "Limited Time Offer" in clean or "month free" in clean.lower()
    assert "});" not in clean, f"clean variant should strip leak; got {clean!r}"
    # Quality flag is one of the expected labels
    assert out["_concession_quality"] in (
        "unclean_orphan_prefix",
        "unclean_script_leak",
        "unclean_style_leak",
        "unclean_dmapi",
    ), f"expected dirty quality flag; got {out['_concession_quality']!r}"


def test_schema_v2_clean_concession_passes_through_unchanged() -> None:
    """Clean input: ``concession_text == concession_text_clean``,
    quality == 'clean'."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit

    clean = "Move in by May 30th and receive 6 weeks free on any new lease."
    unit = {
        "unit_number": "202",
        "floor_plan_name": "2BR",
        "bedrooms": "2",
        "bathrooms": "2",
        "sqft": "1100",
        "market_rent_low": 2200,
        "market_rent_high": 2200,
        "concession": clean,
    }
    out = _format_v2_unit(unit, datetime(2026, 5, 20, 12, 0, 0), "test-prop")
    assert out["concession_text"] == clean
    assert out["concession_text_clean"] == clean
    assert out["_concession_quality"] == "clean"


def test_schema_v2_empty_concession_keeps_all_three_none() -> None:
    """No concession data → all three fields None (no spurious empty
    strings or False-y flags)."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit

    unit = {
        "unit_number": "203",
        "bedrooms": "1",
        "bathrooms": "1",
        "sqft": "750",
        "market_rent_low": 1500,
    }
    out = _format_v2_unit(unit, datetime(2026, 5, 20, 12, 0, 0), "test-prop")
    assert out["concession_text"] is None
    assert out["concession_text_clean"] is None
    assert out["_concession_quality"] is None


# ─────────────────────────────────────────────────────────────────────
# Fix C — daily_failures.py xlsx export reads the v2 field
# ─────────────────────────────────────────────────────────────────────


_DAILY_FAILURES_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "email" / "daily_failures.py"
)


def test_xlsx_export_reads_v2_concession_field() -> None:
    """Pin the export-wiring fix: the xlsx export must read
    ``concession_text_clean`` (preferred) or ``concession_text`` (raw
    fallback) — NOT just the legacy ``concessions`` key.

    Pre-fix: ``u.get("concessions")`` always returned None on v2 input
    → 100% empty Concessions column (0 / 69,992 rows on main 2026-05-20).
    Post-fix: the cleaned text populates the column."""
    src = _DAILY_FAILURES_PATH.read_text(encoding="utf-8")
    # The new export pattern: prefer clean → raw → legacy.
    # All three keys must appear in a single chain.
    pattern = re.compile(
        r"concession_text_clean.*?concession_text.*?concessions",
        re.DOTALL,
    )
    matches = pattern.findall(src)
    # Both call sites (success-row and failed-row paths) should use the chain.
    assert len(matches) >= 2, (
        f"expected at least 2 call sites in daily_failures.py reading the "
        f"new concession field chain; got {len(matches)}. The export-wiring "
        f"fix may have been reverted."
    )


def test_stringify_concessions_handles_v2_clean_field() -> None:
    """The helper takes the cleaned text and renders it unchanged."""
    from ma_poc.scripts.email.daily_failures import _stringify_concessions
    assert _stringify_concessions("6 weeks free on select units") == (
        "6 weeks free on select units"
    )
    assert _stringify_concessions(None) == ""
    assert _stringify_concessions("") == ""
