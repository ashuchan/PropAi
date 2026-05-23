"""Tests for the T2 DQ raw-signal telemetry sweep in jugnu._emit_t2_dq_telemetry.

Each test pins one defect class and confirms that:
  (a) the right EventKind fires when the defect is present,
  (b) the right EventKind does NOT fire when the defect is absent,
  (c) sampling caps each event to ≤1 per property per defect class,
  (d) the emit is never-raises (broken input doesn't crash the run).

See docs/dom_quality_and_llm_reduction_playbook.md §T2 for the cohort
design + canonical PIDs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.observability.events import EventKind
from ma_poc.scripts.runners.jugnu import _emit_t2_dq_telemetry


def _capture_emits(prop: dict[str, Any], pid: str = "TEST_PID") -> list[tuple[EventKind, dict]]:
    """Run the sweep with the emit() helper patched to record calls.

    Returns ``[(EventKind, payload_kwargs), …]`` in emit order.
    """
    captured: list[tuple[EventKind, dict]] = []

    def _fake_emit(kind: EventKind, _property_id: str, **payload: Any) -> None:
        captured.append((kind, payload))

    with patch("ma_poc.observability.events.emit", side_effect=_fake_emit):
        _emit_t2_dq_telemetry(prop, pid)
    return captured


# ── T2.D — SAME_RENT_PROPERTY_OBSERVED ────────────────────────────────────────


def test_same_rent_fires_when_3_units_share_rent_no_real_uids() -> None:
    """The canonical concession-leak shape: 3+ units at one rent value,
    all with inferred (or None) unit_ids. PID 232870 sorrento on the
    2026-05-22 cloud run shipped 6 such rows at $250."""
    prop = {
        "units": [
            {"rent_low": 250, "unit_id": "inferred_aaa", "floor_plan_name": "Studio"},
            {"rent_low": 250, "unit_id": "inferred_bbb", "floor_plan_name": "Studio"},
            {"rent_low": 250, "unit_id": None,          "floor_plan_name": "Studio"},
        ],
        "concessions": None,
    }
    events = _capture_emits(prop)
    same_rent = [(k, p) for k, p in events if k == EventKind.SAME_RENT_PROPERTY_OBSERVED]
    assert len(same_rent) == 1
    _, payload = same_rent[0]
    assert payload["rent_value"] == 250.0
    assert payload["n_same_rent_units"] == 3
    assert payload["has_real_unit_ids"] is False
    assert "Studio" in payload["fpn_set"]


def test_same_rent_does_not_fire_when_real_unit_ids_present() -> None:
    """PID 282648 apolloridge sentinel — 3 units at $720 with REAL unit_ids
    ("4518-C"/"4516-G"/"4518-L"). This is legitimate uniform pricing, not
    a concession leak. Same-rent event must NOT fire."""
    prop = {
        "units": [
            {"rent_low": 720, "unit_id": "4518-C", "floor_plan_name": "Apt"},
            {"rent_low": 720, "unit_id": "4516-G", "floor_plan_name": "Apt"},
            {"rent_low": 720, "unit_id": "4518-L", "floor_plan_name": "Apt"},
        ],
    }
    events = _capture_emits(prop)
    assert not any(k == EventKind.SAME_RENT_PROPERTY_OBSERVED for k, _ in events)


def test_same_rent_does_not_fire_below_3_units() -> None:
    """Two units at the same rent isn't enough signal — could be legitimate
    twin units. Threshold is ≥3."""
    prop = {
        "units": [
            {"rent_low": 250, "unit_id": "inferred_a", "floor_plan_name": "A"},
            {"rent_low": 250, "unit_id": "inferred_b", "floor_plan_name": "A"},
        ],
    }
    events = _capture_emits(prop)
    assert not any(k == EventKind.SAME_RENT_PROPERTY_OBSERVED for k, _ in events)


# ── T2.F — CONCESSION_TO_RENT_LEAK ────────────────────────────────────────────


def test_concession_to_rent_leak_fires_when_rent_in_concession_text() -> None:
    """PID 11727 risebedfordlake — LLM emitted ``concession_text="from $675"``
    AND ``market_rent_low=675`` on every plan. Direct hallucination."""
    prop = {
        "units": [
            {"rent_low": 675, "unit_id": "inferred_a", "floor_plan_name": "A1"},
            {"rent_low": 675, "unit_id": "inferred_b", "floor_plan_name": "A2"},
            {"rent_low": 675, "unit_id": "inferred_c", "floor_plan_name": "A3"},
        ],
        "concessions": "Move-in special: from $675 first month rent!",
    }
    events = _capture_emits(prop)
    leak = [(k, p) for k, p in events if k == EventKind.CONCESSION_TO_RENT_LEAK]
    assert len(leak) == 1, "must fire exactly once per property"
    _, payload = leak[0]
    assert payload["rent_value"] == 675
    assert "$675" in payload["concession_excerpt"]


def test_concession_to_rent_leak_does_not_fire_when_rent_not_in_concession() -> None:
    """A legitimate property: $1,500 rent + "Pet rent $35/month" concession.
    The rent doesn't appear in the concession text — no leak."""
    prop = {
        "units": [
            {"rent_low": 1500, "unit_id": "U101", "floor_plan_name": "A1"},
        ],
        "concessions": "Pet rent $35/month, app fee waived",
    }
    events = _capture_emits(prop)
    assert not any(k == EventKind.CONCESSION_TO_RENT_LEAK for k, _ in events)


