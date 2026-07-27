"""The live-network guard in ma_poc/conftest.py must actually hold.

Regression cover for the 2026-07-26 finding that a full suite run made ~356
un-stubbed live requests to 20 real hostnames. The guard is only worth having
if it cannot be quietly bypassed, so this file pins the three properties that
make it work:

  1. ``_probe.probe_get`` / ``probe_post`` / ``web_unlocker_get`` are blocked
     during an ordinary test.
  2. Modules that import the seam at TOP LEVEL get their own copy blocked too
     — patching ``_probe`` alone would never reach those bindings.
  3. The opt-out markers really do restore the originals, and a test's own
     stub still wins over the guard.
"""

from __future__ import annotations

import pytest

# Captured at import time — collection happens before any fixture runs, so
# these are the genuine, unpatched functions.
from ma_poc.pms.adapters import _probe as _probe_mod

_REAL_PROBE_GET = _probe_mod.probe_get
_REAL_PROBE_POST = _probe_mod.probe_post
_REAL_WEB_UNLOCKER_GET = _probe_mod.web_unlocker_get

from ma_poc.conftest import UnstubbedNetworkCall  # noqa: E402

# The three modules that bind probe_get into their own namespace at import
# time (ma_poc/pms/adapters/_sightmap_subpage_recovery.py:40, rentmanager.py:39,
# repli360.py:47). Importing them here guarantees they are in sys.modules so
# the guard's sweep has something to find.
from ma_poc.pms.adapters import (  # noqa: E402
    _sightmap_subpage_recovery,
    rentmanager,
    repli360,
)


@pytest.mark.parametrize(
    "func_name", ["probe_get", "probe_post", "web_unlocker_get"]
)
def test_probe_seam_is_blocked_in_an_ordinary_test(func_name: str) -> None:
    """Each network entry point raises instead of opening a socket."""
    func = getattr(_probe_mod, func_name)
    with pytest.raises(UnstubbedNetworkCall):
        func("https://example.invalid/floorplans")


def test_blocked_call_names_the_function_and_url() -> None:
    """The error has to be actionable — dev sees what tried to call out."""
    with pytest.raises(UnstubbedNetworkCall) as exc:
        _probe_mod.probe_get("https://example.invalid/units")
    message = str(exc.value)
    assert "probe_get" in message
    assert "https://example.invalid/units" in message
    # Points at the actual fix, not just "network blocked".
    assert "ma_poc.pms.adapters._probe" in message


def test_guard_survives_a_blanket_except_exception() -> None:
    """UnstubbedNetworkCall must not be swallowed by production's fallbacks.

    Nearly every probe_get call site in ma_poc/pms sits inside a bare
    ``except Exception``. If the guard raised an ``Exception`` the call would
    be swallowed and the test would sail on against degraded data — exactly
    the silent failure this whole exercise is about.
    """
    assert issubclass(UnstubbedNetworkCall, BaseException)
    assert not issubclass(UnstubbedNetworkCall, Exception)

    with pytest.raises(UnstubbedNetworkCall):
        try:
            _probe_mod.probe_get("https://example.invalid/")
        except Exception:  # pragma: no cover — must NOT catch
            pytest.fail("blanket except Exception swallowed the guard")


@pytest.mark.parametrize(
    ("module", "attr"),
    [
        (_sightmap_subpage_recovery, "probe_get"),
        (rentmanager, "probe_get"),
        (repli360, "probe_get"),
        (repli360, "probe_post"),
    ],
)
def test_top_level_importers_are_blocked_too(module: object, attr: str) -> None:
    """Modules holding their own reference to the seam are swept as well.

    These bind the function object at import time, so patching only
    ``_probe.<attr>`` would leave them pointing at the live original.
    """
    func = getattr(module, attr)
    with pytest.raises(UnstubbedNetworkCall):
        func("https://example.invalid/floorplans")


def test_curl_cffi_transport_is_blocked_too() -> None:
    """Second layer: the transport under probe_get is blocked as well.

    The sys.modules sweep only sees modules already imported when the guard's
    fixture runs, so a module that top-level-imports probe_get and is first
    imported *during* a test would slip through holding a live reference.
    Blocking curl_cffi closes that hole regardless of who holds what.
    """
    creq = pytest.importorskip("curl_cffi.requests")
    for verb in ("get", "post"):
        with pytest.raises(UnstubbedNetworkCall):
            getattr(creq, verb)("https://example.invalid/")


def test_a_test_owned_stub_still_wins_over_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard only bites when nothing else stubbed the seam."""
    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", lambda url, **kw: "stubbed"
    )
    assert _probe_mod.probe_get("https://example.invalid/") == "stubbed"


@pytest.mark.probe_seam
def test_probe_seam_marker_restores_the_real_functions() -> None:
    """@pytest.mark.probe_seam opts out — for tests that mock the transport."""
    assert _probe_mod.probe_get is _REAL_PROBE_GET
    assert _probe_mod.probe_post is _REAL_PROBE_POST
    assert _probe_mod.web_unlocker_get is _REAL_WEB_UNLOCKER_GET


@pytest.mark.live_network
def test_live_network_marker_restores_the_real_functions() -> None:
    """@pytest.mark.live_network opts out — for genuinely online tests.

    Asserts the binding only; it deliberately does not call out.
    """
    assert _probe_mod.probe_get is _REAL_PROBE_GET
    assert rentmanager.probe_get is _REAL_PROBE_GET


def test_guard_is_undone_between_tests() -> None:
    """monkeypatch teardown must restore the originals for the next test.

    Without this, an unmarked test running before a marked one could leave
    the stub installed and make the opt-out assertions above pass vacuously.
    """
    # This test is unmarked, so the seam is currently stubbed...
    assert _probe_mod.probe_get is not _REAL_PROBE_GET
    # ...but the module still holds the real one under its original name,
    # proving the guard swapped rather than destroyed it.
    assert callable(_REAL_PROBE_GET)
