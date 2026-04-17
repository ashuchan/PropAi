"""Phase 2 — adapter registry tests."""
from __future__ import annotations

import typing as t

import pytest

import pms.adapters as adapters_pkg  # noqa: F401  — triggers registration
from pms.adapters.base import PmsAdapter
from pms.adapters.registry import (
    _registered_names,
    all_adapters,
    get_adapter,
    register,
)
from pms.detector import DetectedPMS

# Literals that must resolve to a concrete, non-generic adapter.
_CONCRETE_PMS_LITERALS = [
    "rentcafe", "entrata", "appfolio", "onesite", "sightmap",
    "realpage_oll", "avalonbay", "squarespace_nopms", "wix_nopms",
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
    for adapter in all_adapters():
        fps = adapter.static_fingerprints()
        if adapter.pms_name == "generic":
            assert fps == []
        else:
            assert fps, adapter.pms_name


def test_every_concrete_adapter_returns_adapter_result() -> None:
    # Phase 3 adapters must return AdapterResult (not raise, not return None).
    import asyncio

    from pms.adapters.base import AdapterContext, AdapterResult

    class _DummyPage:
        pass

    async def _call() -> AdapterResult:
        from pms.detector import detect_pms
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
