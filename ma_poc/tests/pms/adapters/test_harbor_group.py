"""Tests for Harbor Group Management adapter (_harbor_group.py).

Coverage:
  - detect_harbor_group: URL matching
  - harbor_prop_base: base URL derivation
  - parse_harbor_floor_plans: plan slug extraction
  - parse_harbor_units_page: unit card parsing (various field combinations)
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ma_poc.pms.adapters._harbor_group import (
    detect_harbor_group,
    harbor_prop_base,
    parse_harbor_floor_plans,
    parse_harbor_units_page,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.detector import detect_pms

# ── detect_harbor_group ──────────────────────────────────────────────────────

def test_detect_harbor_group_positive():
    url = "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary"
    assert detect_harbor_group(url) is True


def test_detect_harbor_group_trailing_slash():
    url = "https://www.harborgroupmanagement.com/apartments/MA/Bridgewater/Waterford-Village/"
    assert detect_harbor_group(url) is True


def test_detect_harbor_group_deep_subpage():
    url = "https://www.harborgroupmanagement.com/apartments/oh/columbus/the-canterbury/the-dogwood/units"
    assert detect_harbor_group(url) is True


def test_detect_harbor_group_hgliving_mirror():
    # hgliving.com is a Harbor Group mirror that 301s to harborgroupmanagement.com
    assert detect_harbor_group("https://www.hgliving.com/apartments/pa/phoenixville/riverworks") is True
    assert detect_harbor_group("https://www.hgliving.com/apartments/tx/san-antonio/linden-at-the-rim/") is True


def test_detect_harbor_group_negative_other_domain():
    assert detect_harbor_group("https://www.udr.com/apartments/some-property") is False
    # hgliving without the /apartments/ path must NOT match (avoid false positives)
    assert detect_harbor_group("https://www.hgliving.com/contact") is False


def test_detect_harbor_group_empty_string():
    assert detect_harbor_group("") is False


# ── harbor_prop_base ─────────────────────────────────────────────────────────

def test_harbor_prop_base_landing():
    url = "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary"
    assert harbor_prop_base(url) == "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary"


def test_harbor_prop_base_strips_subpage():
    url = "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary/floor-plans"
    assert harbor_prop_base(url) == "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary"


def test_harbor_prop_base_strips_plan_and_units():
    url = "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary/the-azalea/units"
    assert harbor_prop_base(url) == "https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary"


# ── parse_harbor_floor_plans ─────────────────────────────────────────────────

_FLOOR_PLANS_HTML = """
<html><body>
  <div class="floor-plan-list">
    <a href="/apartments/nc/cary/aurella-cary/the-azalea/listing">The Azalea</a>
    <a href="/apartments/nc/cary/aurella-cary/the-camillia/listing">The Camillia</a>
    <a href="/apartments/nc/cary/aurella-cary/the-dahlia/listing">The Dahlia</a>
    <a href="/apartments/nc/cary/aurella-cary/the-iris/listing">The Iris</a>
    <a href="/apartments/nc/cary/aurella-cary/the-laurel/listing">The Laurel</a>
    <a href="/some/other/path">Unrelated link</a>
  </div>
</body></html>
"""

def test_parse_harbor_floor_plans_extracts_slugs():
    slugs = parse_harbor_floor_plans(_FLOOR_PLANS_HTML)
    assert slugs == [
        "the-azalea", "the-camillia", "the-dahlia", "the-iris", "the-laurel"
    ]


def test_parse_harbor_floor_plans_deduplicates():
    html = """
    <html><body>
      <a href="/apartments/nc/cary/aurella-cary/the-azalea/listing">A</a>
      <a href="/apartments/nc/cary/aurella-cary/the-azalea/listing">A (dup)</a>
      <a href="/apartments/nc/cary/aurella-cary/the-camillia/listing">B</a>
    </body></html>
    """
    slugs = parse_harbor_floor_plans(html)
    assert slugs == ["the-azalea", "the-camillia"]


def test_parse_harbor_floor_plans_empty_html():
    assert parse_harbor_floor_plans("") == []


def test_parse_harbor_floor_plans_no_listing_links():
    html = "<html><body><a href='/about'>About</a></body></html>"
    assert parse_harbor_floor_plans(html) == []


def test_parse_harbor_floor_plans_accepts_property_seed_page() -> None:
    """Landing pages remain a valid slug source when /floor-plans is empty."""
    seed_html = """
    <nav><a href="/apartments/MA/Bridgewater/Waterford-Village/berkley/listing">
      Berkley
    </a></nav>
    <a href="/apartments/MA/Bridgewater/Waterford-Village/plymouth/listing">
      Plymouth
    </a>
    """
    assert parse_harbor_floor_plans(seed_html) == ["berkley", "plymouth"]


@pytest.mark.asyncio
async def test_generic_harbor_recovers_when_floorplans_shell_has_no_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Riverworks shape: only the fetched landing page lists plan routes."""
    base_url = (
        "https://www.hgliving.com/apartments/pa/phoenixville/riverworks"
    )
    landing_html = f"""
        <html><body>
          <a href="{base_url}/a1a/listing">A1A</a>
        </body></html>
    """
    calls: list[str] = []

    def fake_probe_get(
        url: str,
        *,
        timeout: int,
        unlocker: bool,
        **_kwargs: object,
    ) -> SimpleNamespace:
        calls.append(url)
        assert timeout == 15
        assert unlocker is False
        if url.endswith("/floor-plans"):
            return SimpleNamespace(status_code=200, text="<html>marketing shell</html>")
        assert url.endswith("/a1a/units")
        return SimpleNamespace(status_code=200, text=_UNITS_HTML)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get",
        fake_probe_get,
    )
    ctx = AdapterContext(
        base_url=base_url,
        detected=detect_pms(base_url),
        profile=None,
        expected_total_units=None,
        property_id="67524",
        property_name="Riverworks",
        address="45 N Main St",
        city="Phoenixville",
        state="PA",
        fetch_result=SimpleNamespace(
            body=landing_html.encode(),
            final_url=base_url,
        ),
        budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
        },
    )

    result = await GenericAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_3_DOM"
    assert {unit["unit_number"] for unit in result.units} == {
        "H-201A4",
        "H-312B",
    }
    assert calls == [
        f"{base_url}/floor-plans",
        f"{base_url}/a1a/units",
    ]


