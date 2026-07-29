"""#69 — the V2 FORMATTER clamp must be recorded, and only the clamp.

Task #69's original premise was that ``extraction.sanity``'s clamp is why
the run has ~7.3K null rents and ~7.8K area sentinels. Measurement corrected
it. On run-2026-07-27-full-0d54ca7 (104,964 rows, full denominator, direct
read of the shipped artifacts):

  * 7,752 area sentinels — 7,722 (99.61%) carry no raw residue at all and
    are undecomposable; 30 (0.39%) are provably clamp-nulled.
  * 7,306 null ``rent_low`` — 7,139 (97.71%) no residue; 167 (2.29%) clamped.
  * 100% of that attributable slice came from ``core.schema_v2._format_area``
    / ``_format_rent``. ``extraction.sanity``'s clamp left zero trace.

So the clamp is a SMALL effect and the dominant cause is an upstream capture
gap. These tests exist so the small slice is auditable rather than inferred —
and so the uninstrumented clamp is no longer the only measurable one.

Three properties are pinned:

  1. the bound, and only the bound, is recorded — a value that never parsed
     is a capture gap, not a clamp, and must NOT be counted as one;
  2. nothing emitted moves;
  3. BOTH v2 unit formatters record, with the producing tier attached.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ma_poc.core.schema_v2 import (
    _format_area,
    _format_rent,
    property_publishes_area,
)
from ma_poc.extraction.sanity import (
    REASON_ABOVE_MAX,
    REASON_BELOW_MIN,
    STAGE_SANITY_BOUND,
    STAGE_V2_FORMATTER,
    clamp_ledger,
    clamp_tier_context,
    reset_clamp_ledger,
    sanity_bound,
)
from ma_poc.scripts.runners.jugnu import _format_rent as _jugnu_format_rent


@pytest.fixture(autouse=True)
def _isolate_clamp_ledger():
    reset_clamp_ledger()
    yield
    reset_clamp_ledger()


# ── The table ────────────────────────────────────────────────────────────────
#
# (label, raw input, expected emitted value, expected recorded reason or None)
#
# ``None`` in the last column is a must-NOT-record row. Half the table is
# must-NOT rows on purpose: over-recording is the failure mode that would turn
# this ledger into noise, and "unparseable" being logged as "clamped" is
# exactly the "couldn't look" → "nothing there" conflation the whole task is
# about.

_AREA_TABLE: list[tuple[str, Any, int, str | None]] = [
    # ── the bound fired: RECORD ──────────────────────────────────────────
    ("published zero",            "0",          -1, REASON_BELOW_MIN),
    ("sightmap placeholder one",  "1",          -1, REASON_BELOW_MIN),
    ("bed count read as sqft",    3,            -1, REASON_BELOW_MIN),
    ("truncated 070",             "070",        -1, REASON_BELOW_MIN),
    ("floor number",              "100",        -1, REASON_BELOW_MIN),
    ("range with zero low",       "0-500",      -1, REASON_BELOW_MIN),
    ("rent read into sqft",       "45000",      -1, REASON_ABOVE_MAX),
    ("phone read into sqft",      "8005551212", -1, REASON_ABOVE_MAX),
    # ── must NOT record ──────────────────────────────────────────────────
    ("in-bounds plain",           "750",       750, None),
    ("in-bounds comma",           "1,050",    1050, None),
    ("in-bounds with unit",       "1,250 sq ft", 1250, None),
    ("exact lower bound",         150,         150, None),
    ("exact upper bound",         10_000,   10_000, None),
    ("absent: None",              None,         -1, None),
    ("absent: -1 sentinel",       -1,           -1, None),
    ("parse gap: empty",          "",           -1, None),
    ("parse gap: no digits",      "Call for details", -1, None),
    ("parse gap: whitespace",     "   ",        -1, None),
]

_RENT_TABLE: list[tuple[str, Any, float | None, str | None]] = [
    # ── the bound fired: RECORD ──────────────────────────────────────────
    ("published $0 string",       "$0",       None, REASON_BELOW_MIN),
    ("published zero numeric",    0,          None, REASON_BELOW_MIN),
    ("negative rent",             -50,        None, REASON_BELOW_MIN),
    ("exactly the bound",         1,          None, REASON_BELOW_MIN),
    ("zero with comma noise",     "$0,00",    None, REASON_BELOW_MIN),
    # ── must NOT record ──────────────────────────────────────────────────
    ("plain rent",                1450,     1450.0, None),
    ("currency + comma",          "$1,450", 1450.0, None),
    ("just above the bound",      1.5,         1.5, None),
    ("absent: None",              None,       None, None),
    ("parse gap: empty",          "",         None, None),
    ("parse gap: prose",          "Call for pricing", None, None),
    ("parse gap: booking bool",   True,       None, None),
    ("parse gap: NaN literal",    "nan",      None, None),
]


def _recorded() -> list[dict[str, Any]]:
    """Every example currently on the ledger, flattened."""
    return [ex for row in clamp_ledger().rows() for ex in row["examples"]]


@pytest.mark.parametrize(
    ("label", "raw", "expected", "reason"),
    _AREA_TABLE,
    ids=[r[0] for r in _AREA_TABLE],
)
def test_area_clamp_table(label: str, raw: Any, expected: int, reason: str | None) -> None:
    """The [150,10000] bound is recorded; a parse gap is not."""
    got = _format_area(raw, record_as="area", property_id="P1", unit_id="U1")
    assert got == expected, label

    samples = _recorded()
    if reason is None:
        assert samples == [], f"{label}: recorded a clamp that did not happen"
        return
    assert len(samples) == 1, label
    assert samples[0]["reason"] == reason
    assert samples[0]["field"] == "area"
    assert samples[0]["stage"] == STAGE_V2_FORMATTER
    assert samples[0]["bounds"] == [150.0, 10_000.0]
    assert samples[0]["property_id"] == "P1"
    assert samples[0]["unit_id"] == "U1"


@pytest.mark.parametrize(
    ("label", "raw", "expected", "reason"),
    _RENT_TABLE,
    ids=[r[0] for r in _RENT_TABLE],
)
def test_rent_clamp_table(
    label: str, raw: Any, expected: float | None, reason: str | None
) -> None:
    """The ``> 1`` bound is recorded; a parse gap is not."""
    got = _format_rent(raw, record_as="rent_low", property_id="P1", unit_id="U1")
    assert got == expected, label

    samples = _recorded()
    if reason is None:
        assert samples == [], f"{label}: recorded a clamp that did not happen"
        return
    assert len(samples) == 1, label
    assert samples[0]["reason"] == reason
    assert samples[0]["field"] == "rent_low"
    assert samples[0]["stage"] == STAGE_V2_FORMATTER
    # The upper bound is open and must not reach disk as ``Infinity``.
    assert samples[0]["bounds"] == [1.0, None]


@pytest.mark.parametrize(
    ("label", "raw", "expected", "reason"),
    _RENT_TABLE,
    ids=[r[0] for r in _RENT_TABLE],
)
def test_rent_clamp_table_holds_for_the_production_fork(
    label: str, raw: Any, expected: float | None, reason: str | None
) -> None:
    """``scripts/runners/jugnu._format_rent`` is a SEPARATE implementation.

    It is not a delegate (it carries a ``parse_rent_range`` fallback the
    canonical copy does not), so it gets the same table rather than a
    promise. The two-copies-one-label defect has cost this repo four
    separate regressions; ``_format_area``'s widening lived in this fork
    alone for two months.
    """
    got = _jugnu_format_rent(raw, record_as="rent_low", property_id="P1")
    assert got == expected, label

    samples = _recorded()
    if reason is None:
        assert samples == [], f"{label}: recorded a clamp that did not happen"
        return
    assert len(samples) == 1, label
    assert samples[0]["reason"] == reason
    assert samples[0]["stage"] == STAGE_V2_FORMATTER


def test_jugnu_rent_fallback_arm_is_inert_today_and_says_so() -> None:
    """``"$0/mo"`` records NOTHING, and that is correct — not a miss.

    The noisy-rent fallback delegates to ``parse_rent_range``, which applies
    its own low-value filter and returns ``(None, None)``. So the fallback
    hands back no number, we cannot know what was thrown away, and calling
    that a clamp would be inventing evidence. Pinned rather than assumed: if
    ``parse_rent_range`` is ever widened to return the zero, this test fails
    and the next test proves the recording arm is ready for it.
    """
    from ma_poc.pms.adapters._parsing import parse_rent_range

    assert parse_rent_range("$0/mo") == (None, None)
    assert _jugnu_format_rent("$0/mo", record_as="rent_low") is None
    assert _recorded() == []


def test_jugnu_rent_fallback_records_when_the_parser_does_return_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback's own bound test is instrumented, not just the fast path.

    Exercised through a widened ``parse_rent_range`` because the real one
    filters first (see the test above). Recording only the fast path would
    leave this arm silent the day that filter moves.
    """
    monkeypatch.setattr(
        "ma_poc.pms.adapters._parsing.parse_rent_range",
        lambda _text: (0, 0),
    )
    assert _jugnu_format_rent("$0/mo", record_as="rent_low") is None
    samples = _recorded()
    assert len(samples) == 1
    assert samples[0]["reason"] == REASON_BELOW_MIN
    assert samples[0]["value"] == pytest.approx(0.0)


