"""Retry policy — decides whether and how to retry after a fetch outcome.

Pure logic. No sleeps — callers handle waiting. Tests pass a clock callable.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from .contracts import FetchOutcome

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryDecision:
    """Immutable decision about whether to retry a failed fetch."""

    should_retry: bool
    wait_ms: int
    rotate_identity: bool


class RetryPolicy:
    """Exponential backoff with jitter, identity/proxy rotation rules.

    Args:
        max_attempts: Maximum total attempts (including the first).
        base_ms: Base wait time in milliseconds for backoff calculation.
    """

    def __init__(self, max_attempts: int = 3, base_ms: int = 500) -> None:
        self._max_attempts = max_attempts
        self._base_ms = base_ms

    def decide(
        self,
        outcome: FetchOutcome,
        attempt: int,
        retry_after_header: str | None = None,
    ) -> RetryDecision:
        """Decide whether to retry based on the outcome and attempt count.

        Args:
            outcome: The FetchOutcome from the most recent attempt.
            attempt: The attempt number just completed (1-based).
            retry_after_header: Value of the Retry-After header, if present.

        Returns:
            A RetryDecision indicating what to do next.
        """
        # Never retry these
        if outcome in (FetchOutcome.OK, FetchOutcome.NOT_MODIFIED, FetchOutcome.HARD_FAIL):
            return RetryDecision(should_retry=False, wait_ms=0, rotate_identity=False)

        # Exhausted attempts
        if attempt >= self._max_attempts:
            return RetryDecision(should_retry=False, wait_ms=0, rotate_identity=False)

        if outcome == FetchOutcome.RATE_LIMITED:
            # Respect Retry-After if present
            wait_ms = self._parse_retry_after(retry_after_header)
            return RetryDecision(should_retry=True, wait_ms=wait_ms, rotate_identity=False)

        if outcome == FetchOutcome.BOT_BLOCKED:
            # Retry once with identity rotation
            if attempt >= 2:
                return RetryDecision(should_retry=False, wait_ms=0, rotate_identity=False)
            wait_ms = self._jittered_backoff(attempt)
            return RetryDecision(should_retry=True, wait_ms=wait_ms, rotate_identity=True)

        if outcome == FetchOutcome.PROXY_ERROR:
            # Retry up to 2 times with fresh proxy
            if attempt >= 3:
                return RetryDecision(should_retry=False, wait_ms=0, rotate_identity=False)
            wait_ms = self._jittered_backoff(attempt)
            return RetryDecision(should_retry=True, wait_ms=wait_ms, rotate_identity=True)

        # TRANSIENT — standard exponential backoff
        wait_ms = self._jittered_backoff(attempt)
        return RetryDecision(should_retry=True, wait_ms=wait_ms, rotate_identity=False)

    def _jittered_backoff(self, attempt: int) -> int:
        """Calculate backoff with ±25% jitter.

        Args:
            attempt: The attempt number (1-based).

        Returns:
            Wait time in milliseconds.
        """
        base = self._base_ms * (2 ** (attempt - 1))
        jitter = random.uniform(0.75, 1.25)
        return int(base * jitter)

    def _parse_retry_after(self, value: str | None) -> int:
        """Parse Retry-After header value to milliseconds.

        Args:
            value: The Retry-After header value (seconds).

        Returns:
            Wait time in milliseconds. Defaults to 5000ms if unparseable.
        """
        if value is None:
            return 5000
        try:
            seconds = int(value)
            return max(seconds * 1000, 1000)
        except ValueError:
            return 5000
