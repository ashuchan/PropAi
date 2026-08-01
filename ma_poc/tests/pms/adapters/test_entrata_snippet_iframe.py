"""Strict EntrataSnippet property-owned iframe recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    _recover_entrata_snippet_units,
)
from ma_poc.pms.detector import DetectedPMS

PARENT_URL = "https://www.exampleplace.com/pricing"
IFRAME_ROOT = "https://entratasnipit.exampleplace.com/city/example-place/"
DETAIL_1 = (
    "https://entratasnipit.exampleplace.com/Apartments/module/"
    "property_floorplans/property%5Bid%5D/1166646/"
    "property_floorplan[id]/1047423/is_premium_view/1/"
    "occupancy_type/conventional/snippet_type/website/"
)
DETAIL_2 = DETAIL_1.replace("1047423", "1047424")


def _ctx(parent_html: str) -> AdapterContext:
    return AdapterContext(
        base_url=PARENT_URL,
        detected=DetectedPMS(pms="entrata", confidence=0.92),
        profile=None,
        expected_total_units=None,
        property_id="59649",
        fetch_result=SimpleNamespace(body=parent_html, final_url=PARENT_URL),
        property_name="Example Place",
        address="100 Main St",
        city="Columbus",
        state="OH",
        zip_code="43240",
        budget={
            "llm_api_calls": 0,
            "llm_dom_calls": 0,
            "llm_monolithic": 0,
            "link_hop": 0,
        },
    )


def _parent(*iframe_srcs: str) -> str:
    iframes = "".join(f'<iframe src="{src}"></iframe>' for src in iframe_srcs)
    return f"<html><body><h1>Example Place</h1>{iframes}</body></html>"


def _index(*links: tuple[str, str]) -> str:
    anchors = "".join(
        '<div class="inner-card-container">'
        f'<h2 class="fp-title">{name}</h2>'
        f'<a aria-label="View details of {name}" href="{url}">View Details</a>'
        "</div>"
        for url, name in links
    )
    return f"<html><body><h1>Example Place</h1>{anchors}</body></html>"


def _detail(unit_number: str, uid: str = "9001", rent: int = 1049) -> str:
    return f"""
    <html><body>
      <div class="unit-card" data-unit-id="{uid}">
        <h3 class="unit-number">{unit_number}</h3>
        <div>1 Bed • 1 Bath • 559 SqFt • Available 08/22/2026</div>
        <div class="unit-pricing"><span class="price-value">${rent:,}</span></div>
        <a data-fpid="1047423" data-uid="{uid}"></a>
      </div>
    </body></html>
    """


@pytest.mark.asyncio
async def test_adapter_recovers_exact_property_owned_snippet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import entrata as entrata_mod

    parent = _parent(
        "//entratasnipit.exampleplace.com/city/example-place/?snippet_type=website"
    )
    index = _index((DETAIL_1, "The Branson"))
    calls: list[tuple[str, bool, dict[str, str] | None]] = []

    async def fake_fetch(
        url: str,
        *,
        unlocker: bool = True,
        headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> str:
        calls.append((url, unlocker, headers))
        if url.startswith(IFRAME_ROOT):
            return index
        if url == DETAIL_1:
            return _detail("315")
        return ""

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake_fetch)
    result = await EntrataAdapter().extract(None, _ctx(parent))  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_DOM_ENTRATA_SNIPPET_UNIT_LEVEL"
    assert result.winning_url == DETAIL_1
    assert len(result.units) == 1
    unit = result.units[0]
    assert unit["unit_number"] == "315"
    assert unit["floor_plan_name"] == "The Branson"
    assert unit["source_property_id"] == "1166646"
    assert unit["source_property_name"] == "Example Place"
    assert unit["source_property_provenance"] == (
        "exact_property_owned_entratasnippet_iframe"
    )
    assert unit["source_portal_url"].startswith(IFRAME_ROOT)
    assert calls and all(unlocker is False for _, unlocker, _ in calls)
    assert calls[0][2] == {"Referer": PARENT_URL}


@pytest.mark.asyncio
async def test_multiple_matching_iframes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import entrata as entrata_mod

    async def forbidden(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ambiguous iframe set must not be fetched")

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", forbidden)
    src = "//entratasnipit.exampleplace.com/city/example-place/"
    assert await _recover_entrata_snippet_units(_ctx(_parent(src, src))) == (
        [],
        "",
        "",
    )


@pytest.mark.asyncio
async def test_non_property_child_iframe_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import entrata as entrata_mod

    async def forbidden(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("foreign iframe must not be fetched")

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", forbidden)
    parent = _parent("//entratasnipit.otherproperty.com/city/example-place/")
    assert await _recover_entrata_snippet_units(_ctx(parent)) == ([], "", "")


@pytest.mark.asyncio
async def test_mixed_internal_property_ids_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import entrata as entrata_mod

    parent = _parent("//entratasnipit.exampleplace.com/city/example-place/")
    foreign = DETAIL_2.replace("1166646", "7777777")
    index = _index((DETAIL_1, "A1"), (foreign, "A2"))

    async def fake_fetch(url: str, **_kwargs: Any) -> str:
        return index if url.startswith(IFRAME_ROOT) else ""

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake_fetch)
    assert await _recover_entrata_snippet_units(_ctx(parent)) == ([], "", "")


@pytest.mark.asyncio
async def test_duplicate_visible_unit_numbers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.pms.adapters import entrata as entrata_mod

    parent = _parent("//entratasnipit.exampleplace.com/city/example-place/")
    index = _index((DETAIL_1, "A1"), (DETAIL_2, "A2"))

    async def fake_fetch(url: str, **_kwargs: Any) -> str:
        if url.startswith(IFRAME_ROOT):
            return index
        if url == DETAIL_1:
            return _detail("315", "9001")
        if url == DETAIL_2:
            return _detail("315", "9002")
        return ""

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake_fetch)
    assert await _recover_entrata_snippet_units(_ctx(parent)) == ([], "", "")
