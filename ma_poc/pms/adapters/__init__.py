"""Adapter package — registers every adapter at import time.

Phase 2 ships stubs. Phase 3 replaces each stub with a real implementation;
the registry wiring in this file does not change.
"""

from __future__ import annotations

from ma_poc.pms.adapters.amli import AmliAdapter
from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.aspensquare import AspenSquareAdapter
from ma_poc.pms.adapters.apts247 import Apts247Adapter
from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult, PmsAdapter
from ma_poc.pms.adapters.cortland import CortlandAdapter
from ma_poc.pms.adapters.encoreskyline_template import (
    EncoreSkylineTemplateAdapter,
)
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.adapters.equity import EquityAdapter
from ma_poc.pms.adapters.essex import EssexAdapter
from ma_poc.pms.adapters.funnel import FunnelAdapter
from ma_poc.pms.adapters.g5 import G5Adapter
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.adapters.irvine import IrvineAdapter
from ma_poc.pms.adapters.knock import KnockAdapter
from ma_poc.pms.adapters.maac import MaacAdapter
from ma_poc.pms.adapters.onesite import OneSiteAdapter
from ma_poc.pms.adapters.realpage_oll import RealPageOllAdapter
from ma_poc.pms.adapters.registry import all_adapters, get_adapter, register
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter
from ma_poc.pms.adapters.rentmanager import RentManagerAdapter
from ma_poc.pms.adapters.rentvision import RentVisionAdapter
from ma_poc.pms.adapters.repli360 import Repli360Adapter
from ma_poc.pms.adapters.residentservices365 import Residentservices365Adapter
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
        Apts247Adapter,
        EntrataAdapter,
        AppFolioAdapter,
        OneSiteAdapter,
        SightMapAdapter,
        RealPageOllAdapter,
        Repli360Adapter,
        RentManagerAdapter,
        RentVisionAdapter,
        Residentservices365Adapter,
        AspenSquareAdapter,
        EssexAdapter,
        AvalonBayAdapter,
        AmliAdapter,
        MaacAdapter,
        IrvineAdapter,
        CortlandAdapter,
        EquityAdapter,
        FunnelAdapter,
        TouchTourAdapter,
        SpherexxAdapter,
        KnockAdapter,
        G5Adapter,
        EncoreSkylineTemplateAdapter,
        SquarespaceNoPmsAdapter,
        WixNoPmsAdapter,
        GenericAdapter,
    ):
        instance = cls()
        if instance.pms_name in already:
            continue
        register(instance)


_bootstrap_registry()
