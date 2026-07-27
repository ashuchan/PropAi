"""A test-owned stub installed ABOVE function scope must survive the guard.

Separate file because it needs a module-scoped autouse fixture, which pytest
sets up before the guard's function-scoped one — the exact ordering that used
to make the guard clobber a legitimate stub and leave the author baffled.

The guard only replaces bindings still identical to the originals it captured
in ``pytest_configure``, so anything a test installed first is left alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.adapters import _probe as _probe_mod

SENTINEL = "module-scoped-stub-survived"


def _fake_probe_get(url: str, **kw: Any) -> str:
    return SENTINEL


@pytest.fixture(autouse=True, scope="module")
def _module_scoped_stub() -> Any:
    """Install a stub from module scope — i.e. BEFORE the guard runs."""
    real = _probe_mod.probe_get
    _probe_mod.probe_get = _fake_probe_get  # type: ignore[assignment]
    yield
    _probe_mod.probe_get = real  # type: ignore[assignment]


def test_module_scoped_stub_is_not_clobbered_by_the_guard() -> None:
    """The guard must not mistake a pre-installed stub for the real function."""
    assert _probe_mod.probe_get("https://example.invalid/") == SENTINEL


def test_still_blocked_for_functions_the_test_did_not_stub() -> None:
    """Stubbing one entry point does not switch the whole guard off."""
    from ma_poc.conftest import UnstubbedNetworkCall

    with pytest.raises(UnstubbedNetworkCall):
        _probe_mod.probe_post("https://example.invalid/", data={})
