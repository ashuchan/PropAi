"""Smoke tests for the ResMan adapter ported on 2026-05-21 (P2a of
MAY21_POST_MERGE_IMPACT_ANALYSIS.md).

ResMan exposes a public availability portal at
``<client>.myresman.com/Portal/Applicants/Availability?a=&p=`` linked from
the marketing /floorplans/ page. The portal embeds a
``var unitTypes = [ ... ]`` JS array with floorplan groups + per-unit
Pricing[]. Not Cloudflare-fronted, so curl_cffi reaches it proxy-less.

Live-network tests are out of scope; these verify registration, detector
routing, the empty-input contract, fingerprint exposure, and parser
behaviour on canonical/edge-case shapes.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.resman import ResManAdapter
from ma_poc.pms.detector import _STRATEGY_BY_PMS, detect_pms


class TestRegistration:
    def test_resman_registers(self):
        from ma_poc.pms.adapters.registry import all_adapters
        names = {a.pms_name for a in all_adapters()}
        assert "resman" in names

    def test_get_adapter_returns_correct_class(self):
        from ma_poc.pms.adapters.registry import get_adapter
        assert isinstance(get_adapter("resman"), ResManAdapter)

    def test_strategy_map_has_api_first(self):
        assert _STRATEGY_BY_PMS["resman"] == "api_first"

    def test_link_hop_priors_populated(self):
        from ma_poc.pms.signal_engine.defaults import DEFAULT_PMS_PRIORS
        assert "resman" in DEFAULT_PMS_PRIORS
        assert DEFAULT_PMS_PRIORS["resman"]


class TestDetectorRouting:
    def test_resman_via_myresman_host(self):
        html = (
            '<a href="https://acme.myresman.com/Portal/Applicants/'
            'Availability?a=123&p=abc">Apply Now</a>'
        )
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "resman"

    def test_resman_via_portal_path(self):
        html = '<a href="/portal/applicants/availability?a=1">x</a>'
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "resman"

    def test_resman_wins_when_paired_with_weak_rentcafe_marker(self):
        """A property that carries BOTH a legacy RentCafe widget link
        and a ResMan portal anchor (real combo seen in canary 842-pool)
        must route to ResMan — the structurally-specific signal."""
        html = (
            '<!-- legacy widget -->'
            '<script src="https://widgets.rentcafe.com/x.js"></script>'
            '<a href="https://acme.myresman.com/Portal/Applicants/'
            'Availability?a=1&p=2">Available units</a>'
        )
        r = detect_pms("https://example.com/", page_html=html)
        assert r.pms == "resman"


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
    async def test_adapter_handles_empty_ctx(self):
        result = await ResManAdapter().extract(None, self._make_ctx())  # type: ignore[arg-type]
        assert result.units == []


def test_fingerprints_exposed():
    fps = ResManAdapter().static_fingerprints()
    assert isinstance(fps, list) and len(fps) > 0


# ────────────────────────────────────────────────────────────────────
# Parser-logic coverage.
# ────────────────────────────────────────────────────────────────────


class TestResManParser:
    def test_parse_resman_unittypes_admits_available_units(self):
        from ma_poc.pms.adapters.resman import parse_resman_unittypes
        data = [
            {
                "Bedrooms": 1,
                "Bathrooms": 1,
                "MinSquareFootage": 650,
                "MaxSquareFootage": 700,
                "MarketRent": 1500,
                "Units": [
                    {"Number": "101", "UnitType": "A1", "Floor": 1,
                     "AvailableDate": "/Date(1735689600000)/",
                     "Pricing": [{"Rent": 1500, "Term": 12}],
                     "SquareFootage": 680},
                ],
            },
        ]
        units = parse_resman_unittypes(data, "https://x")
        assert isinstance(units, list)
        assert len(units) == 1
        u = units[0]
        assert u["unit_number"] == "101"
        assert u["bedrooms"] == "1"
        assert u["availability_status"].upper() == "AVAILABLE"

    def test_parse_resman_plan_level_fallback_emits_when_no_units(self):
        from ma_poc.pms.adapters.resman import parse_resman_unittypes
        data = [{
            "Bedrooms": 2, "Bathrooms": 2, "MarketRent": 2000,
            "MinSquareFootage": 900, "MaxSquareFootage": 1000,
            "Units": [],
        }]
        units = parse_resman_unittypes(data, "https://x")
        # Plan-level row should still emit (downstream classifies it).
        assert len(units) == 1

    def test_parse_resman_handles_empty_input(self):
        from ma_poc.pms.adapters.resman import parse_resman_unittypes
        assert parse_resman_unittypes([], "https://x") == []

    def test_find_resman_availability_url(self):
        from ma_poc.pms.adapters.resman import find_resman_availability_url
        url = (
            'https://acme.myresman.com/Portal/Applicants/'
            'Availability?a=12345&p=abc-def'
        )
        html = f'<a href="{url}">apply</a>'
        out = find_resman_availability_url(html)
        assert out is not None
        assert "myresman.com" in out

    def test_find_resman_availability_url_returns_none_on_empty(self):
        from ma_poc.pms.adapters.resman import find_resman_availability_url
        assert find_resman_availability_url("") is None
        assert find_resman_availability_url("<p>no resman here</p>") is None

    def test_ms_to_iso_handles_canonical_dotnet_date(self):
        from ma_poc.pms.adapters.resman import _ms_to_iso
        # 1735689600000 ms = 2025-01-01 UTC.
        out = _ms_to_iso("/Date(1735689600000)/")
        assert out.startswith("2025-01-01")

    def test_ms_to_iso_handles_minvalue_sentinel(self):
        from ma_poc.pms.adapters.resman import _ms_to_iso
        # ASP.NET DateTime.MinValue serialises as /Date(-62135596800000)/
        out = _ms_to_iso("/Date(-62135596800000)/")
        # Either empty (caller drops it) or carries the placeholder; the
        # important invariant is that the function does NOT raise on
        # negative epoch / MinValue.
        assert isinstance(out, str)

    def test_ms_to_iso_handles_none_and_garbage(self):
        from ma_poc.pms.adapters.resman import _ms_to_iso
        assert _ms_to_iso(None) == ""
        assert _ms_to_iso("garbage") == ""
        assert _ms_to_iso("") == ""
