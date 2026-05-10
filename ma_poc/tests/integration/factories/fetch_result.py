"""Factory for FetchResult test objects.

Keeps construction of FetchResult (a frozen dataclass with many fields)
confined to one place. Tests express only the fields that matter for the
scenario they are verifying.
"""

from __future__ import annotations

from typing import Any

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode


def build_fetch_result(
    *,
    url: str = "https://example.com",
    outcome: FetchOutcome = FetchOutcome.OK,
    html: str | None = None,
    body: bytes | None = None,
    status: int | None = 200,
    headers: dict[str, str] | None = None,
    render_mode: RenderMode = RenderMode.GET,
    final_url: str | None = None,
    network_log: list[dict[str, Any]] | None = None,
    elapsed_ms: int = 1,
    attempts: int = 1,
    captcha_detected: bool = False,
    block_signature: str | None = None,
    error_signature: str | None = None,
    proxy_used: str | None = None,
    fetch_tier_used: int = 0,
    fetch_tier_attempts: list[int] | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Build a :class:`FetchResult` with caller-specified overrides.

    ``html`` is a convenience shortcut: when provided it is encoded to UTF-8
    and used as ``body``. Explicit ``body`` takes precedence over ``html``.

    Examples::

        # Minimal OK result with HTML body
        r = build_fetch_result(html="<html><body>Units</body></html>")

        # Bot-blocked result
        r = build_fetch_result(outcome=FetchOutcome.BOT_BLOCKED, status=403)

        # RENDER mode with network log
        r = build_fetch_result(
            render_mode=RenderMode.RENDER,
            network_log=[{"url": "https://example.com/api/units", "body": "[...]"}],
        )
    """
    if body is None and html is not None:
        body = html.encode("utf-8")

    return FetchResult(
        url=url,
        outcome=outcome,
        status=status,
        body=body,
        headers=headers or {},
        render_mode=render_mode,
        final_url=final_url or url,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        network_log=network_log or [],
        captcha_detected=captcha_detected,
        block_signature=block_signature,
        error_signature=error_signature,
        proxy_used=proxy_used,
        fetch_tier_used=fetch_tier_used,
        fetch_tier_attempts=fetch_tier_attempts or [],
        etag=etag,
        last_modified=last_modified,
    )
