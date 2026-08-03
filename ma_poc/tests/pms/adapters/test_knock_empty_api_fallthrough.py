"""Knock-detected-but-empty-API → emit ``/floorplans`` subpage hints.

2026-05-21: live-probed lochravenapts.com and manchesterlake.com (both
labeled ``T4_code_merge_cross_page`` in production) — the Knock detector
fires correctly, the Knock adapter extracts the IDs correctly, and the
Knock Doorway API returns 200 OK with **0 units**. The operator uses
Knock for marketing/chat but publishes inventory via server-side-rendered
HTML on ``/floorplans``.

Without this fallthrough, those properties land in the merge_cross_page
failure bucket — no adapter ever sees the SSR'd unit data. With the
fallthrough, the orchestrator's link-hop fetches ``/floorplans`` and the
Phase 6.2/6.3 HTML extractors (shipped same day) handle the units.

Real-HTML fixtures in ``ma_poc/tests/fixtures/knock_empty_api/`` are the
exact responses curl returns for both properties today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.adapters.knock import (
    KnockAdapter,
    _knock_empty_emit_subpage_hints,
    find_knock_ids,
    find_published_onesite_portals,
)

# Anchor on this file, not the process CWD — ``pytest tests/pms`` from inside
# ma_poc/ must resolve fixtures the same way a repo-root run does.
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knock_empty_api"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────
# Helper — pure-function behaviour
# ─────────────────────────────────────────────────────────────────────


def test_helper_emits_three_floorplan_paths() -> None:
    """The helper appends the canonical floor-plan path family."""
    result = AdapterResult()
    _knock_empty_emit_subpage_hints(result, "https://www.lochravenapts.com/")
    hints = getattr(result, "_embedded_floorplan_subpage_hints", [])
    urls = [u for u, _ in hints]
    assert urls == [
        "https://www.lochravenapts.com/floorplans",
        "https://www.lochravenapts.com/floor-plans",
        "https://www.lochravenapts.com/availability",
    ]


def test_helper_tags_hints_with_knock_empty_parser_id() -> None:
    """The orchestrator + downstream observability use parser_id to
    bucket where the hint came from. Pin so refactors keep the trace."""
    result = AdapterResult()
    _knock_empty_emit_subpage_hints(result, "https://www.example.com/")
    hints = getattr(result, "_embedded_floorplan_subpage_hints", [])
    parser_ids = {pid for _, pid in hints}
    assert parser_ids == {"knock_empty_fallthrough"}


def test_helper_is_idempotent() -> None:
    """Calling twice doesn't duplicate hints (e.g. when a property is
    re-scraped with intermediate cache state)."""
    result = AdapterResult()
    _knock_empty_emit_subpage_hints(result, "https://www.example.com/")
    first_count = len(getattr(result, "_embedded_floorplan_subpage_hints", []))
    _knock_empty_emit_subpage_hints(result, "https://www.example.com/")
    second_count = len(getattr(result, "_embedded_floorplan_subpage_hints", []))
    assert first_count == second_count == 3


def test_helper_merges_with_pre_existing_hints() -> None:
    """If another adapter already added subpage hints, we append rather
    than overwrite."""
    result = AdapterResult()
    result._embedded_floorplan_subpage_hints = [
        ("https://www.example.com/already-here", "some_other_source")
    ]
    _knock_empty_emit_subpage_hints(result, "https://www.example.com/")
    hints = result._embedded_floorplan_subpage_hints
    assert len(hints) == 4  # existing + 3 new
    assert hints[0] == (
        "https://www.example.com/already-here",
        "some_other_source",
    )


def test_helper_rejects_malformed_base_url() -> None:
    """Bad base_url → no emission, no exception. Protects against
    upstream bugs producing relative-only URLs."""
    result = AdapterResult()
    _knock_empty_emit_subpage_hints(result, "")
    assert not getattr(result, "_embedded_floorplan_subpage_hints", [])
    _knock_empty_emit_subpage_hints(result, "/floorplans")
    assert not getattr(result, "_embedded_floorplan_subpage_hints", [])


def test_helper_handles_base_url_with_path() -> None:
    """If base_url already has a path (e.g. after a same-host redirect),
    the urljoin should still produce origin-rooted hints."""
    result = AdapterResult()
    _knock_empty_emit_subpage_hints(
        result, "https://www.manchesterlake.com/manchester-lake-townhomes-richmond-va"
    )
    hints = getattr(result, "_embedded_floorplan_subpage_hints", [])
    urls = [u for u, _ in hints]
    # urljoin with absolute "/floorplans" replaces the path correctly
    assert urls == [
        "https://www.manchesterlake.com/floorplans",
        "https://www.manchesterlake.com/floor-plans",
        "https://www.manchesterlake.com/availability",
    ]


def test_published_onesite_portals_normalizes_and_deduplicates() -> None:
    html = r"""
    <a href="https://9131096aff.onlineleasing.realpage.com/">Apply</a>
    <script>const apply = "https:\/\/9131096aff.onlineleasing.realpage.com\/";</script>
    """

    assert find_published_onesite_portals(html) == [
        "https://9131096aff.onlineleasing.realpage.com/"
    ]


# ─────────────────────────────────────────────────────────────────────
# Adapter integration — real lochraven + manchesterlake fixtures
# ─────────────────────────────────────────────────────────────────────


def test_lochraven_html_extracts_knock_ids() -> None:
    """Sanity: the real lochraven HTML has the Knock init call. If this
    breaks, find_knock_ids regressed."""
    html = _load("lochraven_homepage.html")
    pub, kind, cid = find_knock_ids(html)
    assert pub == "c7907e16951011ee99af02ef25d8bb93"
    assert kind == "community"
    assert cid == "8d9defc953111ee4"


def test_manchesterlake_html_extracts_knock_ids() -> None:
    """Sanity: manchesterlake (after redirect-following) has the Knock
    init call."""
    html = _load("manchesterlake_homepage.html")
    pub, kind, cid = find_knock_ids(html)
    assert pub == "33ca8dfac11ed9c160271fc7d96ad8d0"
    assert kind == "community"
    assert cid == "ab84f11ef842046d"


class _Body:
    """Mimic the L1 fetch_result.body field on AdapterContext."""

    def __init__(self, html: str) -> None:
        self.body = html.encode("utf-8")
        self.final_url = ""


class _Ctx:
    """Minimal AdapterContext stub for the extract() path."""

    def __init__(self, html: str, base_url: str) -> None:
        self.fetch_result = _Body(html)
        self.base_url = base_url
        self.property_id = "test-prop"
        self.profile = None
        self.budget = {}


@pytest.mark.asyncio
async def test_extract_emits_hints_when_knock_api_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core contract: detector + ID-extraction succeed, the API
    returns empty, → the helper attaches /floorplans hints so link-hop
    can recover via HTML extraction."""
    # Stub the Knock API to return zero units (the real-world failure case)
    async def _stub_empty_api(comm_id: str, kind: str = "community") -> list[dict]:
        return []

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_empty_api
    )

    adapter = KnockAdapter()
    ctx = _Ctx(_load("lochraven_homepage.html"), "https://www.lochravenapts.com/")
    result = await adapter.extract(page=None, ctx=ctx)  # type: ignore[arg-type]

    assert result.units == []
    hints = getattr(result, "_embedded_floorplan_subpage_hints", [])
    urls = {u for u, _ in hints}
    assert "https://www.lochravenapts.com/floorplans" in urls
    assert "https://www.lochravenapts.com/floor-plans" in urls
    assert "https://www.lochravenapts.com/availability" in urls


