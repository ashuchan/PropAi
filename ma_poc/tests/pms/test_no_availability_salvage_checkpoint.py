"""Operator-no-availability flag must survive coroutine cancellation.

Prod 2026-07-12 cohort (pids 10496 / 15693 class): the entry page carries an
explicit "no units available" statement — the generic adapter sets
``result["_operator_no_availability"] = True`` and returns a placeholder —
but the link-hop crawl correctly continues (deeper pages might still list
units). When the property then times out mid-hop, ``_process_one``'s salvage
path recomputed the verdict WITHOUT the flag and stamped FAILED_NO_DATA,
discarding the operator's authoritative zero-inventory answer.

Fix under test: ``scrape_jugnu`` checkpoints the flag into the
caller-supplied ``partial_state`` dict (which lives in ``_process_one``'s
scope and survives ``asyncio.wait_for`` cancellation), and the jugnu salvage
passes ``operator_no_availability=partial_state.get(...)`` to
``compute_verdict`` — which routes zero-unit salvages to
SUCCESS_NO_AVAILABILITY (already covered by
tests/reporting/test_verdict_no_availability.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import ma_poc.pms.scraper as scraper_mod


@dataclass
class FakeCrawlTask:
    url: str = "https://example.com"
    property_id: str = "test_noavail_001"
    render_mode: str = "RENDER"


@dataclass
class FakeFetchResult:
    outcome: object = None
    # Body kept under the 500-char link-hop entry gate so the test never
    # enters _try_link_hop (the checkpoint under test fires before it).
    body: bytes | None = b"<html><body>no units available</body></html>"
    network_log: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.network_log is None:
            self.network_log = []
        if self.outcome is None:
            self.outcome = type("O", (), {"value": "OK"})()

    def ok(self) -> bool:
        return self.outcome.value == "OK"  # type: ignore[union-attr]

    def to_dict(self) -> dict:
        return {"outcome": self.outcome.value}  # type: ignore[union-attr]


def _flagged_scrape_result() -> dict[str, Any]:
    """What scrape() returns when the no-availability detector fired."""
    return {
        "units": [
            {
                "unit_id": "no_availability_placeholder",
                "availability_status": "UNAVAILABLE",
            }
        ],
        "extraction_tier_used": "TIER_1_DOM_NO_AVAILABILITY",
        "errors": [],
        "_operator_no_availability": True,
    }


def _unflagged_scrape_result() -> dict[str, Any]:
    return {
        "units": [],
        "extraction_tier_used": None,
        "errors": [],
    }


@pytest.mark.asyncio
async def test_scrape_jugnu_checkpoints_flag_into_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flagged scrape() result → flag written to partial_state."""

    async def _fake_scrape(**_kw: Any) -> dict[str, Any]:
        return _flagged_scrape_result()

    monkeypatch.setattr(scraper_mod, "scrape", _fake_scrape)
    partial_state: dict[str, Any] = {}
    result = await scraper_mod.scrape_jugnu(
        FakeCrawlTask(),
        FakeFetchResult(),
        partial_state=partial_state,
    )
    assert result.get("_operator_no_availability") is True
    # The contract under test: the flag survives in the caller's dict even
    # if the coroutine is later cancelled mid-hop.
    assert partial_state.get("operator_no_availability") is True


@pytest.mark.asyncio
async def test_scrape_jugnu_no_flag_leaves_partial_state_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag on the scrape result → nothing checkpointed (no false
    SUCCESS_NO_AVAILABILITY salvages for ordinary empty extractions)."""

    async def _fake_scrape(**_kw: Any) -> dict[str, Any]:
        return _unflagged_scrape_result()

    monkeypatch.setattr(scraper_mod, "scrape", _fake_scrape)
    partial_state: dict[str, Any] = {}
    await scraper_mod.scrape_jugnu(
        FakeCrawlTask(),
        FakeFetchResult(),
        partial_state=partial_state,
    )
    assert "operator_no_availability" not in partial_state


@pytest.mark.asyncio
async def test_scrape_jugnu_none_partial_state_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """partial_state=None (scrape_properties.py callers) must not raise."""

    async def _fake_scrape(**_kw: Any) -> dict[str, Any]:
        return _flagged_scrape_result()

    monkeypatch.setattr(scraper_mod, "scrape", _fake_scrape)
    result = await scraper_mod.scrape_jugnu(
        FakeCrawlTask(),
        FakeFetchResult(),
        partial_state=None,
    )
    assert result.get("_operator_no_availability") is True
