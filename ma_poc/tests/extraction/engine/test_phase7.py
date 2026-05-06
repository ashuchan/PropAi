"""TDD tests for Phase7Finalize."""
from __future__ import annotations

import pytest

from extraction.engine.phase_context import PhaseContext
from extraction.engine.phases.phase7_finalize import Phase7Finalize


def _make_unit(avail_status="", avail_date=""):
    return {
        "floor_plan_name": "1BR",
        "unit_number": "101",
        "rent_range": "$1,200",
        "availability_status": avail_status,
        "availability_date": avail_date,
    }


class TestPhase7Finalize:
    @pytest.mark.asyncio
    async def test_applies_availability_defaults_to_units(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = [_make_unit()]
        ctx.page = None

        await Phase7Finalize().run(ctx)

        assert ctx.units[0]["availability_status"] == "AVAILABLE"

    @pytest.mark.asyncio
    async def test_no_units_sets_failed_tier(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = []
        ctx.extraction_tier_used = None
        ctx.page = None

        await Phase7Finalize().run(ctx)

        assert ctx.extraction_tier_used == "FAILED"

    @pytest.mark.asyncio
    async def test_unreachable_sets_failed_unreachable(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = []
        ctx.page_unreachable = True
        ctx.extraction_tier_used = None
        ctx.page = None

        await Phase7Finalize().run(ctx)

        assert ctx.extraction_tier_used == "FAILED_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_preserves_existing_tier_when_units_found(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = [_make_unit()]
        ctx.extraction_tier_used = "TIER_1_API"
        ctx.page = None

        await Phase7Finalize().run(ctx)

        assert ctx.extraction_tier_used == "TIER_1_API"

    @pytest.mark.asyncio
    async def test_sets_winning_page_url_from_ctx(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = [_make_unit()]
        ctx.winning_page_url = None

        class FakePage:
            url = "https://example.com/floor-plans"

        ctx.page = FakePage()

        await Phase7Finalize().run(ctx)

        assert ctx.winning_page_url == "https://example.com/floor-plans"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_winning_url(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = [_make_unit()]
        ctx.winning_page_url = "https://example.com/api/units"

        class FakePage:
            url = "https://example.com/other"

        ctx.page = FakePage()

        await Phase7Finalize().run(ctx)

        assert ctx.winning_page_url == "https://example.com/api/units"
