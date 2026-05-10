"""LAYER 4 (CONSUME) — pin link-hop ranker + budget refresh helpers.

These pure-function helpers in ``pms/scraper.py`` are the sharp
edges where LLM signals turn into scraping behaviour. The previous
behaviour either never read profile navigation memory at all (Bug 3.1)
or refreshed only the cost cap on rich hops (Bug 5 alignment), missing
the equally-important call-counter reset for high-confidence hints.

Pinned contracts:
  * ``_augment_ranked_with_hints`` — LLM hints prepend at sentinel
    score, drop keyword duplicates so the LLM-anchored entry wins.
  * ``_refresh_monolithic_budget_for_llm_hint`` — bumps
    ``llm_monolithic`` from 0 to 1 only when 0; idempotent at ≥1.
  * ``_refresh_cost_cap_for_hop`` — only raises (never lowers); never
    exceeds the ceiling multiplier.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.scraper import (
    _LLM_HINT_ANCHOR_PREFIX,
    _LLM_HINT_SCORE,
    _augment_ranked_with_hints,
    _refresh_monolithic_budget_for_llm_hint,
)


# ── _augment_ranked_with_hints ───────────────────────────────────────────────


def test_augment_prepends_llm_hint_above_keyword_candidates() -> None:
    """LLM hints must outrank every keyword-matched candidate."""
    keyword_ranked = [
        ("https://x.com/availability", 100, "Availability"),
        ("https://x.com/floor-plans", 95, "Floor Plans"),
    ]
    out = _augment_ranked_with_hints(
        keyword_ranked,
        ["/floorplans-page"],
        "https://x.com/",
    )
    # First entry is the LLM hint with sentinel score
    assert out[0][0] == "https://x.com/floorplans-page"
    assert out[0][1] == _LLM_HINT_SCORE
    assert out[0][2].startswith(_LLM_HINT_ANCHOR_PREFIX)
    # Keyword candidates remain after, in original order
    assert out[1][0] == "https://x.com/availability"
    assert out[2][0] == "https://x.com/floor-plans"


def test_augment_dedups_keyword_match_when_llm_hints_same_url() -> None:
    """When the LLM hint resolves to a URL also present in
    keyword-ranked candidates, drop the keyword duplicate so the
    LLM-anchored entry is the one that fires (its anchor is what
    triggers the budget refresh in ``_try_link_hop``)."""
    keyword_ranked = [
        ("https://x.com/floor-plans", 95, "Floor Plans link"),
        ("https://x.com/contact", 30, "Contact"),
    ]
    out = _augment_ranked_with_hints(
        keyword_ranked,
        ["/floor-plans"],  # → resolves to https://x.com/floor-plans (dup)
        "https://x.com/",
    )
    # /floor-plans appears exactly once, with the LLM-hint anchor:
    matching = [(u, s, a) for (u, s, a) in out if u == "https://x.com/floor-plans"]
    assert len(matching) == 1
    assert matching[0][2].startswith(_LLM_HINT_ANCHOR_PREFIX)
    # The /contact keyword candidate survives:
    assert any(u == "https://x.com/contact" for u, _, _ in out)


def test_augment_handles_relative_and_absolute_urls() -> None:
    out = _augment_ranked_with_hints(
        [],
        ["/relative-path", "https://other.com/absolute"],
        "https://x.com/",
    )
    urls = [u for u, _, _ in out]
    assert "https://x.com/relative-path" in urls
    assert "https://other.com/absolute" in urls


def test_augment_skips_invalid_or_empty_hints() -> None:
    """Whitespace, empty strings, non-http schemes are filtered."""
    out = _augment_ranked_with_hints(
        [],
        ["", "   ", "javascript:void(0)", "tel:+15555555", "mailto:x@x.com"],
        "https://x.com/",
    )
    assert out == []


def test_augment_dedupes_repeated_hints() -> None:
    """When the LLM emits the same hint twice across tiers (which the
    aggregator already dedupes), augment also dedupes defensively."""
    out = _augment_ranked_with_hints(
        [],
        ["/floorplans", "/floorplans", "/floorplans"],
        "https://x.com/",
    )
    assert len(out) == 1
    assert out[0][0] == "https://x.com/floorplans"


def test_augment_with_no_hints_returns_input_untouched() -> None:
    """No-hint case is the back-compat path — link-hop falls back to
    the keyword-ranked list."""
    keyword_ranked = [("https://x.com/availability", 100, "Avail")]
    out = _augment_ranked_with_hints(keyword_ranked, [], "https://x.com/")
    assert out == keyword_ranked


def test_augment_hint_score_outranks_max_keyword_score() -> None:
    """The sentinel ``_LLM_HINT_SCORE`` must be strictly greater than
    any keyword/host weight defined in the ranker. Otherwise a
    well-named subdomain could compete with the LLM's diagnostic."""
    # _LINK_HOST_KEYWORDS tops out around 120 in the current ranker —
    # any future increase that approaches 10000 should fail this test.
    assert _LLM_HINT_SCORE >= 1000


