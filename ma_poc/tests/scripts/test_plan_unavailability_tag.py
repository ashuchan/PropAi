"""Plan-level unavailability tagging (2026-07-30).

Hand-verification of the SUCCESS_PLAN_LEVEL cohort found that a property which
ships plan-level is not uniformly a floor-plan brochure — the page often carries
a *positive* "no bookable inventory" signal the pipeline was flattening to a
generic (often defaulted-AVAILABLE) plan row:

  * Gallery Park   — "Wait List" buttons, plans priced "from $1,299"
  * 78 West        — AppFolio widget: "We currently have no available properties
                     for rent."
  * Rosewood       — "Contact for availability"
  * Eagle Creek    — waitlist AND "contact us"  (waitlist is the stronger truth)

These tests pin the classifier to those exact phrasings AND pin the two guards
that keep it from firing on incidental text: the ubiquitous "Contact Us" nav
link and the "prices and availability subject to change" disclaimer.
"""

from __future__ import annotations

import pytest

from ma_poc.core.schema_v2 import (
    apply_plan_unavailability_tag,
    classify_plan_unavailability_signal,
)


# ─────────────────────────── classify: positive signals ──────────────────────


def test_waitlist_button_is_detected() -> None:
    """Gallery Park: the signal is the button text, not availability prose."""
    html = (
        "<div>One Bedroom</div><div>Total Monthly Leasing Price From $1,299</div>"
        '<button>Wait List</button>'
    )
    assert classify_plan_unavailability_signal(html) == "PLAN_WAITLIST"


def test_waitlist_spacing_variants() -> None:
    for token in ("Wait List", "waitlist", "WAIT-LIST", "join the wait list"):
        assert classify_plan_unavailability_signal(f"<p>{token}</p>") == "PLAN_WAITLIST"


def test_appfolio_no_available_properties_is_no_availability() -> None:
    """78 West — the exact AppFolio empty-state string."""
    html = "<div>Current Listings</div><p>We currently have no available properties for rent.</p>"
    assert classify_plan_unavailability_signal(html) == "PLAN_NO_AVAILABILITY"


def test_no_available_units_variants() -> None:
    for phrase in (
        "no available units",
        "no available apartments",
        "no availability at this time",
        "check back soon for availability",
    ):
        assert classify_plan_unavailability_signal(f"<p>{phrase}</p>") == "PLAN_NO_AVAILABILITY"


def test_contact_for_availability_is_detected() -> None:
    """Rosewood — 'Contact for availability'."""
    for phrase in (
        "Contact for availability",
        "Call for availability",
        "Contact us to check availability",
        "Contact our office for current availability",
        "Contact us for pricing and availability",
    ):
        assert (
            classify_plan_unavailability_signal(f"<a>{phrase}</a>")
            == "PLAN_CONTACT_FOR_AVAILABILITY"
        ), phrase


# ─────────────────────────── classify: precedence ────────────────────────────


def test_no_availability_beats_waitlist_beats_contact() -> None:
    """A page that states it has nothing available is a stronger truth than a
    per-plan waitlist, which is stronger than a soft 'call us'. Eagle Creek
    (waitlist + contact us) must resolve to WAITLIST, not contact."""
    both = "<p>Join the wait list</p><a>Contact us for availability</a>"
    assert classify_plan_unavailability_signal(both) == "PLAN_WAITLIST"

    all_three = (
        "<p>We currently have no available units</p>"
        "<p>Wait List</p><a>Contact us for availability</a>"
    )
    assert classify_plan_unavailability_signal(all_three) == "PLAN_NO_AVAILABILITY"


# ─────────────────────────── classify: false-positive guards ─────────────────


def test_bare_contact_us_nav_is_not_a_signal() -> None:
    """Every marketing site has a 'Contact Us' link — it is not availability."""
    assert classify_plan_unavailability_signal("<nav><a>Contact Us</a></nav>") is None


def test_availability_disclaimer_is_not_a_signal() -> None:
    """The standard footer disclaimer must not read as unavailability, even next
    to a Contact Us nav link (New Orleans / Stoneplace shape)."""
    html = (
        "<nav><a>Contact Us</a></nav>"
        "<p>Prices and availability are subject to change at any time. "
        "Please see a representative for details.</p>"
    )
    assert classify_plan_unavailability_signal(html) is None


def test_empty_and_no_signal_return_none() -> None:
    assert classify_plan_unavailability_signal("") is None
    assert classify_plan_unavailability_signal(None) is None  # type: ignore[arg-type]
    assert classify_plan_unavailability_signal("<p>Now leasing! Apply today.</p>") is None


# ─────────────────────────── apply: stamping the rows ────────────────────────


def _plan_row(**kw: object) -> dict:
    row = {"is_floor_plan_level": True, "availability_status": "AVAILABLE"}
    row.update(kw)
    return row


def test_apply_sets_waitlist_and_flag_on_plan_rows() -> None:
    rows = [_plan_row(rent_low=1299.0), _plan_row(rent_low=1750.0)]
    changed = apply_plan_unavailability_tag(rows, "PLAN_WAITLIST")
    assert changed == 2
    for r in rows:
        assert r["availability_status"] == "WAITLIST"
        assert "PLAN_WAITLIST" in r["data_quality_flag"]
        # the published price is never touched
    assert rows[0]["rent_low"] == 1299.0


def test_apply_never_touches_a_real_unit_row() -> None:
    """A unit-level row (is_floor_plan_level falsey) is never coerced by a
    page-level notice."""
    unit = {"is_floor_plan_level": False, "availability_status": "AVAILABLE", "unit_id": "K-201"}
    assert apply_plan_unavailability_tag([unit], "PLAN_NO_AVAILABILITY") == 0
    assert unit["availability_status"] == "AVAILABLE"


def test_apply_only_makes_status_more_restrictive() -> None:
    """A row a source already marked UNAVAILABLE is not softened to WAITLIST."""
    row = _plan_row(availability_status="UNAVAILABLE")
    apply_plan_unavailability_tag([row], "PLAN_WAITLIST")
    assert row["availability_status"] == "UNAVAILABLE"  # unchanged
    assert "PLAN_WAITLIST" in row["data_quality_flag"]  # flag still recorded


def test_apply_contact_downgrades_available_but_not_waitlist() -> None:
    avail = _plan_row(availability_status="AVAILABLE")
    waitlisted = _plan_row(availability_status="WAITLIST")
    apply_plan_unavailability_tag([avail, waitlisted], "PLAN_CONTACT_FOR_AVAILABILITY")
    assert avail["availability_status"] == "UNKNOWN"       # 0 -> 1
    assert waitlisted["availability_status"] == "WAITLIST"  # 2 stays


def test_apply_is_idempotent() -> None:
    row = _plan_row()
    apply_plan_unavailability_tag([row], "PLAN_WAITLIST")
    apply_plan_unavailability_tag([row], "PLAN_WAITLIST")
    assert row["data_quality_flag"].count("PLAN_WAITLIST") == 1


def test_apply_noops_on_empty_reason_or_bad_input() -> None:
    row = _plan_row()
    assert apply_plan_unavailability_tag([row], None) == 0
    assert row["availability_status"] == "AVAILABLE"
    assert apply_plan_unavailability_tag(None, "PLAN_WAITLIST") == 0
    assert apply_plan_unavailability_tag("garbage", "PLAN_WAITLIST") == 0  # type: ignore[arg-type]
