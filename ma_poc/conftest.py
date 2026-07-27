"""Repo-level test guard — the suite must never touch the live internet.

Measured 2026-07-26 at 8b6ee48: a full ``pytest ma_poc/tests/`` made ~356
un-stubbed live requests to 20 real hostnames (x.com, real property sites…).
Every one of them went through the same seam. Tests stub ``httpx.AsyncClient``,
``get_adapter``, ``resolve_target`` or ``detect_pms`` — but production fetches
via :func:`ma_poc.pms.adapters._probe.probe_get`, a *sync* ``curl_cffi`` call
that sails straight past all of those.

So this guard patches the seam itself. Any test that reaches it without
stubbing gets a loud, actionable :class:`UnstubbedNetworkCall`.

Escape hatches, narrowest first:

* ``@pytest.mark.probe_seam`` — the test drives ``probe_get``/``probe_post``
  deliberately and mocks the transport (``curl_cffi``) underneath it, so no
  packets leave the box. See ``tests/pms/adapters/test_probe_cookie_mint.py``.
* ``@pytest.mark.live_network`` — the test genuinely wants the real internet.
* ``ALLOW_LIVE_NETWORK_TESTS=1`` — disables the guard for the whole run
  (local debugging only; never in CI).

Why the guard matters beyond hygiene: ``tests/pms/`` runs in 16.4s with the
seam faked vs 51.7s live (~35s of ``host_throttle`` tax), and any shell with
``PROBE_PROXY_URL`` set burns BrightData residential bandwidth on every call.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

# Functions in _probe that open a socket. Patching the *definitions* is not
# enough on its own — see the sys.modules sweep in _block_live_network.
_NETWORK_FUNCS = ("probe_get", "probe_post", "web_unlocker_get")

# Markers that opt a test out of the guard.
_OPT_OUT_MARKERS = ("live_network", "probe_seam")

# curl_cffi verbs blocked as the second layer. Nothing in this repo uses
# curl_cffi except _probe, so blocking them all is safe.
_CURL_VERBS = ("get", "post", "request", "head", "put")

# The genuine, unpatched callables — captured once in pytest_configure, before
# any fixture (of any scope) has had a chance to stub something.
_REAL_PROBE_FUNCS: dict[str, Any] = {}
_REAL_CURL_VERBS: dict[str, Any] = {}


def pytest_configure(config: pytest.Config) -> None:
    """Snapshot the real network callables at session start.

    Has to happen here rather than inside the fixture: pytest sets up
    session- and module-scoped fixtures *before* function-scoped ones, so a
    test that stubs the seam from a higher-scoped fixture would already have
    replaced the attribute by the time the guard looks. Reading it then would
    capture that stub as "the original" and overwrite it.
    """
    from ma_poc.pms.adapters import _probe

    for name in _NETWORK_FUNCS:
        real = getattr(_probe, name, None)
        if real is not None:
            _REAL_PROBE_FUNCS[name] = real

    try:
        from curl_cffi import requests as _creq
    except ImportError:  # pragma: no cover — curl_cffi always present in CI
        return
    for verb in _CURL_VERBS:
        real_verb = getattr(_creq, verb, None)
        if real_verb is not None:
            _REAL_CURL_VERBS[verb] = real_verb


class UnstubbedNetworkCall(BaseException):
    """A test reached the real network through the ``_probe`` seam.

    Deliberately derived from :class:`BaseException`, not ``Exception``:
    almost every ``probe_get`` call site in ``ma_poc/pms`` sits inside a
    blanket ``except Exception`` fallback, so an ``Exception`` here would be
    swallowed and the test would quietly carry on against degraded data
    instead of telling us it leaks. ``BaseException`` propagates to the test.
    """


def _make_stub(func_name: str) -> Any:
    """Build the replacement for ``_probe.<func_name>``.

    Args:
        func_name: Name of the blocked function, echoed in the error.

    Returns:
        A callable accepting any signature that always raises.

    Raises:
        UnstubbedNetworkCall: on every call, by design.
    """

    # curl_cffi.* is the raw transport; _probe.* is the seam above it. The
    # remedy differs, so name the right target rather than a path that does
    # not exist (there is no ma_poc.pms.adapters._probe.curl_cffi).
    target = func_name if func_name.startswith("curl_cffi.") else (
        f"ma_poc.pms.adapters._probe.{func_name}"
    )
    if func_name.startswith("curl_cffi."):
        why = (
            "Some production code calls curl_cffi DIRECTLY rather than through "
            "probe_get — e.g. the TRANSIENT/BOT_BLOCKED salvage in "
            "ma_poc/pms/scraper.py. Stubbing probe_get alone does not stop it."
        )
    else:
        why = (
            "Production fetches through "
            f"ma_poc.pms.adapters._probe.{func_name}, so stubbing httpx / "
            "get_adapter / resolve_target / detect_pms does not stop it."
        )

    def _blocked(url: Any = "<no url>", *args: Any, **kwargs: Any) -> Any:
        raise UnstubbedNetworkCall(
            f"un-stubbed live network call: {func_name}({url!r})\n"
            f"\n"
            f"The test suite must not touch the internet. {why}\n"
            f"\n"
            f"Fix by stubbing that seam in your test:\n"
            f"    monkeypatch.setattr(\n"
            f'        "{target}", _fake\n'
            f"    )\n"
            f"(modules that import the name at top level need their own copy "
            f"patched too — see ma_poc/conftest.py)\n"
            f"\n"
            f"If the call is intentional, mark the test @pytest.mark.probe_seam "
            f"(transport mocked underneath) or @pytest.mark.live_network "
            f"(really wants the internet)."
        )

    _blocked.__name__ = f"blocked_{func_name}"
    _blocked.__qualname__ = _blocked.__name__
    return _blocked


def _rebinding_modules(real: Any, attr: str) -> Iterator[ModuleType]:
    """Yield loaded modules holding their own reference to *real* as *attr*.

    ``probe_get`` is usually imported inside a function, which resolves the
    patched module attribute at call time. But a few modules import it at top
    level (``_sightmap_subpage_recovery``, ``rentmanager``, ``repli360``),
    binding the original object into their own namespace where patching
    ``_probe`` alone would never reach it. Sweeping ``sys.modules`` by identity
    catches those without hard-coding a list that would rot.

    Args:
        real: The unpatched function object to search for.
        attr: Attribute name to look for on each module.

    Yields:
        Modules whose ``attr`` is the same object as *real*.
    """
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            if getattr(module, attr, None) is real:
                yield module
        except Exception:  # pragma: no cover — lazy-import shims can raise
            continue


@pytest.fixture(autouse=True)
def _block_live_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block the ``_probe`` network seam for the duration of one test.

    Runs for every test. Tests that stub ``probe_get`` themselves simply
    overwrite this stub, so the guard only bites when nothing else did.
    ``monkeypatch`` undoes every patch at teardown.
    """
    if os.getenv("ALLOW_LIVE_NETWORK_TESTS") == "1":
        return
    if any(request.node.get_closest_marker(m) for m in _OPT_OUT_MARKERS):
        return

    # Only ever replace bindings that still point at the ORIGINAL function
    # captured in pytest_configure. A test that installed its own stub from a
    # module- or session-scoped fixture — which pytest sets up *before* this
    # function-scoped one — is left strictly alone. Re-reading the current
    # value here instead would capture that stub as "the real function" and
    # clobber it, which is a nasty trap for anyone stubbing above function
    # scope.
    for name, real in _REAL_PROBE_FUNCS.items():
        stub = _make_stub(name)
        # _probe itself is caught by this same sweep — it is in sys.modules
        # and still holds `real`.
        for module in _rebinding_modules(real, name):
            monkeypatch.setattr(module, name, stub, raising=False)

    # Second layer: block the transport itself. The sweep above only sees
    # modules already imported when this fixture runs, so a module that
    # top-level-imports probe_get and is first imported *during* a test would
    # slip through holding a live reference. Nothing else in the repo uses
    # curl_cffi, so blocking it here is free and closes that hole for good —
    # including for top-level importers added in future.
    for verb, real_verb in _REAL_CURL_VERBS.items():
        module = sys.modules.get("curl_cffi.requests")
        if module is not None and getattr(module, verb, None) is real_verb:
            monkeypatch.setattr(module, verb, _make_stub(f"curl_cffi.{verb}"))
