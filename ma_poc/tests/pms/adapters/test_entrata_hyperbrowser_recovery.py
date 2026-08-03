"""Production-safe Hyperbrowser drill for Entrata conventional grids."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.fetch.hyperbrowser_backend import reset_hyperbrowser_property_counts
from ma_poc.pms.adapters._entrata_hb_recovery import (
    EntrataHbRecovery,
    _validated_units,
    recover_entrata_hb_conventional,
    strict_conventional_url,
)
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    parse_entrata_pp_unit_cards,
    parse_entrata_prospectportal_html,
)
from ma_poc.pms.detector import DetectedPMS
from ma_poc.tests.pms.adapters.test_entrata_prospectportal import _PP_HTML

_FIXTURES = Path(__file__).parent / "fixtures" / "entrata"
_GRID_URL = "https://foxlake.prospectportal.com/knoxville-knoxville/fox-lake-apartment-homes/conventional/"
_DETAIL_URL = (
    "https://foxlake.prospectportal.com/floorplans/knoxville-knoxville-TN/"
    "fox-lake-apartment-homes/abbington-1440-1/"
)


def _index_html() -> str:
    return (_FIXTURES / "prospectportal_index_with_plan_links_foxlake.html").read_text()


def _detail_html() -> str:
    return (_FIXTURES / "prospectportal_per_plan_unit_cards_foxlake.html").read_text()


def _ctx() -> AdapterContext:
    seed = f'<a href="{_GRID_URL}">Floor Plans</a>'
    ctx = AdapterContext(
        base_url="https://www.foxlakeapartments.com/",
        detected=DetectedPMS(
            pms="entrata",
            confidence=0.9,
            recommended_strategy="api_first",
        ),
        profile=None,
        expected_total_units=None,
        property_id="fox-1",
        fetch_result=SimpleNamespace(
            body=seed.encode(),
            final_url="https://www.foxlakeapartments.com/",
        ),
        property_name="Fox Lake Apartment Homes",
    )
    setattr(ctx, "_api_responses", [])
    return ctx


def _roster_row(
    number: str,
    tier: str,
    *,
    building: str = "",
    rent: int = 1800,
) -> dict[str, Any]:
    return {
        "unit_number": number,
        "building": building,
        "market_rent_low": rent,
        "extraction_tier": tier,
    }


def test_coherent_roster_equal_sets_prefer_scoped_detail_rows() -> None:
    rows = [
        _roster_row("101", "TIER_1_DOM_ENTRATA_MODERN"),
        _roster_row("102", "TIER_1_DOM_ENTRATA_MODERN"),
        _roster_row("101", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="A"),
        _roster_row("102", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="A"),
    ]

    selected = _validated_units(rows)

    assert [(row["building"], row["unit_number"]) for row in selected] == [
        ("A", "101"),
        ("A", "102"),
    ]


def test_coherent_roster_modern_strict_superset_wins() -> None:
    rows = [
        _roster_row("101", "TIER_1_DOM_ENTRATA_MODERN"),
        _roster_row("102", "TIER_1_DOM_ENTRATA_MODERN"),
        _roster_row("101", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="A"),
    ]

    selected = _validated_units(rows)

    assert [row["unit_number"] for row in selected] == ["101", "102"]
    assert all(row["extraction_tier"] == "TIER_1_DOM_ENTRATA_MODERN" for row in selected)


def test_coherent_roster_detail_superset_keeps_same_number_across_buildings() -> None:
    rows = [
        _roster_row("101", "TIER_1_DOM_ENTRATA_MODERN"),
        _roster_row("101", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="A"),
        _roster_row("101", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="B"),
        _roster_row("102", "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL", building="B"),
    ]

    selected = _validated_units(rows)

    assert [(row["building"], row["unit_number"]) for row in selected] == [
        ("A", "101"),
        ("B", "101"),
        ("B", "102"),
    ]


class _FakePage:
    def __init__(
        self,
        *,
        detail_status: int = 200,
        detail_body: str = "",
        xhr_body: str = "",
        index_html: str | list[str] | None = None,
    ) -> None:
        self.url = _GRID_URL
        self.detail_status = detail_status
        self.detail_body = detail_body
        self.xhr_body = xhr_body
        self.index_html = index_html or _index_html()
        self.content_calls = 0
        self.goto_calls: list[str] = []
        self.fetch_paths: list[str] = []

    async def goto(self, url: str, **_: Any) -> None:
        self.goto_calls.append(url)

    async def content(self) -> str:
        self.content_calls += 1
        if isinstance(self.index_html, list):
            offset = min(self.content_calls - 1, len(self.index_html) - 1)
            return self.index_html[offset]
        return self.index_html

    async def evaluate(self, _script: str, path: str) -> dict[str, Any]:
        self.fetch_paths.append(path)
        if "view_unit_spaces" in path:
            body = self.xhr_body or "<html></html>"
        else:
            body = self.detail_body if "abbington-1440-1" in path else "<html></html>"
        return {
            "status": self.detail_status if "abbington-1440-1" in path else 200,
            "oversized": False,
            "body": body,
        }


class _FakeSession:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.opened = False
        self.closed = False

    async def open(self) -> _FakePage:
        self.opened = True
        return self.page

    async def close(self) -> None:
        self.closed = True


def test_strict_conventional_url_accepts_identity_matched_pp_twin() -> None:
    html = f'<a href="{_GRID_URL}">Floor Plans</a>'
    assert (
        strict_conventional_url(
            html,
            "https://www.foxlakeapartments.com/",
            "Fox Lake Apartment Homes",
        )
        == _GRID_URL
    )


@pytest.mark.parametrize(
    ("base_url", "name"),
    [
        (
            "https://villagewestside.prospectportal.com/dallas/the-village-westside/conventional/",
            "The Village Westside",
        ),
        (
            "https://www.ptlamgmt.com/eugene/the-pearl/conventional/",
            "The Pearl",
        ),
        (
            "https://www.lakemerrittapartments.com/oakland/grand-lake-towers/conventional/",
            "Grand Lake Towers",
        ),
    ],
)
def test_strict_conventional_url_accepts_identity_matched_source_grid(
    base_url: str,
    name: str,
) -> None:
    assert strict_conventional_url("<html>No self-link</html>", base_url, name) == base_url


@pytest.mark.parametrize(
    ("grid_url", "base_url", "name"),
    [
        (
            "https://www.middlebranchmanor.com/baltimore/"
            "middle-branch-manor-apartments-townhomes/conventional/",
            "http://www.middlebranchmanor.com/",
            "Middle Branch Apartments and Townhomes",
        ),
        (
            "https://www.liveeland.com/phoenixville/eland-downe-townhomes/conventional/",
            "https://www.liveeland.com/",
            "Eland Downe Townhouses",
        ),
        (
            "https://www.springridgeonfletcher.com/hayward/spring-ridge-apartments/conventional/",
            "http://www.springridgeonfletcher.com/",
            "Spring Ridge on Fletcher",
        ),
    ],
)
def test_strict_conventional_url_accepts_safe_property_name_variants(
    grid_url: str,
    base_url: str,
    name: str,
) -> None:
    html = f'<a href="{grid_url}">Floor Plans</a>'
    assert strict_conventional_url(html, base_url, name) == grid_url


def test_strict_conventional_url_accepts_identity_matched_microsite() -> None:
    grid_url = (
        "https://www.greatnorthernvillageapartmentsnortholmsted.com/"
        "north-olmsted/great-northern-village/conventional/"
    )
    html = f'<form action="{grid_url}"></form>'
    assert (
        strict_conventional_url(
            html,
            "http://www.burtoncarol.com/ohio/great-northern-village",
            "Great Northern Village",
        )
        == grid_url
    )


def test_strict_conventional_url_accepts_published_legacy_property_route() -> None:
    legacy_url = (
        "https://www.meritumsheelyfarms.com/Apartments/module/property_info/"
        "property%5Bid%5D/1276519/conventional/"
    )
    html = f'<a href="{legacy_url}">Floor Plans</a>'
    assert (
        strict_conventional_url(
            html,
            "https://www.meritumsheelyfarms.com/",
            "Meritum Sheely Farms",
        )
        == legacy_url
    )


def test_strict_conventional_url_rejects_cross_host_legacy_property_route() -> None:
    legacy_url = (
        "https://meritumsheelyfarms.example/Apartments/module/property_info/"
        "property%5Bid%5D/1276519/conventional/"
    )
    html = f'<a href="{legacy_url}">Floor Plans</a>'
    assert (
        strict_conventional_url(
            html,
            "https://www.meritumsheelyfarms.com/",
            "Meritum Sheely Farms",
        )
        == ""
    )


def test_strict_conventional_url_accepts_published_legacy_property_root() -> None:
    legacy_url = (
        "https://concordcourtapartmentswestover.prospectportal.com/"
        "Apartments/module/property_info/property%5Bid%5D/34247/"
    )
    html = f'<a href="{legacy_url}">Floor Plans</a>'
    assert (
        strict_conventional_url(
            html,
            "http://www.rentconcordcourtapts.com/",
            "Concord Court Apartments",
        )
        == legacy_url
    )


def test_strict_conventional_url_rejects_foreign_legacy_property_root() -> None:
    legacy_url = (
        "https://concordcourtapartments.example/Apartments/module/property_info/property%5Bid%5D/34247/"
    )
    html = f'<a href="{legacy_url}">Floor Plans</a>'
    assert (
        strict_conventional_url(
            html,
            "http://www.rentconcordcourtapts.com/",
            "Concord Court Apartments",
        )
        == ""
    )


@pytest.mark.parametrize(
    ("html", "name"),
    [
        (
            '<a href="https://sibling.prospectportal.com/austin/sibling/conventional/">x</a>',
            "Fox Lake Apartment Homes",
        ),
        (
            f'<a href="{_GRID_URL}?property=other">x</a>',
            "Fox Lake Apartment Homes",
        ),
        (
            '<a href="https://foxlake.prospectportal.com:444/knoxville/fox-lake/conventional/">x</a>',
            "Fox Lake Apartment Homes",
        ),
        (f'<a href="{_GRID_URL}">x</a>', ""),
    ],
)
def test_strict_conventional_url_rejects_unscoped_routes(html: str, name: str) -> None:
    assert (
        strict_conventional_url(
            html,
            "https://www.foxlakeapartments.com/",
            name,
        )
        == ""
    )


@pytest.mark.asyncio
async def test_single_session_walk_recovers_real_priced_units(monkeypatch) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    monkeypatch.setenv("HYPERBROWSER_MAX_CALLS_PER_PROPERTY", "2")
    reset_hyperbrowser_property_counts()
    page = _FakePage(detail_body=_detail_html())
    session = _FakeSession(page)

    outcome = await recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted and outcome.complete
    assert [row["unit_number"] for row in outcome.units] == [
        "8700",
        "8940",
        "8882",
        "8924",
        "8989",
    ]
    assert all(float(row["market_rent_low"]) > 0 for row in outcome.units)
    assert len(outcome.plan_rows) == 7
    assert outcome.html_responses
    assert all(response.get("body") for response in outcome.html_responses)
    assert outcome.unit_source_provenance
    assert {
        row["source_response_sha256"] for row in outcome.units
    } <= {
        record["response_sha256"] for record in outcome.unit_source_provenance
    }
    assert page.goto_calls == [_GRID_URL]
    assert sum("view_unit_spaces" in path for path in page.fetch_paths) == 7
    assert sum("/floorplans/" in path for path in page.fetch_paths) == 7
    assert session.opened and session.closed


@pytest.mark.asyncio
async def test_same_session_retries_false_200_index_shell_once(monkeypatch) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _FakePage(
        xhr_body=_PP_HTML,
        index_html=[
            "<html><body>Checking your browser</body></html>",
            _index_html(),
        ],
    )
    session = _FakeSession(page)

    outcome = await recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted
    assert [row["unit_number"] for row in outcome.units] == ["1306", "1406"]
    assert page.goto_calls == [_GRID_URL, _GRID_URL]
    assert page.content_calls == 2
    assert session.opened and session.closed


@pytest.mark.asyncio
async def test_single_session_uses_identity_matched_seed_plan_when_grid_has_none(
    monkeypatch,
) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    ctx = _ctx()
    ctx.fetch_result = SimpleNamespace(
        body=(
            f'<a href="{_GRID_URL}">Floor Plans</a>'
            f'<a href="{_DETAIL_URL}">Abbington</a>'
            '<a href="https://foxlake.prospectportal.com/austin/sibling/'
            'floorplans/a1-999/">Sibling</a>'
        ),
        final_url="https://www.foxlakeapartments.com/",
    )
    page = _FakePage(
        detail_body=_detail_html(),
        index_html="<html><body>Grid shell without plan links</body></html>",
    )
    session = _FakeSession(page)

    outcome = await recover_entrata_hb_conventional(
        ctx,
        session_factory=lambda: session,
    )

    assert outcome.attempted and not outcome.complete
    assert [row["unit_number"] for row in outcome.units] == [
        "8700",
        "8940",
        "8882",
        "8924",
        "8989",
    ]
    assert sum("/floorplans/" in path for path in page.fetch_paths) == 1
    assert all("sibling" not in path for path in page.fetch_paths)
    assert session.closed


@pytest.mark.asyncio
async def test_single_session_replays_view_unit_spaces_before_plan_pages(
    monkeypatch,
) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    page = _FakePage(xhr_body=_PP_HTML)
    session = _FakeSession(page)

    outcome = await recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted and outcome.complete
    assert [row["unit_number"] for row in outcome.units] == ["1306", "1406"]
    assert page.fetch_paths
    assert all("view_unit_spaces" in path for path in page.fetch_paths)
    assert session.closed


@pytest.mark.asyncio
async def test_fully_observed_empty_details_are_complete_plan_only(monkeypatch) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    index_without_active_xhr = _index_html().replace(
        "view_unit_spaces",
        "view_waitlist",
    )
    session = _FakeSession(
        _FakePage(
            detail_body="<html>No availability</html>",
            index_html=index_without_active_xhr,
        )
    )

    outcome = await recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted and outcome.complete
    assert outcome.units == []
    assert len(outcome.plan_rows) == 7
    assert session.closed


@pytest.mark.asyncio
async def test_failed_detail_fetch_is_not_authoritative_plan_only(monkeypatch) -> None:
    from ma_poc.pms.adapters import _entrata_hb_recovery as recovery

    monkeypatch.setattr(recovery, "_HB_SETTLE_SECONDS", 0)
    reset_hyperbrowser_property_counts()
    session = _FakeSession(_FakePage(detail_status=403, detail_body="<html>blocked</html>"))

    outcome = await recover_entrata_hb_conventional(
        _ctx(),
        session_factory=lambda: session,
    )

    assert outcome.attempted and not outcome.complete
    assert outcome.units == []
    assert session.closed


@pytest.mark.asyncio
async def test_adapter_returns_hb_unit_win_before_static_retry(monkeypatch) -> None:
    rows = parse_entrata_pp_unit_cards(_detail_html(), _DETAIL_URL)

    async def _recover(_ctx: Any) -> EntrataHbRecovery:
        return EntrataHbRecovery(
            attempted=True,
            complete=True,
            units=rows,
            html_responses=[{"url": _DETAIL_URL, "status": 200, "body": _detail_html()}],
            unit_source_provenance=[
                {
                    "provider": "entrata",
                    "source_url": _DETAIL_URL,
                    "response_sha256": "a" * 64,
                    "unit_count": len(rows),
                }
            ],
            winning_url=_GRID_URL,
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters._entrata_hb_recovery.recover_entrata_hb_conventional",
        _recover,
    )

    result = await EntrataAdapter().extract(None, _ctx())

    assert result.tier_used == "TIER_1_DOM_ENTRATA_PP_HYPERBROWSER_UNIT_LEVEL"
    assert [row["unit_number"] for row in result.units] == [
        "8700",
        "8940",
        "8882",
        "8924",
        "8989",
    ]
    assert result.winning_url == _GRID_URL
    assert result.html_responses[0]["url"] == _DETAIL_URL
    assert result.unit_source_provenance[0]["provider"] == "entrata"


@pytest.mark.asyncio
async def test_adapter_stops_after_complete_hb_plan_only(monkeypatch) -> None:
    plans = parse_entrata_prospectportal_html(_index_html(), _GRID_URL)

    async def _recover(_ctx: Any) -> EntrataHbRecovery:
        return EntrataHbRecovery(
            attempted=True,
            complete=True,
            plan_rows=plans,
            winning_url=_GRID_URL,
        )

    async def _must_not_fetch(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("complete HB plan-only result must skip static retries")

    monkeypatch.setattr(
        "ma_poc.pms.adapters._entrata_hb_recovery.recover_entrata_hb_conventional",
        _recover,
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.entrata._entrata_static_fetch",
        _must_not_fetch,
    )

    result = await EntrataAdapter().extract(None, _ctx())

    assert result.units == []
    assert len(result.plan_summaries) == 7
    assert result.tier_used == "TIER_1_DOM_ENTRATA_PP_HYPERBROWSER_PLAN_LEVEL"
    assert "COMPLETE_PLAN_ONLY" in result.errors[-1]
