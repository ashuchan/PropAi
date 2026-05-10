"""Centralised playwright / patchright sys.modules stub.

Every integration test that imports any pms.adapters code transitively pulls
in playwright references. Rather than each test file doing ad-hoc
``sys.modules["playwright"] = MagicMock()`` injection (the pattern spread
across the legacy test_pr* files), the autouse fixture in
tests/integration/conftest.py injects these stubs via monkeypatch so they
are automatically reversed after each test.

``PLAYWRIGHT_STUB_MODULES`` lists every module name that must be stubbed.
Add entries here when new playwright sub-imports appear in the production code.
"""

from __future__ import annotations

import sys
import types

PLAYWRIGHT_STUB_MODULES: tuple[str, ...] = (
    "playwright",
    "playwright.async_api",
    "playwright.sync_api",
    "patchright",
    "patchright.async_api",
    "patchright.sync_api",
)


def build_playwright_stub(name: str) -> types.ModuleType:
    """Build a minimal module stub that satisfies import-time attribute access."""
    mod = types.ModuleType(name)
    # Attributes referenced by type annotations in production code:
    mod.BrowserContext = object  # type: ignore[attr-defined]
    mod.Page = object  # type: ignore[attr-defined]
    mod.async_playwright = object  # type: ignore[attr-defined]
    mod.sync_playwright = object  # type: ignore[attr-defined]
    mod.Browser = object  # type: ignore[attr-defined]
    return mod


def inject_playwright_stubs() -> dict[str, types.ModuleType | None]:
    """Inject stubs into sys.modules; return previous values for teardown.

    Preferred path: use the ``patch_playwright_modules`` pytest fixture from
    conftest.py — it handles teardown automatically via monkeypatch.
    Only call this directly when you need module-level injection (e.g., in
    legacy test files that patch at import time).
    """
    previous: dict[str, types.ModuleType | None] = {}
    for name in PLAYWRIGHT_STUB_MODULES:
        previous[name] = sys.modules.get(name)
        if name not in sys.modules:
            sys.modules[name] = build_playwright_stub(name)
    return previous


def remove_playwright_stubs(previous: dict[str, types.ModuleType | None]) -> None:
    """Restore sys.modules to the state captured by inject_playwright_stubs."""
    for name, orig in previous.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
