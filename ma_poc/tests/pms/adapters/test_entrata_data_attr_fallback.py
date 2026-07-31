"""Entrata data-attr last-chance recovery (#93 prod routing gap, 2026-07-31).

focus100 canary RCA: The Village Lakes (thevillagedallas.com) publishes its full
24-unit roster INLINE on its own marketing page as static
``div.unit-body[data-unit-id][data-unit-number]`` cards. The entrata-native
paths (PP-SSR grid, per-plan unit-card drill, WP available_units,
prospectportal probe) all score 0 on that container shape, so the adapter
empty-exited → Path B re-detected ``generic_plan_text`` → link-hopped the DOM
cascade onto ``/live/`` (0 containers) → shipped 21 plan rows, 0 units.

The fix runs the generic ``extract_units_from_dom`` data-attr extractor on the
captured entry body as a LAST-CHANCE unit-level recovery, right before the
empty-exit. Gated on an empty result + a ``data-unit-id`` token so it cannot
preempt any currently-succeeding entrata path.

Fixture ``avail_table/village_lakes.html`` is the live static entry page
(compliant DIRECT GET 2026-07-31): 24 apartments, all priced.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters.entrata import EntrataAdapter

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "avail_table"
_BODY = (_FIX / "village_lakes.html").read_text(encoding="utf-8", errors="replace")
_URL = "https://www.thevillagedallas.com/properties/the-village-lakes/"


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every entrata-native network fallback must miss (offline empty body)."""

    class _Resp:
        def __init__(self, url: str) -> None:
            self.url = url
            self.status_code = 404
            self.text = ""
            self.content = b""
            self.headers: dict[str, str] = {}

    def _fake(url: str = "", **_kw: Any) -> _Resp:
        return _Resp(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake)
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_post", _fake)


def _ctx(body: str) -> SimpleNamespace:
    fr = SimpleNamespace(body=body, final_url=_URL, status_code=200)
    return SimpleNamespace(
        fetch_result=fr, base_url=_URL, _api_responses=[], property_id="11342"
    )


@pytest.mark.asyncio
async def test_recovers_data_attr_roster_before_empty_exit() -> None:
    res = await EntrataAdapter().extract(None, _ctx(_BODY))  # type: ignore[arg-type]
    units = getattr(res, "units", None) or []
    assert len(units) == 24, f"expected 24, got {len(units)}"
    assert res.tier_used == "TIER_1_DOM_ENTRATA_PP_DATA_ATTR"
    # every recovered unit is gold-shaped: real anchor + numeric rent.
    ids = [str(u.get("unit_id") or "") for u in units]
    assert all(ids) and len(set(ids)) == 24
    assert all(u.get("rent_low") or u.get("market_rent_low") for u in units)


@pytest.mark.asyncio
async def test_no_data_unit_id_token_still_empty_exits() -> None:
    """The gate requires a ``data-unit-id`` token — a plain shell must NOT
    trigger the recovery and must still empty-exit (confidence 0.0)."""
    res = await EntrataAdapter().extract(  # type: ignore[arg-type]
        None, _ctx("<html><body><div id='app'>loading…</div></body></html>")
    )
    assert (getattr(res, "units", None) or []) == []
    assert res.confidence == 0.0
    assert res.tier_used != "TIER_1_DOM_ENTRATA_PP_DATA_ATTR"
