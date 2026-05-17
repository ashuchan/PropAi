"""Tests for ``choose_hop_render_mode`` — the helper that picks the
render mode for a link-hop sub-task based on the parent task's mode.

Shard_10 fix (2026-05-17). Prior to this helper, ``_try_link_hop`` always
created sub-tasks with ``RenderMode.RENDER``, which meant a wedge-rescue
retry running in HTTP-only GET mode would still hop via Playwright. PID
19154 (conwayclub.com) trace showed two consecutive 600s Chromium wedges
that way — entry then hop — driving the per-property wallclock to 63 min.

The helper centralises the decision so the LINK_HOP_FETCHED telemetry
records the actual mode used, and so the rule is unit-testable without
spinning up the full scraper pipeline.

**Rule (post v2):** GET only when the parent task explicitly used GET.
RENDER in every other case — including parents with no shared
``browser_page``. The ``browser_page is None`` heuristic was tested in
the first iteration of this helper and rejected: in production
``scripts/runners/jugnu.py:1113`` passes ``page=None`` to ``scrape_jugnu``
even for RENDER entries, so reading "browser_page is None" as "parent
was HTTP-only" downgrades EVERY hop. Observed locally on PID 19530
brazosthehampton.com — every hop went BOT_BLOCKED in <1s because the
WAF rejects plain GET but lets Playwright stealth through.
"""

from __future__ import annotations

from ma_poc.fetch.contracts import RenderMode
from ma_poc.pms.scraper import choose_hop_render_mode


class TestParentRenderModeEnum:
    def test_parent_get_returns_get(self) -> None:
        """Wedge-rescue case: parent ran in GET, hop must also use GET."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.GET, browser_page=None
            )
            is RenderMode.GET
        )

    def test_parent_get_with_shared_page_still_returns_get(self) -> None:
        """Defensive: if the caller passes a browser_page alongside a GET
        parent (shouldn't happen, but doesn't hurt), GET still wins."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.GET, browser_page=object()
            )
            is RenderMode.GET
        )

    def test_parent_render_with_browser_page_returns_render(self) -> None:
        """Normal path with shared Playwright page → RENDER."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.RENDER, browser_page=object()
            )
            is RenderMode.RENDER
        )

    def test_parent_render_without_browser_page_still_returns_render(self) -> None:
        """Regression guard for the v1 bug. In production
        ``jugnu.py:1113`` always passes ``page=None`` even for RENDER
        entries, so the absence of ``browser_page`` is NOT evidence that
        the parent ran in HTTP-only mode. Hopping in RENDER is correct
        here; the WAF on properties like brazosthehampton.com blocks
        plain GET but lets Playwright stealth through."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.RENDER, browser_page=None
            )
            is RenderMode.RENDER
        )

    def test_parent_head_returns_render(self) -> None:
        """HEAD is a conditional-cache check at L2; never reaches the hop
        in practice. If somehow it does, default to RENDER (the legacy
        default) — not GET, which would risk WAF rejection."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.HEAD, browser_page=object()
            )
            is RenderMode.RENDER
        )


class TestParentRenderModeString:
    def test_parent_get_string_returns_get(self) -> None:
        """String forms must be recognised — e.g. when a CrawlTask is
        serialised across worker boundaries the mode comes back as a
        string."""
        assert (
            choose_hop_render_mode(parent_render_mode="GET", browser_page=None)
            is RenderMode.GET
        )

    def test_parent_get_string_case_insensitive(self) -> None:
        for s in ("get", "Get", "gEt"):
            assert (
                choose_hop_render_mode(parent_render_mode=s, browser_page=None)
                is RenderMode.GET
            )

    def test_parent_render_string_returns_render(self) -> None:
        assert (
            choose_hop_render_mode(
                parent_render_mode="RENDER", browser_page=None
            )
            is RenderMode.RENDER
        )

    def test_unknown_string_returns_render(self) -> None:
        """Unknown / garbage strings preserve the legacy default
        (RENDER). The narrow-cast rule is "GET only when explicitly
        GET" — anything else is treated as conservative RENDER."""
        assert (
            choose_hop_render_mode(
                parent_render_mode="banana", browser_page=None
            )
            is RenderMode.RENDER
        )


class TestParentRenderModeNone:
    def test_none_returns_render(self) -> None:
        """When the parent mode is unknown (older callers, missing
        attribute, etc.) default to RENDER — preserves legacy behaviour
        and matches the WAF-bypass requirement for typical hops."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=None, browser_page=object()
            )
            is RenderMode.RENDER
        )

    def test_none_without_browser_page_still_returns_render(self) -> None:
        """The combination "no parent mode AND no browser_page" is the
        production default in today's runner (``page=None`` always).
        It must NOT trigger a GET downgrade or every hop loses its
        Playwright stealth — that was the bug discovered on the
        post-fix canary against brazosthehampton.com."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=None, browser_page=None
            )
            is RenderMode.RENDER
        )


class TestProductionScenarios:
    """Compact scenarios that mirror real runner invocations."""

    def test_main_pass_render_runner_passes_page_none(self) -> None:
        """``scripts/runners/jugnu.py:1110`` invokes ``scrape_jugnu`` with
        ``page=None`` even when the entry fetch was RENDER. Hop must
        still be RENDER."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.RENDER, browser_page=None
            )
            is RenderMode.RENDER
        )

    def test_wedge_rescue_retry_path(self) -> None:
        """``scripts/runners/jugnu.py:596`` builds a ``CrawlTask`` with
        ``render_mode=RenderMode.GET`` for the rescue. Hop must inherit
        GET so it stays HTTP-only — that was the original shard_10 bug
        on PID 19154 (conwayclub.com double-wedge)."""
        assert (
            choose_hop_render_mode(
                parent_render_mode=RenderMode.GET, browser_page=None
            )
            is RenderMode.GET
        )
