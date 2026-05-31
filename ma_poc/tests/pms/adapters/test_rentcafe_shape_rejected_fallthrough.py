"""RentCafe SHAPE_REJECTED fallthrough (2026-05-31).

Mirrors the Knock empty-API fallthrough pattern. When the RentCafe
adapter declares SHAPE_REJECTED — captured network responses but none
matched the RentCafe envelope/key signature — the property is almost
certainly a detector misroute: operator's homepage has a SecureCafe
LINK (lease application portal) but the actual PMS is custom WP / Wix
/ Squarespace with rents in static HTML.

Surfaces standard floor-plan sub-paths as link-hop hints so the
orchestrator drives the next tier (DOM scan / generic_plan_text /
empty-inventory predicate) against the right URL.

Cohort impact (may13 canary, no-proxy): 404 props share this exact
SHAPE_REJECTED pattern; direct curl_cffi probes confirm ~50% have
extractable rent on the main URL or a /floorplans variant.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.rentcafe import (
    _RENTCAFE_SHAPE_REJECTED_FALLTHROUGH_PATHS,
    _TIER_SHAPE_REJECTED,
    _rentcafe_shape_rejected_emit_subpage_hints,
)


class _FakeResult:
    """Minimal stand-in for AdapterResult so the helper can be unit-tested
    without spinning up the full adapter machinery."""
    def __init__(self) -> None:
        self._embedded_floorplan_subpage_hints: list[tuple[str, str]] | None = None


def test_emit_appends_all_fallthrough_paths_for_clean_result() -> None:
    """First call on a fresh result populates the full path set."""
    r = _FakeResult()
    _rentcafe_shape_rejected_emit_subpage_hints(r, "https://example.com/")
    hints = r._embedded_floorplan_subpage_hints
    assert hints is not None
    assert len(hints) == len(_RENTCAFE_SHAPE_REJECTED_FALLTHROUGH_PATHS)
    urls = {u for u, _ in hints}
    assert "https://example.com/floorplans" in urls
    assert "https://example.com/floor-plans" in urls
    assert "https://example.com/availability" in urls
    # parser_id consistent
    assert all(pid == "rentcafe_shape_rejected_fallthrough" for _, pid in hints)


def test_emit_is_idempotent_on_repeat() -> None:
    """Second call must not double the hint list."""
    r = _FakeResult()
    _rentcafe_shape_rejected_emit_subpage_hints(r, "https://example.com/")
    n_first = len(r._embedded_floorplan_subpage_hints)
    _rentcafe_shape_rejected_emit_subpage_hints(r, "https://example.com/")
    n_second = len(r._embedded_floorplan_subpage_hints)
    assert n_first == n_second


def test_emit_does_not_overwrite_existing_hints_from_other_paths() -> None:
    """Hints from upstream parsers (e.g. knock) must survive."""
    r = _FakeResult()
    r._embedded_floorplan_subpage_hints = [
        ("https://example.com/upstream-hint", "some_other_parser"),
    ]
    _rentcafe_shape_rejected_emit_subpage_hints(r, "https://example.com/")
    urls = [u for u, _ in r._embedded_floorplan_subpage_hints]
    assert "https://example.com/upstream-hint" in urls
    assert "https://example.com/floorplans" in urls


def test_emit_on_malformed_base_url_is_safe() -> None:
    """Empty / bare-string base_url must not raise or emit bad URLs."""
    r = _FakeResult()
    _rentcafe_shape_rejected_emit_subpage_hints(r, "")
    assert r._embedded_floorplan_subpage_hints is None
    _rentcafe_shape_rejected_emit_subpage_hints(r, "not a url")
    assert r._embedded_floorplan_subpage_hints is None


def test_emit_anchors_on_base_url_host() -> None:
    """All hints stay on the property's own host — no cross-domain leakage."""
    r = _FakeResult()
    _rentcafe_shape_rejected_emit_subpage_hints(
        r, "https://www.timberfallsapartments.com/"
    )
    urls = [u for u, _ in r._embedded_floorplan_subpage_hints]
    for u in urls:
        assert "timberfallsapartments.com" in u


def test_tier_constant_unchanged() -> None:
    """Anchor test — if anyone renames _TIER_SHAPE_REJECTED the fallthrough
    wiring at the call site (rentcafe.py:572) breaks silently. Catch here."""
    assert _TIER_SHAPE_REJECTED == "TIER_1_API_RENTCAFE_SHAPE_REJECTED"


@pytest.mark.parametrize("path", _RENTCAFE_SHAPE_REJECTED_FALLTHROUGH_PATHS)
def test_fallthrough_paths_are_floor_plan_shaped(path: str) -> None:
    """Sentinel: every path must look like an inventory-page URL.
    Catches a future edit that accidentally adds a non-inventory path
    (homepage, contact, etc.) which would waste link-hop budget."""
    inventory_tokens = ("floor", "plan", "avail", "apart", "unit", "rent")
    assert any(t in path.lower() for t in inventory_tokens), (
        f"path {path!r} doesn't look like an inventory-page URL"
    )
