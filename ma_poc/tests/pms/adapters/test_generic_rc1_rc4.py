"""RC1 + RC4 + RC3 — production-code tests for GenericAdapter helpers.

RC1: _should_block_endpoint() — blocked-endpoint TTL and noise-verdicts gate.
     Tests import the production function directly; no shadow copies.

RC4: _is_non_data_response() — JS/CSS/font/image hard gate.
     Tests import the production function directly; no shadow copies.

RC3: SourceRanker + ActionDecider wiring — verifies the score produced by
     _source_ranker for an LLM_HINT signal satisfies ActionDecider's
     _HIGH_CONF_HOP_THRESHOLD so the full pipeline defers the monolithic LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ma_poc.pms.adapters.generic import (
    _BLOCK_TTL_DAYS,
    _MIN_NOISE_VERDICTS,
    _is_non_data_response,
    _should_block_endpoint,
    _source_ranker,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

@dataclass
class _BlockedEndpoint:
    """Minimal BlockedEndpoint-like object for testing.

    Uses a dataclass so each instance has independent instance attributes —
    class-attribute sharing across multiple helper calls can't cause confusion.
    """
    url_pattern: str
    blocked_at: datetime
    attempts: int


def _make_blocked_endpoint(
    url: str,
    attempts: int = 2,
    blocked_at_days_ago: int = 5,
) -> _BlockedEndpoint:
    return _BlockedEndpoint(
        url_pattern=url,
        blocked_at=datetime.now(timezone.utc) - timedelta(days=blocked_at_days_ago),
        attempts=attempts,
    )


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── RC1: _should_block_endpoint (production function) ─────────────────────────

def test_rc1_blocked_within_ttl_is_blocked() -> None:
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=3)
    assert _should_block_endpoint(be, _now_utc_naive()) is True


def test_rc1_blocked_ttl_expired_is_readmitted() -> None:
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=15)
    assert _should_block_endpoint(be, _now_utc_naive()) is False


def test_rc1_insufficient_noise_verdicts_readmitted() -> None:
    """Only 1 verdict — not enough evidence to block permanently."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=1, blocked_at_days_ago=2)
    assert _should_block_endpoint(be, _now_utc_naive()) is False


def test_rc1_exactly_min_verdicts_within_ttl_is_blocked() -> None:
    """Exactly 2 verdicts within TTL → still blocked."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=5)
    assert _should_block_endpoint(be, _now_utc_naive()) is True


def test_rc1_exactly_at_ttl_boundary_is_readmitted() -> None:
    """14 days old exactly → re-admitted (age_days >= _BLOCK_TTL_DAYS)."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=3, blocked_at_days_ago=_BLOCK_TTL_DAYS)
    assert _should_block_endpoint(be, _now_utc_naive()) is False


def test_rc1_constants_match_spec() -> None:
    """Module-level constants must match the spec values."""
    assert _BLOCK_TTL_DAYS == 14
    assert _MIN_NOISE_VERDICTS == 2


# ── RC4: _is_non_data_response (production function) ──────────────────────────

def test_rc4_javascript_content_type_blocked() -> None:
    resp = {"url": "https://cdn.rentcafe.com/app.js", "content_type": "text/javascript"}
    assert _is_non_data_response(resp) is True


def test_rc4_css_content_type_blocked() -> None:
    resp = {"url": "https://cdn.example.com/styles.css", "content_type": "text/css"}
    assert _is_non_data_response(resp) is True


def test_rc4_image_content_type_blocked() -> None:
    resp = {"url": "https://example.com/banner.png", "content_type": "image/png"}
    assert _is_non_data_response(resp) is True


def test_rc4_js_url_suffix_blocked_even_without_content_type() -> None:
    resp = {"url": "https://cdngeneralmvc.rentcafe.com/bundle.min.js", "content_type": None}
    assert _is_non_data_response(resp) is True


def test_rc4_woff_font_blocked() -> None:
    resp = {"url": "https://fonts.example.com/font.woff2", "content_type": "font/woff2"}
    assert _is_non_data_response(resp) is True


def test_rc4_json_api_passes_through() -> None:
    resp = {"url": "https://api.example.com/units", "content_type": "application/json"}
    assert _is_non_data_response(resp) is False


def test_rc4_plain_api_url_no_content_type_passes() -> None:
    resp = {"url": "https://example.com/api/v1/floorplans", "content_type": None}
    assert _is_non_data_response(resp) is False


