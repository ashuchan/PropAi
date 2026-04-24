"""Unit tests for scripts/trigger_smoke.py.

Covers the pure-logic pieces (fixture generation, log grouping,
per-shard count assertions, argparse gates) without touching gcloud or
GCS. End-to-end behaviour is validated by running the script in the
reusable-deploy.yml ``smoke`` step against a real Cloud Run job.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Path bootstrap
_here = Path(__file__).resolve().parent.parent.parent
_app = _here.parent
for _p in (_app, _here):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts import trigger_smoke as mod  # noqa: E402


# ── Fixture generation ─────────────────────────────────────────────────────


class TestBuildSmokeCsv:
    def test_produces_six_rows_across_three_shards(self) -> None:
        body, cids = mod._build_smoke_csv("s-20260424120000")
        assert len(cids) == mod.TOTAL_PROPS == 6
        reader = csv.reader(io.StringIO(body.decode("utf-8")))
        rows = list(reader)
        # header + 6 data rows
        assert len(rows) == 7
        assert rows[0] == ["apartmentid", "name", "city", "state", "zip", "website"]

    def test_cids_are_stable_per_run_date(self) -> None:
        """Two calls with the same run_date produce the same cids so
        cleanup can reliably target the rows the execute step created."""
        _, cids1 = mod._build_smoke_csv("s-20260424120000")
        _, cids2 = mod._build_smoke_csv("s-20260424120000")
        assert cids1 == cids2

    def test_cids_differ_across_run_dates(self) -> None:
        """Two smoke runs at different timestamps must not collide in
        the ``properties`` table (which has no run_date scope)."""
        _, cids_a = mod._build_smoke_csv("s-20260424120000")
        _, cids_b = mod._build_smoke_csv("s-20260425010000")
        assert set(cids_a).isdisjoint(set(cids_b))

    def test_urls_use_invalid_tld(self) -> None:
        """RFC 2606 .invalid — guarantees DNS NXDOMAIN so the scrape
        fails fast without hitting real hosts or burning LLM credits."""
        body, _ = mod._build_smoke_csv("s-20260424120000")
        reader = csv.reader(io.StringIO(body.decode("utf-8")))
        next(reader)  # skip header
        for row in reader:
            url = row[5]
            assert url.endswith(".invalid/"), f"URL {url!r} doesn't end with .invalid/"

    def test_even_shard_distribution(self) -> None:
        """Each shard gets exactly PROPS_PER_SHARD cids — the core
        premise of the multi-shard test."""
        import re

        _, cids = mod._build_smoke_csv("s-20260424120000")
        shard_re = re.compile(r"-S(\d+)-P\d+$")
        by_shard: dict[int, int] = {}
        for cid in cids:
            m = shard_re.search(cid)
            assert m is not None, f"cid {cid!r} doesn't match expected shape"
            by_shard[int(m.group(1))] = by_shard.get(int(m.group(1)), 0) + 1
        assert set(by_shard.keys()) == {0, 1, 2}
        assert all(v == mod.PROPS_PER_SHARD for v in by_shard.values())


# ── Log parser ─────────────────────────────────────────────────────────────


class TestLooseParseDict:
    def test_parses_repr_dict(self) -> None:
        text = (
            "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
            "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}"
        )
        assert mod._loose_parse_dict(text) == {
            "state_from_snapshots": 2,
            "ledger_from_snapshots": 2,
            "scrape_events_from_snapshots": 2,
            "extractions_from_snapshots": 2,
        }

    def test_returns_empty_on_garbage(self) -> None:
        """Malformed log lines must not crash the smoke test — degrade
        to "couldn't parse" rather than propagate the SyntaxError up."""
        assert mod._loose_parse_dict("not a dict") == {}
        assert mod._loose_parse_dict("") == {}
        assert mod._loose_parse_dict("{'bad': syntax error}") == {}


class TestVerifyViaLogs:
    """Structured-log parser that groups by task_index — the critical
    bit that handles Cloud Run's task retries correctly.

    ``gcloud logging read --format=json`` returns entries like::

        {"textPayload": "[shard_entry] PG sync complete: {…}",
         "labels": {"run.googleapis.com/task_index": "0", …}}

    With ``max_retries=1`` + invalid-URL fixtures, each task_index may
    appear twice (initial + retry). Grouping prevents 6 lines from
    being mistaken for 6 distinct shards having synced.
    """

    @staticmethod
    def _entry(task_idx: str, summary_text: str) -> dict:
        return {
            "textPayload": f"[shard_entry] PG sync complete: {summary_text}",
            "labels": {"run.googleapis.com/task_index": task_idx},
        }

    def _mock_gcloud_returning(self, entries: list[dict]) -> object:
        import json as _json

        class FakeResult:
            returncode = 0
            stdout = _json.dumps(entries)
            stderr = ""

        return FakeResult()

    def test_happy_path_three_shards(self) -> None:
        entries = [
            self._entry(
                str(i),
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            )
            for i in range(3)
        ]
        with patch.object(
            mod, "run_gcloud", return_value=self._mock_gcloud_returning(entries)
        ):
            ok, summaries = mod._verify_via_logs(
                execution_name="jugnu-scrape-production-abcde",
                project="jugnu-494013",
            )
        assert ok is True
        assert set(summaries.keys()) == {"0", "1", "2"}
        assert all(s["state_from_snapshots"] == 2 for s in summaries.values())

    def test_retry_entries_are_deduped_by_task_index(self) -> None:
        """Cloud Run retries each failed task once (max_retries=1).
        Six log lines — two per task_index — must still report 3
        distinct shards synced, not 6.
        """
        entries: list[dict] = []
        for task_idx in ("0", "1", "2"):
            # Two "PG sync complete" entries per task — initial + retry.
            for _ in range(2):
                entries.append(
                    self._entry(
                        task_idx,
                        "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                        "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
                    )
                )
        with patch.object(
            mod, "run_gcloud", return_value=self._mock_gcloud_returning(entries)
        ):
            ok, summaries = mod._verify_via_logs(
                execution_name="exec", project="proj"
            )
        assert ok is True
        assert set(summaries.keys()) == {"0", "1", "2"}
        # Six raw log lines grouped down to 3 distinct shards.
        assert len(summaries) == 3

    def test_missing_shard_fails_the_check(self) -> None:
        """Only task_index 0 and 1 emitted PG sync complete — shard 2
        never synced. Must return ok=False."""
        entries = [
            self._entry(
                "0",
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            ),
            self._entry(
                "1",
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            ),
        ]
        with patch.object(
            mod, "run_gcloud", return_value=self._mock_gcloud_returning(entries)
        ):
            ok, summaries = mod._verify_via_logs(
                execution_name="exec", project="proj"
            )
        assert ok is False
        assert set(summaries.keys()) == {"0", "1"}

    def test_entries_without_task_index_label_are_skipped(self) -> None:
        """Log entries missing the task_index label (shouldn't happen
        on Cloud Run, but defensive) are silently skipped rather than
        crashing the parser."""
        entries = [
            {"textPayload": "[shard_entry] PG sync complete: {}", "labels": {}},
            self._entry(
                "0",
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            ),
            self._entry(
                "1",
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            ),
            self._entry(
                "2",
                "{'state_from_snapshots': 2, 'ledger_from_snapshots': 2, "
                "'scrape_events_from_snapshots': 2, 'extractions_from_snapshots': 2}",
            ),
        ]
        with patch.object(
            mod, "run_gcloud", return_value=self._mock_gcloud_returning(entries)
        ):
            ok, summaries = mod._verify_via_logs(
                execution_name="exec", project="proj"
            )
        assert ok is True
        assert set(summaries.keys()) == {"0", "1", "2"}

    def test_gcloud_failure_returns_false(self) -> None:
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "permission denied"

        with patch.object(mod, "run_gcloud", return_value=FakeResult()):
            ok, summaries = mod._verify_via_logs(
                execution_name="exec", project="proj"
            )
        assert ok is False
        assert summaries == {}

    def test_garbled_json_returns_false(self) -> None:
        class FakeResult:
            returncode = 0
            stdout = "not json"
            stderr = ""

        with patch.object(mod, "run_gcloud", return_value=FakeResult()):
            ok, summaries = mod._verify_via_logs(
                execution_name="exec", project="proj"
            )
        assert ok is False
        assert summaries == {}


class TestPerShardCountsMatch:
    def test_all_correct(self) -> None:
        summaries = {
            str(i): {
                "state_from_snapshots": 2,
                "ledger_from_snapshots": 2,
                "scrape_events_from_snapshots": 2,
                "extractions_from_snapshots": 2,
            }
            for i in range(3)
        }
        assert mod._per_shard_counts_match(summaries) is True

    def test_one_shard_has_wrong_count_fails(self) -> None:
        """If shard 2 synced only 1 row (not 2), the smoke test must fail.

        Regression for: shard reads fewer properties than expected
        because the CSV slicer rounded differently, or because the sync
        silently dropped a record with missing canonical_id.
        """
        summaries = {
            "0": {
                "state_from_snapshots": 2,
                "ledger_from_snapshots": 2,
                "scrape_events_from_snapshots": 2,
                "extractions_from_snapshots": 2,
            },
            "1": {
                "state_from_snapshots": 2,
                "ledger_from_snapshots": 2,
                "scrape_events_from_snapshots": 2,
                "extractions_from_snapshots": 2,
            },
            "2": {
                "state_from_snapshots": 1,  # only 1, should be 2
                "ledger_from_snapshots": 1,
                "scrape_events_from_snapshots": 1,
                "extractions_from_snapshots": 1,
            },
        }
        assert mod._per_shard_counts_match(summaries) is False

    def test_missing_counter_keys_fail(self) -> None:
        """A summary that's missing one of the four derive-from-snapshots
        keys (say the sync_run_to_pg.py code-path regressed and stopped
        populating scrape_events) must fail the check."""
        summaries = {
            "0": {
                "state_from_snapshots": 2,
                "ledger_from_snapshots": 2,
                # scrape_events_from_snapshots MISSING
                "extractions_from_snapshots": 2,
            },
        }
        assert mod._per_shard_counts_match(summaries) is False


# ── CLI arg handling ───────────────────────────────────────────────────────


class TestCleanupOnly:
    def test_requires_run_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["trigger_smoke.py", "--env", "staging", "--cleanup-only"],
        )
        with (
            patch.object(mod, "verify_gcloud_auth"),
            patch.object(mod, "project_for_env", return_value="test"),
        ):
            rc = mod.main()
        assert rc == 2
