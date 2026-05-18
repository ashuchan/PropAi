"""Bonus test (2026-05-18) — Entrata adapter must downgrade its tier label
when no Entrata-shaped API responses were actually captured.

Yardi-served properties that load Entrata widgets via the ``rcommoncf.entrata
.com`` CDN previously surfaced as ``TIER_1_API_ENTRATA + FAILED_NO_DATA`` in
failures.csv even though zero Entrata API responses were captured. That
polluted the daily failure-bucket analysis (the §5 label-leak class). The
fix: when ``result.api_responses`` (the filtered list of Entrata-shaped
captures) is empty, label the result ``TIER_1_API`` so triage isn't misled.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ma_poc.pms.adapters.entrata import EntrataAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_label_downgrades_to_generic_when_no_api_captured() -> None:
    """Ctx has no _api_responses → adapter never finds Entrata-shaped data
    → tier_used must be the generic ``TIER_1_API``, not the misleading
    ``TIER_1_API_ENTRATA``."""
    adapter = EntrataAdapter()
    ctx = SimpleNamespace(
        property_id="P_TEST",
        base_url="https://example.com/",
        _api_responses=[],
    )
    result = _run(adapter.extract(page=None, ctx=ctx))
    assert result.tier_used == "TIER_1_API", (
        f"Expected TIER_1_API downgrade; got {result.tier_used!r}"
    )
    assert result.api_responses == []
    assert result.confidence == 0.0


def test_label_downgrades_when_only_non_entrata_responses_present() -> None:
    """Captured responses but none match Entrata shape → still downgrade."""
    adapter = EntrataAdapter()
    ctx = SimpleNamespace(
        property_id="P_TEST",
        base_url="https://example.com/",
        _api_responses=[
            # A tracker JSON that isn't Entrata-shaped — doesn't match
            # `floorplan-name`, `no_of_bedroom`, or `square_footage`.
            {"url": "https://callrail.com/api/log", "body": [{"event": "click"}]},
        ],
    )
    result = _run(adapter.extract(page=None, ctx=ctx))
    assert result.tier_used == "TIER_1_API"
    # result.api_responses (filtered) is the empty list — only counts
    # responses that survived the Entrata-shape filter.
    assert result.api_responses == []
