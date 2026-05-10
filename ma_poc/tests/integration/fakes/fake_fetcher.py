"""FakeFetcher — protocol-conforming stub for fetch.fetcher.Fetcher.

Records every call so tests can assert on attempt order, retry counts, etc.
Scripted per URL: call responses_for(url, [FetchResult(...), ...]) before the
test, and the fake will pop them in order. When the queue is exhausted it
falls back to a configurable default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

if TYPE_CHECKING:
    from ma_poc.discovery.contracts import CrawlTask
    from ma_poc.models.scrape_profile import ScrapeProfile


def _default_ok(url: str) -> FetchResult:
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=b"",
        headers={},
        render_mode=RenderMode.GET,
        final_url=url,
        attempts=1,
        elapsed_ms=1,
    )


class FakeFetcher:
    """Scripted stand-in for :class:`ma_poc.fetch.fetcher.Fetcher`.

    Usage::

        fake_fetcher.responses_for("https://example.com/units", [
            build_fetch_result(html="<html>...</html>"),
        ])
        result = await fake_fetcher.fetch(task, profile=None)
        assert fake_fetcher.calls[0][0].url == "https://example.com/units"
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[FetchResult]] = {}
        self._calls: list[tuple[Any, Any]] = []  # (task, profile)
        self._default_factory = _default_ok

    # ------------------------------------------------------------------
    # Configuration API (call from test setup)
    # ------------------------------------------------------------------

    def responses_for(self, url: str, results: list[FetchResult]) -> "FakeFetcher":
        """Queue ordered FetchResults for *url*. Returns self for chaining."""
        self._queues[url].extend(results)
        return self

    def set_default(self, factory: Any) -> "FakeFetcher":
        """Override the fallback used when no scripted response is queued.

        *factory* must be callable as ``factory(url: str) -> FetchResult``.
        """
        self._default_factory = factory
        return self

    def reset(self) -> None:
        """Clear all queued responses and recorded calls."""
        self._queues.clear()
        self._calls.clear()

    # ------------------------------------------------------------------
    # Fetcher protocol
    # ------------------------------------------------------------------

    async def fetch(
        self,
        task: "CrawlTask",
        profile: "ScrapeProfile | None" = None,
    ) -> FetchResult:
        self._calls.append((task, profile))
        url = task.url
        queue = self._queues.get(url)
        if queue:
            result = queue.pop(0)
            if not queue:
                del self._queues[url]
            return result
        return self._default_factory(url)

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    @property
    def calls(self) -> list[tuple[Any, Any]]:
        """All (task, profile) pairs in call order."""
        return self._calls

    @property
    def attempted_urls(self) -> list[str]:
        """Ordered list of URLs requested via fetch()."""
        return [task.url for task, _ in self._calls]

    def call_count_for(self, url: str) -> int:
        return sum(1 for task, _ in self._calls if task.url == url)

    def assert_url_attempted(self, url: str) -> None:
        assert url in self.attempted_urls, (
            f"Expected {url!r} to be fetched, got: {self.attempted_urls}"
        )

    def assert_url_not_attempted(self, url: str) -> None:
        assert url not in self.attempted_urls, (
            f"Expected {url!r} NOT to be fetched, but it was"
        )