def test_concession_to_rent_leak_no_concession_text() -> None:
    """No concession text → no signal to compare against → no emit."""
    prop = {
        "units": [{"rent_low": 250, "unit_id": "inferred_a", "floor_plan_name": "A"}],
        "concessions": None,
    }
    events = _capture_emits(prop)
    assert not any(k == EventKind.CONCESSION_TO_RENT_LEAK for k, _ in events)


# ── T2.E — UNIT_ID_EQUALS_PLAN_NAME ───────────────────────────────────────────


def test_uid_equals_fpn_fires_case_insensitive() -> None:
    """PID 229986 theadleylife — unit_ids A1/A2/A3 == floor_plan_names A1/A2/A3.
    Plan code masquerading as unit identity."""
    prop = {
        "units": [
            {"unit_id": "A1",  "floor_plan_name": "A1",  "rent_low": 1500},
            {"unit_id": "A2",  "floor_plan_name": "A2",  "rent_low": 1600},
            {"unit_id": "scH1","floor_plan_name": "SCH1","rent_low": 1800},
        ],
    }
    events = _capture_emits(prop)
    leak = [(k, p) for k, p in events if k == EventKind.UNIT_ID_EQUALS_PLAN_NAME]
    assert len(leak) == 1, "must fire once per property even when N units affected"
    _, payload = leak[0]
    assert payload["unit_id"] == "A1"
    assert payload["floor_plan_name"] == "A1"


def test_uid_equals_fpn_does_not_fire_when_uid_distinct() -> None:
    """Normal case: unit_id is per-apartment, floor_plan_name is the plan."""
    prop = {
        "units": [
            {"unit_id": "101", "floor_plan_name": "A1", "rent_low": 1500},
            {"unit_id": "102", "floor_plan_name": "A1", "rent_low": 1500},
        ],
    }
    events = _capture_emits(prop)
    assert not any(k == EventKind.UNIT_ID_EQUALS_PLAN_NAME for k, _ in events)


# ── T2.B — FLOOR_PLAN_NAME_LONG ───────────────────────────────────────────────


