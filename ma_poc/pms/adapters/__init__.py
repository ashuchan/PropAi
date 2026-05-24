"""Adapter package — registers every adapter at import time.

Phase 2 ships stubs. Phase 3 replaces each stub with a real implementation;
the registry wiring in this file does not change.
"""

from __future__ import annotations

from ma_poc.pms.adapters.amli import AmliAdapter
from ma_poc.pms.adapters.appfolio import AppFolioAdapter

# 2026-05-13 port (Commit 12): browser-intercept Tier-1 adapters.
from ma_poc.pms.adapters.apts247 import Apts247Adapter

# 2026-05-21 port (Fix 5b): Aspen Square Management operator adapter.
from ma_poc.pms.adapters.aspensquare import AspenSquareAdapter
from ma_poc.pms.adapters.avalonbay import AvalonBayAdapter
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult, PmsAdapter

# 2026-05-13 port (Commit 11): server-only Tier-1 adapters.
from ma_poc.pms.adapters.cortland import CortlandAdapter

# 2026-05-21 port (P1a): EncoreSkyline marketing-template adapter.
from ma_poc.pms.adapters.encoreskyline_template import EncoreSkylineTemplateAdapter
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.adapters.equity import EquityAdapter

# 2026-05-13 port (Commit 13): REIT adapters.
from ma_poc.pms.adapters.essex import EssexAdapter
from ma_poc.pms.adapters.funnel import FunnelAdapter
from ma_poc.pms.adapters.g5 import G5Adapter
from ma_poc.pms.adapters.generic import GenericAdapter

# 2026-05-24 port (parity Commit): last-resort plan-level extractor for
# bespoke sites the LLM-DOM tier currently demotes to floor-plan
# summaries. See project_main_vs_feature_branch_parity_gap memory.
from ma_poc.pms.adapters.generic_plan_text import GenericPlanTextAdapter

# 2026-05-24 port (parity Commit): IMT Residential portfolio "Spaces"
# CMS — ~20 PIDs that currently fall to TIER_4_LLM.
from ma_poc.pms.adapters.imt_spaces import ImtSpacesAdapter
from ma_poc.pms.adapters.irvine import IrvineAdapter
from ma_poc.pms.adapters.knock import KnockAdapter
from ma_poc.pms.adapters.maac import MaacAdapter

# 2026-05-24 port (parity Commit): Market Apartments CMS — Templates A-F
# coverage, ~32 PIDs in the 4,982-property fleet.
from ma_poc.pms.adapters.marketapts import MarketAptsAdapter
from ma_poc.pms.adapters.onesite import OneSiteAdapter

# 2026-05-24 (follow-up): RealPage CWS (Community Website Solution) —
# RPFP widget plan-level extractor. Covers properties served by
# cs-cdn.realpage.com/CWS that ship the RPFP widget client-side.
from ma_poc.pms.adapters.realpage_cws import RealPageCwsAdapter
from ma_poc.pms.adapters.realpage_oll import RealPageOllAdapter
from ma_poc.pms.adapters.registry import all_adapters, get_adapter, register
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter

# 2026-05-24 port (parity Commit): RentCafe modern-theme sub-adapters.
# LayoutTab handles bedroom-tab listing + /floorplans/{slug} drill;
# UnitRoster handles ``.floorplan-block`` + ``.par-units`` modern theme.
from ma_poc.pms.adapters.rentcafe_layout_tab import RentCafeLayoutTabAdapter
from ma_poc.pms.adapters.rentcafe_unit_roster import RentCafeUnitRosterAdapter
from ma_poc.pms.adapters.rentmanager import RentManagerAdapter
from ma_poc.pms.adapters.rentvision import RentVisionAdapter

# 2026-05-21 port (Fix 5c): Repli360 / rrac popup family adapter.
from ma_poc.pms.adapters.repli360 import Repli360Adapter

# 2026-05-21 port (P2a): ResMan public availability portal adapter.
from ma_poc.pms.adapters.resman import ResManAdapter
from ma_poc.pms.adapters.sightmap import SightMapAdapter
from ma_poc.pms.adapters.squarespace_nopms import SquarespaceNoPmsAdapter
from ma_poc.pms.adapters.touchtour import TouchTourAdapter

# 2026-05-24 port (parity Commit): Wix multifamily floor-plan templates.
from ma_poc.pms.adapters.wix_floor_plans import WixFloorPlansAdapter
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
        EntrataAdapter,
        AppFolioAdapter,
        OneSiteAdapter,
        SightMapAdapter,
        RealPageOllAdapter,
        AvalonBayAdapter,
        AmliAdapter,
        FunnelAdapter,
        TouchTourAdapter,
        # 2026-05-13 port (Commit 11): server-only Tier-1 adapters.
        CortlandAdapter,
        EquityAdapter,
        RentManagerAdapter,
        # 2026-05-13 port (Commit 12): browser-intercept Tier-1 adapters.
        G5Adapter,
        KnockAdapter,
        IrvineAdapter,
        Apts247Adapter,
        # 2026-05-13 port (Commit 13): REIT adapters.
        EssexAdapter,
        MaacAdapter,
        RentVisionAdapter,
        # 2026-05-21 port (P1a): EncoreSkyline marketing-template adapter.
        EncoreSkylineTemplateAdapter,
        # 2026-05-21 port (P2a): ResMan public availability portal adapter.
        ResManAdapter,
        # 2026-05-21 port (Fix 5b): Aspen Square operator adapter.
        AspenSquareAdapter,
        # 2026-05-21 port (Fix 5c): Repli360 / rrac popup family adapter.
        Repli360Adapter,
        # 2026-05-24 port (parity Commit): adapters that close the 1,333-PID
        # gap analysed in project_main_vs_feature_branch_parity_gap memory.
        # Order doesn't matter — each adapter is selected by detector signal
        # match; the registry is just a lookup table.
        MarketAptsAdapter,
        RentCafeLayoutTabAdapter,
        RentCafeUnitRosterAdapter,
        WixFloorPlansAdapter,
        ImtSpacesAdapter,
        # 2026-05-24 (follow-up): RealPage CWS RPFP widget adapter.
        RealPageCwsAdapter,
        # GenericPlanTextAdapter is the last-resort fallback — placed before
        # GenericAdapter so detector signals route here first for the
        # plan-text cluster. GenericAdapter still owns ``"unknown"``.
        GenericPlanTextAdapter,
        SquarespaceNoPmsAdapter,
        WixNoPmsAdapter,
        GenericAdapter,
    ):
        instance = cls()
        if instance.pms_name in already:
            continue
        register(instance)


_bootstrap_registry()
