"""Dead-letter queue — parks properties that fail repeatedly.

JSONL file at data/state/dlq.jsonl. Append-only, compacted nightly.
Parking rule: consecutive_unreachable >= 3 (not just any failure).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DlqEntry:
    """A parked property in the dead-letter queue."""

    property_id: str
    parked_at: str  # ISO timestamp
    reason: str
    last_error_signature: str
    retry_at: str  # ISO timestamp — next scheduled retry
    unparked: bool = False


class Dlq:
    """Dead-letter queue for properties that fail repeatedly.

    Args:
        path: Path to the JSONL file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, DlqEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load entries from the JSONL file."""
        if not self._path.exists():
            return
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = DlqEntry(**{k: v for k, v in data.items() if k in DlqEntry.__dataclass_fields__})
                    if entry.unparked:
                        self._entries.pop(entry.property_id, None)
                    else:
                        self._entries[entry.property_id] = entry
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as exc:
            log.warning("Failed to load DLQ: %s", exc)

    def _append(self, entry: DlqEntry) -> None:
        """Append an entry to the JSONL file."""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    def park(self, property_id: str, reason: str, err_sig: str) -> None:
        """Park a property with hourly retries for 6h, then daily.

        Args:
            property_id: The property to park.
            reason: Machine-readable reason code.
            err_sig: Last error signature.
        """
        now = datetime.now(UTC)
        retry_at = now + timedelta(hours=1)
        entry = DlqEntry(
            property_id=property_id,
            parked_at=now.isoformat(),
            reason=reason,
            last_error_signature=err_sig,
            retry_at=retry_at.isoformat(),
        )
        self._entries[property_id] = entry
        self._append(entry)
        log.info("Parked property %s: %s", property_id, reason)

    def is_parked(self, property_id: str) -> bool:
        """Check if a property is currently parked.

        Args:
            property_id: The property to check.

        Returns:
            True if parked.
        """
        return property_id in self._entries

    def due_for_retry(self, now: datetime | None = None) -> list[DlqEntry]:
        """Get entries whose retry window has arrived.

        Args:
            now: Current time (default: UTC now).

        Returns:
            List of entries due for retry.
        """
        if now is None:
            now = datetime.now(UTC)
        due: list[DlqEntry] = []
        for entry in self._entries.values():
            try:
                retry_dt = datetime.fromisoformat(entry.retry_at)
                if retry_dt <= now:
                    due.append(entry)
            except ValueError:
                continue
        return due

    def unpark(self, property_id: str) -> None:
        """Remove a property from the DLQ.

        Args:
            property_id: The property to unpark.
        """
        if property_id in self._entries:
            entry = DlqEntry(
                property_id=property_id,
                parked_at=self._entries[property_id].parked_at,
                reason="unparked",
                last_error_signature="",
                retry_at="",
                unparked=True,
            )
            del self._entries[property_id]
            self._append(entry)
            log.info("Unparked property %s", property_id)

    def reschedule(self, property_id: str) -> None:
        """Reschedule a parked property for later retry.

        Hourly for first 6h, then daily.
        """
        if property_id not in self._entries:
            return
        old = self._entries[property_id]
        parked_at = datetime.fromisoformat(old.parked_at)
        now = datetime.now(UTC)
        hours_parked = (now - parked_at).total_seconds() / 3600

        if hours_parked < 6:
            next_retry = now + timedelta(hours=1)
        else:
            next_retry = now + timedelta(days=1)

        new_entry = DlqEntry(
            property_id=property_id,
            parked_at=old.parked_at,
            reason=old.reason,
            last_error_signature=old.last_error_signature,
            retry_at=next_retry.isoformat(),
        )
        self._entries[property_id] = new_entry
        self._append(new_entry)

    def compact(self) -> None:
        """Collapse multi-line-per-property to latest entry only."""
        if not self._entries:
            self._path.write_text("", encoding="utf-8")
            return
        lines = [json.dumps(asdict(e), default=str) for e in self._entries.values()]
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Compacted DLQ: %d entries", len(self._entries))