# ── parse_harbor_units_page ──────────────────────────────────────────────────

_UNITS_HTML = """
<html><body>
  <div id="listing-container" data-page="1" data-total-pages="1">

    <div class="listing-card">
      <div class="listing-card-top">
        <p class="listing-card-price">$1,155 </p>
        <h4 class="listing-card-title">Apartment #H-201A4</h4>
        <p class="listing-card-info listing-card-type">2 Bed</p>
        <p class="listing-card-info listing-card-bath">1 Bath</p>
        <p class="listing-card-info listing-card-sqft">850 Sq Ft</p>
        <p class="listing-card-amenity-sub-text">Available Now</p>
      </div>
      <a class="listing-card-apply apply-url"
         href="https://api.findigs.com/lookup/?unit_id=AUR-H-201A4&rent=1155&move_in_date=2026-05-19">
        Apply
      </a>
    </div>

    <div class="listing-card">
      <div class="listing-card-top">
        <p class="listing-card-price">$1,285</p>
        <h4 class="listing-card-title">Apartment #H-312B</h4>
        <p class="listing-card-info listing-card-type">2 Bed</p>
        <p class="listing-card-info listing-card-bath">1 Bath</p>
        <p class="listing-card-info listing-card-sqft">850 Sq Ft</p>
        <p class="listing-card-amenity-sub-text">Available June 11, 2026</p>
      </div>
      <a class="listing-card-apply apply-url"
         href="https://api.findigs.com/lookup/?unit_id=AUR-H-312B&rent=1285&move_in_date=2026-06-11">
        Apply
      </a>
    </div>

  </div>
</body></html>
"""

def test_parse_harbor_units_page_basic():
    units = parse_harbor_units_page(
        _UNITS_HTML, plan_slug="the-azalea",
        base_url="https://www.harborgroupmanagement.com/apartments/nc/cary/aurella-cary/the-azalea/units"
    )
    assert len(units) == 2


def test_parse_harbor_units_page_rent():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    rents = [u["market_rent_low"] for u in units]
    assert rents == [1155, 1285]


def test_parse_harbor_units_page_unit_numbers():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    nums = [u["unit_number"] for u in units]
    assert nums == ["H-201A4", "H-312B"]


def test_parse_harbor_units_page_sqft():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    assert all(u["sqft"] == 850 for u in units)


def test_parse_harbor_units_page_available_now():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    u0 = units[0]
    assert u0["availability_status"] == "AVAILABLE"
    # "Available Now" → no ISO date but move_in_date from apply link
    assert u0["available_date"] == "2026-05-19"


def test_parse_harbor_units_page_future_date():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    u1 = units[1]
    assert u1["availability_status"] == "AVAILABLE"
    assert u1["available_date"] == "2026-06-11"


def test_harbor_future_date_survives_production_formatter() -> None:
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    output = _format_v2_unit(
        units[1],
        datetime(2026, 5, 20, 12, tzinfo=UTC),
        "riverworks",
    )

    assert output["available_date"] == "2026-06-11"
    assert output["available_date_raw"] == "2026-06-11"
    assert output["availability_date_provenance"] == "explicit_future"


def test_parse_harbor_units_page_plan_slug_as_fp_id():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    assert all(u["floor_plan_id"] == "the-azalea" for u in units)


def test_parse_harbor_units_page_plan_name_override():
    units = parse_harbor_units_page(
        _UNITS_HTML, plan_slug="the-azalea", plan_name="The Azalea"
    )
    assert all(u["floor_plan_name"] == "The Azalea" for u in units)


def test_parse_harbor_units_page_plan_name_fallback_to_slug():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    # Should title-case the slug when plan_name not given
    assert all(u["floor_plan_name"] == "The Azalea" for u in units)


def test_parse_harbor_units_page_empty_html():
    assert parse_harbor_units_page("") == []


def test_parse_harbor_units_page_no_cards():
    html = "<html><body><div id='listing-container'></div></body></html>"
    assert parse_harbor_units_page(html, plan_slug="the-azalea") == []


def test_parse_harbor_units_page_beds_and_baths():
    units = parse_harbor_units_page(_UNITS_HTML, plan_slug="the-azalea")
    assert all(u["bedrooms"] == 2 for u in units)
    assert all(u["bathrooms"] == 1.0 for u in units)
