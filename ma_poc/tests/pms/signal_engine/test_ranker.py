"""Phase 2 ranker tests — spec §9, cases R1–R4.

Validates SourceRanker scoring behaviour using create_default_ranker().
"""

from __future__ import annotations

import pytest

from ma_poc.pms.signal_engine.defaults import (
    DEFAULT_KIND_BASE_SCORES,
    LLM_HINT_SCORE,
    PMS_PRIOR_SCORE,
    create_default_ranker,
)
from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal
from ma_poc.pms.signal_engine.ranker import RankedSignal


@pytest.fixture(scope="module")
def ranker():
    return create_default_ranker()


# ── R1: LLM_HINT base score ────────────────────────────────────────────────────

def test_r1_llm_hint_score(ranker) -> None:
    sig = SourceSignal(kind=SourceKind.LLM_HINT, url="https://example.com/floorplans")
    result = ranker.rank([sig])
    assert len(result) == 1
    assert result[0].composite_score == LLM_HINT_SCORE
    assert result[0].composite_score == 10_000


# ── R2: API_RESPONSE known endpoint boost ─────────────────────────────────────

def test_r2_api_known_endpoint_boosted(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.example.com/units",
        is_known_endpoint=True,
    )
    result = ranker.rank([sig])
    assert len(result) == 1
    # base=9_500 + 500 boost (API sources ranked above navigation hints)
    assert result[0].composite_score == 10_000


def test_r2b_api_unknown_endpoint_no_boost(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.API_RESPONSE,
        url="https://api.example.com/units",
        is_known_endpoint=False,
    )
    result = ranker.rank([sig])
    assert result[0].composite_score == 9_500


# ── R3: INTERNAL_LINK with anchor keyword scoring ─────────────────────────────

def test_r3_internal_link_anchor_floor_plans(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/some-page",
        anchor_text="floor plans",
    )
    result = ranker.rank([sig])
    assert len(result) == 1
    # base=4000 + keyword bonus. "floor plan" (90) matches "floor plans";
    # rapidfuzz partial scoring can add a small boost. Assert range rather
    # than exact value so the test stays stable across scorer tuning.
    assert 4_000 + 80 <= result[0].composite_score <= 4_000 + 150


def test_r3b_internal_link_with_path_keyword(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/availability",
        anchor_text="",
    )
    result = ranker.rank([sig])
    # /availability path keyword = 95
    assert result[0].composite_score == 4_000 + 95


def test_r3c_internal_link_no_keywords_zero_bonus(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/about",
        anchor_text="about us",
    )
    result = ranker.rank([sig])
    # No matching keywords → no bonus
    assert result[0].composite_score == 4_000


# ── R4: sorted descending ─────────────────────────────────────────────────────

def test_r4_three_mixed_signals_sorted_descending(ranker) -> None:
    signals = [
        SourceSignal(
            kind=SourceKind.INTERNAL_LINK,
            url="https://example.com/about",
        ),
        SourceSignal(kind=SourceKind.LLM_HINT, url="https://example.com/fp"),
        SourceSignal(
            kind=SourceKind.PROFILE_WINNING,
            url="https://example.com/winning",
            profile_score_override=10_001,
        ),
    ]
    result = ranker.rank(signals)
    scores = [r.composite_score for r in result]
    assert scores == sorted(scores, reverse=True), "must be sorted best-first"
    assert result[0].signal.kind == SourceKind.PROFILE_WINNING
    assert result[1].signal.kind == SourceKind.LLM_HINT
    assert result[2].signal.kind == SourceKind.INTERNAL_LINK


# ── Profile score override ─────────────────────────────────────────────────────

def test_profile_winning_override_beats_all(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.PROFILE_WINNING,
        url="https://example.com/winning",
        profile_score_override=10_001,
    )
    result = ranker.rank([sig])
    assert result[0].composite_score == 10_001
    assert result[0].reason.startswith("profile_override:")


# ── Portal host keyword scoring ───────────────────────────────────────────────

