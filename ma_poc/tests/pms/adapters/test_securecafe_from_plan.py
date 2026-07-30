"""The RentCafe plan-only → SecureCafe drill entry (``rentcafe.py:~490``).

PR #110 stopped copying a RentCafe ``floorplanId`` into ``unit_number``. That
is correct — a plan id is shared by every apartment on the plan, so plan rows
were reading as unit-level (false gold) and recovery skipped them. But
``_has_unit_level`` was the ONLY thing keeping this drill entry closed: before
#110 the floorplanId fallback made ``unit_number`` non-empty for every plan
payload, so the branch was effectively dead code. #110 re-armed it on every
RentCafe property whose in-page XHR yields only plan rows.

The re-armed branch REPLACES the plan catalogue with the portal result — no
merge, no fallback, no log line — and ``parse_securecafe_availableunits``
hardcodes ``availability_status="AVAILABLE"`` because the portal is an
AVAILABILITY LIST while ``getFloorplans`` is the CATALOGUE (it includes plans
with ``availableUnitsCount=0``). A portal returning the 2 currently-available
apartments therefore discards every fully-leased plan.

These tests pin the staged remediation:
  * T1 — flag OFF (the default): the probe is never attempted. This is the
    Tranche-1 acceptance test and it restores the pre-#110 blast radius exactly.
  * T2 — portal unreachable, flag ON: catalogue survives, tier unchanged. Locks
    in the fallback that ``test_shape_equivalence.py``'s autouse fixture pins
    only by accident.
  * T3 — portal returns FEWER units than the catalogue's own
    ``sum(available_units)``, flag ON: XFAIL today, green with the Tranche-2
    acceptance guard + merge.

Nothing here touches the OTHER SecureCafe call site (SHAPE_REJECTED), which is
proven — 1,344/4,982 wins on 2026-07-12, 117/1,127 on 2026-07-26-plancohort —
and where ``all_units`` is empty so there is nothing to discard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter
from ma_poc.pms.detector import detect_pms

_FX = Path(__file__).resolve().parent / "fixtures" / "rentcafe"

#: The property's own page, carrying the SecureCafe portal link the drill
#: regex-scans for. Real host/slug from the captured 35593 payload's
#: ``availabilityURL``.
_HOMEPAGE_HTML = (
    '<a href="https://continentaldallas.securecafe.com/onlineleasing/'
    'the-continental-2/availableunits.aspx">Apply Now</a>'
)

_SC_BASE = "https://continentaldallas.securecafe.com/onlineleasing/the-continental-2"


def _availableunits_html(
    units: list[tuple[str, str, str, str]],
    beds_baths: dict[str, tuple[int, float]] | None = None,
) -> str:
    """Render a minimal ``availableunits.aspx`` body the production parser reads.

    *units* is ``(plan_name, apt_number, sqft, rent)``. One floor-plan header
    section per distinct plan name, ``<tr class='AvailUnitRow'>`` per apartment
    — the exact markup ``parse_securecafe_availableunits`` matches.

    *beds_baths* maps plan name → ``(beds, baths)`` for the header line. It is
    NOT cosmetic: the acceptance guard compares bed/bath coverage between the
    catalogue and the portal, so a helper that hardcodes "1 Bedroom, 1.0
    Bathroom" for every plan makes a full 15-apartment roster look like it
    dropped the 2/2 plans, and the accept-path test fails for a fixture reason
    rather than a code reason. Defaults to (1, 1.0) for unlisted plans.
    """
    bb = beds_baths or {}
    out: list[str] = ["<html><body>"]
    for plan in dict.fromkeys(p for p, _, _, _ in units):
        _bd, _ba = bb.get(plan, (1, 1.0))
        out.append(f"<h3>Floor Plan: {plan} - {_bd} Bedroom, {_ba} Bathroom</h3>")
        for _p, apt, sqft, rent in (u for u in units if u[0] == plan):
            out.append(
                "<tr class='AvailUnitRow'>"
                f"<td data-label='Apartment'>#{apt}</td>"
                f"<td data-label='Sq.Ft.'>{sqft}</td>"
                f"<td data-label='Rent'>${rent}</td>"
                "<td data-label='Date Available'>Available</td>"
                "</tr>"
            )
    out.append("</body></html>")
    return "".join(out)


@dataclass
class _FetchResult:
    body: bytes | str = ""
    final_url: str = ""


class _Response:
    """Minimal ``probe_get`` return shape."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers: dict[str, str] = {}


@dataclass
class _ProbeSpy:
    """Records every ``probe_get`` URL and serves canned bodies by substring."""

    routes: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, url: str, *_a: Any, **_kw: Any) -> _Response:
        self.calls.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                return _Response(200, body)
        return _Response(404, "")


