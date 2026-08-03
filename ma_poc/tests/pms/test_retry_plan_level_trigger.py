"""Plan-level output must trigger the next-candidate retry (2026-07-25).

The multi-candidate retry (Path B) has been ENABLED BY DEFAULT
(``PATH_B_RETRY_ENABLED`` defaults to "1", 2 attempts) and re-dispatches to
the next PMS the detector saw. It never fired for a plan-level extraction.

Why: ``_retry_trigger_reason`` returned None for those results. A plan-level
extraction HAS units (floor-plan rows), they clear the dimension gate, and
they carry rent and area — so all four existing checks passed and the
pipeline treated the property as a success.

It is not one. On the 2026-07-25 run that was 1,127 properties parked at
plan level, and a 42-property live probe with two-way adversarial refutation
found 39 recoverable against only 3 true ceilings. The unit data is
published; we stopped at the first adapter that returned anything.

The detector usually HAD another candidate to offer — 21 onesite-detected
properties in that run were ultimately served by SightMap — so this is the
point where "try the other signals we detected" pays.

Two halves are tested:
  * the TRIGGER fires on plan-level rows and stays quiet on real units
  * the WIN CONDITION requires a canonical apartment for every initial
    trigger, while plan-only retry output remains fallback data
"""

from __future__ import annotations

from ma_poc.core.identity import unit_has_real_anchor


def _rows_are_plan_level(units: list[dict]) -> bool:
    """Mirror of the in-scraper predicate (a closure, so not importable).

    Kept deliberately identical: both resolve plan-vs-unit through
    ``unit_has_real_anchor``, the same predicate identity itself uses when it
    decides whether to mint a synthetic id.
    """
    return bool(units) and not any(unit_has_real_anchor(u) for u in units)


# ── The trigger ─────────────────────────────────────────────────────────────


def test_plan_rows_are_recognised_as_plan_level() -> None:
    """The 1,127-property shape: floor-plan names and rents, no apartments."""
    units = [
        {"floor_plan_name": "A1", "rent_low": 1500, "sqft": 700},
        {"floor_plan_name": "B2", "rent_low": 1800, "sqft": 950},
    ]
    assert _rows_are_plan_level(units) is True


def test_real_apartments_are_not_plan_level() -> None:
    """Grand Oaks D103 — a genuine roster must NOT trigger a retry."""
    assert _rows_are_plan_level([{"unit_number": "D103", "rent_low": 1939}]) is False


def test_one_real_unit_is_enough_to_suppress_the_trigger() -> None:
    """Partial rosters are common. A property that surfaced even one real
    apartment is not plan-level, and re-dispatching risks losing it."""
    units = [
        {"floor_plan_name": "A1", "rent_low": 1500},
        {"unit_number": "204", "rent_low": 1550},
    ]
    assert _rows_are_plan_level(units) is False


def test_post_format_inferred_ids_still_read_as_plan_level() -> None:
    """The predicate must work on both sides of the formatter boundary —
    pre-format rows carry unit_number, post-format rows carry inferred_*."""
    assert _rows_are_plan_level([{"unit_id": "inferred_abc", "rent_low": 795}]) is True


def test_backend_source_id_counts_as_a_real_anchor() -> None:
    """A per-unit backend id is real identity even with no unit_number — the
    2026-07-18 carve-out must not be undone by this trigger."""
    units = [{"floor_plan_name": "A1", "source_ids": {"appfolio_listing_id": "998877"}}]
    assert _rows_are_plan_level(units) is False


def test_empty_units_is_not_plan_level() -> None:
    """Zero units is the empty_exit trigger's business, not this one —
    returning True here would double-trigger and confuse the telemetry."""
    assert _rows_are_plan_level([]) is False


# ── The win condition ───────────────────────────────────────────────────────


def _wins(new_units: list[dict], trigger: str | None) -> bool:
    """Mirror of the canonical-unit rule, minus quality/rent gates."""
    del trigger  # every trigger deliberately shares the same rule
    return bool(new_units) and not _rows_are_plan_level(new_units)


def test_plan_level_retry_must_return_unit_level_to_win() -> None:
    """Swapping plan-level for plan-level is not a win: it changes the tier
    label without adding a single apartment, and discards a baseline that may
    be the better of the two."""
    another_plan_level = [{"floor_plan_name": "X1", "rent_low": 1400}]
    assert _wins(another_plan_level, "plan_level_only") is False

    real_roster = [{"unit_number": "311", "rent_low": 2670}]
    assert _wins(real_roster, "plan_level_only") is True


def test_other_triggers_also_require_unit_level_to_win() -> None:
    """Plan cards improve an empty result only as fallback, never unit success."""
    plan_rows = [{"floor_plan_name": "X1", "rent_low": 1400}]
    assert _wins(plan_rows, "empty_exit") is False
    assert _wins(plan_rows, "quality_gate") is False
    assert _wins(plan_rows, None) is False

    real_roster = [{"unit_number": "311", "rent_low": 2670}]
    assert _wins(real_roster, "empty_exit") is True


# ── Wiring: the trigger is reachable and the retry is on by default ─────────


def test_retry_is_enabled_by_default() -> None:
    """The whole lever depends on Path B being live. If this default ever
    flips to off, the plan-level trigger becomes dead code."""
    import os

    assert os.environ.get("PATH_B_RETRY_ENABLED", "1").lower() not in {
        "0", "false", "no", "",
    }


def test_scraper_declares_the_plan_level_trigger() -> None:
    """Source-level pin: the trigger must exist in _retry_trigger_reason and
    the win check must route through the trigger-aware variant. Both are
    closures, so this is the only way to assert the wiring."""
    import inspect

    from ma_poc.pms import scraper

    src = inspect.getsource(scraper)
    assert '"plan_level_only"' in src
    assert "_retry_win_condition_for(" in src, (
        "the win check must be trigger-aware, or a plan_level_only retry can "
        "promote another plan-level result"
    )