@pytest.mark.asyncio
async def test_empty_jonah_knock_api_uses_native_resource_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knock gets first refusal, then exact Jonah inventory can win."""
    html = (
        "<html><script>JonahWidget.knock({init:["
        "'VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP',"
        "'community','3838514011eb718b']})</script></html>"
    )

    async def _stub_empty_api(comm_id: str, kind: str = "community") -> list[dict]:
        return []

    async def _stub_jonah_extract(self, page, ctx):  # type: ignore[no-untyped-def]
        return AdapterResult(
            units=[
                {
                    "unit_number": "B",
                    "source_ids": {"sightmap_unit_id": "4253949"},
                    "market_rent_low": 1460,
                    "bedrooms": "2",
                    "bathrooms": "1.5",
                    "sqft": "1030",
                }
            ],
            tier_used="TIER_1_DOM_JONAH_RESOURCE_JSON",
            winning_url="https://gsc.example/property/floorplans/",
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_empty_api
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.encoreskyline_template."
        "EncoreSkylineTemplateAdapter.extract",
        _stub_jonah_extract,
    )

    result = await KnockAdapter().extract(
        page=None,
        ctx=_Ctx(html, "https://gsc.example/property/"),  # type: ignore[arg-type]
    )
    assert result.tier_used == "TIER_1_DOM_JONAH_RESOURCE_JSON"
    assert result.units[0]["source_ids"] == {"sightmap_unit_id": "4253949"}
    assert not getattr(result, "_embedded_floorplan_subpage_hints", [])


@pytest.mark.asyncio
async def test_extract_does_not_emit_hints_when_api_has_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Don't pollute the hint stream when Knock actually returned data
    — the orchestrator would waste fetches on /floorplans that already
    have the same units."""
    fake_units = [
        {"unit_number": "101", "floor_plan_name": "A1", "bedrooms": "1",
         "bathrooms": "1", "sqft": "720", "market_rent_low": 1500}
    ]

    async def _stub_with_units(comm_id: str, kind: str = "community") -> list[dict]:
        return fake_units

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_with_units
    )

    adapter = KnockAdapter()
    ctx = _Ctx(_load("lochraven_homepage.html"), "https://www.lochravenapts.com/")
    result = await adapter.extract(page=None, ctx=ctx)  # type: ignore[arg-type]

    assert len(result.units) == 1
    hints = getattr(result, "_embedded_floorplan_subpage_hints", None)
    assert not hints, (
        f"hints emitted despite successful Knock API: {hints}"
    )


