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


# ─────────────────────────────────────────────────────────────────────
# Fix D — source_ids surfacing + expanded xlsx columns
#
# Adapters populate ``source_ids={pms_specific_id: value, ...}`` via
# make_unit_dict (SightMap/AppFolio/Spherexx do this today). These are
# the JOIN keys for cross-source diffing (canary vs main vs RealPage).
# Pre-2026-05-20: the v2 schema dropped source_ids entirely, and the
# xlsx export had only 19 columns missing many other v2 fields too.
# Post-fix: source_ids carries through + xlsx surfaces the union.
# ─────────────────────────────────────────────────────────────────────


def test_schema_v2_preserves_source_ids_dict() -> None:
    """v2 schema must carry through ``source_ids`` as a dict (empty
    when adapter didn't populate it)."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit

    unit_with_ids = {
        "unit_number": "201",
        "bedrooms": "1", "bathrooms": "1", "sqft": "750",
        "market_rent_low": 1500,
        "source_ids": {
            "sightmap_unit_id": "12345",
            "sightmap_floor_plan_id": "67890",
        },
    }
    out = _format_v2_unit(unit_with_ids, datetime(2026, 5, 20, 12, 0, 0), "test")
    assert out["source_ids"] == {
        "sightmap_unit_id": "12345",
        "sightmap_floor_plan_id": "67890",
    }


def test_schema_v2_source_ids_empty_when_unset() -> None:
    """Adapters that haven't been wired to populate source_ids → {}.
    Never None (additive, non-breaking for downstream join code that
    iterates ``.items()``)."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit
    unit = {
        "unit_number": "202",
        "bedrooms": "2", "bathrooms": "2", "sqft": "1100",
        "market_rent_low": 2200,
    }
    out = _format_v2_unit(unit, datetime(2026, 5, 20, 12, 0, 0), "test")
    assert out["source_ids"] == {}


def test_stringify_source_ids_dict_renders_sorted_kv() -> None:
    """``{k1: v1, k2: v2}`` → ``"k1=v1; k2=v2"`` (keys sorted for
    deterministic diff/join behavior)."""
    from ma_poc.scripts.email.daily_failures import _stringify_source_ids
    out = _stringify_source_ids({
        "sightmap_unit_id": "12345",
        "sightmap_floor_plan_id": "67890",
    })
    assert out == "sightmap_floor_plan_id=67890; sightmap_unit_id=12345", (
        f"expected sorted-key cell form; got {out!r}"
    )


def test_stringify_source_ids_empty_cases() -> None:
    """Empty / None / non-dict edge cases return empty string."""
    from ma_poc.scripts.email.daily_failures import _stringify_source_ids
    assert _stringify_source_ids(None) == ""
    assert _stringify_source_ids({}) == ""
    assert _stringify_source_ids("") == ""


def test_stringify_source_ids_single_key() -> None:
    """Single-key dict renders cleanly."""
    from ma_poc.scripts.email.daily_failures import _stringify_source_ids
    assert _stringify_source_ids({"appfolio_listing_id": "165"}) == (
        "appfolio_listing_id=165"
    )


def test_scraped_columns_include_source_ids_and_new_columns() -> None:
    """The xlsx column list must surface the new fields. If a future
    refactor drops one, this test fails loudly so the canary output
    doesn't silently lose the join keys again."""
    from ma_poc.scripts.email.daily_failures import _SCRAPED_COLUMNS
    cols = dict(_SCRAPED_COLUMNS)
    # Critical for cross-source joining
    assert cols.get("source_ids") == "Source IDs"
    # Data-quality provenance
    assert cols.get("concession_clean") == "Concession (Cleaned)"
    assert cols.get("concession_quality") == "Concession Quality"
    assert cols.get("inferred_id") == "Inferred ID"
    # v2 fields previously dropped
    assert cols.get("floor_plan_id") == "Floor Plan ID"
    assert cols.get("availability_status") == "Availability Status"
    assert cols.get("move_in_date") == "Move-In Date"
    assert cols.get("rent_range_raw") == "Rent Range (raw)"
    assert cols.get("available_date_raw") == "Available Date (raw)"
    assert cols.get("building") == "Building"
    assert cols.get("deposit") == "Deposit"