def _plan_only_ctx() -> tuple[AdapterContext, list[dict[str, Any]]]:
    """Context primed with the REAL captured Brookfield/Continental payload.

    ``fixtures/rentcafe/35593.json`` — The Continental Dallas, 10 plans,
    ``sum(availableUnitsCount) == 15``, ``_has_unit_level=False``. This is the
    one real production payload in the repo, and it flips skip→PROBE under #110
    exactly like the 5 synthetic ``rentcafe_direct`` fixtures do.
    """
    captured = json.loads((_FX / "35593.json").read_text(encoding="utf-8"))
    url = captured[0]["url"]
    plans = captured[0]["body"]
    ctx = AdapterContext(
        base_url="https://rent.brookfieldproperties.com/the-continental/",
        detected=detect_pms("https://rent.brookfieldproperties.com/the-continental/"),
        profile=None,
        expected_total_units=None,
        property_id="35593",
        property_name="The Continental",
        city="Dallas",
        state="TX",
        zip_code="75201",
    )
    ctx._api_responses = [  # type: ignore[attr-defined]
        {"url": url, "body": plans, "status": 200, "headers": {}}
    ]
    ctx.fetch_result = _FetchResult(body=_HOMEPAGE_HTML)  # type: ignore[attr-defined]
    return ctx, plans


def test_fixture_is_plan_only_with_a_real_availability_count() -> None:
    """Guard the premise: this really is the skip→PROBE shape.

    If someone re-captures the fixture with unit-level rows, every test below
    goes vacuously green, so assert the preconditions explicitly.
    """
    _ctx, plans = _plan_only_ctx()
    assert len(plans) == 10
    assert sum(int(p["availableUnitsCount"]) for p in plans) == 15
    # The availabilityURL FK the Tranche-3 lever would use, and the
    # circumstantial evidence that rentcafe_floorplan_id == securecafe_floorplan_id
    # for the same plan (NOT proof — needs a live probe before merging on it).
    assert f"floorPlans={plans[0]['floorplanId']}" in plans[0]["availabilityURL"]


# ── T1 — Tranche-1 acceptance test: the flag defaults OFF ───────────────────


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drill flag must default to the SAFE value.

    Default-on would ship up to ~1,535s of per-property wall clock (the
    often-quoted ~95s is the floor: ``_probe.probe_get`` escalates internally
    to the Web Unlocker at ``timeout + 95``, and the prod SecureCafe config
    NEEDS ``PROBE_PROXY_URL`` because CF blocks GCP) against jugnu's 600s
    ``asyncio.wait_for``.

    ``delenv`` is load-bearing: without it this reads the AMBIENT process env,
    not the code default, so it goes red in exactly the environment the
    rollout requires — ``ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL=true pytest``
    failed here with ``assert True is False`` while claiming to check a
    default. The test must pin what ``feature_flags.py`` says, and nothing else.
    """
    monkeypatch.delenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", raising=False)
    from ma_poc.config.feature_flags import enable_rentcafe_plan_securecafe_drill

    assert enable_rentcafe_plan_securecafe_drill() is False


@pytest.mark.asyncio
async def test_t1_flag_off_never_attempts_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off ⇒ byte-identical to pre-#110 behaviour for this branch.

    Not "the probe fails harmlessly" — the probe is NEVER REACHED. Asserting
    the spy recorded zero calls is the only assertion that distinguishes
    "guarded" from "guarded but still paying the wall clock".
    """
    monkeypatch.delenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", raising=False)
    spy = _ProbeSpy()
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    ctx, plans = _plan_only_ctx()
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert spy.calls == [], f"probe was attempted with the flag off: {spy.calls}"
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == len(plans) == 10


# ── T2 — portal unreachable, flag ON ────────────────────────────────────────