# ── Property 2: nothing emitted moves ────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "raw"), [(r[0], r[1]) for r in _AREA_TABLE], ids=[r[0] for r in _AREA_TABLE]
)
def test_area_emitted_value_is_untouched_by_instrumentation(label: str, raw: Any) -> None:
    """Recording must be observation. Same input, same output, recorded or not."""
    assert _format_area(raw) == _format_area(raw, record_as="area"), label


@pytest.mark.parametrize(
    ("label", "raw"), [(r[0], r[1]) for r in _RENT_TABLE], ids=[r[0] for r in _RENT_TABLE]
)
def test_rent_emitted_value_is_untouched_by_instrumentation(label: str, raw: Any) -> None:
    """Same, for both rent implementations."""
    assert _format_rent(raw) == _format_rent(raw, record_as="rent_low"), label
    assert _jugnu_format_rent(raw) == _jugnu_format_rent(raw, record_as="rent_low"), label


def test_probe_calls_do_not_record() -> None:
    """``property_publishes_area`` runs ``_format_area`` over EVERY row.

    It is deciding a property-level fact, not emitting anything. If it
    recorded, every out-of-bounds area on a property would be counted twice
    and the ledger's "which tier clamps most" answer would be wrong by a
    factor that varies with the property's row count.
    """
    units = [{"sqft": "0"}, {"sqft": "1"}, {"sqft": "45"}]
    assert property_publishes_area(units) is False
    assert clamp_ledger().total() == 0


