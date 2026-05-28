"""Host-blocklist route handler (2026-05-28).

The 2026-05-27 jugnu-c612-may27 canary surfaced 77 per-property 600s
timeouts where direct curl_cffi fetched the site body in <1s. Pattern
analysis attributed ~45 of the 77 to two third-party bundle classes:
  • Elise AI virtual-leasing-agent (26 props)  — persistent WebSockets
    keep Playwright's network channel busy forever
  • G5 marketing analytics (19 props)          — heavy mutation observers

This test pins the host blocklist that aborts those bundles at the
Playwright route layer. Coverage is intentionally narrow: the blocklist
is short and conservative — false positives can break legitimate SSR
extraction on operator sites that happen to embed marketing pixels.
"""
from __future__ import annotations

import pytest

from ma_poc.fetch.browser_pool import _BLOCKED_HOST_SUFFIXES, _host_is_blocked


# ─── Pure host-matcher unit tests ────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    # ─── BLOCK: confirmed time-sink hosts (c612 canary evidence) ─
    ("https://meetelise.com/chat.js", True),
    ("https://app.meetelise.com/api/v1/conversation", True),
    ("https://cdn.meetelise.com/bundle.js", True),
    ("https://www.g5search.com/dxc-bundle.js", True),
    ("https://cdn.g5dxcdn.com/static/main.js", True),
    ("https://www.googletagmanager.com/gtm.js?id=GTM-XXXX", True),
    ("https://stats.g.doubleclick.net/dc.js", True),
    ("https://static.hotjar.com/c/hotjar-1234567.js", True),
    # ─── PASS: operator's own host must never be blocked ─────────
    ("https://www.crossingsmadison.com/", False),
    ("https://liveatlumina.com/floor-plans", False),
    ("https://api.cynthiagardens.appfolio.com/listings", False),
    ("https://sightmap.com/embed/abc123", False),  # NOT blocked
    ("https://embed.leaseleads.co/uuid/floor-plans", False),  # NOT blocked
    # ─── PASS: empty / malformed URLs degrade gracefully ─────────
    ("", False),
    ("not-a-url", False),
    ("javascript:void(0)", False),
])
def test_host_is_blocked_matrix(url: str, expected: bool) -> None:
    assert _host_is_blocked(url) is expected


def test_host_match_is_suffix_not_substring() -> None:
    """``meetelise.com.evil.com`` must NOT match — anchor on netloc end."""
    # When parsed, the netloc is exactly the suffix-attacker host. We
    # match suffix on dotted boundaries, so attack hosts don't bypass:
    assert _host_is_blocked("https://meetelise.com.evil.example/x") is False
    # And the legitimate suffix DOES match
    assert _host_is_blocked("https://something.meetelise.com/x") is True


def test_blocklist_membership_is_curated_not_speculative() -> None:
    """Sentinel test: don't grow the blocklist unbounded. New entries
    must come from documented canary evidence + a comment in the
    module-level docstring."""
    # Cap at 20 — current is 12. If this trips, audit + bump
    # intentionally with a memo line citing the evidence.
    assert len(_BLOCKED_HOST_SUFFIXES) <= 20, (
        f"blocklist grew to {len(_BLOCKED_HOST_SUFFIXES)}; "
        "audit for false-positive risk before bumping the cap"
    )
    # Anchor checks — these MUST stay in the list (they caused the
    # 2026-05-27 timeout regression). If anyone removes them without
    # evidence the regression is gone, this test catches it.
    assert "meetelise.com" in _BLOCKED_HOST_SUFFIXES
    assert "g5search.com" in _BLOCKED_HOST_SUFFIXES


# ─── Route-handler integration test (mock route object) ──────────────


class _FakeRequest:
    def __init__(self, url: str, resource_type: str = "script") -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeRoute:
    """Minimal mock of Playwright's Route that records abort/continue calls."""
    def __init__(self, req: _FakeRequest) -> None:
        self.request = req
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


@pytest.mark.asyncio
async def test_route_aborts_blocked_host_even_when_resource_type_passes() -> None:
    """xhr/fetch requests are NOT in the resource-type blocklist (only
    image/font/media are). The host check must still abort them when
    they target a known time-sink host."""
    from ma_poc.fetch.browser_pool import _resource_block_route

    route = _FakeRoute(_FakeRequest(
        "https://app.meetelise.com/api/v1/chat", resource_type="xhr"
    ))
    await _resource_block_route(route)
    assert route.aborted is True
    assert route.continued is False


@pytest.mark.asyncio
async def test_route_continues_operator_host() -> None:
    from ma_poc.fetch.browser_pool import _resource_block_route

    route = _FakeRoute(_FakeRequest(
        "https://liveatlumina.com/floor-plans", resource_type="document"
    ))
    await _resource_block_route(route)
    assert route.continued is True
    assert route.aborted is False


@pytest.mark.asyncio
async def test_route_aborts_blocked_resource_type_on_operator_host() -> None:
    """Existing resource-type behavior still works: an `image` request
    to the operator's own host gets aborted regardless of host."""
    from ma_poc.fetch.browser_pool import (
        _BLOCKED_RESOURCE_TYPES, _resource_block_route,
    )
    if "image" not in _BLOCKED_RESOURCE_TYPES:
        pytest.skip("image not in blocked resource types in this env")
    route = _FakeRoute(_FakeRequest(
        "https://liveatlumina.com/hero.jpg", resource_type="image"
    ))
    await _resource_block_route(route)
    assert route.aborted is True


@pytest.mark.asyncio
async def test_route_never_raises_on_torn_down_context() -> None:
    """If route.abort()/continue_() raise (e.g. context already
    closed), the handler must swallow — never propagate or the
    navigation fails."""
    from ma_poc.fetch.browser_pool import _resource_block_route

    class _ExplodingRoute:
        def __init__(self) -> None:
            self.request = _FakeRequest("https://meetelise.com/x")

        async def abort(self) -> None:
            raise RuntimeError("context torn down")

        async def continue_(self) -> None:
            raise RuntimeError("context torn down")

    # Must not raise
    await _resource_block_route(_ExplodingRoute())
