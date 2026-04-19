"""Event ledger — append-only JSONL writer with buffered flushing.

Crash-safe: at most buffer_size events lost on crash.
Line-buffered at Python level; append mode on open.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .events import Event

log = logging.getLogger(__name__)


class EventLedger:
    """Buffered JSONL event writer.

    Args:
        path: Path to the events.jsonl file.
        run_id: Run-level correlation ID.
        buffer_size: Events buffered before auto-flush.
    """

    def __init__(
        self, path: Path, run_id: str, buffer_size: int = 16
    ) -> None:
        self._path = path
        self._run_id = run_id
        self._buffer_size = buffer_size
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._closed = False
        self._disk_error_logged = False
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> None:
        """Buffer an event. Flushes when buffer is full.

        Args:
            event: The event to record. Never raises.
        """
        if self._closed:
            return
        try:
            line = event.to_jsonl()
            with self._lock:
                self._buffer.append(line)
                if len(self._buffer) >= self._buffer_size:
                    self._flush_locked()
        except Exception:
            if not self._disk_error_logged:
                log.warning("Event ledger write failed", exc_info=True)
                self._disk_error_logged = True

    def flush(self) -> None:
        """Flush the buffer to disk."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Flush buffer to disk. Must be called with lock held."""
        if not self._buffer:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for line in self._buffer:
                    f.write(line + "\n")
            self._buffer.clear()
        except Exception:
            if not self._disk_error_logged:
                log.warning("Event ledger flush failed", exc_info=True)
                self._disk_error_logged = True

    def close(self) -> None:
        """Flush and close. Called at run end."""
        self.flush()
        self._closed = True
        log.info("Event ledger closed: %s", self._path)

    def read_all(self) -> list[dict[str, Any]]:
        """Read all events from the ledger file.

        Returns:
            List of event dicts. Skips malformed lines.
        """
        self.flush()
        events: list[dict[str, Any]] = []
        if not self._path.exists():
            return events
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