@pytest.mark.asyncio
async def test_t2_portal_unreachable_keeps_the_plan_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + portal 404 ⇒ full catalogue survives, tier unchanged.

    ``test_shape_equivalence.py``'s autouse fixture pins this same branch, but
    only incidentally (its 404 stub makes ``bases`` empty). Pin it on purpose
    here, with the flag explicitly ON, so the fallback is asserted rather than
    assumed.
    """
    monkeypatch.setenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", "true")
    spy = _ProbeSpy()  # every URL 404s
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    ctx, plans = _plan_only_ctx()
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert spy.calls, "flag is on — the probe should have been attempted"
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == len(plans) == 10
    assert {str(u["floor_plan_name"]) for u in result.units} == {
        str(p["floorplanName"]) for p in plans
    }


# ── T3 — Tranche-2: the acceptance guard ────────────────────────────────────


@pytest.mark.asyncio
async def test_t3_portal_returning_fewer_units_than_available_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A portal short of the catalogue's own availability count must not win.

    The catalogue says 15 apartments are available across 10 plans. The portal
    returns 2, on one plan. Swapping would be a strict loss: 9 plan names,
    their rent ranges and their sqft ranges unrecoverable. ``available_units``
    is already on the admitted rows (``rentcafe.py:215``) so the count floor
    costs one ``sum()`` and no network.

    The flag alone did NOT cover this: it is a kill switch, and with it ON the
    swap happened silently (``result.errors == []``, the ONLY trace being the
    tier string). The guard is what makes the flag safe to flip.
    """
    monkeypatch.setenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", "true")
    spy = _ProbeSpy(
        routes={
            "availableunits.aspx": _availableunits_html(
                [("1C", "301", "787", "1349"), ("1C", "402", "787", "1425")]
            )
        }
    )
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    ctx, plans = _plan_only_ctx()
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert spy.calls, "flag is on — the probe should have been attempted"
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == len(plans) == 10
    # No plan name is lost.
    assert {str(u["floor_plan_name"]) for u in result.units} == {
        str(p["floorplanName"]) for p in plans
    }
    # The rejection must be diagnosable from result.errors ALONE, carrying
    # BOTH counts — a silent rejection is how the replace-bug stayed invisible.
    reasons = [e for e in result.errors if "securecafe_from_plan" in e]
    assert reasons, f"rejected swap recorded nothing: {result.errors}"
    assert "2" in reasons[0] and "15" in reasons[0], reasons[0]


@pytest.mark.asyncio
async def test_t3_full_roster_is_accepted_and_keeps_the_leased_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accept path: the guard must not block a legitimate unit-level win.

    A guard that rejects everything is not a guard, it is the flag again. The
    portal here returns a roster that clears all three checks — every row has a
    real anchor, 15 apartments >= the catalogue's declared 15, and every
    bed/bath combination is covered — so the swap is ACCEPTED.

    What it must NOT do is discard the catalogue. ``availableunits.aspx`` is an
    availability list; the plans with ``availableUnitsCount == 0`` are
    structurally absent from it. Those plan rows are RETAINED, so the result
    carries every apartment AND every plan name.

    This also pins the merge helper choice: ``merge_into_result_units`` is an
    anchor-first merge for two views of the same unit list, and against a plan
    catalogue its rank ladder collapsed a 16-apartment roster onto the 8 plan
    rows — measured. The union in ``_merge_portal_over_catalogue`` is keyed on
    floor plan instead.
    """
    monkeypatch.setenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", "true")

    ctx, plans = _plan_only_ctx()
    # Every plan in the captured payload happens to have availability, so lease
    # out three of them — the ONLY edit, and the shape the guard exists for.
    # BOTH count keys must be zeroed: ``rentcafe.py:192`` reads
    # ``availableunitscount or unitscount``, so a falsy 0 in the first silently
    # falls back to the second and the plan still reads as available. Strings,
    # not ints, for the same reason.
    leased = {"1M", "1Z", "2J"}
    for p in plans:
        if str(p["floorplanName"]) in leased:
            p["availableUnitsCount"] = "0"
            p["unitsCount"] = "0"

    # One apartment per declared available unit, with the plan's REAL bed/bath
    # so the coverage check compares like with like.
    roster: list[tuple[str, str, str, str]] = []
    bb: dict[str, tuple[int, float]] = {}
    for p in plans:
        name = str(p["floorplanName"])
        bb[name] = (int(p["beds"]), float(p["baths"]))
        for i in range(int(p["availableUnitsCount"])):
            roster.append((name, f"{p['floorplanId']}-{i}", "787", "1349"))
    assert len(roster) == 12  # 15 declared minus the 3 we just leased out

    spy = _ProbeSpy(routes={"availableunits.aspx": _availableunits_html(roster, bb)})
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RENTCAFE_SECURECAFE_FROM_PLAN"
    assert not [e for e in result.errors if "securecafe_from_plan" in e]

    # Every portal apartment survives — the merge must not collapse the roster.
    unit_numbers = {str(u.get("unit_number") or "") for u in result.units} - {""}
    assert len(unit_numbers) == 12, f"roster collapsed to {len(unit_numbers)}"

    # …and NO plan name is lost, including the fully-leased ones the portal
    # never mentions. This is the assertion the pre-guard code failed.
    surviving = {str(u.get("floor_plan_name") or "") for u in result.units}
    assert leased <= surviving, f"fully-leased plans dropped: {leased - surviving}"
    assert {str(p["floorplanName"]) for p in plans} <= surviving


# ── #80 — the DIRECT-only FAST availableunits path (opt-in, default OFF) ──────


class _KwSpy:
    """Like ``_ProbeSpy`` but also records each call's kwargs.

    The #80 fast path's load-bearing property is that it passes
    ``unlocker=False`` (so ``probe_get`` cannot escalate to the timeout+95 Web
    Unlocker) — the ONLY thing that distinguishes the bounded DIRECT probe from
    the full ladder in a test env where neither proxy nor unlocker key is set.
    """

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[str] = []
        self.kwcalls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, *_a: Any, **kw: Any) -> _Response:
        self.calls.append(url)
        self.kwcalls.append((url, kw))
        for needle, body in self.routes.items():
            if needle in url:
                return _Response(200, body)
        return _Response(404, "")


def test_fast_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending canary yield measurement, the fast path is opt-in (see the flag
    docstring). ``delenv`` pins the code default, not the ambient env."""
    monkeypatch.delenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", raising=False)
    from ma_poc.config.feature_flags import enable_rentcafe_availunits_fast

    assert enable_rentcafe_availunits_fast() is False
    monkeypatch.setenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", "true")
    assert enable_rentcafe_availunits_fast() is True


