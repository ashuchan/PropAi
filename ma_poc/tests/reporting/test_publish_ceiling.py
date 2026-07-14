"""Publish-ceiling verifier tests — the airtight contract for grading a
zero-unit result as a genuine "no data published" (gold-eligible) verdict.

The centerpiece is the MADRID GUARD: a page carrying an explicit
"no apartments available" marker AND real rent tokens must be graded
EXTRACTION_MISS (not gold) — never a publish-ceiling. This is the exact
false-gold the rentcafe audit caught (pid 18158).
"""

from __future__ import annotations

from ma_poc.reporting.publish_ceiling import (
    PublishCeiling,
    assess_publish_ceiling,
)

_RAN_EMPTY = [
    {"tier_key": "generic:api_narrow", "outcome": "ran_empty"},
    {"tier_key": "generic:dom_scan", "outcome": "ran_empty"},
    {"tier_key": "generic:llm_dom_targeted", "outcome": "ran_empty"},
]


def _assess(**over):
    base = dict(
        units=None,
        plan_summaries=None,
        html_signals={"rent_signal_count": 0, "spa_confidence": 0.1},
        tier_trace=_RAN_EMPTY,
        page_html="<html><body>Welcome to our community</body></html>",
    )
    base.update(over)
    return assess_publish_ceiling(**base)


# ── THE MADRID GUARD ────────────────────────────────────────────────────────

def test_madrid_marker_plus_rent_is_extraction_miss_not_gold() -> None:
    """Explicit no-availability marker AND rent tokens → EXTRACTION_MISS.
    The rentcafe audit's false-gold case: madrid had the marker string and
    2 real units at $1,795. Must NOT be graded a publish-ceiling."""
    r = _assess(
        html_signals={"rent_signal_count": 2, "spa_confidence": 0.1},
        page_html=(
            "<html><body>No apartments were found matching your search "
            "request. <div class='rent'>$1,795</div></body></html>"
        ),
    )
    assert r.verdict is PublishCeiling.EXTRACTION_MISS
    assert r.gold_eligible is False
    assert r.evidence["rent_signal_count"] == 2


def test_rent_present_zero_units_never_gold_even_without_marker() -> None:
    r = _assess(html_signals={"rent_signal_count": 5, "spa_confidence": 0.0})
    assert r.verdict is PublishCeiling.EXTRACTION_MISS
    assert r.gold_eligible is False


# ── Positive (gold-eligible) paths ──────────────────────────────────────────

def test_genuine_no_availability_is_confirmed_no_data() -> None:
    """Cascade ran empty, 0 rent tokens, no embed/vocab, explicit marker."""
    r = _assess(
        page_html=(
            "<html><body>Sorry, there are no available units at this time. "
            "Please check back later.</body></html>"
        ),
    )
    assert r.verdict is PublishCeiling.CONFIRMED_NO_DATA
    assert r.gold_eligible is True
    assert r.confidence >= 0.8
    assert r.evidence["no_availability_marker"] is True


def test_plan_summaries_no_units_is_confirmed_plan_only() -> None:
    r = _assess(
        plan_summaries=[{"plan": "A1", "rent_low": 1500}, {"plan": "B2"}],
        page_html="<html><body>Our floor plans start at $1,500/mo.</body></html>",
    )
    assert r.verdict is PublishCeiling.CONFIRMED_PLAN_ONLY
    assert r.gold_eligible is True


# ── Guards that block false-gold ────────────────────────────────────────────

def test_sightmap_embed_zero_units_is_needs_render() -> None:
    r = _assess(
        page_html="<html><body><iframe src='https://sightmap.com/embed/abc'></iframe></body></html>",
    )
    assert r.verdict is PublishCeiling.NEEDS_RENDER
    assert r.gold_eligible is False
    assert r.evidence["unit_bearing_embed"] == "sightmap.com"


def test_unit_vocab_token_zero_units_is_extraction_miss() -> None:
    r = _assess(
        page_html="<html><body><table><tr class='AvailUnitRow'>...</tr></table></body></html>",
    )
    assert r.verdict is PublishCeiling.EXTRACTION_MISS
    assert r.gold_eligible is False


def test_crashed_cascade_is_not_gold() -> None:
    """If a tier errored/timed out we did not truly observe the page."""
    r = _assess(
        tier_trace=[
            {"tier_key": "generic:dom_scan", "outcome": "ran_empty"},
            {"tier_key": "generic:llm", "outcome": "timeout"},
        ],
    )
    assert r.gold_eligible is False
    assert r.verdict in (PublishCeiling.UNCERTAIN, PublishCeiling.NEEDS_RENDER)


def test_spa_shell_unobserved_is_needs_render() -> None:
    r = _assess(
        html_signals={"rent_signal_count": 0, "spa_confidence": 0.8},
        tier_trace=[{"tier_key": "generic:embedded_json", "outcome": "skipped"}],
    )
    assert r.verdict is PublishCeiling.NEEDS_RENDER
    assert r.gold_eligible is False


def test_bare_page_no_marker_is_uncertain_not_gold() -> None:
    """Cascade ran empty, page bare, but NO explicit no-availability signal.
    Weaker than a marker → UNCERTAIN, must not be counted gold."""
    r = _assess(page_html="<html><body><h1>Contact us</h1></body></html>")
    assert r.verdict is PublishCeiling.UNCERTAIN
    assert r.gold_eligible is False


def test_units_present_is_not_a_ceiling_case() -> None:
    r = _assess(units=[{"unit_number": "101", "rent_low": 1500}])
    assert r.verdict is PublishCeiling.UNCERTAIN
    assert r.gold_eligible is False


def test_evidence_bundle_is_populated() -> None:
    r = _assess(
        page_html="<html><body>no availability at this time</body></html>",
    )
    assert set(r.evidence) >= {
        "n_units", "rent_signal_count", "spa_confidence", "tiers_ran_and_empty",
    }
    assert r.evidence["tiers_ran_and_empty"] is True


# ── Runner-integration contract: the EXACT signal shapes jugnu.py passes ─────

def test_accepts_characterize_html_and_log_attempt_shapes() -> None:
    """Guards against silent shape drift between the runner and the verifier.

    html_signals comes verbatim from _characterize_html (scraper.py) and
    tier_trace from the generic adapter's _log_attempt entries. If either
    field name changes upstream, this test breaks before a prod run does."""
    characterize_html_output = {
        "body_bytes": 40000,
        "text_bytes": 12000,
        "script_count": 8,
        "iframe_count": 0,
        "jsonld_block_count": 0,
        "jsonld_types": [],
        "framework_hints": [],
        "spa_confidence": 0.1,
        "rent_signal_count": 0,
    }
    log_attempt_trace = [
        {"tier_key": "generic:api_narrow", "outcome": "ran_empty",
         "units_found": 0, "reason": "no captured API responses", "duration_ms": 3},
        {"tier_key": "generic:dom_scan", "outcome": "ran_empty",
         "units_found": 0, "reason": "no DOM container matched", "duration_ms": 12},
    ]
    r = assess_publish_ceiling(
        units=[],
        plan_summaries=None,
        html_signals=characterize_html_output,
        tier_trace=log_attempt_trace,
        page_html="<html><body>Sorry, there are no available units at this time.</body></html>",
    )
    # cascade observed to run empty + explicit marker + no rent/embed/vocab
    assert r.verdict is PublishCeiling.CONFIRMED_NO_DATA
    assert r.gold_eligible is True
    assert r.evidence["tiers_ran_and_empty"] is True
