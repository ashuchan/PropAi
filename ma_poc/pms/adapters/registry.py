"""Adapter registry. See claude_refactor.md Phase 2."""
from __future__ import annotations

from ma_poc.pms.adapters.base import PmsAdapter

_REGISTRY: dict[str, PmsAdapter] = {}

# PMS names that intentionally fall through to the generic adapter.
_FALLBACK_NAMES: frozenset[str] = frozenset({"unknown", "custom"})


def register(adapter: PmsAdapter) -> None:
    name = adapter.pms_name
    if not isinstance(name, str) or not name:
        raise ValueError("adapter.pms_name must be a non-empty string")
    if name in _REGISTRY:
        raise ValueError(f"duplicate adapter registration for pms_name={name!r}")
    _REGISTRY[name] = adapter


def get_adapter(pms: str) -> PmsAdapter:
    """Return the adapter for the given PMS name.

    Unknown or ``custom`` PMSs fall through to the generic adapter. If the
    generic adapter itself is missing (e.g. during early Phase 2 bootstrap
    before Phase 3 stubs are registered), raises KeyError so misconfigurations
    surface immediately.
    """
    if pms in _REGISTRY:
        return _REGISTRY[pms]
    if pms in _FALLBACK_NAMES or pms not in _REGISTRY:
        try:
            return _REGISTRY["generic"]
        except KeyError:
            raise KeyError(
                f"no adapter for pms={pms!r} and 'generic' fallback is not registered"
            ) from None
    # Unreachable — kept for mypy strict-completeness.
    raise KeyError(pms)


def all_adapters() -> list[PmsAdapter]:
    return list(_REGISTRY.values())


def _reset_for_tests() -> None:
    """Test-only: clear the registry so import-time registration can be re-run."""
    _REGISTRY.clear()


def _registered_names() -> set[str]:
    return set(_REGISTRY)
