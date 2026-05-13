"""Tests for RobotsConsumer — allow-check, caching, and failure handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.robots import RobotsConsumer


class TestRobotsConsumerFailureCaching:
    """Failed robots.txt fetches must be cached to avoid repeated HTTP requests."""

    @pytest.mark.asyncio
    async def test_fetch_failure_cached_allows_subsequent_call(self) -> None:
        """After a fetch failure, a second call for the same host hits the cache."""
        consumer = RobotsConsumer()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # Simulate network error on robots.txt fetch
            mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

            # First call — triggers HTTP request, fails, caches None
            result1 = await consumer.is_allowed("https://example.com/page", "TestBot/1.0")
            assert result1 is True  # default allow on failure

            # Second call — must NOT trigger another HTTP request
            result2 = await consumer.is_allowed("https://example.com/other", "TestBot/1.0")
            assert result2 is True

            # HTTP client should only have been constructed once
            assert mock_client_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_redirect_loop_failure_cached(self) -> None:
        """TooManyRedirects on robots.txt is cached so retries don't re-fetch."""
        import httpx
        consumer = RobotsConsumer()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(
                side_effect=httpx.TooManyRedirects("too many redirects", request=MagicMock())
            )

            await consumer.is_allowed("https://myresman.com/page", "TestBot/1.0")
            await consumer.is_allowed("https://myresman.com/other", "TestBot/1.0")

            # Should only have made one HTTP attempt before caching the failure
            assert mock_client_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_successful_fetch_cached(self) -> None:
        """Successful robots.txt is still cached (pre-existing behaviour preserved)."""
        consumer = RobotsConsumer()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "User-agent: *\nAllow: /\n"
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            await consumer.is_allowed("https://example.com/page", "TestBot/1.0")
            await consumer.is_allowed("https://example.com/page2", "TestBot/1.0")

            assert mock_client_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_404_treated_as_allow_all(self) -> None:
        """404 on robots.txt means no restrictions — all paths allowed."""
        consumer = RobotsConsumer()

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)

            allowed = await consumer.is_allowed("https://example.com/any-path", "TestBot/1.0")
            assert allowed is True
