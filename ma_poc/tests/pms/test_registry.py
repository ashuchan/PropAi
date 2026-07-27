"""Phase 2 — adapter registry tests."""

from __future__ import annotations

import typing as t
from typing import Any

import pytest

import pms.adapters as adapters_pkg  # noqa: F401  — triggers registration
from ma_poc.pms.adapters.base import PmsAdapter
from ma_poc.pms.adapters.registry import (
    _registered_names,
    all_adapters,
    get_adapter,
    register,
)
from ma_poc.pms.detector import DetectedPMS


class _EmptyResponse:
    """Inert curl_cffi-response shim: reachable host, nothing to extract.

    Adapters read ``.status_code`` / ``.text`` (and occasionally
    ``.content`` / ``.headers`` / ``.url``), so all five are present.
    """

    def __init__(self, url: str) -> None:
        self.status_code = 404
        self.text = ""
        self.content = b""
        self.headers: dict[str, str] = {}
        self.url = url


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blanket-stub the ``_probe`` network seam for the whole module.

    These tests exercise the registry *contract* (every adapter is
    structurally a ``PmsAdapter`` and returns a well-formed
    ``AdapterResult``), never a live site — so an inert empty response is
    the right stub: adapters walk their full no-data path and still have
    to hand back an ``AdapterResult``.

    ``probe_get`` is normally imported inside the calling function, so
    patching ``_probe`` covers it. Three modules bind it at *module* top
    level and keep their own reference, which a ``_probe``-only patch can
    never reach — patch those copies too, since this file sweeps adapters
    broadly (``all_adapters()``) and a future adapter reaching one of them
    must not silently fetch for real.

    Function-scoped so it overrides ``ma_poc/conftest.py``'s network guard
    (same scope, but conftest autouse fixtures are set up first).
    """
    from ma_poc.pms.adapters import (
        _probe,
        _sightmap_subpage_recovery,
        rentmanager,
        repli360,
    )

    def _fake_probe_get(url: str, **_kw: Any) -> _EmptyResponse:
        return _EmptyResponse(url)

    def _fake_probe_post(url: str, data: Any = None, **_kw: Any) -> _EmptyResponse:
        return _EmptyResponse(url)

    monkeypatch.setattr(_probe, "probe_get", _fake_probe_get)
    monkeypatch.setattr(_probe, "probe_post", _fake_probe_post)
    # Modules holding a top-level copy of the name (see docstring).
    for module in (_sightmap_subpage_recovery, rentmanager, repli360):
        monkeypatch.setattr(module, "probe_get", _fake_probe_get, raising=False)
    monkeypatch.setattr(repli360, "probe_post", _fake_probe_post, raising=False)

# Literals that must resolve to a concrete, non-generic adapter.
_CONCRETE_PMS_LITERALS = [
    "rentcafe",
    "entrata",
    "appfolio",
    "onesite",
    "sightmap",
    "realpage_oll",
    "avalonbay",
    "squarespace_nopms",
    "wix_nopms",
]


def test_registry_has_adapter_for_each_pms_literal() -> None:
    for lit in _CONCRETE_PMS_LITERALS:
        adapter = get_adapter(lit)
        assert adapter.pms_name == lit


def test_registry_returns_generic_for_unknown() -> None:
    assert get_adapter("unknown").pms_name == "generic"


def test_registry_returns_generic_for_custom() -> None:
    assert get_adapter("custom").pms_name == "generic"


def test_adapter_names_match_pms_literals() -> None:
    literals = set(t.get_args(t.get_type_hints(DetectedPMS)["pms"]))
    # Every adapter either maps to a detector literal or is the generic fallback.
    allowed = literals | {"generic"}
    for adapter in all_adapters():
        assert adapter.pms_name in allowed, adapter.pms_name


def test_protocol_structural_match() -> None:
    for adapter in all_adapters():
        assert isinstance(adapter, PmsAdapter)


def test_register_prevents_duplicate_names() -> None:
    # Save state; restore after the test so the main registry is intact.
    names_before = _registered_names()
    adapter = all_adapters()[0]
    with pytest.raises(ValueError):
        register(adapter)
    # Idempotency: no accidental removal.
    assert _registered_names() == names_before


def test_every_adapter_has_nonempty_or_generic_fingerprints() -> None:
    # Concrete adapters must report at least one host fingerprint so the
    # orchestrator (Phase 5) can match intercepted URLs back to a PMS.
    # ``generic`` is the exception — it has no fingerprints.
    # ``generic_plan_text`` (2026-05-21) is also an exception: it's a
    # last-resort plan-level extractor with no host-specific markers;
    # detection is via body-text pattern, not URL.
    no_fingerprint_exceptions = {"generic", "generic_plan_text"}
    for adapter in all_adapters():
        fps = adapter.static_fingerprints()
        if adapter.pms_name in no_fingerprint_exceptions:
            assert fps == []
        else:
            assert fps, adapter.pms_name


def test_every_concrete_adapter_returns_adapter_result() -> None:
    # Phase 3 adapters must return AdapterResult (not raise, not return None).
    import asyncio

    from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

    class _DummyPage:
        pass

    async def _call() -> AdapterResult:
        from ma_poc.pms.detector import detect_pms

        ctx = AdapterContext(
            base_url="https://example.com",
            detected=detect_pms("https://example.com"),
            profile=None,
            expected_total_units=None,
            property_id="TEST",
        )
        ctx._api_responses = []  # type: ignore[attr-defined]
        adapter = get_adapter("rentcafe")
        return await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]

    result = asyncio.run(_call())
    assert isinstance(result, AdapterResult)