def test_scraped_columns_no_duplicates() -> None:
    """Each key/label pair appears once — catches accidental duplicates
    from a future refactor that adds the same column twice."""
    from ma_poc.scripts.email.daily_failures import _SCRAPED_COLUMNS
    keys = [k for k, _ in _SCRAPED_COLUMNS]
    labels = [v for _, v in _SCRAPED_COLUMNS]
    assert len(keys) == len(set(keys)), (
        f"duplicate keys in _SCRAPED_COLUMNS: {[k for k in keys if keys.count(k) > 1]}"
    )
    assert len(labels) == len(set(labels)), (
        f"duplicate labels: {[v for v in labels if labels.count(v) > 1]}"
    )


def test_scraped_columns_canonical_order() -> None:
    """Canonical ID stays first; Source IDs is the LAST column (last in
    the row layout, easy to find on the right edge of the xlsx)."""
    from ma_poc.scripts.email.daily_failures import _SCRAPED_COLUMNS
    keys = [k for k, _ in _SCRAPED_COLUMNS]
    assert keys[0] == "canonical_id", (
        f"Canonical ID must be the first column; got {keys[0]!r}"
    )
    assert keys[-1] == "source_ids", (
        f"Source IDs must be the last column (join keys at right edge); "
        f"got {keys[-1]!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Fix E — banner-header capture: "Limited Time Offer!" alone is a
# header, not an offer. Two halves of the fix:
#
#   1. Scraper (pms/scraper.py): after picking the sentence containing
#      the regex match, walk forward into the next 1-2 sentences while
#      total length < 300 chars. Recovers the body that follows the
#      banner ("Move in by 6/15 and get 1 month free rent.").
#
#   2. Classifier (core/concession_clean.py): the new
#      ``unclean_header_only`` quality flag fires when the captured
#      text is a banner phrase only (no $X / weeks / months / move-in
#      date / percentage). Reports the pre-2026-05-20-fix residual
#      where the body was sentence-dropped at the source.
#
# Real-world signal: feature canary 2026-05-19 cid 74567 (Woodland
# Creek) — 46/46 rows just say "Limited Time Offer!" because the
# upstream sentence-splitter dropped the actionable body.
# ─────────────────────────────────────────────────────────────────────


def test_classify_header_only_limited_time_offer() -> None:
    """Banner header with no body → ``unclean_header_only``."""
    from ma_poc.core.concession_clean import classify_concession_quality
    assert classify_concession_quality("Limited Time Offer!") == (
        "unclean_header_only"
    )
    assert classify_concession_quality("  Limited Time Offer!  ") == (
        "unclean_header_only"
    )
    # Variant phrasings
    assert classify_concession_quality("Move-in Special!") == "unclean_header_only"
    assert classify_concession_quality("Special Offer") == "unclean_header_only"
    assert classify_concession_quality("Don't Miss Out!") == "unclean_header_only"


def test_classify_header_plus_specific_terms_is_clean() -> None:
    """Banner + body → clean. The body terms ($X / weeks / months /
    move-in date) make it actionable."""
    from ma_poc.core.concession_clean import classify_concession_quality
    # The post-2026-05-20-scraper-fix shape
    assert classify_concession_quality(
        "Limited Time Offer! Move in by 6/15 and get 1 month free rent."
    ) == "clean"
    assert classify_concession_quality(
        "Limited Time Offer! Save $250 on select 1-bedroom units."
    ) == "clean"
    assert classify_concession_quality(
        "Move-in Special! 6 weeks free on any new lease."
    ) == "clean"


def test_classify_header_only_does_not_shadow_leak_categories() -> None:
    """The header-only check must run AFTER the leak classifiers — a
    row that's both a banner AND has JS leak should classify as
    ``unclean_script_leak`` (more actionable for upstream debugging)."""
    from ma_poc.core.concession_clean import classify_concession_quality
    assert classify_concession_quality(
        "function() {} Limited Time Offer!"
    ) == "unclean_script_leak"


def test_clean_header_only_returns_normalized_text() -> None:
    """``clean_concession_text`` on a header-only input returns the
    whitespace-normalized banner (nothing to extract; quality flag
    tells reporting to display with caution)."""
    from ma_poc.core.concession_clean import clean_concession_text
    assert clean_concession_text("Limited Time Offer!") == "Limited Time Offer!"
    # Multiple whitespace collapsed
    assert clean_concession_text("  Limited   Time  Offer!  ") == (
        "Limited Time Offer!"
    )


def test_scraper_extends_sentence_to_recover_banner_body() -> None:
    """Real-world: the regex anchors on the banner ("Limited Time
    Offer!"), the body lives in the next sentence ("Move in by 6/15
    and get 1 month free rent."), and the pre-fix code returned ONLY
    the header. The post-fix sentence-extension recovers the body
    while staying under the 300-char cap."""
    # Mirror the post-fix scraper logic in-line so this test pins the
    # actual algorithm — not a coincidental side-effect of the regex.
    flat = (
        "Welcome to Woodland Creek apartments. "
        "Limited Time Offer! "
        "Move in by 6/15 and get 1 month free rent on select 1-bedroom "
        "units. Some terms apply."
    )
    PROPERTY_CONCESSION_RE = re.compile(
        r"\blimited[\s-]time\s+(?:offer|special|savings|deal)\b",
        re.I,
    )
    m = PROPERTY_CONCESSION_RE.search(flat)
    assert m is not None
    s, e = m.span()
    win = flat[max(0, s - 200):e + 200]
    off = s - max(0, s - 200)

    parts = re.split(r"(?<=[.!?|•·])\s+", win)
    idx, acc = -1, 0
    for i, p in enumerate(parts):
        if acc <= off < acc + len(p) + 1:
            idx = i
            break
        acc += len(p) + 1
    assert idx >= 0, "match must fall inside one of the split sentences"

    seg = parts[idx]
    # Pre-fix behavior would stop here at "Limited Time Offer!"
    assert "month free" not in seg.lower(), (
        "control: matched sentence alone should NOT contain the body — "
        "if it does, the regex anchored on the body not the banner and "
        "this test is misconfigured"
    )

    # Post-fix extension
    for nxt in parts[idx + 1:idx + 3]:
        candidate = (seg + " " + nxt).strip()
        if len(candidate) > 300:
            break
        seg = candidate

    assert "Limited Time Offer" in seg
    assert "month free" in seg.lower(), (
        f"banner body must be recovered by sentence-extension; got {seg!r}"
    )
    assert "6/15" in seg
    assert len(seg) <= 300


def test_scraper_source_contains_sentence_extension_logic() -> None:
    """Pin the source contract: scraper.py must walk forward into
    subsequent sentences after picking the matched one. Searches for
    the ``_parts[_idx + 1:`` slice pattern unique to the extension
    loop — if a refactor drops it, this test fails loudly."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    assert "_parts[_idx + 1:" in src, (
        "scraper.py no longer extends the matched sentence forward — "
        "banner headers like 'Limited Time Offer!' will lose their "
        "body again."
    )


def test_dirty_input_with_header_only_yields_non_empty_clean_output() -> None:
    """The preserve-and-flag invariant must hold for header-only too:
    a row that's just 'Limited Time Offer!' returns non-empty
    ``concession_text_clean`` (the banner itself), with quality flag
    ``unclean_header_only``. Never silently discarded."""
    from datetime import datetime

    from ma_poc.core.schema_v2 import _format_v2_unit
    unit = {
        "unit_number": "301",
        "bedrooms": "1", "bathrooms": "1", "sqft": "800",
        "market_rent_low": 1600,
        "concession": "Limited Time Offer!",
    }
    out = _format_v2_unit(unit, datetime(2026, 5, 20, 12, 0, 0), "test")
    assert out["concession_text"] == "Limited Time Offer!"
    assert out["concession_text_clean"] == "Limited Time Offer!"
    assert out["_concession_quality"] == "unclean_header_only"
