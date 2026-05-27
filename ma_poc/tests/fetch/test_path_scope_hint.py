"""Path-scope clearance hint plumbing — blockwall v2 Action 3.

The 300-property A/B (investigations/2026-05-21-t3-grind/artifacts/
blockwall_v2/STRATEGY.md) found 83 properties where the homepage is
NOT CF-protected but ``/conventional/`` (or another deep target path)
IS. Patchright mints ``cf_clearance`` scoped to ``/`` on the homepage
goto, but CF bot-fight scopes clearance per-path, so the cookie is
useless when the curl_cffi adapter probes ``/conventional/``.

Fix: ``fetch()`` derives a ``path_scope_hint`` from
``profile.navigation.winning_page_url``, threads it through
``_do_request`` → ``_do_render``. After the primary ``page.goto(homepage)``
captures the body, ``_do_render`` does a best-effort secondary
``page.goto(path_scope_hint)`` to mint the path-scoped clearance, which
``_harvest_clearance_cookies`` then captures.

The full integration requires Playwright + a real CF-protected target,
so these are contract tests: they pin the parameter plumbing + the
guard conditions, so a future refactor that drops a step fails loudly.
"""
from __future__ import annotations

import re
from pathlib import Path


_FETCHER_PATH = Path(__file__).resolve().parents[2] / "fetch" / "fetcher.py"


def _src() -> str:
    return _FETCHER_PATH.read_text(encoding="utf-8")


def test_do_request_accepts_path_scope_hint() -> None:
    """The kwarg must exist on _do_request — otherwise the threading
    from fetch() breaks at call-time."""
    src = _src()
    assert re.search(
        r"async def _do_request\([^)]*path_scope_hint:\s*str\s*\|\s*None\s*=\s*None",
        src,
        re.DOTALL,
    ), "_do_request is missing the path_scope_hint kwarg"


def test_do_render_accepts_path_scope_hint() -> None:
    """Same contract on _do_render — the actual consumer of the hint."""
    src = _src()
    assert re.search(
        r"async def _do_render\([^)]*path_scope_hint:\s*str\s*\|\s*None\s*=\s*None",
        src,
        re.DOTALL,
    ), "_do_render is missing the path_scope_hint kwarg"


def test_fetch_passes_path_scope_hint_into_do_request() -> None:
    """The fetch() retry loop must forward the hint into _do_request,
    otherwise the kwarg is dead code."""
    src = _src()
    assert "path_scope_hint=path_scope_hint" in src, (
        "fetch() doesn't forward path_scope_hint into _do_request — "
        "the plumbing is broken end-to-end."
    )


def test_path_scope_hint_only_set_for_render_mode() -> None:
    """RENDER is the only mode that runs Playwright and can mint CF
    clearance via a secondary goto. HEAD/GET have no browser context
    so the hint would be wasted. The fetch() guard must check the
    render_mode before deriving the hint."""
    src = _src()
    # Find the path_scope_hint derivation block in fetch()
    match = re.search(
        r"path_scope_hint:\s*str\s*\|\s*None\s*=\s*None\s*\n\s*if\s*\(([^)]+)\)",
        src,
        re.DOTALL,
    )
    assert match is not None, "Couldn't find the path_scope_hint guard block"
    guard = match.group(1)
    assert "RenderMode.RENDER" in guard, (
        "path_scope_hint guard must check task.render_mode == RenderMode.RENDER"
    )
    assert "profile is not None" in guard, (
        "path_scope_hint guard must verify profile is not None"
    )


def test_secondary_goto_runs_after_body_capture() -> None:
    """Critical invariant: the chain-navigation must happen AFTER
    body_text is encoded so the L1 result still reflects the homepage
    (detector signals, JSON-LD, PMS fingerprint all run on the
    homepage body). If the order flips, the L1 result becomes the
    target page content which breaks the rest of the pipeline."""
    src = _src()
    body_encode_idx = src.find('body = body_text.encode("utf-8", errors="replace")')
    secondary_goto_idx = src.find("if (\n                path_scope_hint")
    assert body_encode_idx > 0 and secondary_goto_idx > 0
    assert body_encode_idx < secondary_goto_idx, (
        "Secondary path-scope goto must run AFTER body is encoded so "
        "the L1 FetchResult reflects the homepage content, not the "
        "target page."
    )


def test_secondary_goto_runs_before_clearance_harvest() -> None:
    """The whole point of the secondary goto is to mint additional
    path-scoped clearance cookies BEFORE _harvest_clearance_cookies
    runs. If the harvest happens first, we don't get the target's
    clearance."""
    src = _src()
    secondary_goto_idx = src.find("if (\n                path_scope_hint")
    harvest_idx = src.find("clearance_cookies = await _harvest_clearance_cookies(page)")
    assert secondary_goto_idx > 0 and harvest_idx > 0
    assert secondary_goto_idx < harvest_idx, (
        "Secondary path-scope goto must run BEFORE clearance harvest, "
        "otherwise the path-scoped cf_clearance never makes it into "
        "the harvested cookie set."
    )


def test_secondary_goto_is_bounded_and_silent() -> None:
    """Two safety invariants: (a) the secondary goto MUST be time-
    bounded so a slow target can't extend the L1 fetch indefinitely;
    (b) failure MUST NOT propagate — the L1 body is already captured
    and an exception here would forfeit the whole fetch."""
    src = _src()
    block_start = src.find("if (\n                path_scope_hint")
    assert block_start > 0
    block = src[block_start:block_start + 2000]
    assert "asyncio.wait_for" in block, (
        "Secondary goto must be wrapped in asyncio.wait_for so a slow "
        "CF challenge can't extend the L1 fetch beyond budget."
    )
    assert "except Exception:" in block and "pass" in block, (
        "Secondary goto failure must be silently swallowed — the L1 "
        "body is already captured by this point."
    )


def test_path_scope_hint_only_fires_on_clean_primary_response() -> None:
    """If the primary goto failed (nav_exc) or returned a 4xx/5xx, the
    page state is unreliable and chasing a secondary URL wastes budget.
    The guard must require both nav_exc is None AND a 2xx/3xx primary
    response."""
    src = _src()
    block_start = src.find("if (\n                path_scope_hint")
    assert block_start > 0
    block = src[block_start:block_start + 600]
    assert "nav_exc is None" in block, (
        "Secondary goto must skip when primary goto raised (nav_exc set)."
    )
    assert "resp is not None" in block, (
        "Secondary goto must check the primary response exists."
    )
    assert "200 <=" in block, (
        "Secondary goto must require primary status be in the 2xx/3xx "
        "range — chasing clearance on a 4xx homepage is meaningless."
    )
