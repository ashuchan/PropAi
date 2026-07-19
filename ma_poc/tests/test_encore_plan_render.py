"""Encore per-plan render fan-out (task #37 Track 2b).

At page=None the encoreskyline adapter finds the /floorplans/{slug}/ URLs in the
body but can't fetch+click them. _encore_plan_render_units renders each plan URL
(fetch_fn — with INTERACTION_REVEAL the fetcher fires the Check-Availability
click) and parses the post-click roster. These pin: plan discovery from the entry
body, per-plan fan-out + dedup, the elapsed-budget guard, and never-fail.
"""

from __future__ import annotations

import pytest

from ma_poc.discovery.contracts import CrawlTask, RenderMode, TaskReason
from scripts.runners.jugnu import _encore_plan_render_units, _result_tier

# A per-plan page's post-"Check Availability" rendered roster (one real unit row
# per plan) — the shape parse_encoreskyline_units matches (#<unit> … sq. ft. $rent).
_PLAN_A_BODY = (
    "<html><body><h1>Spruce</h1><div>1 bed 1 bath 703 sq. ft. Check Availability</div>"
    "<div>#B-302 Floor 3 703 sq. ft. $1,750 $400 Deposit Available Jul 3</div></body></html>"
)
_PLAN_B_BODY = (
    "<html><body><h1>BBR</h1><div>1 bed 1 bath 820 sq. ft. Check Availability</div>"
    "<div>#308 Floor 1 820 sq. ft. Starting at $2,025 Available Now Lease Now</div></body></html>"
)
_ENTRY_HTML = (
    "<html><body><script>jonahdigital.init()</script>"
    "<a href='/floorplans/spruce/'>Spruce</a>"
    "<a href='/floorplans/bbr/'>BBR</a></body></html>"
)


def _task(url: str = "https://encore.example/") -> CrawlTask:
    return CrawlTask(
        url=url,
        property_id="enc-1",
        priority=0,
        reason=TaskReason.SCHEDULED,
        render_mode=RenderMode.GET,
        budget_ms=180_000,
    )


class _FR:
    def __init__(self, body: str | bytes | None, ok: bool = True) -> None:
        self.body = body
        self._ok = ok

    def ok(self) -> bool:
        return self._ok


def test_result_tier_reads_both_shapes() -> None:
    assert _result_tier({"extraction_tier_used": "ENCORESKYLINE_NO_UNITS"}) == "ENCORESKYLINE_NO_UNITS"
    assert _result_tier({}) == ""


@pytest.mark.asyncio
async def test_fan_out_recovers_units_per_plan() -> None:
    bodies = {
        "https://encore.example/floorplans/spruce/": _PLAN_A_BODY,
        "https://encore.example/floorplans/bbr/": _PLAN_B_BODY,
    }
    fetched: list[str] = []

    async def _fetch(t: CrawlTask) -> _FR:
        fetched.append(t.url)
        assert t.render_mode == RenderMode.RENDER  # fan-out forces RENDER
        return _FR(bodies.get(t.url))

    units = await _encore_plan_render_units(
        _task(), _ENTRY_HTML, "https://encore.example/", _fetch,
        started_monotonic=0.0, budget_guard_s=1e9, max_plans=6,
    )
    nums = {u.get("unit_number") for u in units}
    assert "B-302" in nums and "308" in nums
    assert len(fetched) == 2  # one render per discovered plan


@pytest.mark.asyncio
async def test_no_plans_in_body_no_fetch() -> None:
    calls: list[str] = []

    async def _fetch(t: CrawlTask) -> _FR:
        calls.append(t.url)
        return _FR("")

    units = await _encore_plan_render_units(
        _task(), "<html><body>no plan anchors</body></html>", "https://encore.example/",
        _fetch, started_monotonic=0.0, budget_guard_s=1e9, max_plans=6,
    )
    assert units == []
    assert calls == []


@pytest.mark.asyncio
async def test_budget_guard_stops_fan_out() -> None:
    """Elapsed >= guard before the loop → no fetches at all."""
    calls: list[str] = []

    async def _fetch(t: CrawlTask) -> _FR:
        calls.append(t.url)
        return _FR(_PLAN_A_BODY)

    # started at 0, guard 10s, but monotonic() is already >> guard → guard trips
    units = await _encore_plan_render_units(
        _task(), _ENTRY_HTML, "https://encore.example/", _fetch,
        started_monotonic=-1e9, budget_guard_s=10.0, max_plans=6,
    )
    assert units == []
    assert calls == []


@pytest.mark.asyncio
async def test_max_plans_caps_fetches() -> None:
    async def _fetch(t: CrawlTask) -> _FR:
        return _FR(_PLAN_A_BODY)  # same unit → dedup to 1

    entry = "".join(f"<a href='/floorplans/p{i}/'>P{i}</a>" for i in range(10))
    calls = 0

    async def _counting(t: CrawlTask) -> _FR:
        nonlocal calls
        calls += 1
        return _FR(_PLAN_A_BODY)

    await _encore_plan_render_units(
        _task(), f"<html>{entry}</html>", "https://encore.example/", _counting,
        started_monotonic=0.0, budget_guard_s=1e9, max_plans=3,
    )
    assert calls == 3  # capped at max_plans


@pytest.mark.asyncio
async def test_fetch_error_is_skipped_never_raises() -> None:
    async def _boom(t: CrawlTask) -> _FR:
        raise RuntimeError("fetch died")

    units = await _encore_plan_render_units(
        _task(), _ENTRY_HTML, "https://encore.example/", _boom,
        started_monotonic=0.0, budget_guard_s=1e9, max_plans=6,
    )
    assert units == []


@pytest.mark.asyncio
async def test_non_ok_and_empty_body_skipped() -> None:
    async def _fetch(t: CrawlTask) -> _FR:
        if "spruce" in t.url:
            return _FR(None, ok=False)  # not ok
        return _FR("", ok=True)  # empty body

    units = await _encore_plan_render_units(
        _task(), _ENTRY_HTML, "https://encore.example/", _fetch,
        started_monotonic=0.0, budget_guard_s=1e9, max_plans=6,
    )
    assert units == []
