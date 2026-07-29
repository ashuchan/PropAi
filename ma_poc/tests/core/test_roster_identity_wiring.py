"""Task #73 — the guard must be CALLED, and it must only ANNOTATE.

``ma_poc/core/roster_identity.py`` shipped as two divergent copies with
``apply_roster_identity`` invoked from nowhere: correct detection, validated against 40
blind-verified properties, with no effect on a single emitted record. These tests fail if
that returns.

They also fail if the wiring is ever quietly strengthened. Annotation is not a weaker
first step towards demotion — it is the conclusion of the measurement recorded in the
module docstring (82.5% of flags contain the property's own city; 3 of the 6 flags
settleable by live probe were false or ambiguous). Dropping rows on this signal
manufactures an absence out of an inference.

Fixture rows are the verbatim AppFolio scattered-site shape from property 32716
(Porto Bella, Norwalk CA): the address lives in ``floor_plan_name``, every field is
correct, and the apartments belong to five other communities.
"""

from __future__ import annotations

import inspect
from typing import Any

from ma_poc.core.roster_identity import (
    ACTION_ANNOTATED,
    ACTION_KEY,
    EVIDENCE_KEY,
    QUARANTINE_KEY,
    QUARANTINE_PLANS_KEY,
    SCOPE_KEY,
    UNVERIFIED_VERDICT,
    apply_roster_identity,
)

_FOREIGN_ADDRESSES = [
    "2045 South Haster Street #M-1, Anaheim, CA 92802",
    "111 West Orangewood Avenue #N-1, Anaheim, CA 92802",
    "821 West Stevens Avenue #15, Santa Ana, CA 92707",
    "4945 Hayter Avenue, Lakewood, CA 90712",
    "6390 De Longpre Avenue #1708, Los Angeles, CA 90028",
    "2830 West Ball Road #Q-44, Anaheim, CA 92804",
]


def _foreign_rows() -> list[dict[str, Any]]:
    return [
        {
            "unit_id": f"u{i}",
            "unit_name": f"u{i}",
            "floor_plan_name": addr,
            "beds": 1,
            "baths": 1,
            "area": 750,
            "rent_low": 2500 + i,
            "rent_high": 2500 + i,
            "availability_status": "AVAILABLE",
            "is_floor_plan_level": False,
        }
        for i, addr in enumerate(_FOREIGN_ADDRESSES)
    ]


def _clean_rows(n: int = 12) -> list[dict[str, Any]]:
    return [
        {
            "unit_id": str(100 + i),
            "unit_name": str(100 + i),
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1,
            "area": 750,
            "rent_low": 1500,
            "rent_high": 1500,
            "availability_status": "AVAILABLE",
            "is_floor_plan_level": False,
        }
        for i in range(n)
    ]


def _record(rows: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "apartment_id": "32716",
        "proj_name": "Porto Bella",
        "units": rows,
        "_meta": {"verdict": "SUCCESS", "verdict_reason": "all checks passed"},
    }
    rec.update(kw)
    return rec


# ── the annotate-only contract ──────────────────────────────────────────────