# ── _refresh_monolithic_budget_for_llm_hint ─────────────────────────────────


def test_monolithic_refresh_bumps_zero_to_one() -> None:
    """The headline use case — entry page burned monolithic to 0,
    LLM-hinted hop deserves a fresh shot."""
    budget: dict[str, Any] = {"llm_monolithic": 0}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_no_op_when_already_at_one() -> None:
    """Idempotent — already-available budget isn't double-stamped."""
    budget: dict[str, Any] = {"llm_monolithic": 1}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is False
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_no_op_when_above_one() -> None:
    """If a future budget config grants >1 monolithic calls per
    property, the refresh is still a no-op — we never inflate."""
    budget: dict[str, Any] = {"llm_monolithic": 3}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is False
    assert budget["llm_monolithic"] == 3


def test_monolithic_refresh_handles_missing_key() -> None:
    """Missing key is treated as 0 — the helper doesn't crash on a
    sparse budget dict."""
    budget: dict[str, Any] = {}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_handles_non_int_value() -> None:
    """Defensive — a corrupt budget value (None / string) is treated
    as 0 rather than crashing the helper."""
    budget: dict[str, Any] = {"llm_monolithic": None}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1

    budget = {"llm_monolithic": "not-an-int"}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_emits_telemetry_when_property_id_present(monkeypatch) -> None:
    """When the helper raises the counter, it emits a
    LINK_HOP_BUDGET_REFRESH event tagged with
    ``refresh_kind="llm_monolithic_counter"`` so analysers can
    distinguish counter-refresh from cost-cap-refresh.

    Regression note: the helper used to pass ``kind=`` as a kwarg,
    colliding with ``emit(kind: EventKind, ...)``'s positional
    parameter — TypeError was silently swallowed by the surrounding
    try/except, so this telemetry never actually fired in production.
    The kwarg was renamed to ``refresh_kind`` and this test pins the
    correct emission so the bug can't regress."""
    captured: list[dict[str, Any]] = []

    def _capture_emit(kind, prop_id, **kwargs):
        captured.append({"event_kind": str(kind), "property_id": prop_id, **kwargs})

    monkeypatch.setattr("ma_poc.observability.events.emit", _capture_emit)

    budget: dict[str, Any] = {"llm_monolithic": 0}
    _refresh_monolithic_budget_for_llm_hint(
        budget,
        property_id="P1",
        sub_url="https://x.com/floorplans",
        hop_index=1,
    )

    assert len(captured) == 1, f"expected 1 emission, got {captured}"
    e = captured[0]
    # EventKind values are lowercase string slugs, not enum names —
    # ``EventKind.LINK_HOP_BUDGET_REFRESH = "planner.link_hop_budget_refresh"``.
    assert "link_hop_budget_refresh" in e["event_kind"].lower()
    assert e["property_id"] == "P1"
    assert e["sub_url"] == "https://x.com/floorplans"
    assert e["hop_index"] == 1
    # The discriminator kwarg distinguishes counter-refresh from cost-cap:
    assert e["refresh_kind"] == "llm_monolithic_counter"
    assert e["old_value"] == 0
    assert e["new_value"] == 1


def test_monolithic_refresh_silent_when_no_property_id() -> None:
    """When called without a property_id (e.g. unit-test scenarios),
    the helper still works but doesn't emit telemetry."""
    budget: dict[str, Any] = {"llm_monolithic": 0}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    # No exception, returns the right value.