def test_rentcafe_host_keyword_boosts_external_portal(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.EXTERNAL_PORTAL,
        url="https://www.rentcafe.com/apartments/property/floorplans/",
        anchor_text="floor plans",
    )
    result = ranker.rank([sig])
    # base for EXTERNAL_PORTAL = 10_000, host keyword ".rentcafe.com" = 120
    assert result[0].composite_score == 10_000 + 120


# ── DEFAULT_KIND_BASE_SCORES completeness ─────────────────────────────────────

def test_all_source_kinds_have_base_score() -> None:
    for kind in SourceKind:
        assert kind in DEFAULT_KIND_BASE_SCORES, f"Missing base score for {kind}"


# ── Constants imported by scraper.py are correct values ───────────────────────

def test_scraperpy_constant_imports_correct_values() -> None:
    from ma_poc.pms.scraper import (
        _LLM_HINT_SCORE,
        _EMBEDDED_PORTAL_SCORE,
        _PMS_PRIOR_SCORE,
    )
    assert _LLM_HINT_SCORE == 10_000
    assert _EMBEDDED_PORTAL_SCORE == 10_000
    assert _PMS_PRIOR_SCORE == 5_000


def test_scraperpy_pms_priors_imported() -> None:
    from ma_poc.pms.scraper import _PMS_SUB_PATH_PRIORS, _UNIVERSAL_SUB_PATH_PRIORS
    assert "rentcafe" in _PMS_SUB_PATH_PRIORS
    assert "/floorplans" in _UNIVERSAL_SUB_PATH_PRIORS


# ── Regression: profile:availability_link preserves LLM_HINT_SCORE ────────────
# Phase 2 regression: _anchor_to_kind maps profile:availability_link →
# PROFILE_NAV_HINT (base 9_000), dropping score from 10_000 to 9_000.
# The ranker delegation must pass profile_score_override=s so the scraper's
# is_llm_hint check (score == _LLM_HINT_SCORE) stays True.

def test_profile_availability_link_retains_llm_hint_score(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.PROFILE_NAV_HINT,
        url="https://example.com/availability",
        anchor_text="profile:availability_link",
        profile_score_override=10_000,
    )
    result = ranker.rank([sig])
    assert result[0].composite_score == 10_000, (
        "profile:availability_link must retain profile_score_override=10_000 "
        "so is_llm_hint stays True in _try_link_hop"
    )
    assert result[0].reason.startswith("profile_override:")


# ── Regression: multi-signal INTERNAL_LINK boost survives ranker delegation ───
# _rank_internal_links boosts dual-signal links to max(score, 4_000).
# After Phase 2, the ranker gives INTERNAL_LINK base 4_000 + kw_score,
# so boosted links score >= 4_000 naturally. Confirm with the
# alistermontclair.com class: "Find Your Home" anchor + /conventional/ path.

def test_multi_signal_internal_link_above_boost_floor(ranker) -> None:
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/conventional/",
        anchor_text="find your home",
    )
    result = ranker.rank([sig])
    # base 4_000 + max(anchor "find your home"=88, path "/conventional/"=85) = 4_088
    assert result[0].composite_score >= 4_000, (
        "Dual-signal internal link must score >= 4_000 (the multi-signal boost floor)"
    )


# ── Semantic anchor-text scoring ──────────────────────────────────────────────
# partial_ratio returns 100 for exact substring containment, so existing tests
# that assert composite_score == 4_000 + keyword_score still pass unchanged.
# These new tests verify the fuzzy path: near-matches produce proportionally
# reduced scores, and unrelated text stays below the fuzzy_threshold (80).

