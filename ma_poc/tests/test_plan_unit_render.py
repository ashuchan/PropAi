"""Generalized plan→unit render lever + circuit-breaker (task #45, 2026-07-19).

Pinned contract:
- ``_is_plan_level``: rows present AND zero rows with a canonical per-unit
  identity — platform-agnostic (the generalization of the Entrata-scoped #42
  trigger).
- ``_is_entrata_plan_level`` unchanged in behavior (tier-scoped wrapper).
- ``_plan_render_allowed``: circuit-breaker on
  ``profile.quality.plan_render_attempts_held`` — allow below the cap, block at
  the cap, RE-ARM one attempt when the last render is older than the re-arm
  window. None-safe (COLD/missing profile ⇒ allowed).
- profile_updater feedback loop: ``_plan_render_attempted`` on the result
  advances ``plan_render_attempts_held`` only when the final result is still
  not unit-level (attempted-and-HELD); ANY unit-level success resets it; a
  plan-level success WITHOUT the stamp (flag off / render skipped) does NOT
  advance it.
- shadow-DOM capture: ``capture_rendered_dom`` appends open shadow-root
  content and falls back safely.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_store import ProfileStore
from services.profile_updater import update_profile_after_extraction

# ── predicate + breaker helpers (import from the runner) ──────────────────────


def _runner():
    from scripts.runners import jugnu

    return jugnu


def test_is_plan_level_true_on_planonly_rows() -> None:
    j = _runner()
    result = {"units": [{"unit_number": "", "floor_plan_name": "A1", "rent_low": 1200}]}
    assert j._is_plan_level(result) is True


def test_is_plan_level_false_on_zero_units() -> None:
    # zero-units belongs to render-on-empty, not the plan lever
    j = _runner()
    assert j._is_plan_level({"units": []}) is False
    assert j._is_plan_level({}) is False


def test_is_plan_level_true_on_canonical_plan_channel() -> None:
    """Stage-2 moves plan rows out of ``units``; the render lever must still
    recognise that as plan-level rather than empty."""
    j = _runner()
    result = {
        "units": [],
        "plan_summaries": [
            {"floor_plan_name": "A1", "market_rent_low": 1500}
        ],
    }
    assert j._is_plan_level(result) is True


def test_unit_level_count_rejects_plan_scoped_numeric_id() -> None:
    """A numeric Entrata floor-plan id is not an apartment identity."""
    j = _runner()
    result = {
        "units": [
            {
                "unit_number": "819409",
                "floor_plan_name": "A1",
                "source_ids": {"entrata_fpid": "819409"},
            }
        ]
    }
    assert j._unit_level_row_count(result) == 0
    assert j._is_plan_level(result) is True


def test_route_shadow_counts_only_physical_units(tmp_path: Any) -> None:
    """Plan summaries must keep the observe-only router on a recovery path."""
    import json
    from types import SimpleNamespace

    j = _runner()
    fetch_result = SimpleNamespace(
        outcome="SUCCESS",
        status=200,
        body="Floorplan A1 rent $1500",
        content_type="text/html",
        network_log=[],
        captcha_detected=False,
    )
    result = {
        "units": [],
        "plan_summaries": [
            {"floor_plan_name": "A1", "market_rent_low": 1500}
        ],
    }

    j._emit_route_shadow(
        fetch_result,
        result,
        SimpleNamespace(property_id="plan-only"),
        tmp_path,
        None,
        "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "SUCCESS_PLAN_LEVEL",
    )

    record = json.loads((tmp_path / "route_shadow.jsonl").read_text())
    assert record["signals"]["units_extracted"] == 0
    assert record["router_action"] != "STOP"


def test_is_plan_level_false_when_any_unit_number() -> None:
    j = _runner()
    result = {"units": [{"unit_number": "101"}, {"unit_number": ""}]}
    assert j._is_plan_level(result) is False


def test_is_plan_level_is_platform_agnostic() -> None:
    """The generic predicate must fire for a NON-Entrata tier (the whole point)."""
    j = _runner()
    result = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [{"unit_number": "", "rent_low": 1000}],
    }
    assert j._is_plan_level(result) is True
    assert j._is_entrata_plan_level(result) is False  # tier-scoped wrapper intact


def test_entrata_plan_level_still_fires_for_entrata() -> None:
    j = _runner()
    result = {
        "extraction_tier_used": "TIER_1_DOM_ENTRATA_PP_SSR",
        "units": [{"unit_number": ""}],
    }
    assert j._is_entrata_plan_level(result) is True


# ── circuit-breaker ───────────────────────────────────────────────────────────


def _profile_with_held(held: int, last_at: datetime | None) -> ScrapeProfile:
    p = ScrapeProfile(canonical_id="cb-x")
    p.quality.plan_render_attempts_held = held
    p.quality.last_plan_render_at = last_at
    return p


def test_breaker_allows_none_profile() -> None:
    j = _runner()
    assert j._plan_render_allowed(None) is True


def test_breaker_allows_below_cap() -> None:
    j = _runner()
    p = _profile_with_held(2, datetime.utcnow())  # cap default 3
    assert j._plan_render_allowed(p) is True


def test_breaker_blocks_at_cap() -> None:
    j = _runner()
    p = _profile_with_held(3, datetime.utcnow())
    assert j._plan_render_allowed(p) is False


def test_breaker_rearms_after_window() -> None:
    """At-cap but last attempt 8 days ago (>7d window) → one fresh render."""
    j = _runner()
    p = _profile_with_held(3, datetime.utcnow() - timedelta(days=8))
    assert j._plan_render_allowed(p) is True


def test_breaker_at_cap_missing_timestamp_allows_reprobe() -> None:
    j = _runner()
    p = _profile_with_held(5, None)
    assert j._plan_render_allowed(p) is True


# ── profile_updater feedback loop ─────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Any) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def test_attempted_and_held_advances_counter(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="fb-1")
    store.save(p)
    held_result = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [{"unit_number": "", "rent_low": 1100}],
        "_plan_render_attempted": True,
    }
    p = update_profile_after_extraction(p, held_result, 1, store)
    assert p.quality.plan_render_attempts_held == 1
    assert p.quality.last_plan_render_at is not None
    p = update_profile_after_extraction(p, held_result, 1, store)
    assert p.quality.plan_render_attempts_held == 2


def test_plan_level_without_attempt_does_not_advance(store: ProfileStore) -> None:
    """Flag-off periods must never inflate the breaker (the pre-existing-streak
    trap): a plan-level success with NO render attempted leaves the counter."""
    p = ScrapeProfile(canonical_id="fb-2")
    store.save(p)
    plan_no_attempt = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [{"unit_number": "", "rent_low": 1100}],
    }
    p = update_profile_after_extraction(p, plan_no_attempt, 1, store)
    assert p.quality.plan_render_attempts_held == 0
    assert p.quality.consecutive_plan_level == 1  # the quality streak still counts


def test_unit_level_success_resets_counter(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="fb-3")
    p.quality.plan_render_attempts_held = 2
    store.save(p)
    upgraded = {
        "extraction_tier_used": "TIER_1_API_ENTRATA",
        "units": [{"unit_number": "204", "rent_low": 1400}],
        "_plan_render_attempted": True,  # the render WON — reset, not advance
    }
    p = update_profile_after_extraction(p, upgraded, 1, store)
    assert p.quality.plan_render_attempts_held == 0
    assert p.quality.consecutive_plan_level == 0


def test_contaminated_flap_cannot_evade_breaker(store: ProfileStore) -> None:
    """PLAN↔CONTAMINATED flapping resets consecutive_plan_level but must NOT
    reset the breaker counter — only a genuine unit-level success clears it."""
    p = ScrapeProfile(canonical_id="fb-4")
    p.quality.plan_render_attempts_held = 2
    store.save(p)
    contaminated = {
        "extraction_tier_used": "TIER_1_API_APPFOLIO",
        # 200 rows, expected 20 → CONTAMINATED flag
        "units": [{"unit_number": str(i)} for i in range(200)],
        "_expected_total_units": 20,
        "_plan_render_attempted": True,
    }
    p = update_profile_after_extraction(p, contaminated, 1, store)
    # not unit-level + attempted → advances (2→3), never resets
    assert p.quality.plan_render_attempts_held == 3


def test_breaker_round_trips_through_store(store: ProfileStore) -> None:
    p = ScrapeProfile(canonical_id="fb-5")
    store.save(p)
    held = {
        "extraction_tier_used": "TIER_1_DOM_GENERIC_PLAN_TEXT",
        "units": [{"unit_number": ""}],
        "_plan_render_attempted": True,
    }
    update_profile_after_extraction(p, held, 1, store)
    loaded = store.load("fb-5")
    assert loaded is not None
    assert loaded.quality.plan_render_attempts_held == 1


# ── shadow-DOM capture ────────────────────────────────────────────────────────


class _FakePage:
    """Playwright Page stub: content() + evaluate()."""

    def __init__(self, content: str, shadow: str | Exception | None) -> None:
        self._content = content
        self._shadow = shadow

    async def content(self) -> str:
        return self._content

    async def evaluate(self, _js: str) -> str:
        if isinstance(self._shadow, Exception):
            raise self._shadow
        return self._shadow or ""


@pytest.mark.asyncio
async def test_capture_appends_shadow_content() -> None:
    from pms.adapters._render_capture import capture_rendered_dom

    page = _FakePage(
        "<html><entrata-pp-unit-cards></entrata-pp-unit-cards></html>",
        '<shadow-content data-shadow-host="entrata-pp-unit-cards">'
        '<a data-jd-fp-selector="unit-card">Unit 204 $1,400</a></shadow-content>',
    )
    out = await capture_rendered_dom(page, fallback=None)
    assert out is not None
    assert "Unit 204" in out  # shadow roster now visible to DOM parsers
    assert out.startswith("<html>")  # light DOM preserved first


@pytest.mark.asyncio
async def test_capture_light_dom_only_page_unchanged() -> None:
    from pms.adapters._render_capture import capture_rendered_dom

    page = _FakePage("<html><div>plain</div></html>", "")
    out = await capture_rendered_dom(page, fallback=None)
    assert out == "<html><div>plain</div></html>"


@pytest.mark.asyncio
async def test_capture_shadow_error_keeps_light_dom() -> None:
    from pms.adapters._render_capture import capture_rendered_dom

    page = _FakePage("<html>ok</html>", RuntimeError("csp"))
    out = await capture_rendered_dom(page, fallback=None)
    assert out == "<html>ok</html>"


@pytest.mark.asyncio
async def test_capture_none_page_returns_fallback() -> None:
    from pms.adapters._render_capture import capture_rendered_dom

    assert await capture_rendered_dom(None, fallback="prior") == "prior"


# ── 2026-07-19: shadow-walk JS must stay self-terminating (render-hang fix) ──


def test_shadow_serialize_js_is_bounded() -> None:
    """The shadow-DOM walk must carry hard internal bounds — a hung/slow
    page.evaluate is NOT cancellable by the caller's asyncio.wait_for, so the
    only reliable guard is the JS terminating itself. Pin the caps so they
    can't be silently dropped (which reintroduces the 600s render-hang)."""
    from pms.adapters._render_capture import _SHADOW_SERIALIZE_JS as JS

    assert "TIME_MS" in JS and "Date.now()" in JS  # wall-clock budget
    assert "MAX_NODES" in JS  # total node cap
    assert "MAX_DEPTH" in JS  # recursion depth cap
    assert "MAX_OUT" in JS  # output-size cap
    assert "seen" in JS and "new Set()" in JS  # shadow-root cycle guard
    assert "budgetHit" in JS
