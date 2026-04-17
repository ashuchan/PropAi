"""Adapter package — registers every adapter at import time.

Phase 2 ships stubs. Phase 3 replaces each stub with a real implementation;
the registry wiring in this file does not change.
"""
from __future__ import annotations

from pms.adapters.appfolio import AppFolioAdapter
from pms.adapters.avalonbay import AvalonBayAdapter
from pms.adapters.base import AdapterContext, AdapterResult, PmsAdapter
from pms.adapters.entrata import EntrataAdapter
from pms.adapters.generic import GenericAdapter
from pms.adapters.onesite import OneSiteAdapter
from pms.adapters.realpage_oll import RealPageOllAdapter
from pms.adapters.registry import all_adapters, get_adapter, register
from pms.adapters.rentcafe import RentCafeAdapter
from pms.adapters.sightmap import SightMapAdapter
from pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from pms.adapters.wix_nopms import WixNoPmsAdapter

__all__ = [
    "AdapterContext",
    "AdapterResult",
    "PmsAdapter",
    "all_adapters",
    "get_adapter",
    "register",
]


def _bootstrap_registry() -> None:
    # Idempotent — registering twice is a hard error, so we guard against
    # double-import (pytest reloads, script re-invocations).
    from pms.adapters.registry import _registered_names

    already = _registered_names()
    for cls in (
        RentCafeAdapter,
        EntrataAdapter,
        AppFolioAdapter,
        OneSiteAdapter,
        SightMapAdapter,
        RealPageOllAdapter,
        AvalonBayAdapter,
        SquarespaceNoPmsAdapter,
        WixNoPmsAdapter,
        GenericAdapter,
    ):
        instance = cls()
        if instance.pms_name in already:
            continue
        register(instance)


_bootstrap_registry()