def test_fpn_long_fires_on_concatenated_names() -> None:
    """LLM concatenated plan+property+community: "A3 - Wellesley - Lenox
    Village & Regent". Length 36+ AND ≥2 ` - ` separators."""
    prop = {
        "units": [
            {"floor_plan_name": "A3 - Wellesley - Lenox Village & Regent", "rent_low": 1500},
        ],
    }
    events = _capture_emits(prop)
    leak = [(k, p) for k, p in events if k == EventKind.FLOOR_PLAN_NAME_LONG]
    assert len(leak) == 1
    assert leak[0][1]["floor_plan_name"].startswith("A3 - Wellesley")


def test_fpn_long_does_not_fire_on_short_or_single_separator_names() -> None:
    """Legitimate plan names with one ` - ` separator must pass: "Garden View
    - Upstairs" is not junk. Threshold is ≥2 separators AND len > 35."""
    cases = [
        "A1",                                 # short
        "Garden View - Upstairs",             # one " - " only
        "Two Bedroom Premium Renovated",      # long but no " - "
    ]
    for fpn in cases:
        prop = {"units": [{"floor_plan_name": fpn, "rent_low": 1500}]}
        events = _capture_emits(prop)
        assert not any(k == EventKind.FLOOR_PLAN_NAME_LONG for k, _ in events), (
            f"unexpected fire on {fpn!r}"
        )


# ── T2.C — BEDS_ZERO_NON_STUDIO ───────────────────────────────────────────────


def test_beds_zero_non_studio_fires() -> None:
    """LLM defaulted beds=0 under uncertainty. fpn doesn't have studio/
    efficiency/sro/loft → almost certainly a 1BR/2BR mis-counted as 0."""
    prop = {
        "units": [
            {"beds": 0, "floor_plan_name": "Canterbury", "unit_id": "U1", "rent_low": 1500},
        ],
    }
    events = _capture_emits(prop)
    leak = [(k, p) for k, p in events if k == EventKind.BEDS_ZERO_NON_STUDIO]
    assert len(leak) == 1
    assert leak[0][1]["floor_plan_name"] == "Canterbury"


def test_beds_zero_studio_name_does_not_fire() -> None:
    """beds=0 + "Studio" / "Efficiency" / "SRO" / "Loft" in name → genuinely a
    studio, no defect signal."""
    for fpn in ("Studio", "The Studio One", "Efficiency", "Loft 12", "SRO Unit"):
        prop = {"units": [{"beds": 0, "floor_plan_name": fpn, "rent_low": 1500}]}
        events = _capture_emits(prop)
        assert not any(k == EventKind.BEDS_ZERO_NON_STUDIO for k, _ in events), (
            f"unexpected fire on studio-named row {fpn!r}"
        )


def test_beds_zero_non_studio_samples_once_per_property() -> None:
    """A 200-unit property with 100 beds=0/non-studio rows must emit ONE
    event, not 100. Sampling cap prevents ledger floods."""
    units = [{"beds": 0, "floor_plan_name": f"P{i}", "unit_id": f"U{i}",
              "rent_low": 1500} for i in range(50)]
    events = _capture_emits({"units": units})
    leak = [(k, p) for k, p in events if k == EventKind.BEDS_ZERO_NON_STUDIO]
    assert len(leak) == 1


# ── Sweep-level invariants ────────────────────────────────────────────────────


def test_empty_property_emits_nothing() -> None:
    """A property with no units → no telemetry to emit. Must not raise."""
    assert _capture_emits({"units": []}) == []
    assert _capture_emits({}) == []
    assert _capture_emits({"units": None}) == []


def test_malformed_units_never_raise() -> None:
    """L5 observability contract: emit() never raises. The sweep wraps each
    defect class in try/except — malformed unit dicts must not crash."""
    weird = {
        "units": [
            None,
            "string-not-a-dict",
            {"unit_id": object()},                                   # un-stringable
            {"rent_low": "not-a-number"},
            {"floor_plan_name": 12345},                              # int fpn
            {"beds": "studio"},                                      # string instead of int
        ],
    }
    # If this raises, the test fails. Output content is not asserted —
    # the only contract is "doesn't crash".
    _capture_emits(weird)
