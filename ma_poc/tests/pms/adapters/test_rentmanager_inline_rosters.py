from __future__ import annotations

from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentmanager import (
    RentManagerAdapter,
    parse_rentmanager_inline_unitcount,
    parse_rentmanager_leaseleads_cards,
)
from ma_poc.pms.detector import detect_pms

INLINE_UNITCOUNT_HTML = """
<html><body>
<a href="https://beacon.twa.rentmanager.com/">Resident Portal</a>
<script>var units = []; var unitcount = [];</script>
<script>units.push({uid:"3297", bedrooms:"2", fpname:'The Packard',
 bathrooms:'1.0', sqft:'940', rent:'$2,404.00', rentoverride:'1639.00',
 location:'1', pid:'39', matchfpname:'The Packard'});</script>
<script>unitcount.push({matchtype:'The Packard', unit:'660-205-R',
 availdate:'9/1/2026'});</script>
<script>unitcount.push({matchtype:'The Packard', unit:'655-215-R',
 availdate:'8/1/2026'});</script>
<script>unitcount.push({matchtype:'The Packard', unit:'655-215-R',
 availdate:'8/1/2026'});</script>
<script>unitcount.push({matchtype:'Unknown Plan', unit:'999',
 availdate:'8/1/2026'});</script>
</body></html>
"""


LEASELEADS_HTML = """
<html><body>
<a href="https://sterling.twa.rentmanager.com/">Resident Portal</a>
<div data-ll-floor-plan data-ll-event-label="1BR - 1BTH DLX PR">
  <h2>Upgraded 1BR - 1BTH DLX PR</h2>
  <p>1 Bedroom 1 Bathroom 783 Sq. Ft.</p>
  <div data-ll-floor-plan-unit-carousel-item>
    <div data-ll-floor-plan-unit-carousel-item-name>Unit <span>3220B</span></div>
    <div data-ll-floor-plan-unit-carousel-item-price>
      <a data-ll-floor-plan-unit-carousel-item-id="14200">$1,220 /month</a>
    </div>
    <div data-ll-floor-plan-unit-carousel-item-available-on>Available Now</div>
  </div>
  <div data-ll-floor-plan-unit-carousel-item>
    <div data-ll-floor-plan-unit-carousel-item-name>Unit <span>3260A</span></div>
    <div data-ll-floor-plan-unit-carousel-item-price>
      <a data-ll-floor-plan-unit-carousel-item-id="14194">$1,220 /month</a>
    </div>
    <div data-ll-floor-plan-unit-carousel-item-available-on>Not Available</div>
  </div>
</div>
</body></html>
"""


def test_inline_unitcount_joins_only_exact_dimensioned_plan() -> None:
    rows = parse_rentmanager_inline_unitcount(INLINE_UNITCOUNT_HTML, "https://hv.test/")

    assert [row["unit_number"] for row in rows] == ["660-205-R", "655-215-R"]
    assert rows[0]["floor_plan_name"] == "The Packard"
    assert rows[0]["bedrooms"] == "2"
    assert rows[0]["bathrooms"] == "1.0"
    assert rows[0]["sqft"] == "940"
    assert rows[0]["market_rent_low"] == 1639
    assert rows[0]["market_rent_high"] == 2404
    assert rows[0]["availability_date"] == "2026-09-01"


def test_inline_unitcount_requires_plan_side_of_join() -> None:
    html = """
    <script>var unitcount = [];</script>
    <script>unitcount.push({matchtype:'A1', unit:'101', availdate:'8/1/2026'});</script>
    """
    assert parse_rentmanager_inline_unitcount(html, "https://example.test/") == []


def test_leaseleads_cards_require_attribution_and_available_row() -> None:
    rows = parse_rentmanager_leaseleads_cards(
        LEASELEADS_HTML,
        "https://sterling.test/",
    )

    assert len(rows) == 1
    assert rows[0]["unit_number"] == "3220B"
    assert rows[0]["floor_plan_name"] == "1BR - 1BTH DLX PR"
    assert rows[0]["sqft"] == "783"
    assert rows[0]["market_rent_low"] == 1220
    assert rows[0]["source_ids"] == {"rentmanager_uid": "14200"}

    unattributed = LEASELEADS_HTML.replace(
        '<a href="https://sterling.twa.rentmanager.com/">Resident Portal</a>',
        "",
    )
    assert parse_rentmanager_leaseleads_cards(unattributed, "u") == []


class _Page:
    async def content(self) -> str:
        return INLINE_UNITCOUNT_HTML


@pytest.mark.asyncio
async def test_adapter_prefers_inline_roster_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_probe(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("inline SSR roster must short-circuit network probing")

    monkeypatch.setattr("ma_poc.pms.adapters.rentmanager.probe_get", unexpected_probe)
    ctx = AdapterContext(
        base_url="https://hv.test/",
        detected=detect_pms(
            "https://hv.test/",
            page_html=INLINE_UNITCOUNT_HTML,
        ),
        property_id="1721",
        profile=None,
        expected_total_units=None,
    )
    ctx.fetch_result = SimpleNamespace(
        body=INLINE_UNITCOUNT_HTML,
        final_url="https://hv.test/",
    )

    result = await RentManagerAdapter().extract(_Page(), ctx)  # type: ignore[arg-type]

    assert len(result.units) == 2
    assert result.tier_used == "TIER_1_DOM_RENTMANAGER_INLINE_UNITCOUNT"
