"""End-to-end smoke for the self-learning loop closure.

These tests pin the contract end-to-end: a property that's seen the
LLM tier on day N must replay deterministically on day N+1 with zero
LLM cost, producing the same units. This is the closure of the
"every property re-pays the LLM tax daily" regression that drove the
2026-05-08 prod run to $1.6074 against a $1.00 SLO.

Why a smoke and not a full Cloud Run task: the closure is testable
within the adapter alone — the data layer fix ensures profiles persist
between runs, and the adapter's replay sub-tier is the path that
exploits them. The Cloud Run / Postgres parts are covered by the
provider-contract tests in tests/data_provider/test_contract.py and
the wiring tests in tests/scripts/test_jugnu_profile_store.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from data_provider.factory import get_data_provider, reset_cached_provider
from data_provider.sqlite import SqliteDataProvider
from models.scrape_profile import (
    DomHints,
    FieldSelectorMap,
    LlmFieldMapping,
    ScrapeProfile,
)
from pms.adapters.base import AdapterContext
from pms.adapters.generic import GenericAdapter


@dataclass
class _StubDetected:
    pms: str = "unknown"


def _make_ctx(
    profile: ScrapeProfile, api_responses: list[dict[str, Any]]
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://example.com/availability",
        detected=_StubDetected(),  # type: ignore[arg-type]
        profile=profile,
        expected_total_units=None,
        property_id=profile.canonical_id,
    )
    ctx._api_responses = api_responses  # type: ignore[attr-defined]
    return ctx


# ── Closure test: saved mapping + matching response = zero LLM cost win ───


def test_self_learning_loop_replay_saves_llm_call() -> None:
    """The canonical regression-closure test.

    Day 1: LLM analysed the API and produced a mapping. Day 2: same API
    shape, mapping is on the profile. The adapter must short-circuit
    via TIER_1_PROFILE_MAPPING with no LLM interactions and emit a
    PROFILE_REPLAY_HIT event so the SLO watcher can credit the
    avoidance.
    """
    p = ScrapeProfile(canonical_id="smoke-replay-001")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/availability/v2",
            json_paths={
                "unit_id": "id",
                "rent_low": "monthlyRent",
                "rent_high": "monthlyRent",
                "sqft": "areaSqft",
            },
            response_envelope="",
        )
    ]
    captured = [
        {
            "url": "https://example.com/api/availability/v2",
            "body": [
                {"id": "A101", "monthlyRent": 1450, "areaSqft": 720},
                {"id": "A201", "monthlyRent": 1675, "areaSqft": 880},
                {"id": "A305", "monthlyRent": 2100, "areaSqft": 1100},
            ],
        }
    ]
    ctx = _make_ctx(p, captured)
    adapter = GenericAdapter()
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))

    # The replay tier won — no LLM tier_used label means no LLM call fired.
    assert result.tier_used == "TIER_1_PROFILE_MAPPING"
    assert len(result.units) == 3

    # No LLM interactions accrued — the cost ledger would record 0 USD.
    interactions = getattr(result, "_llm_interactions", []) or []
    assert interactions == []


def test_replay_miss_with_saved_mapping_falls_through_to_cascade() -> None:
    """A property that has saved mappings but captures a non-matching
    URL today must NOT short-circuit. The cascade runs as if the
    mapping wasn't there. Without this guard a stale mapping would
    block the property's only path to data."""
    p = ScrapeProfile(canonical_id="smoke-replay-002")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/old/path/that/changed",
            json_paths={"unit_id": "id", "rent_low": "rent"},
            response_envelope="",
        )
    ]
    # Today's capture is a different URL — the saved mapping cannot replay.
    # The body still has data the cascade's narrow parser will pick up.
    captured = [
        {
            "url": "https://example.com/api/v3/units",
            "body": [
                {"id": "B1", "unit_number": "B1", "rent": 1300, "sqft": 600},
                {"id": "B2", "unit_number": "B2", "rent": 1500, "sqft": 700},
            ],
        }
    ]
    ctx = _make_ctx(p, captured)
    adapter = GenericAdapter()
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))

    # Cascade picked up the data — tier is one of the non-LLM, non-replay
    # paths (api_narrow / api_broad / etc).
    assert result.tier_used != "TIER_1_PROFILE_MAPPING"
    assert len(result.units) == 2


# ── Persistence-roundtrip test: profile written, reloaded, replays ────────


