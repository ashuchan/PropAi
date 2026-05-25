"""Market Apartments CMS adapter (2026-05-21, greenfield).

Probe data captured live from the 13-site GoPrisma-tagged cohort plus
the 4 marketapts-tagged probe failures (results_deep.jsonl, 600-prop
grind). Two SSR template variants are pinned here:

  * Template A — inline ``.floorplan-unit-single`` rows. Captured byte-
    for-byte from sandpiperapartmentssaltlakecity.com/floorplans
    (property code ``623SAN``).
  * Template B — drill-to-``/unit/{plan-slug}`` with ``.unit-table-row``
    rows. Captured from aspirethunderbird.com/floorplans (``1073ATHB``)
    and the per-plan drill ``/unit/1x1-cp3``.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters import get_adapter
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.marketapts import (
    MarketAptsAdapter,
    parse_marketapts_template_a,
    parse_marketapts_template_b,
    parse_marketapts_template_c,
    parse_marketapts_template_e,
    parse_marketapts_template_f,
)
from ma_poc.pms.detector import detect_pms


# ── Template A fixtures ─────────────────────────────────────────────
# Sandpiper Apts: ``/floorplans`` SSR — .floorplan-block with two
# inline .floorplan-unit-single rows (verified live; data-when /
# data-price / unit-number all byte-identical).
_PLAN_A_TWO_UNITS = {
    "template": "A",
    "beds": "2",
    "baths": "1",
    "name": "2 Bedroom 1 Bath",
    "startingPrice": "Starting at $1,749",
    "units": [
        {
            "unitNumber": "213",
            "dataWhen": "2026-05-21",
            "dataPrice": "1769",
            "dataBeds": "2",
            "dataBaths": "1",
            "priceText": "$1769",
            "availText": "May 21, 2026",
        },
        {
            "unitNumber": "117",
            "dataWhen": "2026-06-10",
            "dataPrice": "1749",
            "dataBeds": "2",
            "dataBaths": "1",
            "priceText": "$1749",
            "availText": "June 10, 2026",
        },
    ],
}
# Plan with no unit rows → plan-level row fallback.
_PLAN_A_PLAN_ONLY = {
    "template": "A",
    "beds": "3",
    "baths": "2",
    "name": "3 Bedroom 2 Bath",
    "startingPrice": "From $2,199",
    "units": [],
}


# ── Template B fixtures ─────────────────────────────────────────────
# Aspire Thunderbird /floorplans plan card → /unit/1x1-cp3 drill.
# 2 inline unit-table-row entries on the drill page (verified live;
# column order Unit / Rent / Available / Special / Features / Apply).
_PLAN_B_TWO_UNITS = {
    "template": "B",
    "title": "1X1-CP3",
    "features": "SQ FEET: 500\nBEDROOMS: 1\nBATHROOMS: 1\nDEPOSIT: $500",
    "startingPrice": "$949",
    "drillPath": "/unit/1x1-cp3",
    "units": [
        {
            "cells": ["1060", "$949", "Now", "", "First Floor, Prior Reno 3", "APPLY"],
            "dataAttrs": {},
        },
        {
            "cells": ["1063", "$949", "Now", "", "Prior Reno 3", "APPLY"],
            "dataAttrs": {},
        },
    ],
}
# Plan whose drill returned nothing or "Contact Us for More Details" —
# adapter must surface a plan-level row.
_PLAN_B_PLAN_ONLY = {
    "template": "B",
    "title": "1X1-CC",
    "features": "SQ FEET: 500\nBEDROOMS: 1\nBATHROOMS: 1\nDEPOSIT: $OAC",
    "startingPrice": "Contact Us for More Details",
    "drillPath": "/unit/1x1-cc",
    "units": [],
}


# ── Template C fixtures ─────────────────────────────────────────────
# Reserve at Water Tower Village /floorplans gallery (.floorplan-container)
# with two plan badges linking to /unit/{slug}. Drill page exposes
# .unit-details rows with Unit / SqFt / Rent / Available cells.
# 2026-05-21: user-flagged that the "N Available" badge IS the drill
# anchor — the listing-side gallery looks plan-only at first glance but
# the drill exposes the per-unit roster.
_PLAN_C_STUDIO = {
    "template": "C",
    "title": "Studio",
    "specsBlob": "Studio BED: Studio BATH: 1 SQ. FEET: 450 ...",
    "drillPath": "/unit/studio",
    "units": [
        {
            "cells": ["2-106", "450", "$1200", "07/17/2026", "Apply Now"],
            "dataAttrs": {},
        },
        {
            "cells": ["4-114", "450", "$1125", "Now", "Apply Now"],
            "dataAttrs": {},
        },
    ],
}
_PLAN_C_ONE_BEDROOM = {
    "template": "C",
    "title": "1 Bedroom",
    "specsBlob": "1 Bedroom BED: 1 BATH: 1 SQ. FEET: 560 ...",
    "drillPath": "/unit/1bedroom1bathroom",
    "units": [
        {
            "cells": ["3-201", "560", "$1235", "06/01/2026", "Apply Now"],
            "dataAttrs": {},
        },
    ],
}
# Drill that returned no rows — empty roster but plan still surfaced.
_PLAN_C_EMPTY_DRILL = {
    "template": "C",
    "title": "2 Bedroom",
    "specsBlob": "2 Bedroom BED: 2 BATH: 2 SQ. FEET: 950",
    "drillPath": "/unit/2bedroom",
    "units": [],
}


# ── Template D fixtures ─────────────────────────────────────────────
# Riverbank Apts (20RIB) /floor-plans → /apartments/{slug}. Drill page
# uses ``.unit-table-row`` (SAME selector as Template B) but with the
# bonus that each row carries ``data-available-date`` (ISO) — captured
# 2026-05-21 from /apartments/plan1.
_PLAN_D_TWO_UNITS = {
    "template": "D",
    "title": "Plan1",
    "features": "BEDROOM: 1 BATHROOM: 1.0 SQ. FEET: 702 DEPOSIT: $1,000 FROM: $1,565",
    "startingPrice": "$1,565",
    "drillPath": "/apartments/plan1",
    "units": [
        {
            "cells": ["19-411", "$1,565", "Today", "Corner Unit", "APPLY NOW"],
            "dataAttrs": {
                "availableDate": "05/21/2026",
                "beds": "1",
                "baths": "1.0",
            },
        },
        {
            "cells": ["24-527", "$1,575", "Today", "Corner Unit", "APPLY NOW"],
            "dataAttrs": {
                "availableDate": "05/21/2026",
                "beds": "1",
                "baths": "1.0",
            },
        },
    ],
}


class _FakePage:
    """Tiny stand-in for a Playwright page. ``extract`` only calls
    ``page.evaluate`` and ``page.url``."""

    def __init__(
        self,
        payload: object,
        url: str = "https://www.sandpiperapartmentssaltlakecity.com/floorplans",
    ) -> None:
        self._payload = payload
        self.url = url

    async def evaluate(self, _js: str, *_a: object) -> object:
        return self._payload


def _ctx(base_url: str = "https://www.sandpiperapartmentssaltlakecity.com/") -> AdapterContext:
    return AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="P_TEST",
    )


# ── Template A parser tests ─────────────────────────────────────────


def test_template_a_unit_level_two_rows() -> None:
    units = parse_marketapts_template_a([_PLAN_A_TWO_UNITS], "u")
    assert len(units) == 2
    u0 = units[0]
    assert u0["floor_plan_name"] == "2 Bedroom 1 Bath"
    assert u0["unit_number"] == "213"
    assert u0["bedrooms"] == "2"
    assert u0["bathrooms"] == "1"
    assert u0["market_rent_low"] == 1769
    assert u0["market_rent_high"] == 1769
    assert u0["availability_date"] == "2026-05-21"  # data-when (ISO) wins
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"

    u1 = units[1]
    assert u1["unit_number"] == "117"
    assert u1["market_rent_low"] == 1749
    assert u1["availability_date"] == "2026-06-10"


def test_template_a_plan_level_fallback_no_units() -> None:
    rows = parse_marketapts_template_a([_PLAN_A_PLAN_ONLY], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["unit_number"] == ""
    assert p["floor_plan_name"] == "3 Bedroom 2 Bath"
    assert p["bedrooms"] == "3"
    assert p["market_rent_low"] == 2199


def test_template_a_mixed_plans() -> None:
    rows = parse_marketapts_template_a(
        [_PLAN_A_TWO_UNITS, _PLAN_A_PLAN_ONLY], "u"
    )
    assert len(rows) == 3  # 2 unit-level + 1 plan-level fallback


# ── Template B parser tests ─────────────────────────────────────────


def test_template_b_unit_level_two_rows() -> None:
    units = parse_marketapts_template_b([_PLAN_B_TWO_UNITS], "u")
    assert len(units) == 2
    u0 = units[0]
    assert u0["floor_plan_name"] == "1X1-CP3"
    assert u0["unit_number"] == "1060"
    assert u0["market_rent_low"] == 949
    assert u0["availability_date"] == ""  # "Now" → blank date
    assert u0["bedrooms"] == "1"
    assert u0["bathrooms"] == "1"
    assert u0["sqft"] == "500"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"
    assert units[1]["unit_number"] == "1063"


def test_template_b_plan_level_when_drill_empty() -> None:
    rows = parse_marketapts_template_b([_PLAN_B_PLAN_ONLY], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["unit_number"] == ""
    assert p["floor_plan_name"] == "1X1-CC"
    assert p["bedrooms"] == "1"
    assert p["sqft"] == "500"
    # "Contact Us for More Details" has no $ → no starting price → UNAVAILABLE
    assert p["market_rent_low"] is None
    assert p["availability_status"] == "UNAVAILABLE"


def test_template_b_date_extraction_variants() -> None:
    """The drill table cell order isn't 100% stable; the parser must
    walk all cells positionally to find rent + availability date and not
    consume the wrong one (regression against the Apply-button cell
    sometimes interleaving)."""
    plan = {
        "template": "B",
        "title": "B2",
        "features": "SQ FEET: 900\nBEDROOMS: 2\nBATHROOMS: 2",
        "startingPrice": "$1,300",
        "drillPath": "/unit/b2",
        "units": [
            {
                "cells": ["2410", "$1,350", "May 21, 2026", "First Floor", "APPLY"],
                "dataAttrs": {},
            },
            {
                "cells": ["2412", "$1,400", "2026-07-01", "", "APPLY"],
                "dataAttrs": {},
            },
            {
                "cells": ["2414", "$1,425", "06/15/2026", "", "APPLY"],
                "dataAttrs": {},
            },
        ],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert len(rows) == 3
    assert rows[0]["availability_date"] == "May 21, 2026"
    assert rows[1]["availability_date"] == "2026-07-01"
    assert rows[2]["availability_date"] == "06/15/2026"


def test_template_b_studio_features_blob() -> None:
    """A studio plan has BEDROOMS: Studio (no integer); _parse_specs_blob
    must map this to ``bedrooms == "0"``."""
    plan = {
        "template": "B",
        "title": "ST1",
        "features": "SQ FEET: 450\nBEDROOMS: Studio\nBATHROOMS: 1\nDEPOSIT: $400",
        "startingPrice": "$1,125",
        "drillPath": "",
        "units": [],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert rows[0]["bedrooms"] == "0"
    assert rows[0]["sqft"] == "450"
    assert rows[0]["market_rent_low"] == 1125


# ── Template C parser tests ─────────────────────────────────────────


def test_template_c_studio_drill_two_units() -> None:
    units = parse_marketapts_template_c([_PLAN_C_STUDIO], "u")
    assert len(units) == 2
    u0 = units[0]
    assert u0["floor_plan_name"] == "Studio"
    assert u0["unit_number"] == "2-106"
    assert u0["bedrooms"] == "0"  # "BED: Studio" → 0
    assert u0["bathrooms"] == "1"
    assert u0["sqft"] == "450"
    assert u0["market_rent_low"] == 1200
    assert u0["availability_date"] == "07/17/2026"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"
    # Second unit — "Now" → blank date.
    u1 = units[1]
    assert u1["unit_number"] == "4-114"
    assert u1["market_rent_low"] == 1125
    assert u1["availability_date"] == ""


def test_template_c_one_bedroom_drill_single_unit() -> None:
    units = parse_marketapts_template_c([_PLAN_C_ONE_BEDROOM], "u")
    assert len(units) == 1
    u = units[0]
    assert u["floor_plan_name"] == "1 Bedroom"
    assert u["bedrooms"] == "1"
    assert u["sqft"] == "560"
    assert u["market_rent_low"] == 1235
    assert u["availability_date"] == "06/01/2026"


def test_template_c_empty_drill_surfaces_plan_level_row() -> None:
    rows = parse_marketapts_template_c([_PLAN_C_EMPTY_DRILL], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["unit_number"] == ""
    assert p["floor_plan_name"] == "2 Bedroom"
    assert p["bedrooms"] == "2"
    assert p["sqft"] == "950"
    assert p["availability_status"] == "UNAVAILABLE"


def test_template_c_mixed_plans() -> None:
    rows = parse_marketapts_template_c(
        [_PLAN_C_STUDIO, _PLAN_C_ONE_BEDROOM, _PLAN_C_EMPTY_DRILL], "u"
    )
    # 2 (studio drill) + 1 (1br drill) + 1 (empty drill plan-level) = 4
    assert len(rows) == 4


def test_template_c_per_unit_sqft_from_drill_cell() -> None:
    """When the specs blob doesn't carry SQ. FEET but the drill row has
    a numeric SqFt cell, the row's sqft populates from the drill cell."""
    plan = {
        "template": "C",
        "title": "Studio",
        "specsBlob": "BED: Studio BATH: 1",  # NO sqft in specs
        "drillPath": "/unit/studio",
        "units": [
            {
                "cells": ["2-106", "450", "$1200", "07/17/2026", "Apply Now"],
                "dataAttrs": {},
            },
        ],
    }
    rows = parse_marketapts_template_c([plan], "u")
    assert rows[0]["sqft"] == "450"  # from drill cell, not specs blob


