"""Factory for constructing run directory structures in tmp_path.

The file-system layout for a run is::

    {base}/
    ├── runs/
    │   └── {date}/
    │       ├── properties.json
    │       ├── issues.jsonl
    │       └── report.json          (optional)
    ├── state/
    │   ├── property_index.json      (optional)
    │   └── unit_index.json          (optional)
    └── scrape_events.jsonl          (optional)

This layout is what FileSystemDataProvider expects. Tests that use the FS
provider should build their run directory via this factory so the layout
stays in sync with the production code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RunDirectoryFactory:
    """Builds realistic run directory trees under *tmp_path*.

    Obtain via the ``run_dir`` pytest fixture; do not instantiate directly
    in test code unless you need a non-default base.
    """

    def __init__(self, tmp_path: Path) -> None:
        self._base = tmp_path

    @property
    def base(self) -> Path:
        return self._base

    def build(
        self,
        date: str = "2026-05-10",
        *,
        properties: list[dict[str, Any]] | None = None,
        issues: list[dict[str, Any]] | None = None,
        report: dict[str, Any] | None = None,
        scrape_events: list[dict[str, Any]] | None = None,
    ) -> Path:
        """Create the on-disk layout for one run and return the run date dir.

        Args:
            date: Run date string, e.g. ``"2026-05-10"``.
            properties: List of property dicts written to ``properties.json``.
            issues: List of issue dicts written to ``issues.jsonl`` (one per line).
            report: Optional report dict written to ``report.json``.
            scrape_events: Optional list of events written to
                ``{base}/scrape_events.jsonl``.

        Returns:
            Path to the ``runs/{date}`` directory.

        Example::

            run_path = run_dir.build(
                date="2026-05-10",
                properties=[{"canonical_id": "prop-001", "verdict": "SUCCESS"}],
                issues=[{"level": "WARN", "msg": "low confidence"}],
            )
            # Pass run_dir.base as base_dir to FileSystemDataProvider
        """
        run_path = self._base / "runs" / date
        run_path.mkdir(parents=True, exist_ok=True)

        (run_path / "properties.json").write_text(
            json.dumps(properties or []), encoding="utf-8"
        )

        issues_lines = "\n".join(json.dumps(i) for i in (issues or []))
        (run_path / "issues.jsonl").write_text(issues_lines, encoding="utf-8")

        if report is not None:
            (run_path / "report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )

        if scrape_events is not None:
            events_path = self._base / "scrape_events.jsonl"
            events_lines = "\n".join(json.dumps(e) for e in scrape_events)
            events_path.write_text(events_lines, encoding="utf-8")

        # Ensure state/ dir exists so FileSystemDataProvider doesn't fail on
        # first-access reads.
        (self._base / "state").mkdir(exist_ok=True)

        return run_path

    def build_fs_provider_dirs(self) -> tuple[Path, Path]:
        """Create and return (base_dir, config_dir) ready for FileSystemDataProvider.

        config_dir holds profiles and properties.csv; base_dir holds runs/,
        state/, and scrape_events.jsonl.
        """
        config_dir = self._base / "config"
        (config_dir / "profiles").mkdir(parents=True, exist_ok=True)
        # Minimal empty CSV so the catalog source doesn't choke on first read.
        (config_dir / "properties.csv").write_text(
            "canonical_id,url\n", encoding="utf-8"
        )
        (self._base / "state").mkdir(exist_ok=True)
        (self._base / "runs").mkdir(exist_ok=True)
        return self._base, config_dir
