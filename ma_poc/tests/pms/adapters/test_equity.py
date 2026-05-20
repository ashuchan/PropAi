"""Equity Residential adapter tests.

2026-05-20 cluster #7 — 25 props tagged TIER_1_API_EQUITY with 0
strict units in the canary feature run. Main extracts via various
paths (TIER_1_API_SIGHTMAP, TIER_3_DOM, TIER_4_LLM_DOM) — so the
property data IS recoverable, just not via the Equity-specific
adapter on the canary's fetched HTML.

These tests pin:
  * EquityAdapter.extract emits the right tier label per outcome
    (success / parse-rejected / no-blocks-found)
  * Empty-exit labels are recognized by the registry so the Path B/C
    retry hook fires and Step 8 generic fallback runs
  * The HTML parser ``parse_equity_units`` handles the real-shape
    ea5-unit blocks documented in the adapter docstring
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.equity import (
    OLL_TIER,
    EquityAdapter,
    parse_equity_units,
)


def _make_ctx(html: bytes | str | None) -> AdapterContext:
    """Minimal AdapterContext for the Equity adapter.

    EquityAdapter reads the HTML via ``_html_for(ctx, page)`` which
    pulls from ``ctx.fetch_result.body`` when available, else falls
    back to ``page.content()`` (None here)."""

    class _FetchResult:
        # The adapter reads body as bytes; encode here for realism.
        if isinstance(html, str):
            body = html.encode("utf-8")
        else:
            body = html
        final_url = "https://www.equityapartments.com/some-property"

    return AdapterContext(
        base_url="https://www.equityapartments.com/some-property",
        detected=None,  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="P-equity-test",
        fetch_result=_FetchResult(),  # type: ignore[arg-type]
    )


class _DummyPage:
    """No Playwright page in unit tests; the adapter handles None."""

    async def content(self) -> str:  # pragma: no cover — unused
        return ""


# A real Equity ea5-unit block shape, derived from the adapter's
# research-log docstring (Westerly propertyId 4274, unitId 175).
_REAL_EA5_BLOCK = (
    "<!-- ledgerId: 24112, buildingId: 1, unitId: 175 -->"
    '<ea5-unit value="vm.BedroomTypes[0].AvailableUnits[0]">'
    '<div class="unit"><div class="unit-expanded-card">'
    '<span class="pricing">$1,150</span>'
    '<span class="time-period">12 mo</span>'
    "0 Bed <b>/</b> 1 Bath"
    "<span>576 sq.ft.</span>"
    "Available 6/4/2026"
    '<img class="static" src="/img/plan-383881-0-1-576" alt="S3" />'
    "</div></div></ea5-unit>"
)


# ─────────────────────────────────────────────────────────────────────
# Parser behavior
# ─────────────────────────────────────────────────────────────────────


def test_parse_equity_units_extracts_real_block_shape() -> None:
    """Sanity — the real-shape block from the adapter docstring parses
    out beds/baths/sqft/rent/available_date/floor_plan_name."""
    html = f"<html><body>{_REAL_EA5_BLOCK}</body></html>"
    units = parse_equity_units(html, "https://www.equityapartments.com/x")
    assert len(units) == 1
    u = units[0]
    # Beds=0 (studio), baths=1, sqft=576, rent=$1150
    assert u["bedrooms"] in {"0", "Studio"} or "studio" in u.get("bed_label", "").lower()
    assert u["bathrooms"] == "1"
    assert u["sqft"] == "576"
    assert "1,150" in u["rent_range"] or "1150" in u["rent_range"]
    # Plan name from img alt
    assert "S3" in (u.get("floor_plan_name") or "")


def test_parse_equity_units_empty_html_returns_empty() -> None:
    assert parse_equity_units("", "https://x.com/") == []
    assert parse_equity_units("<html><body>no units here</body></html>", "x") == []


def test_parse_equity_units_dedups_multiple_blocks() -> None:
    """Two ea5-unit blocks → two units (different unitIds)."""
    block2 = _REAL_EA5_BLOCK.replace("unitId: 175", "unitId: 176").replace(
        "$1,150", "$1,400"
    )
    html = f"<html><body>{_REAL_EA5_BLOCK}{block2}</body></html>"
    units = parse_equity_units(html, "x")
    assert len(units) == 2


# ─────────────────────────────────────────────────────────────────────
# Adapter tier-label contract (the cluster #7 fix)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_equity_emits_bare_success_label_on_real_data() -> None:
    """Regression guard — when ea5-unit blocks ARE present and post-
    process admits them, the adapter must keep the bare ``TIER_1_API_EQUITY``
    label so Path B/C does NOT retry on success."""
    html = f"<html><body>{_REAL_EA5_BLOCK}</body></html>"
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == OLL_TIER, (
        f"real data must keep bare success label; got {result.tier_used!r}"
    )
    assert len(result.units) >= 1


@pytest.mark.asyncio
async def test_equity_emits_no_response_when_no_ea5_blocks() -> None:
    """Cluster #7 pattern — equityapartments.com URL returned a body
    with no ea5-unit blocks (legacy .aspx redirect, Cloudflare
    challenge, or genuine no-availability). Adapter must emit
    ``TIER_1_API_EQUITY_NO_RESPONSE`` so retry/fallback can engage."""
    html = (
        "<html><body><h1>Welcome</h1>"
        "<p>This page doesn't have any unit blocks.</p>"
        "</body></html>"
    )
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == f"{OLL_TIER}_NO_RESPONSE", (
        f"expected _NO_RESPONSE label; got {result.tier_used!r}"
    )


@pytest.mark.asyncio
async def test_equity_emits_empty_when_blocks_fail_validity() -> None:
    """Edge case — ea5-unit blocks were parsed but post_process
    rejected them all (e.g. malformed rent/beds). Adapter must emit
    ``TIER_1_API_EQUITY_EMPTY``."""
    # Construct an ea5-unit block missing both rent AND any physical
    # dimension — should be rejected by the validity gate.
    bad_block = (
        "<!-- ledgerId: 1, buildingId: 1, unitId: BAD -->"
        '<ea5-unit value="">'
        '<div class="unit"><div class="unit-expanded-card">'
        # No pricing, no beds, no sqft — should be rejected.
        "</div></div></ea5-unit>"
    )
    html = f"<html><body>{bad_block}</body></html>"
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    # Either parse_equity_units returns [] (no rent → drop early) or
    # post_process rejects. Either way, the tier_used must signal
    # empty-exit so retry/fallback runs.
    assert result.units == []
    assert result.tier_used in {
        f"{OLL_TIER}_NO_RESPONSE",
        f"{OLL_TIER}_EMPTY",
    }, f"expected empty-exit label, got {result.tier_used!r}"


@pytest.mark.asyncio
async def test_equity_empty_labels_in_empty_exit_registry() -> None:
    """Both empty-exit labels must be recognized by ``is_empty_exit``
    so the Path B/C retry hook in scraper.py fires on them."""
    from ma_poc.pms.empty_exit import is_empty_exit
    assert is_empty_exit(f"{OLL_TIER}_NO_RESPONSE") is True
    assert is_empty_exit(f"{OLL_TIER}_EMPTY") is True
    # Bare success label must NOT trigger retry.
    assert is_empty_exit(OLL_TIER) is False


def test_equity_static_fingerprints_nonempty() -> None:
    assert EquityAdapter().static_fingerprints()