def test_rc4_js_suffix_with_query_string_blocked() -> None:
    """Query params must not fool the suffix check."""
    resp = {"url": "https://cdn.example.com/app.js?v=123", "content_type": None}
    assert _is_non_data_response(resp) is True


# ── RC3: SourceRanker + ActionDecider wiring ───────────────────────────────────
# Verifies the full pipeline: _source_ranker scores an LLM_HINT signal at a
# value that satisfies ActionDecider._HIGH_CONF_HOP_THRESHOLD, and that
# ActionDecider.decide() returns HOP_TO_URL with llm_monolithic NOT decremented.

def test_rc3_wiring_llm_hint_score_satisfies_decider_threshold() -> None:
    """LLM_HINT score from _source_ranker must be ≥ 9_000 to trigger RC3."""
    from ma_poc.pms.signal_engine.decider import ActionDecider, ActionType, DecisionContext, DOMAnalysisResult
    from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal

    assert _source_ranker is not None, "_source_ranker must initialise at import time"
    hint_sig = SourceSignal(kind=SourceKind.LLM_HINT, url="https://example.com/floorplans")
    ranked = _source_ranker.rank([hint_sig])
    assert ranked[0].composite_score >= 9_000, (
        f"LLM_HINT composite_score={ranked[0].composite_score} < 9_000 threshold"
    )

    ctx = DecisionContext(
        ranked_signals=ranked,
        current_unit_count=0,
        budget={"llm_monolithic": 1, "link_hop": 3},
        dom_analysis_result=DOMAnalysisResult(
            unit_count=0,
            navigation_hint="https://example.com/floorplans",
        ),
    )
    decision = ActionDecider().decide(ctx)
    assert decision.action_type == ActionType.HOP_TO_URL
    assert decision.rationale == "dom_analysis_defer_monolithic_to_hop"
    assert decision.budget_after["llm_monolithic"] == 1, "llm_monolithic must NOT be decremented on RC3 deferral"


def test_rc3_wiring_no_deferral_when_ranker_unavailable_scenario() -> None:
    """When ActionDecider falls to Rule 4, the top LLM_HINT still hops correctly."""
    from ma_poc.pms.signal_engine.decider import ActionDecider, ActionType, DecisionContext
    from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal

    assert _source_ranker is not None
    hint_sig = SourceSignal(kind=SourceKind.LLM_HINT, url="https://example.com/floorplans")
    ranked = _source_ranker.rank([hint_sig])

    # llm_monolithic=0 so RC3 Rule 2 does not fire; falls to Rule 4
    ctx = DecisionContext(
        ranked_signals=ranked,
        current_unit_count=0,
        budget={"llm_monolithic": 0, "link_hop": 3},
        dom_analysis_result=None,
    )
    decision = ActionDecider().decide(ctx)
    assert decision.action_type == ActionType.HOP_TO_URL
    assert decision.rationale.startswith("top_signal:")


# ── create_rentcafe_qualifier() factory invariants ────────────────────────────

def test_rentcafe_qualifier_has_exactly_three_combinations() -> None:
    """create_rentcafe_qualifier() must yield exactly the 3 RentCafe shapes."""
    from ma_poc.pms.signal_engine.defaults import create_rentcafe_qualifier
    q = create_rentcafe_qualifier()
    assert len(q.combinations) == 3
    labels = {c.label for c in q.combinations}
    assert labels == {"rentcafe_floor_plan", "rentcafe_unit", "rentcafe_unit_rent"}


def test_rentcafe_qualifier_excludes_unit_generic() -> None:
    """unit_generic must not appear — it would admit any generic API as RentCafe."""
    from ma_poc.pms.signal_engine.defaults import create_rentcafe_qualifier
    q = create_rentcafe_qualifier()
    assert all(c.label != "unit_generic" for c in q.combinations)


def test_rentcafe_qualifier_uses_shared_media_filter() -> None:
    """Both qualifier factories must share the same DEFAULT_MEDIA_FILTER instance."""
    from ma_poc.pms.signal_engine.defaults import (
        DEFAULT_MEDIA_FILTER,
        create_default_qualifier,
        create_rentcafe_qualifier,
    )
    rc_q = create_rentcafe_qualifier()
    default_q = create_default_qualifier()
    assert rc_q.media_filter is DEFAULT_MEDIA_FILTER
    assert default_q.media_filter is DEFAULT_MEDIA_FILTER