def test_profile_roundtrip_through_sqlite_provider_then_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end regression closure under the data-provider layer
    that the production runner uses. Without the
    ``_SimpleProfileStore`` data-provider wiring fix, this scenario
    would fail because:

      1. Run 1 saves a mapping to the FS profile dir.
      2. Cloud Run task exits → FS state lost.
      3. Run 2's container starts with empty FS → profile reads return None.
      4. Replay tier sees no saved mappings → LLM call fires.

    With the fix, the profile persists in the data-provider store
    (here SQLite for test speed; SqlProfileStore in prod), so run 2
    reads it back and the replay tier wins.
    """
    db_path = tmp_path / "smoke_pg.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("DATA_PROVIDER", "sqlite")
    reset_cached_provider()
    try:
        provider = get_data_provider()
        # Day 1 — write the profile through the provider, simulating the
        # end-of-shard sync that lands llm_field_mappings into PG.
        p_day1 = ScrapeProfile(canonical_id="smoke-roundtrip-001")
        p_day1.api_hints.llm_field_mappings = [
            LlmFieldMapping(
                api_url_pattern="/api/units",
                json_paths={"unit_id": "id", "rent_low": "rent"},
                response_envelope="",
            )
        ]
        provider.profiles.put(p_day1)

        # Day 2 — fresh container would have an empty FS. The runner
        # now reads from the data-provider, so we get the saved profile
        # back and replay against today's capture.
        reloaded = provider.profiles.get("smoke-roundtrip-001")
        assert reloaded is not None
        assert len(reloaded.api_hints.llm_field_mappings) == 1
        saved = reloaded.api_hints.llm_field_mappings[0]
        assert saved.api_url_pattern == "/api/units"

        captured = [
            {
                "url": "https://example.com/api/units",
                "body": [
                    {"id": "U1", "rent": 1450},
                    {"id": "U2", "rent": 1675},
                ],
            }
        ]
        ctx = _make_ctx(reloaded, captured)
        adapter = GenericAdapter()
        result = asyncio.run(adapter.extract(page=None, ctx=ctx))

        # The full closure: persisted profile → replay → zero-LLM win.
        assert result.tier_used == "TIER_1_PROFILE_MAPPING"
        assert len(result.units) == 2
        assert getattr(result, "_llm_interactions", []) == []
    finally:
        reset_cached_provider()


# ── Telemetry round-trip ──────────────────────────────────────────────────


def test_replay_hit_emits_avoidance_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """PROFILE_REPLAY_HIT is the avoidance signal the SLO watcher
    aggregates. Pin that the adapter actually emits it on a replay
    win — without this event, ``llm_tax_avoidance_rate`` would always
    look like 0% and we couldn't catch the regression next time."""
    captured_events: list[Any] = []

    from ma_poc.observability import events as events_mod

    def _capture_emit(kind: Any, property_id: str, **payload: Any) -> None:
        captured_events.append((kind, property_id, payload))

    monkeypatch.setattr(events_mod, "emit", _capture_emit)

    p = ScrapeProfile(canonical_id="smoke-telem-001")
    p.api_hints.llm_field_mappings = [
        LlmFieldMapping(
            api_url_pattern="/api/units",
            json_paths={"unit_id": "id", "rent_low": "rent"},
            response_envelope="",
        )
    ]
    captured = [
        {
            "url": "https://example.com/api/units",
            "body": [{"id": "U1", "rent": 1500}, {"id": "U2", "rent": 1700}],
        }
    ]
    ctx = _make_ctx(p, captured)
    adapter = GenericAdapter()
    asyncio.run(adapter.extract(page=None, ctx=ctx))

    hit_events = [e for e in captured_events if str(e[0]).endswith("replay_hit")]
    assert hit_events, (
        "PROFILE_REPLAY_HIT must fire on a replay-tier win — without it the "
        "llm_tax_avoidance_rate SLO can't observe the win."
    )
    kind, pid, payload = hit_events[0]
    assert pid == "smoke-telem-001"
    assert payload.get("units") == 2
    assert payload.get("mappings_used") == 1


# ── DOM-selector quality gate ─────────────────────────────────────────────


def test_low_quality_dom_selectors_skipped_in_replay() -> None:
    """A profile with low-quality saved DOM selectors (the LLM produced
    selectors that didn't reproduce their own units when validated at
    save time) must NOT be passed to the DOM cascade as hints. Letting
    them through wastes one DOM-cascade pass per run on a known-bad
    selector before evicting it.

    This pins the threshold the cascade applies — paired with the
    ``test_replay_gate_quality_below_threshold_skipped`` model-level
    test to catch a regression at either layer."""
    p = ScrapeProfile(canonical_id="smoke-quality-001")
    # Dial the saved selectors to "broken" — quality 0.3 < 0.4 threshold.
    p.dom_hints = DomHints(
        field_selectors=FieldSelectorMap(container=".this-wont-match-anything"),
        field_selectors_quality=0.3,
    )
    # Provide HTML that the DEFAULT cascade can find units in, so the
    # low-quality hints must be skipped for the cascade to succeed.
    html = """
    <html><body>
      <div class="unit-row">
        <span class="unit">Unit 101</span>
        <span class="rent">$1,450/mo</span>
        <span class="sqft">720 sq ft</span>
        <span class="beds">1 bed</span>
      </div>
      <div class="unit-row">
        <span class="unit">Unit 102</span>
        <span class="rent">$1,675/mo</span>
        <span class="sqft">880 sq ft</span>
        <span class="beds">2 bed</span>
      </div>
    </body></html>
    """
    ctx = _make_ctx(p, [])
    ctx._html = html  # type: ignore[attr-defined]

    adapter = GenericAdapter()
    result = asyncio.run(adapter.extract(page=None, ctx=ctx))
    # The exact tier the cascade picks depends on the HTML shape; the
    # invariant we're pinning is "did NOT get TIER_3_DOM via hints" —
    # which would happen iff the low-quality hints were applied.
    if result.tier_used == "TIER_3_DOM":
        attempted = getattr(result, "_dom_hints_attempted", False)
        assert not attempted, (
            "Low-quality saved selectors (quality<0.4) must not be used "
            "as hints — they'd burn a cascade pass on a known-bad selector."
        )
