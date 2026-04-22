"""robots.txt consumer with in-memory TTL cache.

Uses urllib.robotparser. On fetch failure, defaults to allow.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


class RobotsConsumer:
    """Checks robots.txt rules with per-host caching.

    Args:
        cache_ttl_hours: How long to cache robots.txt per host.
    """

    def __init__(self, cache_ttl_hours: int = 24) -> None:
        self._cache_ttl_seconds = cache_ttl_hours * 3600
        self._cache: dict[str, tuple[urllib.robotparser.RobotFileParser, float]] = {}

    async def is_allowed(self, url: str, user_agent: str) -> bool:
        """Check whether the URL is allowed by robots.txt.

        Args:
            url: The URL to check.
            user_agent: The User-Agent string.

        Returns:
            True if allowed or robots.txt is unavailable.
        """
        parser = await self._get_parser(url)
        if parser is None:
            return True
        return parser.can_fetch(user_agent, url)

    async def crawl_delay(self, host: str, user_agent: str) -> float | None:
        """Get the Crawl-delay for a host, if specified.

        Args:
            host: The hostname.
            user_agent: The User-Agent string.

        Returns:
            Delay in seconds, or None if not specified.
        """
        url = f"https://{host}/"
        parser = await self._get_parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(user_agent)
        if delay is not None:
            return float(delay)
        return None

    async def _get_parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and cache the robots.txt for a URL's host.

        Returns None on fetch failure (default: allow).
        """
        parsed = urlparse(url)
        host = parsed.netloc
        robots_url = f"{parsed.scheme}://{host}/robots.txt"

        # Check cache
        if host in self._cache:
            parser, cached_at = self._cache[host]
            if time.monotonic() - cached_at < self._cache_ttl_seconds:
                return parser

        # Fetch robots.txt
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(robots_url)
                if resp.status_code == 404:
                    # No robots.txt = allow all
                    self._cache[host] = (None, time.monotonic())  # type: ignore[assignment]
                    return None
                resp.raise_for_status()
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
                self._cache[host] = (parser, time.monotonic())
                return parser
        except Exception as exc:
            log.debug("Failed to fetch robots.txt for %s: %s", host, exc)
            return None