@pytest.mark.asyncio
async def test_extract_does_not_emit_hints_when_no_knock_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hints are only meaningful when we have positive Knock detection.
    A page with no knockDoorway.init call shouldn't trigger them — that
    would amount to 'try /floorplans on every property' which spams
    link-hop."""
    # Stub fetch in case the by-domain path tries
    async def _stub_empty(*a, **kw):  # type: ignore[no-untyped-def]
        return []
    async def _stub_by_domain(*a, **kw):  # type: ignore[no-untyped-def]
        return None, []

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_empty
    )
    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units_by_domain",
        _stub_by_domain,
    )

    adapter = KnockAdapter()
    html = "<html><body>No Knock here</body></html>"
    ctx = _Ctx(html, "https://www.example.com/")
    result = await adapter.extract(page=None, ctx=ctx)  # type: ignore[arg-type]

    assert result.units == []
    hints = getattr(result, "_embedded_floorplan_subpage_hints", None)
    assert not hints, (
        "hints emitted without a positive Knock detection — would "
        "spam link-hop on every non-Knock property"
    )


@pytest.mark.asyncio
async def test_extract_emits_hints_on_manchesterlake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling property — same pattern, different IDs. Confirms the
    fallthrough generalises beyond a single property."""
    async def _stub_empty(comm_id: str, kind: str = "community") -> list[dict]:
        return []

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_empty
    )

    adapter = KnockAdapter()
    ctx = _Ctx(
        _load("manchesterlake_homepage.html"),
        "https://www.manchesterlake.com/manchester-lake-townhomes-richmond-va",
    )
    result = await adapter.extract(page=None, ctx=ctx)  # type: ignore[arg-type]

    assert result.units == []
    urls = {u for u, _ in (result._embedded_floorplan_subpage_hints or [])}
    # urljoin with absolute "/floorplans" replaces the long path
    assert "https://www.manchesterlake.com/floorplans" in urls


@pytest.mark.asyncio
async def test_extract_emits_hints_when_knock_api_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Knock API throws (network error, 5xx, etc.) we still hit
    the fallthrough — empty result is empty result regardless of
    whether it came from a clean 200 or a thrown exception."""
    async def _stub_throws(*a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_throws
    )

    adapter = KnockAdapter()
    ctx = _Ctx(_load("lochraven_homepage.html"), "https://www.lochravenapts.com/")
    result = await adapter.extract(page=None, ctx=ctx)  # type: ignore[arg-type]

    # The exception is caught + logged; the fallthrough still runs.
    assert any("knock-api-error" in e for e in result.errors)
    urls = {u for u, _ in (result._embedded_floorplan_subpage_hints or [])}
    assert "https://www.lochravenapts.com/floorplans" in urls


@pytest.mark.asyncio
async def test_dynamic_dni_config_reaches_property_scoped_knock_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic Harbor Group config must take the ordinary Tier-1 API path."""
    html = """
    <script>
      const config = {
        dniLibrary: "https://doorway.knck.io/latest/doorway.min.js",
        dniId: "91011ebb76019d4d",
        dniApiKey: "ad96e5d25f696e657111eb979d127cae"
      };
    </script>
    """
    calls: list[tuple[str, str]] = []

    async def _stub_api(comm_id: str, kind: str = "community") -> list[dict]:
        calls.append((comm_id, kind))
        return [
            {
                "unit_number": "807",
                "unit_id": "knock_unit_id-6f34b85e",
                "source_ids": {"knock_unit_id": "6f34b85e"},
                "source_property_id": "2011814",
                "source_api_url": (
                    "https://doorway-api.knockrentals.com/"
                    "v1/property/2011814/units"
                ),
                "market_rent_low": 990,
            }
        ]

    monkeypatch.setattr(
        "ma_poc.pms.adapters.knock._fetch_knock_units", _stub_api
    )

    result = await KnockAdapter().extract(
        page=None,
        ctx=_Ctx(
            html,
            "https://www.hgliving.com/apartments/fl/jacksonville/bridgepoint/",
        ),  # type: ignore[arg-type]
    )

    assert calls == [("91011ebb76019d4d", "community")]
    assert len(result.units) == 1
    assert result.units[0]["source_property_id"] == "2011814"
    assert result.winning_url == (
        "https://doorway-api.knockrentals.com/"
        "v1/property/community/91011ebb76019d4d"
    )
    assert not getattr(result, "_embedded_floorplan_subpage_hints", [])