class TestAnnotateOnly:
    def test_no_row_is_dropped_or_quarantined(self) -> None:
        rows = _foreign_rows()
        out, report = apply_roster_identity([_record(rows)], demote=False)
        assert len(out[0]["units"]) == len(rows)
        assert QUARANTINE_KEY not in out[0]
        assert QUARANTINE_PLANS_KEY not in out[0]
        assert report.demoted_property_ids == ()
        assert report.demoted_rows == 0

    def test_the_verdict_is_not_touched(self) -> None:
        """The cardinal rule: a heuristic does not overwrite an observed verdict."""
        out, _ = apply_roster_identity([_record(_foreign_rows())], demote=False)
        assert out[0]["_meta"]["verdict"] == "SUCCESS"
        assert out[0]["_meta"]["verdict_reason"] == "all checks passed"

    def test_the_evidence_is_written_and_says_it_annotated(self) -> None:
        out, report = apply_roster_identity([_record(_foreign_rows())], demote=False)
        ev = out[0]["_meta"][EVIDENCE_KEY]
        assert ev["signal"] == "ADDRESS_DISPERSION"
        assert ev[ACTION_KEY] == ACTION_ANNOTATED
        assert len(ev["locations"]) > 3
        assert report.annotated_property_ids == ("32716",)
        assert report.annotated_rows == len(_FOREIGN_ADDRESSES)

    def test_the_scope_tag_is_still_written(self) -> None:
        out, _ = apply_roster_identity([_record(_clean_rows())], demote=False)
        assert out[0][SCOPE_KEY] == "AVAILABLE_ONLY"

    def test_a_clean_record_gets_no_evidence(self) -> None:
        out, report = apply_roster_identity([_record(_clean_rows())], demote=False)
        assert EVIDENCE_KEY not in out[0]["_meta"]
        assert report.annotated_property_ids == ()
        assert out[0]["_meta"]["verdict"] == "SUCCESS"

    def test_signal_a_annotates_every_member_without_emptying_any(self) -> None:
        """A collision is the case where demotion would be most destructive: two records
        share a roster, at most one is wrong, and demoting takes both."""
        a = _record(_clean_rows(), apartment_id="62306", proj_name="Riverwalk I")
        b = _record(_clean_rows(), apartment_id="66980", proj_name="Riverwalk II")
        out, report = apply_roster_identity([a, b], demote=False)
        assert [len(p["units"]) for p in out] == [12, 12]
        assert sorted(report.annotated_property_ids) == ["62306", "66980"]
        for p in out:
            assert p["_meta"][EVIDENCE_KEY]["signal"] == "SHARED_ROSTER"
            assert p["_meta"]["verdict"] == "SUCCESS"

    def test_the_input_is_not_mutated(self) -> None:
        rec = _record(_foreign_rows())
        apply_roster_identity([rec], demote=False)
        assert EVIDENCE_KEY not in rec["_meta"]

    def test_demoting_mode_still_exists_for_the_library(self) -> None:
        """The capability is kept and tested; it is the WIRING that annotates."""
        out, report = apply_roster_identity([_record(_foreign_rows())], demote=True)
        assert out[0]["units"] == []
        assert len(out[0][QUARANTINE_KEY]) == len(_FOREIGN_ADDRESSES)
        assert out[0]["_meta"]["verdict"] == UNVERIFIED_VERDICT
        assert report.annotated_property_ids == ()


# ── the production wiring ───────────────────────────────────────────────────


class TestJugnuRunBoundary:
    """Source assertions rather than a full-run harness: the ORDER is the whole point."""

    @staticmethod
    def _src() -> str:
        from ma_poc.scripts.runners import jugnu

        return inspect.getsource(jugnu.run_jugnu)

    def test_apply_roster_identity_is_called_at_all(self) -> None:
        assert "apply_roster_identity(" in self._src(), (
            "the roster-identity guard has no production caller again — that is the "
            "state task #73 existed to end"
        )

    def test_it_runs_on_the_merged_list_and_before_the_write(self) -> None:
        src = self._src()
        merge_at = src.index("_merge_with_existing_properties(")
        guard_at = src.index("apply_roster_identity(")
        write_at = src.index("_write_properties_incremental(")
        assert merge_at < guard_at < write_at, (
            "apply_roster_identity must run on the MERGED list and BEFORE the write. "
            "Per shard, signal A flags 2 properties where cross-shard it flags 56; after "
            "the write, the evidence never reaches properties.json."
        )

    def test_the_production_call_annotates(self) -> None:
        src = self._src()
        call = src[src.index("apply_roster_identity(") :]
        call = call[: call.index(")") + 1]
        assert "demote=False" in call, (
            "the production call must pass demote=False. Dropping rows or demoting a "
            "verdict on this heuristic manufactures an absence out of an inference — see "
            "the measured precision in ma_poc/core/roster_identity.py before changing it."
        )

    def test_there_is_exactly_one_production_caller(self) -> None:
        """Two boundaries means two chances to disagree about what a flag does."""
        from pathlib import Path

        from ma_poc.core import roster_identity as ri

        root = Path(inspect.getfile(ri)).resolve().parents[1]
        callers = [
            p.as_posix()
            for p in sorted(root.rglob("*.py"))
            if "/tests/" not in p.as_posix()
            and not p.name.startswith("test_")
            and "apply_roster_identity(" in p.read_text()
            and p.name != "roster_identity.py"
        ]
        assert callers == [
            (root / "scripts" / "runners" / "jugnu.py").as_posix()
        ], f"expected exactly one production caller, found {callers}"
