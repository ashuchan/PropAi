"""Bounded Hyperbrowser fallback for exact Entrata conventional indexes."""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.config import feature_flags
from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.detector import DetectedPMS

_EXACT_URL = "https://westwind.example/fort-worth/west-wind/conventional/"
_LANDING_WITH_EXACT_LINK = (
    '<html><body><a href="'
    + _EXACT_URL
    + '">View floor plans</a><script src="commoncf.entrata.com/x.js"></script></body></html>'
)
_HB_GRID = """
<html><body>
  <ul>
    <li class="fp-group-item">
      <a class="fp-name-link" href="/floorplans/fort-worth/west-wind/a1-100-1/">A1</a>
      <div class="fp-col bed-bath"><span class="fp-col-text">1 bd / 1 ba</span></div>
      <div class="fp-col rent"><span class="fp-col-text">From $1,245</span></div>
      <div class="fp-col sq-feet"><span class="fp-col-text">715</span></div>
    </li>
  </ul>
</body></html>
"""


def _ctx(body: str) -> AdapterContext:
    fetch_result = FetchResult(
        url="https://westwind.example/",
        outcome=FetchOutcome.OK,
        status=200,
        body=body.encode(),
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url="https://westwind.example/",
        attempts=1,
        elapsed_ms=100,
    )
    return AdapterContext(
        base_url="https://westwind.example/",
        detected=DetectedPMS(pms="entrata", confidence=0.9),
        profile=None,
        expected_total_units=None,
        property_id="11543",
        fetch_result=fetch_result,
    )


async def _empty_fetch(*_args: Any, **_kwargs: Any) -> str:
    return ""


@pytest.mark.asyncio
async def test_exact_blocked_conventional_url_uses_one_hb_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.fetch import hyperbrowser_backend
    from ma_poc.pms.adapters import entrata as entrata_mod

    calls: list[tuple[str, str]] = []

    async def fake_hb(url: str, property_id: str) -> tuple[int, str]:
        calls.append((url, property_id))
        return 200, _HB_GRID

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _empty_fetch)
    monkeypatch.setattr(entrata_mod, "_entrata_fetch_ssr", _empty_fetch)
    monkeypatch.setattr(feature_flags, "hb_enabled", lambda: True)
    monkeypatch.setattr(hyperbrowser_backend, "hb_raw_get", fake_hb)

    result = await EntrataAdapter().extract(None, _ctx(_LANDING_WITH_EXACT_LINK))  # type: ignore[arg-type]

    assert calls == [(_EXACT_URL, "11543")]
    assert len(result.plan_summaries) == 1
    assert result.plan_summaries[0]["floor_plan_name"] == "A1"
    assert result.plan_summaries[0]["market_rent_low"] == 1245
    assert result.tier_used == "TIER_1_DOM_ENTRATA_PP_SSR"
    assert any(
        row.get("via") == "entrata_pp_hyperbrowser_raw"
        for row in result.api_responses
    )


@pytest.mark.asyncio
async def test_hb_disabled_never_opens_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.fetch import hyperbrowser_backend
    from ma_poc.pms.adapters import entrata as entrata_mod

    async def forbidden(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise AssertionError("Hyperbrowser must stay disabled")

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _empty_fetch)
    monkeypatch.setattr(entrata_mod, "_entrata_fetch_ssr", _empty_fetch)
    monkeypatch.setattr(feature_flags, "hb_enabled", lambda: False)
    monkeypatch.setattr(hyperbrowser_backend, "hb_raw_get", forbidden)

    result = await EntrataAdapter().extract(None, _ctx(_LANDING_WITH_EXACT_LINK))  # type: ignore[arg-type]

    assert result.units == []


@pytest.mark.asyncio
async def test_guessed_conventional_path_never_spends_hb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ma_poc.fetch import hyperbrowser_backend
    from ma_poc.pms.adapters import entrata as entrata_mod

    async def forbidden(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
        raise AssertionError("A guessed path must not spend Hyperbrowser")

    landing_without_exact_link = (
        '<html><body><script src="commoncf.entrata.com/x.js"></script></body></html>'
    )
    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _empty_fetch)
    monkeypatch.setattr(entrata_mod, "_entrata_fetch_ssr", _empty_fetch)
    monkeypatch.setattr(feature_flags, "hb_enabled", lambda: True)
    monkeypatch.setattr(hyperbrowser_backend, "hb_raw_get", forbidden)

    result = await EntrataAdapter().extract(None, _ctx(landing_without_exact_link))  # type: ignore[arg-type]

    assert result.units == []
