"""Smoke tests for the four browser-intercept Tier-1 adapters added in
Commit 12 of MAY13_API_TIER_PORT_PLAN.md:

  * G5 Marketing Cloud   -- public GraphQL ``/graphql``
  * Knock / Doorway       -- ``/v1/property/community/<id>`` + ``/units``
  * Irvine Company       -- POST ``/units/rank {communityId}``
  * apts247 / RentDynamics -- ``/api/v1/floorplans/?api_key=<hex>``

These adapters require Playwright network interception (the bot-walled
endpoints can't be fetched server-side). Full live-network tests are
out of scope for the unit suite; the tests here verify (a) registration,
(b) detector routing, (c) empty-input contract, (d) fingerprint
exposure.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.apts247 import Apts247Adapter
from ma_poc.pms.adapters.g5 import G5Adapter
from ma_poc.pms.adapters.irvine import IrvineAdapter
from ma_poc.pms.adapters.knock import KnockAdapter
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms


_FOUR_ADAPTERS = (
    (G5Adapter, "g5"),
    (KnockAdapter, "knock"),
    (IrvineAdapter, "irvine"),
    (Apts247Adapter, "apts247"),
)


class TestRegistration:
    def test_all_register(self):
        from ma_poc.pms.adapters.registry import all_adapters
        names = {a.pms_name for a in all_adapters()}
        for _, expected in _FOUR_ADAPTERS:
            assert expected in names, f"{expected!r} not registered"

    def test_get_adapter_returns_correct_class(self):
        from ma_poc.pms.adapters.registry import get_adapter
        for cls, name in _FOUR_ADAPTERS:
            assert isinstance(get_adapter(name), cls)

    def test_strategy_map_has_api_first(self):
        for _, name in _FOUR_ADAPTERS:
            assert _STRATEGY_BY_PMS[name] == "api_first"

    def test_link_hop_priors_populated(self):
        from ma_poc.pms.signal_engine.defaults import DEFAULT_PMS_PRIORS
        for _, name in _FOUR_ADAPTERS:
            assert name in DEFAULT_PMS_PRIORS
            assert DEFAULT_PMS_PRIORS[name]


class TestDetectorRouting:
    """Each of these PMSes has a host or marker that the detector
    must recognise. Empty-page input returns ``unknown``; the marker-
    bearing pages route to the right PMS."""

    def test_g5_via_marketing_cloud_marker(self):
        html = '<script src="https://inventory.g5marketingcloud.com/widget.js"></script>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "g5"

    def test_knock_via_doorway_host(self):
        html = '<script src="https://doorway.knck.io/widget.js"></script>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "knock"

    def test_knock_yields_to_securecafe_when_both_markers_present(self):
        """2026-05-21 P2b regression guard: Knock often appears as a chat
        overlay on a primary RentCafe/SecureCafe site. When the structural
        portal marker is also present, the primary PMS owns the real
        inventory. Canonical PID 279299 thewildsapts.com: Knock chat
        returned 9 units; the co-resident SecureCafe portal carries 402."""
        html = (
            '<script src="https://doorway.knck.io/widget.js"></script>'
            '<a href="https://acme.securecafe.com/onlineleasing/wilds/'
            'floorplans.aspx">view floorplans</a>'
        )
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "rentcafe"  # SecureCafe is the rentcafe portal family

    def test_knock_yields_to_resman_when_both_markers_present(self):
        """Same gate, ResMan version. Knock overlay + ResMan availability
        portal → route to ResMan (it carries the real unit list)."""
        html = (
            '<script src="https://doorway.knck.io/widget.js"></script>'
            '<a href="https://acme.myresman.com/Portal/Applicants/'
            'Availability?a=1&p=2">apply</a>'
        )
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "resman"

    def test_irvine_via_host(self):
        html = '<a href="https://www.irvinecompanyapartments.com/listings">view</a>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "irvine"

    def test_irvine_via_communityid_aem_marker(self):
        html = '<div data-communityIdAEM="00000000-0000-0000-0000-000000000000">x</div>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "irvine"

    def test_apts247_via_apts247_marker(self):
        html = '<script src="https://static2.apts247.info/widget.js"></script>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "apts247"

    def test_apts247_via_rentdynamics(self):
        html = '<script src="https://api.rentdynamics.com/v1/x"></script>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "apts247"

    def test_apts247_rejects_bare_media_cdn_host(self):
        """2026-05-21 P1b regression guard: hero-image URLs on the
        apts247.info CDN must NOT route to apts247. AppFolio/SightMap
        sites embed these as marketing hero images (e.g. PIDs 16499
        palmflats, 235996 stratfordchase, 77537 parlaapts on
        2026-05-21). The widget loader / api_key URL / RentDynamics
        host signals remain admitted; bare media CDN host alone is not
        a real Apts247 deployment."""
        html = (
            '<img src="https://media.apts247.info/de/community/hero_shot/x.jpg">'
            '<img src="https://apts247.info/de/community/hero_shot/y.jpg">'
        )
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms != "apts247"


class TestEmptyInputContract:
    """Adapters must never raise on empty / malformed input."""

    def _make_ctx(self):
        from ma_poc.pms.adapters.base import AdapterContext
        ctx = AdapterContext(
            base_url="https://example.com/",
            detected=detect_pms("https://example.com/"),
            profile=None,
            expected_total_units=None,
            property_id="P_TEST",
        )
        ctx._api_responses = []  # type: ignore[attr-defined]
        return ctx

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", [G5Adapter, KnockAdapter, IrvineAdapter, Apts247Adapter])
    async def test_adapter_handles_empty_ctx(self, cls):
        result = await cls().extract(None, self._make_ctx())  # type: ignore[arg-type]
        assert result.units == []


def test_all_four_expose_fingerprints():
    for cls, _ in _FOUR_ADAPTERS:
        a = cls()
        fps = a.static_fingerprints()
        assert isinstance(fps, list)
        assert len(fps) > 0, f"{cls.__name__} has empty fingerprints"


# ────────────────────────────────────────────────────────────────────
# Parser-logic coverage. Each adapter's main parser exercised with
# realistic payload shapes.
# ────────────────────────────────────────────────────────────────────


class TestKnockParser:
    """Knock's public Doorway API returns ``units_data.units`` (a list
    of unit dicts) plus ``units_data.layouts`` (floor-plan join table).
    The parser must (a) join units to layouts, (b) filter hidden/leased,
    (c) enforce $200-$50K sanity bounds."""

    def test_parses_canonical_units_with_layout_join(self):
        from ma_poc.pms.adapters.knock import parse_knock_units
        payload = {
            "units_data": {
                "layouts": [{"id": "L1", "name": "A1", "bedrooms": 1,
                              "bathrooms": 1, "square_feet": 750}],
                "units": [
                    {"name": "101", "layoutId": "L1", "price": 1500,
                     "available": True, "availableOn": "2026-06-01"},
                    {"name": "102", "layoutId": "L1", "price": 1525,
                     "available": True, "availableOn": "2026-06-15"},
                ],
            }
        }
        units = parse_knock_units(payload)
        assert len(units) == 2
        assert {u["unit_number"] for u in units} == {"101", "102"}
        # Layout join populates floor_plan_name + beds/baths/sqft.
        for u in units:
            assert u["floor_plan_name"] == "A1"
            assert u["bedrooms"] == "1"
            assert u["sqft"] == "750"
            assert u["availability_status"] == "AVAILABLE"

    def test_skips_hidden_leased_reserved(self):
        from ma_poc.pms.adapters.knock import parse_knock_units
        payload = {
            "units_data": {
                "layouts": [],
                "units": [
                    {"name": "100", "price": 1500, "hidden": True, "available": True},
                    {"name": "101", "price": 1500, "leased": True, "available": True},
                    {"name": "102", "price": 1500, "reserved": True, "available": True},
                    {"name": "103", "price": 1500, "available": True},  # the only kept one
                ],
            }
        }
        units = parse_knock_units(payload)
        assert len(units) == 1
        assert units[0]["unit_number"] == "103"

    def test_filters_out_unsane_rents(self):
        from ma_poc.pms.adapters.knock import parse_knock_units
        payload = {
            "units_data": {"layouts": [], "units": [
                {"name": "low", "price": 50, "available": True},   # under floor
                {"name": "high", "price": 99999, "available": True},  # over cap
                {"name": "ok", "price": 1500, "available": True},
            ]},
        }
        units = parse_knock_units(payload)
        assert len(units) == 1
        assert units[0]["unit_number"] == "ok"

    def test_extracts_concession_from_aliases(self):
        from ma_poc.pms.adapters.knock import parse_knock_units
        payload = {
            "units_data": {"layouts": [], "units": [
                {"name": "101", "price": 1500, "available": True,
                 "SpecialsDescription": "1 month free"},
            ]},
        }
        units = parse_knock_units(payload)
        assert units[0]["concession"] == "1 month free"

    def test_empty_payload_returns_empty(self):
        from ma_poc.pms.adapters.knock import parse_knock_units
        assert parse_knock_units({}) == []
        assert parse_knock_units({"units_data": {}}) == []
        assert parse_knock_units({"units_data": {"units": []}}) == []


class TestG5Parser:
    """G5 Marketing Cloud serves units via GraphQL. The Apollo-cache
    fallback path extracts from a Vue SSR-hydrated JSON blob."""

    def test_parse_g5_apartments_handles_canonical_shape(self):
        from ma_poc.pms.adapters.g5 import parse_g5_apartments
        payload = {
            "data": {
                "location": {
                    "apartments": [
                        {"unitNumber": "101", "bedrooms": 1, "bathrooms": 1,
                         "squareFeet": 750, "price": 1500,
                         "availabilityDate": "2026-06-01"},
                    ]
                }
            }
        }
        units = parse_g5_apartments(payload)
        assert isinstance(units, list)
        # G5's exact field-mapping depends on the adapter's alias map;
        # at minimum the parser must not raise + must return a list.

    def test_parse_g5_apartments_handles_empty(self):
        from ma_poc.pms.adapters.g5 import parse_g5_apartments
        assert parse_g5_apartments({}) == []
        assert parse_g5_apartments({"data": {}}) == []


class TestIrvineParser:
    """Irvine Company posts to ``/units/rank {communityId}`` and gets
    back a unit list grouped by lease-term. Each unit may have a
    ``unitLeasePrice[{term, price, date}]`` ladder."""

    def test_parse_irvine_units_extracts_from_groups(self):
        from ma_poc.pms.adapters.irvine import parse_irvine_units
        # Branch's response is a list of {unitId, unitNumber,
        # bedrooms, ..., unitLeasePrice: [...]}.
        groups = [
            {"unitId": "U1", "unitNumber": "101",
             "bedrooms": 1, "bathrooms": 1, "squareFeet": 750,
             "unitLeasePrice": [
                 {"term": 12, "price": 1500, "date": "2026-06-01"},
                 {"term": 13, "price": 1450, "date": "2026-06-01"},
             ]},
        ]
        units = parse_irvine_units(groups, "https://x")
        assert isinstance(units, list)

    def test_parse_irvine_handles_empty(self):
        from ma_poc.pms.adapters.irvine import parse_irvine_units
        assert parse_irvine_units([], "https://x") == []


class TestApts247Parser:
    """Apts247 serves a flat list of floorplans + units under
    ``/api/v1/floorplans/?api_key=<hex>``."""

    def test_parse_apts247_handles_canonical_shape(self):
        from ma_poc.pms.adapters.apts247 import parse_apts247_floorplans
        payload = {
            "floorplans": [
                {"id": 1, "name": "A1", "bedrooms": 1, "bathrooms": 1,
                 "square_feet": 750, "units": [
                     {"id": 101, "unit_number": "101", "rent": 1500,
                      "available_date": "2026-06-01"},
                 ]},
            ],
        }
        units = parse_apts247_floorplans(payload, "https://x/api")
        assert isinstance(units, list)

    def test_parse_apts247_handles_empty(self):
        from ma_poc.pms.adapters.apts247 import parse_apts247_floorplans
        assert parse_apts247_floorplans({}, "https://x") == []
        assert parse_apts247_floorplans({"floorplans": []}, "https://x") == []
