"""Adapter package — registers every adapter at import time.

Phase 2 ships stubs. Phase 3 replaces each stub with a real implementation;
the registry wiring in this file does not change.
"""

from __future__ import annotations

from ma_poc.pms.adapters.amli import AmliAdapter
from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult, PmsAdapter
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.adapters.funnel import FunnelAdapter
from ma_poc.pms.adapters.g5 import G5Adapter
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.adapters.knock import KnockAdapter
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.adapters.realpage_oll import RealPageOllAdapter
from ma_poc.pms.adapters.registry import all_adapters, get_adapter, register
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter
from ma_poc.pms.adapters.resman import ResManAdapter
from ma_poc.pms.adapters.sightmap import SightMapAdapter
from ma_poc.pms.adapters.spherexx import SpherexxAdapter
from ma_poc.pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from ma_poc.pms.adapters.touchtour import TouchTourAdapter
from ma_poc.pms.adapters.wix_nopms import WixNoPmsAdapter

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
    from ma_poc.pms.adapters.registry import _registered_names

    already = _registered_names()
    for cls in (
        RentCafeAdapter,
        ResManAdapter,
        EntrataAdapter,
        AppFolioAdapter,
        OneSiteAdapter,
        SightMapAdapter,
        RealPageOllAdapter,
        AvalonBayAdapter,
        AmliAdapter,
        FunnelAdapter,
        TouchTourAdapter,
        SpherexxAdapter,
        KnockAdapter,
        G5Adapter,
        SquarespaceNoPmsAdapter,
        WixNoPmsAdapter,
        GenericAdapter,
    ):
        instance = cls()
        if instance.pms_name in already:
            continue
        register(instance)


_bootstrap_registry()
