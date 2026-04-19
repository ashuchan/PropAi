"""Replay store — load raw HTML + events for a (property_id, date) pair.

Enables reproduction of parser failures from stored artifacts.
"""
from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ReplayPayload:
    """Reconstructed scrape artifacts for replay."""

    property_id: str
    date: str
    raw_html: bytes | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    extract_result: dict[str, Any] | None = None


class ReplayStore:
    """Looks up stored raw HTML and events for replay.

    Args:
        runs_root: Path to data/runs/ directory.
        raw_html_root: Path to data/raw_html/ directory.
    """

    def __init__(self, runs_root: Path, raw_html_root: Path) -> None:
        self._runs_root = runs_root
        self._raw_html_root = raw_html_root

    def load(self, property_id: str, date: str) -> ReplayPayload:
        """Load replay data for a property on a given date.

        Args:
            property_id: Canonical property ID.
            date: Date string (YYYY-MM-DD).

        Returns:
            ReplayPayload with available data.
        """
        payload = ReplayPayload(property_id=property_id, date=date)

        # Load raw HTML
        html_path = self._raw_html_root / date / f"{property_id}.html.gz"
        if html_path.exists():
            try:
                payload.raw_html = gzip.decompress(html_path.read_bytes())
            except Exception as exc:
                log.warning("Failed to decompress %s: %s", html_path, exc)

        # Load events
        events_path = self._runs_root / date / "events.jsonl"
        if events_path.exists():
            try:
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if event.get("property_id") == property_id:
                            payload.events.append(event)
                    except json.JSONDecodeError:
                        continue
            except Exception as exc:
                log.warning("Failed to read events: %s", exc)

        # Reconstruct extract result from events
        for event in payload.events:
            if event.get("kind") == "extract.tier_won":
                payload.extract_result = event

        return payload

    def list_available_dates(self, property_id: str) -> list[str]:
        """List dates where raw HTML exists for this property.

        Args:
            property_id: Canonical property ID.

        Returns:
            Sorted list of date strings.
        """
        dates: list[str] = []
        if not self._raw_html_root.exists():
            return dates
        for date_dir in sorted(self._raw_html_root.iterdir()):
            if not date_dir.is_dir():
                continue
            html_path = date_dir / f"{property_id}.html.gz"
            if html_path.exists():
                dates.append(date_dir.name)
        return dates
