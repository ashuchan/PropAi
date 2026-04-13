"""
Append-only JSONL event logger for ScrapeEvents + per-property extraction output writer.

Acceptance criteria (CLAUDE.md PR-01):
- One ScrapeEvent per scrape appended to data/scrape_events.jsonl
- Concurrent appends safe (asyncio.Lock guards file handle)
- Always serialize via model.model_dump(mode="json") — never .dict()
- Per-property extraction output written to data/extraction_output/{property_id}/{date}.json
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiofiles

from models.scrape_event import ScrapeEvent


class EventLog:
    """Async append-only JSONL writer with a single lock to prevent torn lines."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def append(self, event: ScrapeEvent) -> None:
        """Append a ScrapeEvent as a single JSON line. Thread-safe across coroutines."""
        line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        async with self._lock:
            async with aiofiles.open(self.path, mode="a", encoding="utf-8") as f:
                await f.write(line + "\n")


async def write_extraction_output(
    output_dir: Path | str,
    property_id: str,
    scrape_date: date,
    payload: dict[str, Any],
) -> Path:
    """
    Write per-property extraction output to data/extraction_output/{property_id}/{date}.json.

    payload is a dict already serialized via model_dump(mode="json"); this function
    only writes it. Returns the path written.
    """
    out_dir = Path(output_dir) / property_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scrape_date.isoformat()}.json"
    body = json.dumps(payload, separators=(",", ":"), default=_json_default)
    async with aiofiles.open(out_path, mode="w", encoding="utf-8") as f:
        await f.write(body)
    return out_path


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
