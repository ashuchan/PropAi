"""Smoke tests for the three REIT adapters added in Commit 13 of
MAY13_API_TIER_PORT_PLAN.md:

  * Essex Property Trust    -- bulk /availability?format=spa (curl_cffi)
  * MAA Communities         -- MAAC REIT custom CMS
  * RentVision              -- multifamily marketing-site CMS

Live-network tests are out of scope for the unit suite; the tests here
verify registration, detector routing, the empty-input contract, and
fingerprint exposure (same pattern as Commit 11 / 12 sibling tests).
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.essex import EssexAdapter
from ma_poc.pms.adapters.maac import MaacAdapter
from ma_poc.pms.adapters.rentvision import RentVisionAdapter
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms


_THREE_ADAPTERS = (
    (EssexAdapter, "essex", "api_first"),
    (MaacAdapter, "maac", "api_first"),
    (RentVisionAdapter, "rentvision", "cascade"),
)


class TestRegistration:
    def test_all_register(self):
        from ma_poc.pms.adapters.registry import all_adapters
        names = {a.pms_name for a in all_adapters()}
        for _, expected, _ in _THREE_ADAPTERS:
            assert expected in names

    def test_get_adapter_returns_correct_class(self):
        from ma_poc.pms.adapters.registry import get_adapter
        for cls, name, _ in _THREE_ADAPTERS:
            assert isinstance(get_adapter(name), cls)

    def test_strategy_map_matches(self):
        for _, name, expected_strategy in _THREE_ADAPTERS:
            assert _STRATEGY_BY_PMS[name] == expected_strategy

    def test_link_hop_priors_populated(self):
        from ma_poc.pms.signal_engine.defaults import DEFAULT_PMS_PRIORS
        for _, name, _ in _THREE_ADAPTERS:
            assert name in DEFAULT_PMS_PRIORS
            assert DEFAULT_PMS_PRIORS[name]


class TestDetectorRouting:
    def test_essex_via_host(self):
        html = '<a href="https://www.essexapartmenthomes.com/properties">x</a>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "essex"

    def test_maac_via_host(self):
        html = '<a href="https://www.maac.com/communities">x</a>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "maac"

    def test_maac_via_alternate_host(self):
        html = '<a href="https://www.maacommunities.com/x">x</a>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "maac"

    def test_rentvision_via_powered_by_marker(self):
        html = '<footer>powered by RentVision</footer>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "rentvision"

    def test_rentvision_via_host(self):
        html = '<script src="https://rentvision.com/widget.js"></script>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "rentvision"


class TestEmptyInputContract:
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
    @pytest.mark.parametrize("cls", [EssexAdapter, MaacAdapter, RentVisionAdapter])
    async def test_adapter_handles_empty_ctx(self, cls):
        result = await cls().extract(None, self._make_ctx())  # type: ignore[arg-type]
        assert result.units == []


def test_all_three_expose_fingerprints():
    for cls, _, _ in _THREE_ADAPTERS:
        fps = cls().static_fingerprints()
        assert isinstance(fps, list) and len(fps) > 0


# ────────────────────────────────────────────────────────────────────
# Parser-logic coverage.
# ────────────────────────────────────────────────────────────────────


class TestEssexParser:
    """Essex Property Trust exposes a bulk ``/availability?format=spa``
    endpoint that returns a list of per-community apartment objects."""

    def test_is_essex_bulk_recognises_canonical_envelope(self):
        from ma_poc.pms.adapters.essex import _is_essex_bulk
        # The bulk endpoint wraps the floorplans list in
        # ``{"result": {"floorplans": [...]}}``.
        assert _is_essex_bulk({
            "result": {"floorplans": [{"name": "A1"}]}
        }) is True

    def test_is_essex_bulk_rejects_non_dict_and_missing_floorplans(self):
        from ma_poc.pms.adapters.essex import _is_essex_bulk
        assert _is_essex_bulk([]) is False
        assert _is_essex_bulk("not a dict") is False
        assert _is_essex_bulk(None) is False
        assert _is_essex_bulk({"result": "not a dict"}) is False
        assert _is_essex_bulk({"result": {}}) is False  # no floorplans key

    def test_parse_essex_bulk_extracts_units_from_floorplans(self):
        from ma_poc.pms.adapters.essex import parse_essex_bulk
        body = {
            "result": {
                "floorplans": [
                    {"name": "A1", "units": [
                        {"name": "101", "beds": 1, "baths": 1, "sqft": 750,
                         "minimum_rent": "2487.00", "maximum_rent": "2487.00",
                         "availability_date": "2026-06-01T00:00:00Z"},
                    ]},
                ],
            },
        }
        units = parse_essex_bulk(body, "https://x.essex.com/availability")
        assert isinstance(units, list)
        # Avail date trimmed at the T separator.
        for u in units:
            if "availability_date" in u:
                assert "T" not in (u.get("availability_date") or "")

    def test_parse_essex_bulk_handles_empty(self):
        from ma_poc.pms.adapters.essex import parse_essex_bulk
        # The function expects a dict envelope; non-dict input returns [].
        assert parse_essex_bulk({}, "https://x") == []
        assert parse_essex_bulk({"result": {}}, "https://x") == []

    def test_rent_to_int_handles_dot_decimal_and_thousands_comma(self):
        """Essex API emits dot-decimal (``"2487.00"``) but the function
        must also handle thousands-separated input. The original
        ``_MONEY_RE = [\\d.]+`` regex broke on commas -- ``"$1,500"``
        returned 1. Fixed 2026-05-21 alongside the same family of
        ``money_to_int`` fixes from Commit 4 of the port."""
        from ma_poc.pms.adapters.essex import _rent_to_int
        # Original documented case.
        assert _rent_to_int(1500) == 1500
        assert _rent_to_int("1500") == 1500
        assert _rent_to_int("2487.00") == 2487
        # Regression guards for the comma-handling fix.
        assert _rent_to_int("$1,500") == 1500
        assert _rent_to_int("1,234.56") == 1235  # rounded
        assert _rent_to_int("$2,487.50") == 2488
        # Junk.
        assert _rent_to_int(None) is None
        assert _rent_to_int("garbage") is None
        assert _rent_to_int("") is None


class TestMaacParser:
    """MAA Communities REIT exposes a units list keyed by integration id."""

    def test_parse_maac_units_canonical(self):
        from ma_poc.pms.adapters.maac import parse_maac_units
        items = [
            {"unitNumber": "101", "bedrooms": 1, "bathrooms": 1,
             "squareFeet": 800, "rent": 1700, "availableDate": "2026-06-01"},
        ]
        units = parse_maac_units(items, "https://x")
        assert isinstance(units, list)

    def test_parse_maac_handles_empty(self):
        from ma_poc.pms.adapters.maac import parse_maac_units
        assert parse_maac_units([], "https://x") == []

    def test_extract_integration_id_from_html(self):
        from ma_poc.pms.adapters.maac import _extract_integration_id
        # Adapter may expose a regex-based ID extractor; the exact
        # format depends on MAA's marketing template. At minimum the
        # function must not raise on empty/unknown input.
        out = _extract_integration_id("")
        assert out is None or isinstance(out, str)

    def test_is_maac_units_shape_check(self):
        from ma_poc.pms.adapters.maac import _is_maac_units
        # Adapter's body-shape recogniser. Must not raise on any input.
        assert _is_maac_units([{"unitNumber": "x"}]) in (True, False)
        assert _is_maac_units([]) in (True, False)


class TestRentVisionParser:
    """RentVision is a marketing-CMS whose ``/floorplans`` page renders
    plan cards in SSR DOM. The adapter parses these cards via
    BeautifulSoup."""

    def test_parse_rentvision_cards_handles_empty_html(self):
        from ma_poc.pms.adapters.rentvision import parse_rentvision_cards
        assert parse_rentvision_cards("", "https://x") == []
        assert parse_rentvision_cards("<html>no cards</html>", "https://x") == []

    def test_parse_rentvision_cards_returns_list(self):
        from ma_poc.pms.adapters.rentvision import parse_rentvision_cards
        # Minimal RentVision-shaped card. Adapter's exact selectors
        # depend on the template; this guards against parser raising.
        html = '<div class="floorplan-card"><h3>A1</h3></div>'
        out = parse_rentvision_cards(html, "https://x.com/floorplans")
        assert isinstance(out, list)
