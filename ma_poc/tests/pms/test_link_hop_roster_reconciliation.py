"""Evidence-backed repeated-roster reconciliation regressions.

Pinned production shapes:

* Lake Haven: two equivalent Jonah URL spellings returned overlapping
  36-apartment snapshots with a routine price change.
* Village of Cross Creek / Brandon Place / Centennial Gardens: a Razz/Vike
  full catalogue overlapped ResMan's public available subset.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.pms.scraper import (
    _build_link_hop_roster_snapshot,
    _merge_unit_source_provenance,
    _reconcile_accumulated_rosters,
)


def _snapshot(
    units: list[dict[str, Any]],
    *,
    url: str,
    tier: str,
) -> dict[str, Any]:
    return _build_link_hop_roster_snapshot(
        units=units,
        source_url=url,
        final_url=url,
        tier=tier,
        adapter="resman" if "RESMAN" in tier else "generic",
        body={"url": url, "units": units},
        status=200,
        property_id="TEST",
    )


def test_lake_haven_price_change_does_not_create_a_second_apartment() -> None:
    first = [
        {
            "unit_id": f"LH-{i:03d}",
            "floor_plan_id": "A-RENOVATED",
            "floor_plan_name": "A Renovated",
            "sqft": 700,
            "rent_low": 1_900 + i,
            "availability_date": "2025-09-05",
            "availability_status": "AVAILABLE",
        }
        for i in range(1, 37)
    ]
    second = [
        {
            "unit_id": f"LH-{i:03d}",
            "floor_plan_id": "A-RENOVATED",
            "floor_plan_name": "A Renovated",
            "sqft": 700,
            "rent_low": 2_000 + i,
            "availability_date": "2025-09-05",
            "availability_status": "AVAILABLE",
        }
        for i in range(2, 38)
    ]

    rows = _reconcile_accumulated_rosters(
        [
            _snapshot(
                first,
                url="https://lakehavenluxury.com/floorplans",
                tier="TIER_1_DOM_JONAH_SSR_UNITS",
            ),
            _snapshot(
                second,
                url="http://www.lakehavenluxury.com/floorplans/",
                tier="TIER_1_DOM_JONAH_SSR_UNITS",
            ),
        ]
    )

    assert len(rows) == 37
    assert len({row["unit_id"] for row in rows}) == 37
    # Same-tier repeated snapshots use deterministic first-success precedence;
    # the mutable price is never part of apartment identity.
    assert next(row for row in rows if row["unit_id"] == "LH-002")["rent_low"] == 1_902


@pytest.mark.parametrize(
    ("catalogue_count", "available_count"),
    [(234, 13), (200, 10), (713, 17)],
    ids=("village-cross-creek", "brandon-place", "centennial-gardens"),
)
def test_resman_available_subset_overlays_full_catalogue_without_duplicates(
    catalogue_count: int,
    available_count: int,
) -> None:
    catalogue = [
        {
            "unit_id": f"U-{i:04d}",
            "floor_plan_id": f"PLAN-{i % 4}",
            "floor_plan_name": f"Plan {i % 4}",
            "beds": i % 3,
            "baths": 1,
            "sqft": 700 + (i % 4) * 100,
            "rent_low": 1_200 + i,
            "availability_status": "AVAILABLE",
            "available_date": "2026-08-02",
        }
        for i in range(catalogue_count)
    ]
    available = [
        {
            "unit_id": f"U-{i:04d}",
            "floor_plan_id": f"PLAN-{i % 4}",
            "floor_plan_name": f"Plan {i % 4}",
            "rent_low": 1_500 + i,
            "availability_status": "AVAILABLE",
            "availability_date": "2026-09-15",
            "floor": 2,
            "lease_term": "12",
        }
        for i in range(available_count)
    ]

    snapshots = [
        _snapshot(
            available,
            url=("https://cig.myresman.com/Portal/Applicants/Availability?a=1072&p=property-guid"),
            tier="TIER_1_API_RESMAN",
        ),
        _snapshot(
            catalogue,
            url="https://property.example/models",
            tier="TIER_1_5_EMBEDDED",
        ),
    ]
    rows = _reconcile_accumulated_rosters(snapshots)

    assert len(rows) == catalogue_count
    assert len({row["unit_id"] for row in rows}) == catalogue_count

    overlapping = next(row for row in rows if row["unit_id"] == "U-0000")
    assert overlapping["rent_low"] == 1_500
    assert overlapping["availability_date"] == "2026-09-15"
    assert overlapping["lease_term"] == "12"
    assert overlapping["sqft"] == 700

    catalogue_only = next(row for row in rows if row["unit_id"] == f"U-{available_count:04d}")
    assert catalogue_only["availability_status"] == "UNAVAILABLE"
    assert "available_date" not in catalogue_only
    assert "availability_date" not in catalogue_only

    provenance = _merge_unit_source_provenance(snapshots)
    assert len(provenance) == 2
    assert {item["response_kind"] for item in provenance} == {
        "available_subset",
        "unit_roster",
    }
    assert len({item["response_sha256"] for item in provenance}) == 2


def test_repeated_public_label_with_distinct_buildings_fails_open() -> None:
    rows = _reconcile_accumulated_rosters(
        [
            _snapshot(
                [
                    {
                        "unit_id": "101",
                        "building": "North",
                        "floor_plan_id": "A1",
                        "rent_low": 1_500,
                    },
                    {
                        "unit_id": "101",
                        "building": "South",
                        "floor_plan_id": "A1",
                        "rent_low": 1_600,
                    },
                ],
                url="https://example.com/floorplans",
                tier="TIER_1_DOM",
            )
        ]
    )

    assert len(rows) == 2
    assert {row["building"] for row in rows} == {"North", "South"}


def test_generated_ids_reconcile_on_shared_source_unit_id() -> None:
    rows = _reconcile_accumulated_rosters(
        [
            _snapshot(
                [{"unit_id": "88763b67-B106", "source_unit_id": "B106", "rent_low": 1400}],
                url="https://example.com/models",
                tier="TIER_1_5_EMBEDDED",
            ),
            _snapshot(
                [{"unit_id": "a903f812-B106", "source_unit_id": "B106", "rent_low": 1500}],
                url="https://example.com/floorplans/b1",
                tier="TIER_1_5_EMBEDDED",
            ),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["source_unit_id"] == "B106"


def test_resman_native_overlap_merges_even_when_catalogue_plan_is_stale() -> None:
    rows = _reconcile_accumulated_rosters(
        [
            _snapshot(
                [
                    {
                        "unit_id": "new-U1",
                        "source_unit_id": "U1",
                        "floor_plan_name": "Current Plan",
                        "rent_low": 1700,
                    }
                ],
                url="https://x.myresman.com/Portal/Applicants/Availability?a=1",
                tier="TIER_1_API_RESMAN",
            ),
            _snapshot(
                [
                    {
                        "unit_id": "old-U1",
                        "source_unit_id": "U1",
                        "floor_plan_name": "Legacy Plan",
                        "rent_low": 1400,
                    },
                    {"unit_id": "old-U2", "source_unit_id": "U2", "rent_low": 1450},
                ],
                url="https://property.example/models",
                tier="TIER_1_5_EMBEDDED",
            ),
        ]
    )
    assert len(rows) == 2
    current = next(row for row in rows if row["source_unit_id"] == "U1")
    assert current["rent_low"] == 1700


class _Outcome:
    def __init__(self, value: str) -> None:
        self.value = value


class _Fetch:
    def __init__(self, url: str, outcome: str = "OK") -> None:
        self.outcome = _Outcome(outcome)
        self.status = 200 if outcome == "OK" else 503
        self.body = f"<html>{url}</html>".encode()
        self.final_url = url
        self.elapsed_ms = 1
        self.content_type = "text/html"
        self.captcha_detected = False
        self.error_signature = None
        self.identity_ua_hash = "test"
        self.render_mode = type("Mode", (), {"value": "RENDER"})()
        self.headers: dict[str, str] = {}

    def to_dict(self) -> dict[str, str]:
        return {"outcome": self.outcome.value}


@pytest.mark.asyncio
async def test_successful_equivalent_url_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc import fetch as fetch_mod
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.pms.detector import detect_pms

    canonical = "https://example.test/floorplans"
    equivalent = "http://www.example.test/floorplans/"
    detail = "https://example.test/floorplans/a1/"
    fetched: list[str] = []

    async def fake_fetch(task: Any) -> _Fetch:
        fetched.append(task.url)
        return _Fetch(task.url)

    async def fake_scrape(*, base_url: str, **_: Any) -> dict[str, Any]:
        if base_url == canonical:
            return {
                "units": [{"unit_id": "101", "rent_low": 1_500}],
                "_embedded_floorplan_subpage_hints": [(detail, "floorplan_subpage")],
                "extraction_tier_used": "TIER_1_DOM_JONAH_SSR_UNITS",
            }
        if base_url == equivalent:
            pytest.fail("equivalent successful surface must not be re-scraped")
        if base_url == detail:
            return {
                "units": [{"unit_id": "102", "rent_low": 1_700}],
                "extraction_tier_used": "TIER_1_DOM_JONAH_SSR_UNITS",
            }
        return {"units": []}

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_BUDGET_S", 30)
    with (
        patch.object(fetch_mod, "fetch", new=fake_fetch),
        patch.object(scraper_mod, "scrape", new=fake_scrape),
    ):
        result = await scraper_mod._try_link_hop(
            entry_url="https://example.test/",
            entry_page_html="<html></html>",
            detected=detect_pms("https://example.test/"),
            profile=None,
            expected_total_units=None,
            property_id="URL-SUCCESS",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=[canonical, equivalent],
        )

    assert equivalent not in fetched
    assert detail in fetched
    assert result is not None
    assert {row["unit_id"] for row in result["units"]} == {"101", "102"}


@pytest.mark.asyncio
async def test_equivalent_url_remains_eligible_after_actual_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc import fetch as fetch_mod
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.pms.detector import detect_pms

    failed = "https://example.test/floorplans"
    recovered = "http://www.example.test/floorplans/"
    fetched: list[str] = []

    async def fake_fetch(task: Any) -> _Fetch:
        fetched.append(task.url)
        return _Fetch(task.url, "HARD_FAIL" if task.url == failed else "OK")

    async def fake_scrape(*, base_url: str, **_: Any) -> dict[str, Any]:
        assert base_url == recovered
        return {
            "units": [{"unit_id": "201", "rent_low": 1_800}],
            "extraction_tier_used": "TIER_1_DOM_JONAH_SSR_UNITS",
        }

    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_BUDGET_S", 30)
    with (
        patch.object(fetch_mod, "fetch", new=fake_fetch),
        patch.object(scraper_mod, "scrape", new=fake_scrape),
    ):
        result = await scraper_mod._try_link_hop(
            entry_url="https://example.test/",
            entry_page_html="<html></html>",
            detected=detect_pms("https://example.test/"),
            profile=None,
            expected_total_units=None,
            property_id="URL-FALLBACK",
            csv_row=None,
            max_hops=2,
            llm_navigation_hints=[failed, recovered],
        )

    assert fetched[:2] == [failed, recovered]
    assert result is not None
    assert result["units"][0]["unit_id"] == "201"
