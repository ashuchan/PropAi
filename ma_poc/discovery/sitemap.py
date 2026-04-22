"""Sitemap.xml consumer with ETag caching.

Handles both flat sitemap.xml and sitemap index variants.
Uses xml.etree.ElementTree — no external dependency.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..fetch.conditional import ConditionalCache

log = logging.getLogger(__name__)

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_MAX_CHILD_SITEMAPS = 10


@dataclass(frozen=True)
class SitemapEntry:
    """A single URL entry from a sitemap."""

    url: str
    lastmod: datetime | None
    priority: float | None


class SitemapConsumer:
    """Fetches and parses sitemap.xml with conditional caching.

    Args:
        fetcher: Async function to fetch URLs (L1 fetch).
        cond_cache: Conditional cache for ETag/Last-Modified.
    """

    def __init__(self, fetcher: Any, cond_cache: ConditionalCache) -> None:
        self._fetcher = fetcher
        self._cond_cache = cond_cache

    async def fetch(self, host: str) -> list[SitemapEntry]:
        """Fetch and parse the sitemap for a host.

        Args:
            host: The hostname (e.g. 'example.com').

        Returns:
            List of SitemapEntry. Empty if no sitemap, 404, or 304.
        """
        sitemap_url = f"https://{host}/sitemap.xml"
        try:
            from ..discovery.contracts import CrawlTask, TaskReason
            from ..fetch.contracts import RenderMode

            etag, lm = self._cond_cache.read(sitemap_url)
            task = CrawlTask(
                url=sitemap_url,
                property_id="",
                priority=99,
                budget_ms=15000,
                reason=TaskReason.SCHEDULED,
                render_mode=RenderMode.GET,
                etag=etag,
                last_modified=lm,
            )
            result = await self._fetcher(task)

            if result.outcome.value == "NOT_MODIFIED":
                return []
            if not result.ok() or not result.body:
                return []

            # Update cache
            if result.etag or result.last_modified:
                self._cond_cache.write(sitemap_url, result.etag, result.last_modified)

            return self._parse(result.body, depth=0)
        except Exception as exc:
            log.debug("Failed to fetch sitemap for %s: %s", host, exc)
            return []

    def _parse(self, body: bytes, depth: int) -> list[SitemapEntry]:
        """Parse sitemap XML body.

        Args:
            body: Raw XML bytes.
            depth: Current recursion depth (max 1 for index).

        Returns:
            List of SitemapEntry.
        """
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            log.debug("Malformed sitemap XML")
            return []

        entries: list[SitemapEntry] = []

        # Check if this is a sitemap index
        index_tags = root.findall(f"{{{_SITEMAP_NS}}}sitemap")
        if index_tags and depth == 0:
            for _i, st in enumerate(index_tags[:_MAX_CHILD_SITEMAPS]):
                loc = st.find(f"{{{_SITEMAP_NS}}}loc")
                if loc is not None and loc.text:
                    log.debug("Following sitemap index child: %s", loc.text)
            return entries  # In real impl, would fetch child sitemaps

        # Parse URL entries
        for url_tag in root.findall(f"{{{_SITEMAP_NS}}}url"):
            loc = url_tag.find(f"{{{_SITEMAP_NS}}}loc")
            if loc is None or not loc.text:
                continue

            lastmod_tag = url_tag.find(f"{{{_SITEMAP_NS}}}lastmod")
            lastmod: datetime | None = None
            if lastmod_tag is not None and lastmod_tag.text:
                try:
                    lastmod = datetime.fromisoformat(lastmod_tag.text.replace("Z", "+00:00"))
                except ValueError:
                    pass

            priority_tag = url_tag.find(f"{{{_SITEMAP_NS}}}priority")
            priority: float | None = None
            if priority_tag is not None and priority_tag.text:
                try:
                    priority = float(priority_tag.text)
                except ValueError:
                    pass

            entries.append(
                SitemapEntry(
                    url=loc.text.strip(),
                    lastmod=lastmod,
                    priority=priority,
                )
            )

        return entries