@pytest.mark.asyncio
async def test_empty_knock_uses_sole_published_onesite_native_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact-only Knock marker must not hide current OneSite units."""
    html = """
    <a class="knockDoorwayButton"
       href="https://9131096aff.onlineleasing.realpage.com/">Apply</a>
    """

    async def _stub_onesite(self, page, ctx):  # type: ignore[no-untyped-def]
        return AdapterResult(
            units=[
                {
                    "unit_number": "15",
                    "source_property_id": "4629273",
                    "source_api_url": (
                        "https://leasing.realpage.com/"
                        "RP.Leasing.AppService.WebHost/workflowstartup/"
                        "v1/4629273/English?BpmId=OLL.WorkflowStartUp"
                    ),
                    "market_rent_low": 1120,
                }
            ],
            tier_used="TIER_1_API_ONESITE_WORKFLOW",
            winning_url=(
                "https://leasing.realpage.com/"
                "RP.Leasing.AppService.WebHost/workflowstartup/"
                "v1/4629273/English"
            ),
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters.onesite.OneSiteAdapter.extract", _stub_onesite
    )

    result = await KnockAdapter().extract(
        page=None,
        ctx=_Ctx(html, "https://copperpointeapts.com/"),  # type: ignore[arg-type]
    )

    assert result.tier_used == "TIER_1_API_ONESITE_WORKFLOW"
    assert [unit["unit_number"] for unit in result.units] == ["15"]
    assert result.units[0]["source_property_id"] == "4629273"


@pytest.mark.asyncio
async def test_onesite_plan_summary_cannot_replace_empty_knock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <script src="https://doorway.knck.io/latest/doorway.min.js"></script>
    <a href="https://1733786.onlineleasing.realpage.com/">Apply</a>
    """

    async def _stub_plan_only(self, page, ctx):  # type: ignore[no-untyped-def]
        return AdapterResult(
            units=[
                {
                    "unit_number": "",
                    "source_property_id": "3370576",
                    "source_api_url": (
                        "https://leasing.realpage.com/"
                        "RP.Leasing.AppService.WebHost/workflowstartup/"
                        "v1/3370576/English"
                    ),
                    "market_rent_low": 1299,
                }
            ],
            plan_summaries=[{"floor_plan_name": "A1"}],
            tier_used="TIER_1_API_ONESITE_WORKFLOW",
        )

    monkeypatch.setattr(
        "ma_poc.pms.adapters.onesite.OneSiteAdapter.extract", _stub_plan_only
    )

    result = await KnockAdapter().extract(
        page=None,
        ctx=_Ctx(html, "https://coveatoverlakeapts.com/"),  # type: ignore[arg-type]
    )

    assert result.units == []
    assert any("no knockDoorway.init" in error for error in result.errors)


@pytest.mark.asyncio
async def test_multiple_onesite_portals_do_not_cross_property_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <span class="knockDoorwayButton"></span>
    <a href="https://111.onlineleasing.realpage.com/">One</a>
    <a href="https://222.onlineleasing.realpage.com/">Two</a>
    """

    async def _must_not_call(self, page, ctx):  # type: ignore[no-untyped-def]
        raise AssertionError("OneSite fallback must not run across two portals")

    monkeypatch.setattr(
        "ma_poc.pms.adapters.onesite.OneSiteAdapter.extract", _must_not_call
    )

    result = await KnockAdapter().extract(
        page=None,
        ctx=_Ctx(html, "https://portfolio.example/"),  # type: ignore[arg-type]
    )

    assert result.units == []
