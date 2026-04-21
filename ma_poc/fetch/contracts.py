"""Shared contracts for L1 — Fetch layer.

FetchResult is the single output contract that crosses the L1/L2 boundary.
It is never raised as an exception. L1 catches all transient and hard errors
and returns a FetchResult with the appropriate outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RenderMode(StrEnum):
    """How the URL should be fetched."""

    HEAD = "HEAD"  # cheap change probe
    GET = "GET"  # static HTML / JSON
    RENDER = "RENDER"  # Playwright with network capture


class FetchOutcome(StrEnum):
    """Outcome classification for a fetch attempt."""

    OK = "OK"  # 2xx, body available
    NOT_MODIFIED = "NOT_MODIFIED"  # 304, use carry-forward
    BOT_BLOCKED = "BOT_BLOCKED"  # CAPTCHA / 403 pattern
    RATE_LIMITED = "RATE_LIMITED"  # 429 with Retry-After
    TRANSIENT = "TRANSIENT"  # 5xx, timeout, retriable
    HARD_FAIL = "HARD_FAIL"  # SSL, DNS, 4xx non-retriable
    PROXY_ERROR = "PROXY_ERROR"  # 407, proxy exhausted


@dataclass(slots=True, frozen=True)
class FetchResult:
    """Immutable result of a single fetch operation.

    Passed from L1 to L2/L3. Never raised as an exception.
    """

    url: str
    outcome: FetchOutcome
    status: int | None  # HTTP status (None if no response)
    body: bytes | None  # Raw body; None for HEAD or failures
    headers: dict[str, str]  # Lowercased header names
    render_mode: RenderMode
    final_url: str  # After redirects
    attempts: int  # Total attempts made (>=1)
    elapsed_ms: int
    # Present only when render_mode == RENDER
    network_log: list[dict[str, Any]] = field(default_factory=list)
    # Populated by the conditional-GET layer
    etag: str | None = None
    last_modified: str | None = None
    # Populated by response_classifier on retriable outcomes
    error_signature: str | None = None
    proxy_used: str | None = None

    def ok(self) -> bool:
        """True when fetch succeeded with a 2xx response."""
        return self.outcome == FetchOutcome.OK

    def should_carry_forward(self) -> bool:
        """True when the caller should reuse prior data instead of re-extracting."""
        return self.outcome in (
            FetchOutcome.NOT_MODIFIED,
            FetchOutcome.TRANSIENT,
            FetchOutcome.BOT_BLOCKED,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for event emission."""
        return {
            "url": self.url,
            "outcome": self.outcome.value,
            "status": self.status,
            "headers": self.headers,
            "render_mode": self.render_mode.value,
            "final_url": self.final_url,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "network_log_count": len(self.network_log),
            "etag": self.etag,
            "last_modified": self.last_modified,
            "error_signature": self.error_signature,
            "proxy_used": self.proxy_used,
        }
