"""Pin the b186b5b follow-up fixes to CamoufoxPool.

Two production bugs prevented ENABLE_CAMOUFOX=true from ever succeeding
on an L1 fetch (blockwall v2 Action 4):

1. ``new_context(os=...)`` — Playwright's BrowserContext rejects ``os``
   as an unexpected keyword argument → TypeError on every acquire(). The
   ``os`` setting belongs on the AsyncCamoufox() constructor, not on
   context options.

2. ``AsyncCamoufox.__aexit__(self._browser, ...)`` on close — the code
   stored the result of ``__aenter__`` (the browser) but passed it as
   the ``self`` argument of ``AsyncCamoufox.__aexit__``. The context-
   manager instance was never retained, so shutdown raised TypeError
   and the browser process leaked.

These are contract tests against the source — full integration would
require an installed Camoufox + Firefox binary which isn't available in
CI.
"""
from __future__ import annotations

from pathlib import Path


_CAMOUFOX_PATH = Path(__file__).resolve().parents[2] / "fetch" / "camoufox_pool.py"


def test_camoufox_new_context_does_not_pass_os_kwarg() -> None:
    """The ``os=`` kwarg was leaking into BrowserContext.new_context()
    where it raised TypeError on every L1 fetch with ENABLE_CAMOUFOX=true.
    Verify the context_opts dict no longer includes ``"os":`` in the
    acquire path."""
    src = _CAMOUFOX_PATH.read_text(encoding="utf-8")
    # The acquire path builds context_opts. Find the dict block and
    # assert "os" key is not present.
    acquire_marker = "async def acquire"
    ensure_marker = "async def _ensure_browser"
    a_idx = src.find(acquire_marker)
    e_idx = src.find(ensure_marker)
    assert a_idx >= 0 and e_idx > a_idx, "acquire/_ensure_browser not found"
    acquire_block = src[a_idx:e_idx]
    # Look specifically inside the context_opts assignment.
    assert '"os":' not in acquire_block, (
        'CamoufoxPool.acquire() context_opts still passes "os" to '
        "new_context() — this raises TypeError. The os setting belongs "
        "on AsyncCamoufox(os=...) in _ensure_browser, not on "
        "BrowserContext.new_context()."
    )


def test_camoufox_retains_context_manager_for_close() -> None:
    """close() needs the AsyncCamoufox() *instance* to call __aexit__
    on, not the browser object returned by __aenter__. Verify the
    ``_cf_ctx`` attribute is set in _ensure_browser and used in close."""
    src = _CAMOUFOX_PATH.read_text(encoding="utf-8")
    assert "self._cf_ctx" in src, (
        "CamoufoxPool must retain the AsyncCamoufox context-manager "
        "instance as self._cf_ctx so close() can call its __aexit__."
    )
    # Confirm close() uses _cf_ctx, not _browser
    close_marker = "async def close"
    c_idx = src.find(close_marker)
    assert c_idx >= 0
    close_block = src[c_idx:c_idx + 1000]
    # Either pattern is acceptable (`await self._cf_ctx.__aexit__(...)`)
    # but the old buggy pattern (`AsyncCamoufox.__aexit__(self._browser, ...)`)
    # must NOT appear anywhere in the file.
    assert "self._cf_ctx.__aexit__" in close_block, (
        "close() must call self._cf_ctx.__aexit__(...) — the context-"
        "manager pattern. Without this the Firefox process leaks."
    )
    assert "AsyncCamoufox.__aexit__(self._browser" not in src, (
        "Old buggy close() pattern still present — passes the browser "
        "as the `self` arg of AsyncCamoufox.__aexit__, which is a "
        "TypeError waiting to happen."
    )


def test_camoufox_pool_module_imports_cleanly() -> None:
    """The module must import without side effects even when camoufox
    is not installed (it has a graceful fallback). Catches typos that
    only surface on import."""
    import importlib
    import ma_poc.fetch.camoufox_pool as cam
    importlib.reload(cam)
    # Public API still in place.
    assert hasattr(cam, "CamoufoxPool")
    assert hasattr(cam, "get_browser_pool")
    assert hasattr(cam, "is_available")
    assert hasattr(cam, "ENABLE_CAMOUFOX")
