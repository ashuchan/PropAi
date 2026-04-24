"""Regression tests for deploy_csv_sync.py.

Covers:
  - Dual upload path (full list + canary).
  - Canary contains exactly CANARY_ROWS data rows plus the header.
  - CI env name ("prod") maps to the TF env name ("production") used in
    the bucket URI.
  - Validation tolerates the real CSV header (apartmentid,...).

No network — gsutil calls are captured via a subprocess.check_call patch.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from unittest.mock import patch

_here = Path(__file__).resolve().parent.parent.parent
for _p in (_here,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts import deploy_csv_sync  # noqa: E402


def _make_csv(tmp_path: Path, row_count: int = 10) -> Path:
    src = tmp_path / "props.csv"
    with src.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["apartmentid", "name", "address", "city", "state", "zip", "website"])
        for i in range(row_count):
            w.writerow([f"id-{i}", f"Name {i}", "1 Main St", "City", "CA", "90210", f"https://x/{i}"])
    return src


def test_upload_sends_both_full_and_canary(tmp_path: Path) -> None:
    """upload() must issue exactly two gsutil cp calls — full list + canary."""
    src = _make_csv(tmp_path, row_count=10)
    captured: list[tuple[Path, str]] = []

    def fake_check_call(cmd: list[str]) -> int:
        # gsutil cp <src> <dest>
        assert cmd[0] == "gsutil"
        src_p = Path(cmd[-2])
        dest = cmd[-1]
        # Snapshot the canary CSV before the script deletes it.
        captured.append((Path(src_p.read_text(encoding="utf-8")), dest))  # type: ignore[arg-type]
        return 0

    with patch("scripts.deploy_csv_sync.subprocess.check_call", side_effect=fake_check_call):
        deploy_csv_sync.upload(src, env="prod")

    assert len(captured) == 2
    dests = [dest for _, dest in captured]
    assert dests == [
        "gs://jugnu-raw-production/property-list/properties.csv",
        "gs://jugnu-raw-production/canary/properties.csv",
    ]


def test_canary_contains_exactly_n_data_rows(tmp_path: Path) -> None:
    """The canary file must have the header plus CANARY_ROWS data rows."""
    src = _make_csv(tmp_path, row_count=50)
    canary_bodies: list[str] = []

    def fake_check_call(cmd: list[str]) -> int:
        dest = cmd[-1]
        if dest.endswith("/canary/properties.csv"):
            canary_bodies.append(Path(cmd[-2]).read_text(encoding="utf-8"))
        return 0

    with patch("scripts.deploy_csv_sync.subprocess.check_call", side_effect=fake_check_call):
        deploy_csv_sync.upload(src, env="prod")

    assert len(canary_bodies) == 1
    lines = canary_bodies[0].strip().splitlines()
    # header + CANARY_ROWS data rows
    assert len(lines) == 1 + deploy_csv_sync.CANARY_ROWS
    assert lines[0].startswith("apartmentid,")
    # First data row preserves ordering from source
    assert lines[1].startswith("id-0,")


def test_canary_tolerates_source_smaller_than_canary_rows(tmp_path: Path) -> None:
    """If source has fewer rows than CANARY_ROWS, canary gets all of them — no crash."""
    src = _make_csv(tmp_path, row_count=2)
    canary_bodies: list[str] = []

    def fake_check_call(cmd: list[str]) -> int:
        if cmd[-1].endswith("/canary/properties.csv"):
            canary_bodies.append(Path(cmd[-2]).read_text(encoding="utf-8"))
        return 0

    with patch("scripts.deploy_csv_sync.subprocess.check_call", side_effect=fake_check_call):
        deploy_csv_sync.upload(src, env="prod")

    lines = canary_bodies[0].strip().splitlines()
    assert len(lines) == 1 + 2  # header + 2 data rows


def test_staging_maps_to_staging_tf_env(tmp_path: Path) -> None:
    src = _make_csv(tmp_path)
    dests: list[str] = []

    def fake_check_call(cmd: list[str]) -> int:
        dests.append(cmd[-1])
        return 0

    with patch("scripts.deploy_csv_sync.subprocess.check_call", side_effect=fake_check_call):
        deploy_csv_sync.upload(src, env="staging")

    assert all("jugnu-raw-staging" in d for d in dests), dests


def test_canary_cleanup_removes_tempfile_even_on_failure(tmp_path: Path) -> None:
    """A failed canary upload must still clean up the temp file."""
    src = _make_csv(tmp_path)
    created_tempfiles: list[Path] = []

    def fake_check_call(cmd: list[str]) -> int:
        p = Path(cmd[-2])
        created_tempfiles.append(p)
        if cmd[-1].endswith("/canary/properties.csv"):
            raise RuntimeError("simulated gsutil failure on canary")
        return 0

    with patch("scripts.deploy_csv_sync.subprocess.check_call", side_effect=fake_check_call):
        try:
            deploy_csv_sync.upload(src, env="prod")
        except RuntimeError:
            pass

    canary_temp = next(
        (p for p in created_tempfiles if p.name.endswith(".csv") and p != src), None
    )
    assert canary_temp is not None
    assert not canary_temp.exists(), f"leftover canary tempfile: {canary_temp}"