def test_semantic_near_match_anchor_scores_proportionally() -> None:
    """Near-match (ratio < 100) produces int(score * ratio // 100), not full score.

    Uses an isolated single-keyword ranker so the winning keyword is known and
    the expected score can be computed deterministically.
    """
    from rapidfuzz import fuzz
    from ma_poc.pms.signal_engine.ranker import SourceRanker, ScoringTables

    kw, kw_score = "floor plan", 90
    # "floor-plan" in the anchor differs from "floor plan" by one char (hyphen vs space)
    # → partial_ratio ≈ 90 (near-match, not exact substring containment).
    anchor = "view our floor-plan details"
    ratio = int(fuzz.partial_ratio(kw, anchor))
    assert 80 <= ratio < 100, f"Precondition: need near-match ratio in [80,100), got {ratio}"

    tables = ScoringTables(
        kind_base_scores={SourceKind.INTERNAL_LINK: 4_000},
        anchor_keywords=((kw, kw_score),),
        path_keywords=(), host_keywords=(), pms_priors={}, universal_priors=(),
    )
    ranker_test = SourceRanker(tables, fuzzy_threshold=80)
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/some-page",
        anchor_text=anchor,
    )
    result = ranker_test.rank([sig])
    expected_bonus = int(kw_score * ratio // 100)
    assert result[0].composite_score == 4_000 + expected_bonus
    assert result[0].composite_score < 4_000 + kw_score, "Near-match must yield less than full score"


def test_semantic_exact_anchor_retains_full_score(ranker) -> None:
    """Exact substring containment (ratio=100) gives the full keyword score."""
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/some-page",
        anchor_text="check availability here",
    )
    result = ranker.rank([sig])
    # "availability" keyword score=100; ratio=100 → full value
    assert result[0].composite_score == 4_000 + 100


def test_unrelated_anchor_scores_zero_bonus(ranker) -> None:
    """Anchor text with no keyword overlap stays below fuzzy_threshold → no bonus."""
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/contact",
        anchor_text="contact us today",
    )
    result = ranker.rank([sig])
    assert result[0].composite_score == 4_000


def test_url_path_uses_exact_not_fuzzy(ranker) -> None:
    """Path keyword matching remains exact — no partial-ratio inflation."""
    # "/availabilities" contains "/availability" as a substring → exact hit
    sig_exact = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/availabilities",
        anchor_text="",
    )
    result = ranker.rank([sig_exact])
    assert result[0].composite_score == 4_000 + 95   # /availabilities score=95

    # A URL that is *similar* but not a substring must NOT match
    sig_similar = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/available-units",
        anchor_text="",
    )
    result_similar = ranker.rank([sig_similar])
    # /available-units does not contain /availability or /availabilities exactly
    assert result_similar[0].composite_score == 4_000


def test_fuzzy_threshold_parameter_respected() -> None:
    """At threshold=100 a near-match (ratio<100) is suppressed; at threshold=80 it fires.

    Uses an isolated single-keyword ranker so no other keyword can interfere.
    """
    from rapidfuzz import fuzz
    from ma_poc.pms.signal_engine.ranker import SourceRanker, ScoringTables

    kw, kw_score = "floor plan", 90
    anchor = "view our floor-plan details"   # ratio ≈ 90 — near-match, not exact
    ratio = fuzz.partial_ratio(kw, anchor)
    assert 80 <= ratio < 100, f"Precondition: need near-match ratio in [80,100), got {ratio}"

    tables = ScoringTables(
        kind_base_scores={SourceKind.INTERNAL_LINK: 4_000},
        anchor_keywords=((kw, kw_score),),
        path_keywords=(), host_keywords=(), pms_priors={}, universal_priors=(),
    )
    sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/x",
        anchor_text=anchor,
    )

    # Permissive threshold: near-match fires → some bonus
    lax_ranker = SourceRanker(tables, fuzzy_threshold=80)
    assert lax_ranker.rank([sig])[0].composite_score > 4_000

    # Strict threshold: ratio < 100 is suppressed → no bonus
    strict_ranker = SourceRanker(tables, fuzzy_threshold=100)
    assert strict_ranker.rank([sig])[0].composite_score == 4_000


def test_multi_signal_link_stays_below_pms_prior(ranker) -> None:
    dual_sig = SourceSignal(
        kind=SourceKind.INTERNAL_LINK,
        url="https://example.com/conventional/",
        anchor_text="find your home",
    )
    pms_sig = SourceSignal(
        kind=SourceKind.PMS_PRIOR,
        url="https://example.com/floorplans",
        anchor_text="",
    )
    result = ranker.rank([dual_sig, pms_sig])
    # PMS_PRIOR (base 5_000 + kw) must outrank dual-signal INTERNAL_LINK (base 4_000 + kw)
    assert result[0].signal.kind == SourceKind.PMS_PRIOR
    assert result[1].signal.kind == SourceKind.INTERNAL_LINK