# ── Template D parser tests ─────────────────────────────────────────
# Template D reuses ``parse_marketapts_template_b`` — the drill selector
# (``.unit-table-row``) and cell shape are identical to Template B. The
# only divergence is the row-level ``data-available-date`` attr, which
# the shared B parser consumes when present.


def test_template_d_drill_uses_iso_data_available_date() -> None:
    """When ``data-available-date`` is present on the row (Template D's
    drill rows have it), the parser prefers it over the visible cell
    text ("Today" → would have rendered empty)."""
    rows = parse_marketapts_template_b([_PLAN_D_TWO_UNITS], "u")
    assert len(rows) == 2
    u0 = rows[0]
    assert u0["floor_plan_name"] == "Plan1"
    assert u0["unit_number"] == "19-411"
    assert u0["bedrooms"] == "1"
    assert u0["bathrooms"] == "1.0"
    assert u0["sqft"] == "702"
    assert u0["market_rent_low"] == 1565
    # ISO data-available-date wins over the "Today" cell text.
    assert u0["availability_date"] == "05/21/2026"
    assert u0["availability_status"] == "AVAILABLE"
    assert u0["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"


def test_template_d_falls_back_to_cell_text_when_no_data_attr() -> None:
    """If a drill row carries no ``data-available-date`` (defensive —
    Template B's drill rows don't always have it), the cell-walking
    fallback still finds the date."""
    plan = {
        "template": "D",
        "title": "Plan2",
        "features": "BEDROOM: 2 BATHROOM: 2 SQ. FEET: 950",
        "startingPrice": "$1,800",
        "drillPath": "/apartments/plan2",
        "units": [
            {
                "cells": ["32-101", "$1,800", "05/30/2026", "Pool View", "APPLY NOW"],
                "dataAttrs": {},
            },
        ],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert len(rows) == 1
    assert rows[0]["availability_date"] == "05/30/2026"


# ── Template E fixtures + tests ─────────────────────────────────────
# sylispm.com / Woodway Apartments (subpath on a shared-host CMS).
# Single-page tabbed accordion at /woodway-apartments/#floorplans —
# .floorplan-name cards inside .tab-pane bedroom-count panes, each
# with .list-group items "0 Bedroom"/"1 Bathroom"/"504 Square Feet"/
# "880" (bare number = rent). Plan-level only, no per-unit drill.
_PLAN_E_STUDIO = {
    "template": "E",
    "name": "Woodway Efficiency",
    "infoItems": [
        "0 Bedroom",
        "1 Bathroom",
        "504 Square Feet",
        "880",
        "8100 Pinebrook Drive, San Antonio, TX",
        "Apply Now For More Information",
    ],
}
_PLAN_E_ONE_BR = {
    "template": "E",
    "name": "One Bedroom With Den",
    "infoItems": [
        "1 Bedroom",
        "1 Bathroom",
        "728 Square Feet",
        "1010",
        "8100 Pinebrook Drive, San Antonio, TX",
        "Apply Now For More Information",
    ],
}


def test_template_e_studio_plan() -> None:
    rows = parse_marketapts_template_e([_PLAN_E_STUDIO], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["floor_plan_name"] == "Woodway Efficiency"
    assert p["bedrooms"] == "0"  # "0 Bedroom" → studio
    assert p["bathrooms"] == "1"
    assert p["sqft"] == "504"
    assert p["market_rent_low"] == 880
    assert p["availability_status"] == "AVAILABLE"
    assert p["unit_number"] == ""  # plan-only, no unit number


def test_template_e_one_bedroom_plan() -> None:
    rows = parse_marketapts_template_e([_PLAN_E_ONE_BR], "u")
    assert len(rows) == 1
    p = rows[0]
    assert p["bedrooms"] == "1"
    assert p["sqft"] == "728"
    assert p["market_rent_low"] == 1010


def test_template_e_filters_out_address_number_as_rent() -> None:
    """The address starts with "8100" — a plausible rent value. The
    parser must consume "504" / "1010" (bare numbers earlier in the
    list) as the rent, NOT the "8100" inside the address string. This
    test ensures the regex doesn't fall through to address numbers."""
    rows = parse_marketapts_template_e([_PLAN_E_ONE_BR], "u")
    assert rows[0]["market_rent_low"] == 1010  # not 8100


def test_template_e_multiple_plans() -> None:
    rows = parse_marketapts_template_e([_PLAN_E_STUDIO, _PLAN_E_ONE_BR], "u")
    assert len(rows) == 2


# ── Template F fixtures + tests ─────────────────────────────────────
# 223etown.com /availability — table.table-hover with bedroom-count
# tables, columns: Beds / Bath / Rent / Sq.Ft. / Style / Features /
# Available. Cell positions are stable across all confirmed tables.
_ROW_F_AVAILABLE_DATE = {
    "cells": ["1 Bed", "1.0 Bath", "$1,425", "661 Sq Ft", "The Franklin", "Tech Package Upgrade", "06/10/2026"],
    "tableName": "table table-hover f1 Bedroom Apartments",
    "dataAttrs": {},
}
_ROW_F_CALL_FOR_DETAILS = {
    "cells": ["Studio Bed", "0.0 Bath", "$8,514", "4730 Sq Ft", "COMM", "", "Call For Details"],
    "tableName": "table table-hover fStudio Apartments",
    "dataAttrs": {},
}


def test_template_f_unit_level_row_with_date() -> None:
    rows = parse_marketapts_template_f([_ROW_F_AVAILABLE_DATE], "u")
    assert len(rows) == 1
    u = rows[0]
    assert u["floor_plan_name"] == "The Franklin"  # "Style" cell
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1.0"
    assert u["sqft"] == "661"
    assert u["market_rent_low"] == 1425
    assert u["availability_date"] == "06/10/2026"
    assert u["availability_status"] == "AVAILABLE"
    assert u["unit_number"] == ""  # no unit number in this table format
    assert u["extraction_tier"] == "TIER_1_DOM_MARKETAPTS"


def test_template_f_call_for_details_is_unavailable() -> None:
    """When the Available cell says "Call For Details", the unit isn't
    publicly available — surface UNAVAILABLE so downstream consumers
    don't treat the row as immediately leasable."""
    rows = parse_marketapts_template_f([_ROW_F_CALL_FOR_DETAILS], "u")
    assert len(rows) == 1
    assert rows[0]["availability_status"] == "UNAVAILABLE"
    assert rows[0]["floor_plan_name"] == "COMM"


def test_template_f_studio_row_parses_bed_count_correctly() -> None:
    """"Studio Bed" should map to bedrooms="0" (not None or "Studio")
    so downstream bed-count aggregation works."""
    rows = parse_marketapts_template_f([_ROW_F_CALL_FOR_DETAILS], "u")
    assert rows[0]["bedrooms"] == "0"


def test_template_f_multiple_rows_across_bedroom_tables() -> None:
    """Rows from different bedroom-count tables (different ``tableName``)
    all flow through the same parser and emit independent rows."""
    rows = parse_marketapts_template_f(
        [_ROW_F_AVAILABLE_DATE, _ROW_F_CALL_FOR_DETAILS], "u"
    )
    assert len(rows) == 2


# ── Adapter end-to-end ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_template_a_extract() -> None:
    payload = {"template": "A", "plans": [_PLAN_A_TWO_UNITS]}
    result = await MarketAptsAdapter().extract(_FakePage(payload), _ctx())  # type: ignore[arg-type]
    # Two unit-level rows → ``_UNIT_LEVEL`` suffix per the 2026-05-25
    # deep-probe tier-label upgrade.
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_A_UNIT_LEVEL"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "213"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_b_extract() -> None:
    payload = {"template": "B", "plans": [_PLAN_B_TWO_UNITS]}
    fake = _FakePage(payload, url="https://www.aspirethunderbird.com/floorplans")
    result = await MarketAptsAdapter().extract(fake, _ctx("https://www.aspirethunderbird.com/"))  # type: ignore[arg-type]
    # Drill walked → unit-level tier label.
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_B_UNIT_LEVEL"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "1060"
    assert result.units[0]["market_rent_low"] == 949
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_c_extract() -> None:
    """End-to-end: Template C payload → Tier-1 DOM rows with tier
    label ``TIER_1_DOM_MARKETAPTS_C_UNIT_LEVEL`` (drilled)."""
    payload = {"template": "C", "plans": [_PLAN_C_STUDIO, _PLAN_C_ONE_BEDROOM]}
    fake = _FakePage(payload, url="https://www.thereserveatwatertowervillage.com/floorplans")
    result = await MarketAptsAdapter().extract(
        fake, _ctx("https://www.thereserveatwatertowervillage.com/")
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_C_UNIT_LEVEL"
    assert len(result.units) == 3
    unit_numbers = sorted(u["unit_number"] for u in result.units)
    assert unit_numbers == ["2-106", "3-201", "4-114"]
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_d_extract() -> None:
    """End-to-end: Template D payload → Tier-1 DOM rows with tier
    label ``TIER_1_DOM_MARKETAPTS_D_UNIT_LEVEL`` (D reuses the B parser
    internally but the adapter stamps the D label so reporting can
    tell them apart; ``_UNIT_LEVEL`` is appended because the drill
    walked)."""
    payload = {"template": "D", "plans": [_PLAN_D_TWO_UNITS]}
    fake = _FakePage(payload, url="https://www.riverbankapartments.com/floor-plans")
    result = await MarketAptsAdapter().extract(
        fake, _ctx("https://www.riverbankapartments.com/")
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_D_UNIT_LEVEL"
    assert len(result.units) == 2
    assert result.units[0]["unit_number"] == "19-411"
    assert result.units[0]["availability_date"] == "05/21/2026"
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_e_extract() -> None:
    """End-to-end: Template E payload → plan-level rows with tier label
    ``TIER_1_DOM_MARKETAPTS_E``."""
    payload = {"template": "E", "plans": [_PLAN_E_STUDIO, _PLAN_E_ONE_BR]}
    fake = _FakePage(payload, url="https://www.sylispm.com/woodway-apartments/")
    result = await MarketAptsAdapter().extract(
        fake, _ctx("https://www.sylispm.com/woodway-apartments/")
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_E"
    assert len(result.units) == 2
    assert result.units[0]["floor_plan_name"] == "Woodway Efficiency"
    assert result.units[0]["market_rent_low"] == 880
    assert result.confidence > 0.7


@pytest.mark.asyncio
async def test_adapter_template_f_extract() -> None:
    """End-to-end: Template F payload (rows under the ``rows`` key, not
    ``plans``) → unit-level rows with tier label
    ``TIER_1_DOM_MARKETAPTS_F``."""
    payload = {
        "template": "F",
        "plans": [],  # F doesn't use plans
        "rows": [_ROW_F_AVAILABLE_DATE, _ROW_F_CALL_FOR_DETAILS],
    }
    fake = _FakePage(payload, url="https://223etown.com/floorplans")
    result = await MarketAptsAdapter().extract(
        fake, _ctx("https://223etown.com/")
    )  # type: ignore[arg-type]
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_F"
    assert len(result.units) == 2
    # First row is AVAILABLE-dated; second is "Call For Details".
    franklin = next(u for u in result.units if u["floor_plan_name"] == "The Franklin")
    assert franklin["market_rent_low"] == 1425
    assert franklin["availability_date"] == "06/10/2026"
    comm = next(u for u in result.units if u["floor_plan_name"] == "COMM")
    assert comm["availability_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_adapter_no_plans_returns_failure() -> None:
    payload = {"template": "NONE", "plans": []}
    result = await MarketAptsAdapter().extract(_FakePage(payload), _ctx())  # type: ignore[arg-type]
    assert result.units == []
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_adapter_non_dict_payload_returns_failure() -> None:
    """DOM JS returns None or a list under hostile pages — adapter must
    not crash and must surface confidence 0."""
    result = await MarketAptsAdapter().extract(_FakePage(None), _ctx())  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert "non-dict" in " ".join(result.errors)


# ── Detector + registry ─────────────────────────────────────────────


def test_detector_routes_marketapts_assets_marker() -> None:
    """The CMS asset host marker should route to the marketapts adapter
    (not the generic LLM cascade)."""
    from ma_poc.pms.detector import _iter_html_markers
    html = '<img src="https://assets.marketapts.com/assets/converted/623SAN/images/x.jpg">'
    markers = list(_iter_html_markers(html))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_detector_routes_marketapts_api_marker() -> None:
    """The widget-config API host is the second canonical marker; also
    routes to marketapts."""
    from ma_poc.pms.detector import _iter_html_markers
    html = '<script src="https://api.marketapts.com/v1/widget-config/623SAN.json"></script>'
    markers = list(_iter_html_markers(html))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_detector_routes_marketapts_powered_by_footer() -> None:
    """``Powered by MarketApts`` in the footer is the third canonical
    marker — works on properties whose marketing site forwards assets
    through a CDN that hides the marketapts host."""
    from ma_poc.pms.detector import _iter_html_markers
    html = "<footer>...Powered by MarketApts...</footer>"
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "marketapts" for m in markers), markers


def test_adapter_registered() -> None:
    adapter = get_adapter("marketapts")
    assert isinstance(adapter, MarketAptsAdapter)
    assert adapter.pms_name == "marketapts"


def test_adapter_static_fingerprints_include_canonical_hosts() -> None:
    adapter = MarketAptsAdapter()
    fps = adapter.static_fingerprints()
    assert "marketapts.com" in fps
    assert "assets.marketapts.com" in fps
    assert "api.marketapts.com" in fps


def test_strategy_for_marketapts_is_dom_first() -> None:
    """marketapts must be classified ``dom_first`` so the orchestrator
    runs the DOM extractor before falling to API-first / LLM cascade."""
    from ma_poc.pms.detector import _STRATEGY_BY_PMS
    assert _STRATEGY_BY_PMS["marketapts"] == "dom_first"


# ── 2026-05-21 HAR-validation regressions ────────────────────────────


def test_detector_combined_signal_recovers_sylispm_style() -> None:
    """HAR-validation finding: sylispm.com is a real Market Apartments
    site that uses ``www.marketapts.com/iframes/`` + ``/images/apartments/``
    instead of the canonical ``assets.marketapts.com`` asset host. The
    strict signal misses it; the combined host+selector signal must
    recover it. Sample is a minimal sylispm-shaped fragment."""
    from ma_poc.pms.detector import _iter_html_markers
    # Sylispm-style HTML: weak MA host + Template-E ``floorplan-name`` selector.
    html = """
    <html><body>
    <iframe src="https://www.marketapts.com/iframes/fee-guide?code=WOODWAYA"></iframe>
    <div class="card"><h3 class="floorplan-name card-title">Woodway Efficiency</h3>
    <ul class="list-group floorplan-info"><li class="list-group-item">880</li></ul></div>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    assert any(m[0] == "marketapts" for m in markers), (
        f"sylispm-style site (weak host + .floorplan-name selector) must "
        f"route to marketapts; got: {markers}"
    )


def test_detector_combined_signal_still_excludes_g5_fee_guide_embed() -> None:
    """The combined-signal rule must NOT false-fire on G5 sites that
    just embed a Market Apartments fee-guide widget. The discriminator
    is the absence of any MA template selector in the HTML body. Sample
    is a minimal bellavista-shaped fragment."""
    from ma_poc.pms.detector import _iter_html_markers
    # Bellavista-style HTML: marketapts.com/iframes/fee-guide present but
    # NO template selectors (real site is G5).
    html = """
    <html><body>
    <iframe src="https://www.marketapts.com/iframes/fee-guide?code=533BLV"></iframe>
    <script src="https://www.marketapts.com/js/iframeResizer.min.js"></script>
    <link rel="stylesheet" href="https://www.marketapts.com/css/2023/bootstrap.min.css">
    <!-- G5 markers below, no MA template selectors -->
    <script src="https://g5marketingcloud.com/widget.js"></script>
    </body></html>
    """
    markers = list(_iter_html_markers(html.lower()))
    # marketapts must NOT appear; g5 SHOULD appear as the primary signal.
    ma_markers = [m for m in markers if m[0] == "marketapts"]
    assert not ma_markers, (
        f"G5 fee-guide-embed site must NOT route to marketapts (no template "
        f"selectors in body); got: {ma_markers}"
    )
    g5_markers = [m for m in markers if m[0] == "g5"]
    assert g5_markers, f"expected g5 to be detected on this fragment; got: {markers}"


def test_template_a_guard_requires_unit_single_selector() -> None:
    """Class-collision guard: ``.floorplan-block`` alone is NOT a
    sufficient marker for Template A — RentCafe/Yardi ASPX legacy theme
    (somaresidences.com, thefrankestate.com) uses the SAME class. Template
    A's guard MUST also require ``.floorplan-unit-single`` to discriminate.

    This is a source-shape contract test, parallel to
    test_render_gate_source_contract in test_top_level_fetch_profile_arg.py.
    Without this guard, 9+ RentCafe-ASPX sites would false-route to the
    MA adapter and emit malformed rows.
    """
    import inspect
    from ma_poc.pms.adapters.marketapts import _MARKETAPTS_DOM_JS
    src = _MARKETAPTS_DOM_JS
    # The Template A branch MUST AND-guard on .floorplan-unit-single.
    assert ".floorplan-unit-single" in src, (
        "Template A guard requires .floorplan-unit-single selector"
    )
    # The discriminator comment must be present so future maintainers
    # know not to simplify this guard.
    assert "CLASS-COLLISION GUARD" in src, (
        "Template A class-collision guard documentation must be preserved "
        "in source; do not simplify the .floorplan-block guard without "
        "reading the rationale comment"
    )


def test_dom_js_url_probe_includes_availability_path() -> None:
    """HAR finding: 223etown.com publishes unit-level data on
    ``/availability`` (Template F) rather than ``/floorplans``. The DOM JS
    self-fetch loop must probe ``/availability`` after the two floorplans
    spellings so Template F sites resolve from the property root.
    """
    from ma_poc.pms.adapters.marketapts import _MARKETAPTS_DOM_JS
    src = _MARKETAPTS_DOM_JS
    # All three probe paths must be in the self-fetch list, in order.
    fp_idx = src.find("/floorplans'")
    fph_idx = src.find("/floor-plans'")
    avail_idx = src.find("/availability'")
    assert fp_idx > 0, "/floorplans path must be probed"
    assert fph_idx > 0, "/floor-plans (hyphenated) path must be probed"
    assert avail_idx > 0, "/availability path must be probed (Template F sites)"
    # Order: /floorplans → /floor-plans → /availability
    assert fp_idx < fph_idx < avail_idx, (
        "URL probe order must be /floorplans → /floor-plans → /availability"
    )


# ── 2026-05-25 deep-probe regression: n_full=0 cohort fixes ─────────
#
# 15 TIER_1_DOM_MARKETAPTS properties routed correctly but emitted
# zero unit-level rows in the canary. Live probe of Brookstone
# Apartments (Template D) and Hill Country Villas (Template B)
# revealed three bugs:
#
#   1. Template B drill-anchor regex only matched
#      "View Available / View Details / See Available" — but live
#      anchors read "3 Units Available", "1 Apartment Available",
#      etc. Drill was never walked → plan-only rows.
#   2. Drill row cells include mobile-only label spans
#      (``<span class="visible-xs"><b>Unit:</b></span>``) that
#      contaminate desktop textContent with "Unit: 0827" instead of
#      "0827" → unit_number malformed.
#   3. Template D drill anchors emit relative paths without a leading
#      slash (``apartments/1-bedroom``); the previous origin-join only
#      handled ``/-leading`` paths so the relative form fetched the
#      wrong URL (resolving against the current page's directory
#      instead of the site root).
#
# Fixtures are byte-snippets captured 2026-05-25 from the live sites.


import re as _re_for_tests  # noqa: E402  intentionally aliased for test isolation
from pathlib import Path  # noqa: E402

from ma_poc.pms.adapters.marketapts import (  # noqa: E402
    _MARKETAPTS_DOM_JS,
    _strip_drill_cell_label,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "marketapts"


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _js_drill_anchor_matches(href: str, text: str) -> bool:
    """Python mirror of the Template B drill-anchor matcher in
    ``_MARKETAPTS_DOM_JS``. Single source of truth for the regex —
    extracted to a helper so the test below can exercise it against
    live anchor wordings.
    """
    t = text.lower()
    return bool(
        _re_for_tests.search(r"/unit/", href)
        or _re_for_tests.search(r"view\s+available|view\s+details|see\s+available", t)
        or _re_for_tests.search(r"\b\d+\s+units?\s+available\b", t)
        or _re_for_tests.search(r"\b\d+\s+apartments?\s+available\b", t)
    )


def test_template_b_drill_anchor_regex_matches_units_available_text() -> None:
    """Hill Country Villas — drill anchor reads "3 Units Available" with
    href ``/unit/a1-631``. The pre-2026-05-25 regex missed this wording
    and the drill was never walked. Both ``href`` and ``text`` variants
    must match to be defensive against future template tweaks."""
    # Live-captured anchor.
    assert _js_drill_anchor_matches("/unit/a1-631", "3 Units Available")
    # Singular variant — observed on plans with exactly one unit.
    assert _js_drill_anchor_matches("/unit/a1-710", "1 Unit Available")
    # Brookstone-style apartments+available wording (Template D, but the
    # Template B regex extension also covers it as defense-in-depth).
    assert _js_drill_anchor_matches("apartments/1-bedroom", "2 Apartments Available")
    assert _js_drill_anchor_matches("apartments/2-bedroom", "1 Apartment Available")


def test_template_b_drill_anchor_regex_still_matches_legacy_wordings() -> None:
    """Legacy "View Available / View Details" anchors observed on
    Aspire Thunderbird / Coventry / Landmark must still match — the
    2026-05-25 fix EXTENDS the regex, never narrows it."""
    assert _js_drill_anchor_matches("/unit/1x1-cp3", "View Available Units")
    assert _js_drill_anchor_matches("/unit/1x1-cc", "View Details")
    assert _js_drill_anchor_matches("/unit/2x2", "See Available")


def test_template_b_drill_anchor_regex_does_not_false_match_unrelated() -> None:
    """The regex must NOT match marketing links like "Apply Online" or
    image-lightbox anchors with empty text. Otherwise the drill walker
    would fetch wrong URLs."""
    # Image lightbox — empty text, href is a CDN image.
    assert not _js_drill_anchor_matches(
        "https://www.marketapts.com/images/apartments/floorplans/abc.jpg", ""
    )
    # Apply-online — different intent, must not be treated as drill.
    assert not _js_drill_anchor_matches("/apply-online", "Apply Now")
    # Echo Pointe / Portola Redlands "Limited | MORE INFO" — these
    # properties have NO drill page, so the anchor must be ignored.
    assert not _js_drill_anchor_matches("/apply-online", "Limited | MORE INFO")
    # Schedule-a-tour — common sibling anchor in Template D blocks.
    assert not _js_drill_anchor_matches("/schedule-a-tour", "Schedule a Tour")


def test_js_source_contains_extended_drill_regex() -> None:
    """Source-shape contract: the JS must carry the four-clause matcher
    so a future maintainer doesn't unwittingly revert to the legacy
    wording-only regex. If this assertion fails, the
    ``_js_drill_anchor_matches`` Python mirror is out of sync with the
    JS and the runtime behaviour drifts from tested behaviour."""
    assert r"\d+\s+units?\s+available" in _MARKETAPTS_DOM_JS, (
        "JS drill regex must match 'N Units Available' wording "
        "(Hill Country Villas / Mountain Ridge / Franklin Flats)"
    )
    assert r"\d+\s+apartments?\s+available" in _MARKETAPTS_DOM_JS, (
        "JS drill regex must match 'N Apartments Available' wording "
        "(Brookstone / Azlee / Riverbank)"
    )
    assert r"/unit/" in _MARKETAPTS_DOM_JS, (
        "JS drill regex must also accept ``/unit/`` href as a defensive "
        "secondary signal (catches sites with idiosyncratic anchor text)"
    )


def test_js_source_normalises_relative_drill_urls() -> None:
    """Brookstone Template D emits ``apartments/1-bedroom`` (no leading
    slash). The previous origin-join only handled ``/-leading`` paths so
    relative fetches resolved against the current page's directory
    instead of the origin. The fix uses ``new URL(href,
    document.baseURI)`` for explicit origin-anchored resolution.
    """
    # The new-URL idiom must appear at least once per drill loop (B, C, D).
    assert _MARKETAPTS_DOM_JS.count("new URL(drillUrl, document.baseURI)") >= 2, (
        "Templates B and D drill loops must normalise relative URLs via "
        "new URL(drillUrl, document.baseURI)"
    )


def test_js_source_strips_mobile_label_spans() -> None:
    """The drill-row cell extraction must remove ``.visible-xs`` and
    ``.visible-sm`` label spans before reading textContent. Otherwise
    Hill Country's "Unit: 0827" cell value becomes the literal unit
    number — downstream parsing produces malformed rows."""
    assert ".visible-xs, .visible-sm" in _MARKETAPTS_DOM_JS, (
        "Drill row extraction must strip .visible-xs/.visible-sm mobile "
        "label spans before reading cell text"
    )
    # The clone+remove pattern must be present — mutating the live DOM
    # would corrupt subsequent extractions.
    assert "rowClone.querySelectorAll" in _MARKETAPTS_DOM_JS, (
        "Must clone the row before stripping label spans (avoid mutating "
        "the live DOM)"
    )


def test_strip_drill_cell_label_removes_known_labels() -> None:
    """Python-side defensive fallback for the JS label-strip — if a
    site renders the label spans in a class the JS guard doesn't catch,
    the cell text comes through as "Unit: 0827" instead of "0827". The
    Python parser strips a known label prefix as a backstop."""
    assert _strip_drill_cell_label("Unit: 0827") == "0827"
    assert _strip_drill_cell_label("Rent: $749") == "$749"
    assert _strip_drill_cell_label("Available: Now") == "Now"
    assert _strip_drill_cell_label("Special:") == ""
    assert _strip_drill_cell_label("Features: Corner Unit") == "Corner Unit"
    # Already-clean cells pass through.
    assert _strip_drill_cell_label("0827") == "0827"
    assert _strip_drill_cell_label("$749") == "$749"
    # Non-label content with a colon mid-string is left alone.
    assert _strip_drill_cell_label("Apply Now: extra") == "Apply Now: extra"


def test_template_b_parser_strips_contaminated_unit_number() -> None:
    """When the JS label-strip didn't fire (defensive case), the Python
    parser must still emit a clean unit_number. Hill Country drill cells
    captured live: ``["Unit: 0827", "Rent: $749", "Available: Now",
    "Special:", "Features: Corner Unit", "Apply"]``."""
    plan = {
        "template": "B",
        "title": "A1-631",
        "features": "Sq Feet: 631 Bedrooms: 1 Bathrooms: 1 Deposit: $200",
        "startingPrice": "$ 749",
        "drillPath": "/unit/a1-631",
        "units": [
            {
                "cells": [
                    "Unit: 0827",
                    "Rent: $749",
                    "Available: Now",
                    "Special:",
                    "Features: Corner Unit",
                    "Apply",
                ],
                "dataAttrs": {},
            },
            {
                "cells": [
                    "Unit: 0833",
                    "Rent: $749",
                    "Available: Now",
                    "Special:",
                    "Features:",
                    "Apply",
                ],
                "dataAttrs": {},
            },
        ],
    }
    rows = parse_marketapts_template_b([plan], "u")
    assert len(rows) == 2, f"expected 2 unit rows, got {len(rows)}: {rows}"
    assert rows[0]["unit_number"] == "0827"
    assert rows[1]["unit_number"] == "0833"
    assert rows[0]["market_rent_low"] == 749
    assert rows[1]["market_rent_low"] == 749
    # "Now" → blank availability date (= immediately available).
    assert rows[0]["availability_date"] == ""
    assert rows[0]["bedrooms"] == "1"
    assert rows[0]["sqft"] == "631"
    assert rows[0]["floor_plan_name"] == "A1-631"


def test_hillcountry_live_drill_html_python_mirror() -> None:
    """End-to-end against the live Hill Country drill fixture: parse
    the row HTML with the Python JS-mirror, feed the resulting cells
    into ``parse_marketapts_template_b``, expect 3 unit-level rows.
    This is the documentary regression that ties the live HTML byte
    pattern to the parser's expected output."""
    drill_html = _read_fixture("hillcountry_unit_a1_631.html")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(drill_html, "html.parser")
    rows_dom = soup.select(".unit-table-row")
    assert len(rows_dom) == 3, (
        f"fixture must contain 3 unit-table-row entries; got {len(rows_dom)}"
    )

    # Mirror the JS clone+strip+children-text behaviour.
    cells_per_row: list[list[str]] = []
    for row in rows_dom:
        # Strip mobile label spans (mirrors the JS clone+remove).
        for n in row.select(".visible-xs, .visible-sm, .hidden-md, .hidden-lg"):
            n.decompose()
        cells = [
            " ".join(c.get_text(" ", strip=True).split())
            for c in row.find_all(recursive=False)
            if c.name
        ]
        cells_per_row.append(cells)

    plan = {
        "template": "B",
        "title": "A1-631",
        "features": "Sq Feet: 631 Bedrooms: 1 Bathrooms: 1 Deposit: $200",
        "startingPrice": "$ 749",
        "drillPath": "/unit/a1-631",
        "units": [{"cells": c, "dataAttrs": {}} for c in cells_per_row],
    }
    parsed = parse_marketapts_template_b([plan], "u")
    assert len(parsed) == 3
    unit_numbers = sorted(p["unit_number"] for p in parsed)
    assert unit_numbers == ["0827", "0833", "1238"], (
        f"unit_numbers={unit_numbers} — JS label-strip pattern must "
        f"yield clean values; got contaminated cells if any prefix remains"
    )
    assert all(p["market_rent_low"] == 749 for p in parsed)
    assert all(p["bedrooms"] == "1" for p in parsed)
    assert all(p["sqft"] == "631" for p in parsed)
    assert all(p["floor_plan_name"] == "A1-631" for p in parsed)


def test_brookstone_live_drill_html_python_mirror() -> None:
    """End-to-end against the live Brookstone Template D drill fixture:
    2 unit-table-row entries with ``data-available-date`` ISO attrs.
    The cells are plain (no Hill Country-style label prefix) but the
    test exercises the same path so a future template tweak doesn't
    silently break this site."""
    drill_html = _read_fixture("brookstone_apartments_1br.html")
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(drill_html, "html.parser")
    rows_dom = soup.select(".unit-table-row")
    assert len(rows_dom) == 2

    cells_per_row: list[list[str]] = []
    data_attrs_per_row: list[dict[str, str]] = []
    for row in rows_dom:
        for n in row.select(".visible-xs, .visible-sm, .hidden-md, .hidden-lg"):
            n.decompose()
        cells = [
            " ".join(c.get_text(" ", strip=True).split())
            for c in row.find_all(recursive=False)
            if c.name
        ]
        cells_per_row.append(cells)
        # Convert kebab-case data attrs to camelCase (mirrors JS dataset).
        attrs = {}
        for k, v in row.attrs.items():
            if k.startswith("data-"):
                key = k[5:]
                # Hyphen-to-camelcase: "available-date" → "availableDate"
                parts = key.split("-")
                cam = parts[0] + "".join(p.capitalize() for p in parts[1:])
                attrs[cam] = v
        data_attrs_per_row.append(attrs)

    plan = {
        "template": "D",
        "title": "1 Bedroom",
        "features": "BEDROOM: 1 BATHROOM: 1.0 SQ. FEET: 686",
        "startingPrice": "$1,117",
        "drillPath": "apartments/1-bedroom",  # no leading slash — relative
        "units": [
            {"cells": cells_per_row[i], "dataAttrs": data_attrs_per_row[i]}
            for i in range(2)
        ],
    }
    parsed = parse_marketapts_template_b([plan], "u")
    assert len(parsed) == 2
    unit_numbers = sorted(p["unit_number"] for p in parsed)
    assert unit_numbers == ["136", "233"]
    # data-available-date is the authoritative ISO; cell text "June 10,
    # 2026" is the human-rendered form and the data attr wins.
    avail_dates = sorted(p["availability_date"] for p in parsed)
    assert avail_dates == ["06/10/2026", "07/10/2026"], (
        f"Brookstone drill rows have data-available-date; got {avail_dates}"
    )
    assert sorted(p["market_rent_low"] for p in parsed) == [1117, 1242]


def test_tier_label_plan_only_does_not_get_unit_level_suffix() -> None:
    """When the drill fails (e.g. plan-only sites like Echo Pointe /
    Portola Redlands with no /unit/ drill at all), every emitted row
    has unit_number="" — the tier label must stay
    ``TIER_1_DOM_MARKETAPTS_B`` without the ``_UNIT_LEVEL`` suffix.
    Reporting uses this to separate "drill walked" from "plan-only"
    extractions."""
    plan = {
        "template": "B",
        "title": "1 Bedroom 1 Bathroom",
        "features": "Sq Feet: 775 Bedrooms: 1 Bathrooms: 1 Deposit: $OAC",
        "startingPrice": "$ 850",
        "drillPath": "",  # no drill anchor matched
        "units": [],
    }
    payload = {"template": "B", "plans": [plan]}
    import asyncio

    async def _run() -> object:
        return await MarketAptsAdapter().extract(
            _FakePage(payload, url="https://www.liveatechopointe.com/floorplans"),  # type: ignore[arg-type]
            _ctx("https://www.liveatechopointe.com/"),
        )

    result = asyncio.run(_run())
    # Plan-level fallback row emitted; no unit_number → no ``_UNIT_LEVEL``.
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_B", (  # type: ignore[attr-defined]
        f"plan-only payload must NOT get _UNIT_LEVEL suffix; "
        f"got {result.tier_used}"  # type: ignore[attr-defined]
    )


def test_tier_label_drilled_gets_unit_level_suffix_for_template_b() -> None:
    """Mirror of the previous test: when the drill walked and any
    admitted row carries a unit_number, ``_UNIT_LEVEL`` must be
    appended. Same Template B but with drilled rows."""
    plan = {
        "template": "B",
        "title": "A1-631",
        "features": "Sq Feet: 631 Bedrooms: 1 Bathrooms: 1 Deposit: $200",
        "startingPrice": "$ 749",
        "drillPath": "/unit/a1-631",
        "units": [
            {
                "cells": ["Unit: 0827", "Rent: $749", "Available: Now", "", "", "Apply"],
                "dataAttrs": {},
            },
        ],
    }
    payload = {"template": "B", "plans": [plan]}
    import asyncio

    async def _run() -> object:
        return await MarketAptsAdapter().extract(
            _FakePage(payload, url="https://www.hillcountryvillasapartments.com/floorplans"),  # type: ignore[arg-type]
            _ctx("https://www.hillcountryvillasapartments.com/"),
        )

    result = asyncio.run(_run())
    assert result.tier_used == "TIER_1_DOM_MARKETAPTS_B_UNIT_LEVEL"  # type: ignore[attr-defined]
    assert len(result.units) == 1  # type: ignore[attr-defined]
    assert result.units[0]["unit_number"] == "0827"  # type: ignore[attr-defined]


def test_hillcountry_listing_fixture_matches_template_b_layout() -> None:
    """The Hill Country listing fixture must expose the markers that
    Template B's branch (``.floorplan-item`` × N) keys on. If a future
    template refactor renames the class, this test catches the drift
    before the production fleet does."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_fixture("hillcountry_floorplans.html"), "html.parser")
    items = soup.select(".floorplan-item")
    assert len(items) >= 2, (
        f"hillcountry fixture must carry >=2 .floorplan-item cards "
        f"(template B); got {len(items)}"
    )
    first = items[0]
    # Title, features, num — Template B's three plan-level fields.
    assert first.select_one(".floorplan-title")
    assert first.select_one(".floorplan-features")
    assert first.select_one(".floorplan-num")
    # And the drill anchor — text must contain a "N Units Available"
    # wording that the new regex catches.
    drill_anchors = [
        a for a in first.find_all("a")
        if "/unit/" in (a.get("href") or "")
    ]
    assert drill_anchors, "fixture must contain a /unit/{slug} drill anchor"
    text = drill_anchors[0].get_text(" ", strip=True).lower()
    assert _re_for_tests.search(r"\d+\s+units?\s+available", text), (
        f"drill anchor text {text!r} must match the 'N Units Available' "
        f"wording the fixed regex keys on"
    )


def test_brookstone_listing_fixture_matches_template_d_layout() -> None:
    """The Brookstone listing fixture must expose ``.floor-plans-block``
    cards with ``apartments/{slug}`` (no leading slash) drill anchors.
    This documents the live shape so a future template renaming is
    caught immediately."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_fixture("brookstone_floor_plans.html"), "html.parser")
    blocks = soup.select(".floor-plans-block")
    assert len(blocks) >= 2, (
        f"brookstone fixture must carry >=2 .floor-plans-block cards; "
        f"got {len(blocks)}"
    )
    first = blocks[0]
    # The drill anchor has the relative-URL shape (no leading slash) —
    # this is the exact case the ``new URL(drillUrl, document.baseURI)``
    # fix targets.
    drill_anchors = [
        a for a in first.find_all("a")
        if "apartments/" in (a.get("href") or "")
        and not (a.get("href") or "").startswith("/")
        and not (a.get("href") or "").startswith("http")
    ]
    assert drill_anchors, (
        "brookstone fixture must contain a relative ``apartments/{slug}`` "
        "drill anchor (the URL-normalisation regression case)"
    )
    # Text must match the "N Apartments Available" wording.
    text = drill_anchors[0].get_text(" ", strip=True).lower()
    assert _re_for_tests.search(r"\d+\s+apartments?\s+available", text), (
        f"drill anchor text {text!r} must match the 'N Apartments "
        f"Available' wording"
    )
