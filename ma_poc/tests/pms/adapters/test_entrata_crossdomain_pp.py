"""Cross-domain Prospect Portal discovery (2026-07-11 DOM-tier debug).

Plan-card marketing sites (WordPress etc.) link to
``{slug}.prospectportal.com`` for availability. The portal carries the
full fp-card unit index, but before this fix neither discovery path in
``EntrataAdapter.extract`` could reach it:

- the deep-candidate href regex is same-host-only
  (``netloc.endswith(_base_host)``), and
- the pid-mining block fired only when the ENTRY base was already
  ``prospectportal.com`` (the PR#54 direct-entry shape).

Net effect: adapter dies ``ENTRATA_SHAPE_REJECTED``, Path-B retry
promotes ``generic_plan_text`` plan rows, and the property ships
plan-level despite unit-level data one hop away. Repro:
liveatthemirage.com → themirage.prospectportal.com (pid 100052396,
16 fp-cards) — live-verified 2026-07-11, post-fix run extracts 18
real-uid units with rent+sqft (TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL,
verdict SUCCESS).

The fix mines the portal origin from the marketing body via
``_PP_HOST_RE``, statically fetches the portal shell for the internal
``property_id``, and appends the ``/Apartments/module/property_info/``
URL to ``deep_candidates`` — Step 3's existing PP-SSR parser does the
rest.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ma_poc.pms.adapters  # noqa: F401  # populate adapter registry
from ma_poc.pms.adapters import entrata as entrata_mod
from ma_poc.pms.adapters.entrata import EntrataAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"

_PORTAL_ORIGIN = "https://themirage.prospectportal.com"
_MODULE_URL = (
    f"{_PORTAL_ORIGIN}/Apartments/module/property_info/property_id/100052396"
)

# Marketing page: plan cards only, cross-domain PP apply link.
_MARKETING_HTML = """
<html><body>
  <div class="plan-card"><h3>Studio</h3><p>From $979</p>
    <a href="https://themirage.prospectportal.com/">Check Availability</a>
  </div>
</body></html>
"""

# Portal shell: SPA chrome, no fp-cards, but carries the internal pid.
_PORTAL_SHELL = """
<html><head><script>
  window.app = {"property_id":"100052396","name":"The Mirage"};
</script></head><body><div id="spa-root"></div></body></html>
"""


def _ctx(body: str, url: str) -> Any:
    return SimpleNamespace(
        _api_responses=[],
        base_url=url,
        property_id="10291",
        fetch_result=SimpleNamespace(final_url=url, body=body),
        address="",
        zip_code="",
    )


def _fake_static_fetch(responses: dict[str, str]):
    calls: list[str] = []

    async def fetch(url: str, *, unlocker: bool = True) -> str:
        calls.append(url)
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return body
        return ""

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.mark.asyncio
async def test_crossdomain_pp_mines_pid_and_extracts_units(monkeypatch):
    fp_card_body = (FIXTURES / "prospectportal_fp_card_bellemeade.html").read_text()
    fake = _fake_static_fetch({
        _PORTAL_ORIGIN + "/Apartments/module": fp_card_body,
        _PORTAL_ORIGIN: _PORTAL_SHELL,
    })
    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake)

    result = await EntrataAdapter().extract(
        None, _ctx(_MARKETING_HTML, "https://liveatthemirage.com/")
    )

    # Portal shell fetched for pid, module endpoint fetched for fp-cards.
    assert any(u.startswith(_PORTAL_ORIGIN + "/") for u in fake.calls)
    assert any(u.startswith(_PORTAL_ORIGIN + "/Apartments/module") for u in fake.calls)
    assert result.units, f"expected units, got errors={result.errors}"
    assert "ENTRATA" in (result.tier_used or "")


@pytest.mark.asyncio
async def test_crossdomain_pp_portal_shell_blocked_degrades(monkeypatch):
    # CF-403 shape: static fetch returns "" for everything → no candidate
    # appended, adapter falls through to its pre-fix failure classification
    # instead of crashing (havenatsouthmountain shape).
    fake = _fake_static_fetch({})
    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake)

    result = await EntrataAdapter().extract(
        None, _ctx(_MARKETING_HTML, "https://liveatthemirage.com/")
    )
    assert not result.units
    assert result.tier_used  # classified failure, not an exception


@pytest.mark.asyncio
async def test_no_pp_href_no_extra_fetches(monkeypatch):
    # Marketing body without any prospectportal href must not trigger
    # portal-shell fetches (the ${subdomain} template artifact on
    # thevillagedallas does not match _PP_HOST_RE either).
    html = '<a href="https://${subdomain}.prospectportal.com/">apply</a>'
    fake = _fake_static_fetch({})
    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", fake)

    await EntrataAdapter().extract(None, _ctx(html, "https://example.com/"))
    assert not any("prospectportal.com/Apartments" in u for u in fake.calls)
