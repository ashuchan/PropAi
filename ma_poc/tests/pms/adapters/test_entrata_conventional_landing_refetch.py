"""Lead 3 (2026-07-30) — recover the modern ``/conventional/`` roster when the
captured body is the RENDERED DOM that stripped the discovery href.

Root cause the fix closes
-------------------------
The Entrata adapter discovers the ``/{city}/{slug}/conventional/`` plan index
from ``fetch_result.body``. When the render tier SUCCEEDS-BUT-EMPTY — the page
loaded but no unit XHR fired, the norm for these server-rendered ProspectPortal
themes — that body is the Playwright DOM, whose SPA nav has stripped the
server-rendered ``conventional`` anchor. Discovery then finds nothing, the modern
``var unitsData`` roster is never fetched, and a property whose full roster is one
static GET away ships ``ENTRATA_NO_RESPONSE``. That is exactly the p1fix canary
loss shape for Apollo Ridge / Rise Bedford Lake / The Abigail / Rise at the
Preserve (all xhr=0, ``fetch=OK``, ``render_mode=RENDER``).

The fix re-fetches the RAW ORIGIN statically when the captured body yields no
conventional href (mirrors the pid re-fetch already in the adapter), and the
code-only recovery fetches go DIRECT-first (the Web Unlocker was live-verified to
return an EMPTY body for these ``/conventional/`` pages while a direct curl
returned the whole roster).

These tests stub the single network seam (``_probe.probe_get``) — no live I/O —
and assert the adapter recovers the roster from a render-stripped body, does NOT
pay for the extra origin GET when the href was already present, and keeps the
DIRECT-first escalation wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.pms.adapters.entrata import EntrataAdapter

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "entrata_modern"
_CONV_BODY = (_FIX / "rise_listing.html").read_text(encoding="utf-8", errors="replace")

_BASE = "https://www.riselisting-test.com"
_CONV_URL = _BASE + "/bedford/rise-bedford-lake/conventional/"
# Landing HTML as the RAW curl shell serves it: the server-rendered anchor the
# rendered SPA DOM had stripped.
_LANDING = f'<html><body><a href="{_CONV_URL}">Floor Plans</a></body></html>'
# The captured body the adapter is handed: a render-stripped SPA shell with NO
# conventional href and none of the plan markers.
_RENDERED_DOM = '<html><body><div id="app">loading…</div></body></html>'


class _Resp:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _install_fake_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    landing: str,
    calls: list[tuple[str, bool]],
) -> None:
    """Route ``probe_get`` to fixtures by URL; record (url, unlocker) per call."""

    def _fake_probe_get(url: str, **kw: Any) -> _Resp:
        calls.append((url, bool(kw.get("unlocker"))))
        if url in (_BASE + "/", _BASE):
            return _Resp(200, landing)
        if url == _CONV_URL:
            return _Resp(200, _CONV_BODY)
        # Every other discovery probe (shallow /conventional/ guess, deep
        # candidates, pid mining) is a miss — never the live internet.
        return _Resp(200, "")

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get
    )


def _ctx(body: str) -> SimpleNamespace:
    fr = SimpleNamespace(body=body, final_url=_BASE + "/", status_code=200)
    return SimpleNamespace(
        fetch_result=fr, base_url=_BASE + "/", _api_responses=[], property_id=None
    )


@pytest.mark.asyncio
async def test_recovers_roster_from_render_stripped_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The p1fix loss shape: captured body has no conventional href, so the
    adapter must re-fetch the origin, discover the canonical index, and parse
    the modern ``unitsData`` roster (7 units in the Rise fixture)."""
    calls: list[tuple[str, bool]] = []
    _install_fake_probe(monkeypatch, landing=_LANDING, calls=calls)

    res = await EntrataAdapter().extract(None, _ctx(_RENDERED_DOM))
    units = getattr(res, "units", None) or []

    assert len(units) == 7, [getattr(u, "unit_number", u) for u in units]
    # The origin was re-fetched (discovery could not use the stripped body).
    assert (_BASE + "/") in {u for u, _ in calls}
    # And the /conventional/ page was fetched to get the roster.
    assert _CONV_URL in {u for u, _ in calls}


@pytest.mark.asyncio
async def test_direct_first_then_unlocker_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery fetches try DIRECT (unlocker=False) before the Web Unlocker —
    the unlocker returns an empty body for these pages, so a direct-first order
    is what makes the roster reachable at all."""
    calls: list[tuple[str, bool]] = []
    _install_fake_probe(monkeypatch, landing=_LANDING, calls=calls)

    await EntrataAdapter().extract(None, _ctx(_RENDERED_DOM))

    conv_fetches = [unlocker for url, unlocker in calls if url == _CONV_URL]
    assert conv_fetches, "the /conventional/ page was never fetched"
    # The FIRST attempt on the conventional page is DIRECT. (It succeeds in this
    # fixture, so no unlocker escalation is needed — but the first try is direct.)
    assert conv_fetches[0] is False


@pytest.mark.asyncio
async def test_no_extra_origin_fetch_when_href_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost guard: when the captured body ALREADY carries the conventional href,
    the adapter must not pay for the extra origin GET — it discovers from the
    body it was handed."""
    calls: list[tuple[str, bool]] = []
    # Body already contains the canonical href (e.g. the fetch handed us the
    # static curl shell, not a render-stripped DOM).
    body_with_href = (
        f'<html><body><a href="{_CONV_URL}">Floor Plans</a></body></html>'
    )
    _install_fake_probe(monkeypatch, landing=_LANDING, calls=calls)

    res = await EntrataAdapter().extract(None, _ctx(body_with_href))
    units = getattr(res, "units", None) or []

    assert len(units) == 7
    # The conventional page is fetched, but the bare origin is NOT re-fetched
    # for discovery (the href was already in hand).
    assert _CONV_URL in {u for u, _ in calls}
    assert (_BASE + "/") not in {u for u, _ in calls}
