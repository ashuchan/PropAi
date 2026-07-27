"""Binding tests for verdict's ``source_ids`` evidence view.

WHY THIS FILE EXISTS — a mutation survived without it. Replacing
``reporting/verdict._has_per_unit_source_id``'s registry lookup with the
hand-maintained 12-key frozenset it replaced (verbatim, the deleted literal)
passed the FULL suite with a byte-identical failure set. Nothing anywhere
distinguished the two sets, even though they genuinely disagree on 487 rows in
2026-07-12, 514 in the 2026-07-18 canary and 55 in 2026-07-26-plancohort.

The reason the old suite could not catch it: the only ``source_ids`` key any
verdict test used was ``sightmap_unit_id``, which is in BOTH sets. Every key
that differs — the four dropped (``camden_unit_id``, ``edifice_unit_id``,
``thinkreside_unit``, ``securecafe_id``) and the six added
(``realpage_cws_unit_id``, ``fortresstech_unit_id``, ``onsite_unit_id``,
``venterra_unit_code``, ``realpage_oll_unit_id``, ``securecafe_apartment_id``)
— appeared in no test at all.

So these tests assert on keys that DIFFER between the two sets. A future
registry edit that changes what verdict credits now fails here instead of
shipping silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.core.source_ids import (
    PER_UNIT_EVIDENCE_KEYS,
    PER_UNIT_IDENTITY_KEYS,
    SOURCE_ID_SCOPES,
    SourceIdScope,
)
from ma_poc.reporting.verdict import _has_per_unit_source_id, _units_are_unit_level

#: The exact frozenset that used to live in ``reporting/verdict.py``. Kept here
#: ONLY as the mutation target these tests must be able to tell apart from the
#: registry view. Do not import it into production code.
_LEGACY_12 = frozenset(
    {
        "sightmap_unit_id", "entrata_uid", "apts247_unit_id", "camden_unit_id",
        "udr_unitid", "edifice_unit_id", "spherexx_unit_id", "appfolio_listing_id",
        "appfolio_listable_uid", "appfolio_id", "thinkreside_unit", "securecafe_id",
    }
)


def _row(key: str, value: Any = "9001") -> dict[str, Any]:
    """A unit row whose ONLY identity evidence is *key* in ``source_ids``.

    No ``unit_id``/``unit_number``: ``_units_are_unit_level`` short-circuits on
    ``unit_has_real_anchor`` first, so a row with a natural number would credit
    unit-level regardless of the registry and the test would prove nothing.
    """
    return {"source_ids": {key: value}, "rent_low": 1500, "market_rent_low": 1500}


# ── the keys the two sets disagree on ───────────────────────────────────────


@pytest.mark.parametrize(
    "key", sorted(_LEGACY_12 - PER_UNIT_EVIDENCE_KEYS)
)
def test_dropped_key_is_no_longer_unit_level_evidence(key: str) -> None:
    """A key the legacy list credited must NOT be credited now.

    ``camden_unit_id`` is the one that matters: ``_camden.py:251`` reads
    ``plan["realPageUnitId"]`` off the PLAN object, under the literal comment
    "Plan-level fingerprint shared across all units of this plan", and stamps
    it identically into every emitted row (measured 366 rows / 129 distinct,
    and it rotates on 27.2% of rows joined across 07-12 → 07-18). Crediting it
    as per-unit evidence is the PR #110 false-gold shape.
    """
    assert key in _LEGACY_12
    assert _has_per_unit_source_id(_row(key)) is False
    assert _units_are_unit_level([_row(key)]) is False


@pytest.mark.parametrize(
    "key", sorted(PER_UNIT_EVIDENCE_KEYS - _LEGACY_12)
)
def test_added_key_is_unit_level_evidence(key: str) -> None:
    """A real per-unit backend id the legacy list missed must now be credited.

    ``entrata_unit_id`` sat in identity's whitelist for months with no writer
    while the real Entrata key was absent from it; these six are the same class
    of miss, measured off adapter code rather than key spelling.
    """
    assert key not in _LEGACY_12
    assert _has_per_unit_source_id(_row(key)) is True
    assert _units_are_unit_level([_row(key)]) is True


def test_the_two_sets_actually_differ() -> None:
    """Guard the premise: if the sets ever coincide, the tests above go vacuous.

    Both parametrised tests above generate ZERO cases when the difference is
    empty, and an empty parametrize list is a silent pass.
    """
    assert _LEGACY_12 - PER_UNIT_EVIDENCE_KEYS, "no dropped keys left to test"
    assert PER_UNIT_EVIDENCE_KEYS - _LEGACY_12, "no added keys left to test"


# ── the UNIT_STABLE / UNIT_VOLATILE split ───────────────────────────────────


def test_volatile_keys_are_evidence_but_never_anchors() -> None:
    """The whole point of having two derived views.

    A key measured to ROTATE its value across runs is still proof the row is a
    single apartment (verdict needs uniqueness only), but it must never become
    the daily-join key (identity needs cross-run stability) — a rotating anchor
    makes the same apartment read as "disappeared + new" every run.

    ``appfolio_listing_id`` is the case that forced the split: registered
    UNIT_STABLE and sitting FIRST in the anchor preference order while rotating
    on 44 of 303 joined rows (14.5%) — the same order of magnitude as the 27.2%
    that sent ``camden_unit_id`` to PLAN.
    """
    volatile = {
        k for k, s in SOURCE_ID_SCOPES.items() if s is SourceIdScope.UNIT_VOLATILE
    }
    assert volatile, "UNIT_VOLATILE is empty — this invariant is untested"
    assert "appfolio_listing_id" in volatile

    for key in volatile:
        assert key in PER_UNIT_EVIDENCE_KEYS, f"{key} lost its verdict view"
        assert key not in PER_UNIT_IDENTITY_KEYS, f"{key} may not mint a unit_id"
        assert _has_per_unit_source_id(_row(key)) is True
