"""#69 — pin the STRING COUPLING between sanity.py and schema_v2.py.

``core.schema_v2._SANITY_AREA_LABELS`` hard-references two label strings that
``extraction.sanity`` writes into ``_sanity_dropped``: ``"area"`` (from the
``_sanitize_field`` call for ``SQFT_KEYS``) and ``"area_implausible_for_beds"``
(from ``_sanitize_sqft_vs_beds``). The ``NOT_CAPTURED`` arm of the area-absence
taxonomy is built on that reference.

Nothing enforces it. Rename either label in ``sanity.py`` and
``_sanity_nulled_area`` silently returns ``False`` forever: no import breaks,
no type checks, no test fails, and every clamp casualty quietly stops being
labelled ``NOT_CAPTURED`` — it becomes indistinguishable from a row nobody
ever published a square footage for. That is precisely the claim-about-the-
operator the taxonomy exists to avoid making by accident.

This is the same class of string coupling that has produced four separate
defects in this repo. These tests derive the labels by RUNNING ``sanity_bound``
rather than restating them, so a rename fails here instead of in production
six weeks later.
"""

from __future__ import annotations

from typing import Any

import pytest
from ma_poc.core.schema_v2 import (
    _SANITY_AREA_LABELS,
    AREA_ABSENCE_NOT_CAPTURED,
    _sanity_nulled_area,
    classify_area_absence,
)
from ma_poc.extraction.sanity import (
    REASON_ABOVE_MAX,
    REASON_BELOW_MIN,
    REASON_IMPLAUSIBLE_FOR_BEDS,
    reset_clamp_ledger,
    sanity_bound,
)


@pytest.fixture(autouse=True)
def _isolate_clamp_ledger():
    reset_clamp_ledger()
    yield
    reset_clamp_ledger()


# One specimen per clamp path ``sanity_bound`` can take, and whether that path
# destroys a SQUARE FOOTAGE. The must-NOT rows are load-bearing: a coupling
# test that only checks the positives passes just as happily when
# ``_sanity_nulled_area`` is rewritten to ``return True``.
#
# (label, unit, is_an_area_clamp)
_SPECIMENS: list[tuple[str, dict[str, Any], bool]] = [
    # ── area clamps: MUST be detected ────────────────────────────────────
    ("area below the absolute floor",  {"area": 45},                 True),
    ("area above the absolute cap",    {"area": 45_000},             True),
    ("area impossible for bed count",  {"beds": 3, "area": 310},     True),
    # ── other clamps: must NOT be detected as an area clamp ──────────────
    ("rent_low below floor",           {"rent_low": 1},             False),
    ("rent_low above cap",             {"rent_low": 8005551212},    False),
    ("rent_high below floor",          {"rent_high": 1},            False),
    ("beds out of range",              {"beds": 99},                False),
    ("baths out of range",             {"baths": 99},               False),
    # ── no clamp at all: must NOT be detected ────────────────────────────
    ("everything in range",  {"beds": 2, "baths": 2, "area": 950, "rent_low": 1800}, False),
    ("area simply absent",             {"beds": 2, "rent_low": 1800}, False),
    ("empty unit",                     {},                          False),
    # ── near-miss shapes: must NOT be detected ───────────────────────────
    ("area at the exact floor",        {"area": 150},               False),
    ("area at the exact cap",          {"area": 10_000},            False),
    ("sqft exactly at the beds floor", {"beds": 3, "area": 700},    False),
]


@pytest.mark.parametrize(
    ("label", "unit", "is_area_clamp"), _SPECIMENS, ids=[s[0] for s in _SPECIMENS]
)
def test_area_clamp_detection_matches_what_sanity_actually_labels(
    label: str, unit: dict[str, Any], is_area_clamp: bool
) -> None:
    """``_sanity_nulled_area`` must fire on exactly the area clamps.

    Run through ``sanity_bound`` so the labels come from the production code
    path. Rename a label in ``sanity.py`` and the True rows fail here.
    """
    out = sanity_bound(dict(unit))
    assert _sanity_nulled_area(out) is is_area_clamp, label


def test_every_label_in_the_frozenset_is_one_sanity_can_actually_emit() -> None:
    """The other direction: no dead string in ``_SANITY_AREA_LABELS``.

    A renamed label leaves the old spelling sitting in the frozenset, still
    importing cleanly, matching nothing. Deriving the producible set by
    running the code is what makes that visible.
    """
    produced: set[str] = set()
    for _label, unit, _is_area in _SPECIMENS:
        dropped = sanity_bound(dict(unit)).get("_sanity_dropped") or []
        produced.update(str(d).strip().lower() for d in dropped)

    orphans = set(_SANITY_AREA_LABELS) - produced
    assert not orphans, (
        f"_SANITY_AREA_LABELS references labels sanity.py no longer emits: "
        f"{sorted(orphans)}. Producible labels are {sorted(produced)}."
    )


def test_the_taxonomy_arm_that_depends_on_the_coupling_still_fires() -> None:
    """End-to-end: the coupling exists to serve THIS classification.

    A clamped area is OUR failure (``NOT_CAPTURED``). If the label match
    breaks, the row falls through to a label that makes a claim about the
    operator instead — which is the harm, not the broken string.
    """
    for label, unit, is_area_clamp in _SPECIMENS:
        if not is_area_clamp:
            continue
        out = sanity_bound(dict(unit))
        # sanity nulled every sqft alias, so the formatter is handed nothing.
        absence, evidence = classify_area_absence(
            out, formatted_area=-1, supplied_value=out.get("area")
        )
        assert absence == AREA_ABSENCE_NOT_CAPTURED, label
        assert evidence == "value_dropped_by_sanity_bounds", label


def test_a_rent_clamp_must_not_license_the_area_label() -> None:
    """A clamped RENT says nothing about the square footage.

    ``_sanity_dropped`` is a flat list of labels for every field, so a
    substring-ish or over-broad match here would let a rent clamp claim we
    captured-and-lost an area we never saw.
    """
    out = sanity_bound({"rent_low": 1, "rent_high": 1})
    assert out.get("_sanity_dropped") == ["rent_low", "rent_high"]
    assert _sanity_nulled_area(out) is False
    absence, evidence = classify_area_absence(out, formatted_area=-1)
    assert evidence != "value_dropped_by_sanity_bounds"


def test_reason_codes_are_shared_constants_not_retyped_literals() -> None:
    """``core.schema_v2`` records clamps with bare reason strings.

    It cannot import ``extraction.sanity`` at module scope, so the reason
    codes are written out by hand at the two formatter clamp sites. Pin them
    to the constants — a renamed reason would otherwise split one bucket
    into two in every aggregation.
    """
    import inspect

    import ma_poc.core.schema_v2 as sv2

    for fn in (sv2._format_area, sv2._format_rent):
        src = inspect.getsource(fn)
        for literal in (REASON_BELOW_MIN, REASON_ABOVE_MAX):
            if literal in src:
                break
        else:  # pragma: no cover - only reached on a rename
            pytest.fail(
                f"{fn.__name__} records no recognised reason code; "
                f"expected one of {REASON_BELOW_MIN!r} / {REASON_ABOVE_MAX!r}"
            )

    # The third reason belongs to sanity's cross-field pass only — it must not
    # have leaked into the formatter, which has no bed count to reason about.
    assert REASON_IMPLAUSIBLE_FOR_BEDS not in inspect.getsource(sv2._format_area)
