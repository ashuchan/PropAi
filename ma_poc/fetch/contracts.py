"""Shared contracts for L1 — Fetch layer.

FetchResult is the single output contract that crosses the L1/L2 boundary.
It is never raised as an exception. L1 catches all transient and hard errors
and returns a FetchResult with the appropriate outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Re-exported so fetch-internal modules (e.g. http_client) can import FetchTier
# from here without a circular dependency through ma_poc.models.
from ma_poc.models.fetch_tier import FetchTier as FetchTier  # noqa: F401


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
    HARD_FAIL = "HARD_FAIL"  # SSL, 4xx non-retriable other than dead-URL
    PROXY_ERROR = "PROXY_ERROR"  # 407, proxy exhausted
    #: Stage 3 (2026-05-12): the URL is **dead** — 404/410/451 final status
    #: or NXDOMAIN. Terminal — never retry. The site has explicitly
    #: declared the resource gone (404/410), legally unavailable (451), or
    #: the DNS authority denies the name (NXDOMAIN). Distinct from
    #: ``HARD_FAIL`` so reporting can exclude these from the success-rate
    #: denominator (counting them is unfair — we never had a chance to
    #: extract from them) and route them to a re-discovery queue.
    #: See docs/2026_05_11_regressions_fix_design.md (Stage 3).
    DEAD_URL = "DEAD_URL"
    #: RC5: HTTP 200 response whose body is empty or below the meaningful-
    #: content threshold (< 16 bytes). Distinct from TRANSIENT (which means
    #: retriable server error) — an empty-body 200 should not be retried with
    #: the same parameters because the server intentionally returned nothing.
    #: Routes to a ``FAILED_FETCH_EMPTY`` verdict in scraper.py so dashboards
    #: can distinguish bot-wall blank pages from real server errors.
    EMPTY_BODY = "EMPTY_BODY"
    #: RC-A (2026-05-15 PM): the per-property `asyncio.wait_for` deadline at
    #: scripts/runners/jugnu.py:_process_property fired while the fetcher was
    #: parked inside Playwright IPC (page.goto, page.content, the 20s CF
    #: auto-solve sleep at fetcher.py:821, or a wedged `context.close()`).
    #: Pre-fix the `except Exception` at fetcher.py:256 did not catch
    #: asyncio.CancelledError (a BaseException since 3.8), so the
    #: `FETCH_COMPLETED` emit at fetcher.py:295 was skipped entirely — the
    #: PID showed only `fetch.started` with no completion event and the
    #: shard wallclock-killed the whole worker after 30+ minutes (cloud run
    #: 2026-05-15 shard 64: 29 of 50 PIDs orphan-killed this way). The
    #: try/finally added 2026-05-15 PM now emits this outcome on the path
    #: out so the analyzer sees the property's death cause instead of
    #: silence. See data/reports/cloud_run_2026-05-15/TRIAGE.md RC-A.
    CANCELLED = "CANCELLED"


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
    # Fetch-tier escalation fields (Phase E1+)
    fetch_tier_used: int = 0  # FetchTier value — int avoids circular import
    fetch_tier_attempts: list[int] = field(default_factory=list)
    block_signature: str | None = None
    # F1.2 (2026-05-08 plan): True when ``looks_like_captcha`` matched the
    # body the fetcher salvaged. Populated by Fetcher.fetch around line 264
    # via dataclasses.replace so the orchestrator's rescue gate at
    # pms/scraper.py can short-circuit instead of feeding interstitial HTML
    # into the LLM. Default False so all pre-F1.2 callers stay valid.
    captcha_detected: bool = False
    # Cookie-mint reuse (option b, 2026-05-18). Subset of cookies harvested
    # from the post-render Playwright context whose names match the
    # bot-wall clearance set (cf_clearance, __cf_bm, datadome, __ddg*,
    # incap_ses*, visid_incap*, nlbi_*). The orchestrator installs these
    # via ``ma_poc.pms.adapters._probe.set_clearance_cookies`` for the
    # duration of one property's adapter dispatch so the cheap curl_cffi
    # probes auto-attach them and skip re-solving the wall. Empty by
    # default ⇒ no behaviour change off the cookie-mint path.
    clearance_cookies: dict[str, str] = field(default_factory=dict)
    # 2026-05-24 — set to True by fetcher when the OK outcome was
    # achieved AFTER at least one L1 proxy escalation hop (i.e. the
    # direct attempt failed and the BrightData / proxy-pool retry
    # recovered). Read by ``services.profile_updater.update_profile_after_extraction``
    # to call ``proxy_gate.mark_host_needs_proxy`` so the NEXT run
    # picks the proxy on the first attempt instead of burning a direct
    # attempt + escalation. Default False ⇒ pre-2026-05-24 callers
    # see no behaviour change.
    recovered_via_proxy: bool = False

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
