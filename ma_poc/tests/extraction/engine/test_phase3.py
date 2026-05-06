"""TDD tests for Phase3KnownPatternExtraction."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from extraction.engine.phase_context import PhaseContext
from extraction.engine.phases.phase3_known_patterns import Phase3KnownPatternExtraction


def _make_unit(rent="$1,200", unit_id="101"):
    return {"rent_range": rent, "unit_number": unit_id, "floor_plan_name": "1BR"}


class TestPhase3Accept:
    """Quality gate that rejects bogus Phase 3 results."""

    @pytest.mark.asyncio
    async def test_accepts_units_with_rent_and_id(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.page = AsyncMock()
        ctx.page.content = AsyncMock(return_value="<html></html>")

        units = [_make_unit()] * 3

        with patch(
            "extraction.engine.phases.phase3_known_patterns.try_known_patterns",
            return_value=units,
        ):
            with patch(
                "extraction.engine.phases.phase3_known_patterns.extract_embedded_json",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch(
                    "extraction.engine.phases.phase3_known_patterns.parse_jsonld",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    with patch(
                        "extraction.engine.phases.phase3_known_patterns.parse_dom",
                        new_callable=AsyncMock,
                        return_value=[],
                    ):
                        await Phase3KnownPatternExtraction().run(ctx)

        assert len(ctx.units) == 3

    @pytest.mark.asyncio
    async def test_skips_when_units_already_found(self):
        """Phase 3 should be a no-op when ctx.units is already populated."""
        ctx = PhaseContext(base_url="https://example.com")
        ctx.units = [_make_unit()]
        ctx.page = AsyncMock()

        with patch(
            "extraction.engine.phases.phase3_known_patterns.try_known_patterns"
        ) as mock_try:
            await Phase3KnownPatternExtraction().run(ctx)
            mock_try.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_extraction_tier_on_api_success(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.page = AsyncMock()
        ctx.page.content = AsyncMock(return_value="<html></html>")
        units = [_make_unit()] * 5

        with patch(
            "extraction.engine.phases.phase3_known_patterns.try_known_patterns",
            return_value=units,
        ):
            with patch("extraction.engine.phases.phase3_known_patterns.extract_embedded_json", new_callable=AsyncMock, return_value=[]):
                with patch("extraction.engine.phases.phase3_known_patterns.parse_jsonld", new_callable=AsyncMock, return_value=[]):
                    with patch("extraction.engine.phases.phase3_known_patterns.parse_dom", new_callable=AsyncMock, return_value=[]):
                        await Phase3KnownPatternExtraction().run(ctx)

        assert ctx.extraction_tier_used in ("TIER_1_API", "TIER_1_PROFILE_MAPPING")

    @pytest.mark.asyncio
    async def test_falls_through_to_jsonld(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.page = AsyncMock()
        ctx.page.content = AsyncMock(return_value="<html></html>")
        jsonld_units = [_make_unit()] * 2

        with patch("extraction.engine.phases.phase3_known_patterns.try_known_patterns", return_value=[]):
            with patch("extraction.engine.phases.phase3_known_patterns.extract_embedded_json", new_callable=AsyncMock, return_value=[]):
                with patch("extraction.engine.phases.phase3_known_patterns.parse_jsonld", new_callable=AsyncMock, return_value=jsonld_units):
                    with patch("extraction.engine.phases.phase3_known_patterns.parse_dom", new_callable=AsyncMock, return_value=[]):
                        await Phase3KnownPatternExtraction().run(ctx)

        assert ctx.units == jsonld_units
        assert ctx.extraction_tier_used == "TIER_2_JSONLD"

    @pytest.mark.asyncio
    async def test_falls_through_to_dom(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.page = AsyncMock()
        ctx.page.content = AsyncMock(return_value="<html></html>")
        dom_units = [_make_unit()] * 4

        with patch("extraction.engine.phases.phase3_known_patterns.try_known_patterns", return_value=[]):
            with patch("extraction.engine.phases.phase3_known_patterns.extract_embedded_json", new_callable=AsyncMock, return_value=[]):
                with patch("extraction.engine.phases.phase3_known_patterns.parse_jsonld", new_callable=AsyncMock, return_value=[]):
                    with patch("extraction.engine.phases.phase3_known_patterns.parse_dom", new_callable=AsyncMock, return_value=dom_units):
                        await Phase3KnownPatternExtraction().run(ctx)

        assert ctx.units == dom_units
        assert ctx.extraction_tier_used == "TIER_3_DOM"


class TestPhase3LowSignalRejection:
    @pytest.mark.asyncio
    async def test_rejects_units_with_no_rent_and_no_id(self):
        ctx = PhaseContext(base_url="https://example.com")
        ctx.page = AsyncMock()
        ctx.page.content = AsyncMock(return_value="<html></html>")
        # Units with no rent and no unit_id — low signal
        low_signal_units = [{"floor_plan_name": "1BR"} for _ in range(3)]

        with patch("extraction.engine.phases.phase3_known_patterns.try_known_patterns", return_value=low_signal_units):
            with patch("extraction.engine.phases.phase3_known_patterns.extract_embedded_json", new_callable=AsyncMock, return_value=[]):
                with patch("extraction.engine.phases.phase3_known_patterns.parse_jsonld", new_callable=AsyncMock, return_value=[]):
                    with patch("extraction.engine.phases.phase3_known_patterns.parse_dom", new_callable=AsyncMock, return_value=[]):
                        await Phase3KnownPatternExtraction().run(ctx)

        assert ctx.units == []