# ── Property 3: both formatters record, with the tier ────────────────────────


def _scrape_result(tier: str) -> dict[str, Any]:
    return {
        "_extract_result": {"tier_used": tier, "llm_cost_usd": 0.0},
        "units": [
            {"unit_id": "A1", "market_rent_low": "$0", "sqft": "0"},
            {"unit_id": "A2", "market_rent_low": 1500, "sqft": "750"},
        ],
        "property_metadata": {},
        "plan_summaries": [],
    }


def _emit_via_core(tier: str) -> list[dict[str, Any]]:
    from ma_poc.core.schema_v2 import build_v2_property

    result = _scrape_result(tier)
    prop = build_v2_property(
        {"apartment_id": "999"}, None, result, result["units"]
    )
    return list(prop["units"])


def _emit_via_jugnu(tier: str) -> list[dict[str, Any]]:
    from ma_poc.scripts.runners.jugnu import _format_v2

    result = _scrape_result(tier)
    return list(_format_v2(result, {"apartment_id": "999"})["units"])


@pytest.mark.parametrize("emit", [_emit_via_core, _emit_via_jugnu], ids=["core", "jugnu"])
def test_clamp_formatter_parity(emit: Any) -> None:
    """BOTH v2 unit formatters record, and attribute to the producing tier.

    The tier is a PROPERTY-level fact; ``_format_area`` / ``_format_rent``
    are pure value functions that cannot see it. If either formatter stops
    wrapping its row emit in ``clamp_tier_context``, every clamp collapses
    to ``UNKNOWN`` and the ledger stops answering the one question #69
    exists to answer.
    """
    units = emit("TIER_1_5_EMBEDDED")

    # The emitted contract is unchanged: clamped row keeps the sentinels,
    # clean row keeps its values.
    assert [u["area"] for u in units] == [-1, 750]
    assert [u["rent_low"] for u in units] == [None, 1500.0]

    led = clamp_ledger()
    assert led.by_field("area") == {"TIER_1_5_EMBEDDED": 1}
    assert led.by_field("rent_low") == {"TIER_1_5_EMBEDDED": 1}
    assert led.to_dict()["per_property"]["999"] == {
        "area:BELOW_MIN": 1,
        "rent_low:BELOW_MIN": 1,
    }


