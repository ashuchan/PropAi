"""TDD tests for extraction.engine.network_capture.

Written before the module exists — these drive the extraction of network
capture logic out of scripts/entrata.py into a focused, testable class.
"""
from __future__ import annotations

import asyncio

import pytest

from extraction.engine.network_capture import NetworkCapture


class _FakeResponse:
    """Minimal Playwright response stub for testing the capture handler."""

    def __init__(
        self,
        url: str,
        status: int = 200,
        content_type: str = "application/json",
        body=None,
    ):
        self.url = url
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


class TestNetworkCapture:
    def test_starts_empty(self):
        nc = NetworkCapture()
        assert nc.responses == []

    def test_reset_clears_responses(self):
        nc = NetworkCapture()
        nc._api_responses.append({"url": "x", "body": {}})
        nc.reset()
        assert nc.responses == []

    @pytest.mark.asyncio
    async def test_handler_captures_availability_url(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        resp = _FakeResponse("https://example.com/api/units", body=[{"unit_id": "A1"}])
        await handler(resp)
        assert len(nc.responses) == 1
        assert nc.responses[0]["url"] == "https://example.com/api/units"
        assert nc.responses[0]["body"] == [{"unit_id": "A1"}]

    @pytest.mark.asyncio
    async def test_handler_ignores_non_availability_url(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        resp = _FakeResponse("https://hotjar.com/api/capture")
        await handler(resp)
        assert nc.responses == []

    @pytest.mark.asyncio
    async def test_handler_ignores_non_json_content_type(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        resp = _FakeResponse(
            "https://example.com/api/units",
            content_type="text/html",
        )
        await handler(resp)
        assert nc.responses == []

    @pytest.mark.asyncio
    async def test_handler_ignores_error_status(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        resp = _FakeResponse(
            "https://example.com/api/units",
            status=404,
        )
        await handler(resp)
        assert nc.responses == []

    @pytest.mark.asyncio
    async def test_handler_deduplicates_same_url(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        resp1 = _FakeResponse("https://example.com/api/units", body={"a": 1})
        resp2 = _FakeResponse("https://example.com/api/units", body={"b": 2})
        await handler(resp1)
        await handler(resp2)
        assert len(nc.responses) == 1

    @pytest.mark.asyncio
    async def test_handler_allows_duplicate_widget_urls(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        widget_url = "https://example.com/apartments/module/widgets/floor_plans"
        resp1 = _FakeResponse(widget_url, body={"widget_name": "floor_plans"})
        resp2 = _FakeResponse(widget_url, body={"widget_name": "availability"})
        await handler(resp1)
        await handler(resp2)
        # Widget URL gets both because each widget response is distinct data
        assert len(nc.responses) == 2

    def test_reset_also_clears_seen_urls(self):
        nc = NetworkCapture()
        nc._seen_api_urls.add("https://example.com/api/units")
        nc.reset()
        assert len(nc._seen_api_urls) == 0

    @pytest.mark.asyncio
    async def test_multiple_distinct_urls_all_captured(self):
        nc = NetworkCapture()
        handler = nc.create_handler()
        urls = [
            "https://example.com/api/units",
            "https://example.com/api/floor-plans",
            "https://example.com/availabilities",
        ]
        for url in urls:
            await handler(_FakeResponse(url))
        assert len(nc.responses) == 3