@pytest.mark.asyncio
async def test_both_flags_off_never_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The T1 contract must hold with BOTH flags at their default: no probe.

    This is what keeps the fast flag's default-off honest — if it ever flips on,
    this goes red rather than silently adding a fetch to every plan-level prop.
    """
    monkeypatch.delenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", raising=False)
    monkeypatch.delenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", raising=False)
    spy = _KwSpy()
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    ctx, plans = _plan_only_ctx()
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert spy.calls == [], f"probe attempted with both flags off: {spy.calls}"
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == len(plans) == 10


@pytest.mark.asyncio
async def test_fast_path_is_direct_only_no_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast flag ON, drill OFF, availableunits is a CF-shell (200, no rows):
    the catalogue survives and the availableunits fetch used ``unlocker=False``
    — the fast path must never pay the escalation the full drill would."""
    monkeypatch.delenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", raising=False)
    monkeypatch.setenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", "true")
    spy = _KwSpy(routes={"availableunits.aspx": "<html>cf challenge — no rows</html>"})
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    ctx, plans = _plan_only_ctx()
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    au = [(u, kw) for u, kw in spy.kwcalls if "availableunits.aspx" in u]
    assert au, "fast path should attempt availableunits when the flag is on"
    assert all(kw.get("unlocker") is False for _, kw in au), au
    # CF-shell yields no rows → catalogue kept untouched.
    assert result.tier_used == "TIER_1_API_RENTCAFE"
    assert len(result.units) == len(plans) == 10


@pytest.mark.asyncio
async def test_fast_path_accepts_full_roster_and_keeps_leased_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fast path reaches unit-level AND reuses the merge guard: a full
    roster is accepted, the fully-leased plans the portal omits are retained,
    and the availableunits fetch stayed DIRECT (``unlocker=False``). Mirrors the
    full-drill accept test, proving the fast path shares its data safety."""
    monkeypatch.delenv("ENABLE_RENTCAFE_PLAN_SECURECAFE_DRILL", raising=False)
    monkeypatch.setenv("ENABLE_RENTCAFE_AVAILUNITS_FAST", "true")

    ctx, plans = _plan_only_ctx()
    leased = {"1M", "1Z", "2J"}
    for p in plans:
        if str(p["floorplanName"]) in leased:
            p["availableUnitsCount"] = "0"
            p["unitsCount"] = "0"
    roster: list[tuple[str, str, str, str]] = []
    bb: dict[str, tuple[int, float]] = {}
    for p in plans:
        name = str(p["floorplanName"])
        bb[name] = (int(p["beds"]), float(p["baths"]))
        for i in range(int(p["availableUnitsCount"])):
            roster.append((name, f"{p['floorplanId']}-{i}", "787", "1349"))
    assert len(roster) == 12

    spy = _KwSpy(routes={"availableunits.aspx": _availableunits_html(roster, bb)})
    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", spy)

    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]

    assert result.tier_used == "TIER_1_API_RENTCAFE_SECURECAFE_FROM_PLAN"
    assert not [e for e in result.errors if "securecafe_from_plan" in e]
    au = [kw for u, kw in spy.kwcalls if "availableunits.aspx" in u]
    assert au and all(kw.get("unlocker") is False for kw in au), "fast path escalated"
    unit_numbers = {str(u.get("unit_number") or "") for u in result.units} - {""}
    assert len(unit_numbers) == 12, f"roster collapsed to {len(unit_numbers)}"
    surviving = {str(u.get("floor_plan_name") or "") for u in result.units}
    assert leased <= surviving, f"merge guard dropped leased plans: {leased - surviving}"