@pytest.mark.parametrize("emit", [_emit_via_core, _emit_via_jugnu], ids=["core", "jugnu"])
def test_formatter_clamps_are_not_double_counted_per_row(emit: Any) -> None:
    """One clamped value, one record — however many times the row is read.

    Each formatter also runs ``property_publishes_area`` over the same rows.
    A count of 2 here means a probe started recording.
    """
    emit("TIER_3_DOM_GENERIC")
    assert clamp_ledger().by_stage(STAGE_V2_FORMATTER)["area"] == 1


# ── The stage axis ───────────────────────────────────────────────────────────


def test_by_stage_separates_the_two_clamps_while_by_field_sums_them() -> None:
    """The corrected premise has to stay checkable on a live run.

    "The formatter owns the whole attributable slice" is a measurement, not
    a claim in a commit message — a future run that contradicts it must be
    able to say so. ``by_field`` still answers the aggregate question.
    """
    with clamp_tier_context("TIER_4_LLM_DOM"):
        sanity_bound({"area": 45}, property_id="P1")          # sanity stage
        _format_area("0", record_as="area", property_id="P1")  # formatter stage

    led = clamp_ledger()
    assert led.by_stage(STAGE_SANITY_BOUND) == {"area": 1}
    assert led.by_stage(STAGE_V2_FORMATTER) == {"area": 1}
    # Aggregate view is unchanged for every pre-existing consumer.
    assert led.by_field("area") == {"TIER_4_LLM_DOM": 2}
    assert led.to_dict()["by_stage"] == {
        STAGE_SANITY_BOUND: 1,
        STAGE_V2_FORMATTER: 1,
    }


def test_formatter_records_are_strict_json() -> None:
    """The open rent bound must not land on disk as the ``Infinity`` literal."""
    _format_rent("$0", record_as="rent_low", property_id="P1")
    text = json.dumps(clamp_ledger().to_dict())
    assert "Infinity" not in text and "NaN" not in text


def test_unwrapped_emit_attributes_to_unknown_not_to_the_previous_property() -> None:
    """A leaked tier context would mis-attribute every following clamp.

    ``clamp_tier_context`` restores on exit, so a clamp fired outside any
    block is ``UNKNOWN`` — honest — rather than inheriting whichever
    property happened to run last.
    """
    with clamp_tier_context("TIER_1_API_SIGHTMAP"):
        _format_area("0", record_as="area")
    _format_area("0", record_as="area")
    assert clamp_ledger().by_field("area") == {"TIER_1_API_SIGHTMAP": 1, "UNKNOWN": 1}
