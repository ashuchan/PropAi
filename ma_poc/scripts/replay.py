"""
Jugnu J5 — Replay tool.

Reconstructs a property's scrape from stored raw HTML + events.

Usage:
    python scripts/replay.py --cid 5317 --date 2026-04-17
    python scripts/replay.py --cid 5317 --date 2026-04-17 --rerun
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("replay")

_MA_POC_ROOT = Path(__file__).resolve().parent.parent  # ma_poc/


def _schema_data_root(data_dir: Path) -> Path:
    """Return data/v2/ or data/ depending on SCHEMA_VERSION env var."""
    version = os.getenv("SCHEMA_VERSION", "v1").strip().lower()
    return data_dir / "v2" if version == "v2" else data_dir


def main() -> int:
    """Entry point for the replay CLI tool."""
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Jugnu replay tool")
    parser.add_argument("--cid", required=True, help="Canonical property ID")
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument("--rerun", action="store_true", help="Re-run extractor on raw HTML")
    parser.add_argument("--out", type=Path, default=None, help="Output markdown path")
    _data_root = _schema_data_root(_MA_POC_ROOT / "data")
    parser.add_argument("--runs-root", type=Path, default=_data_root / "runs")
    parser.add_argument("--html-root", type=Path, default=_data_root / "raw_html")
    args = parser.parse_args()

    from ma_poc.observability.replay_store import ReplayStore

    store = ReplayStore(args.runs_root, args.html_root)
    payload = store.load(args.cid, args.date)

    if not payload.raw_html and not payload.events:
        log.error("No data found for cid=%s date=%s", args.cid, args.date)
        return 1

    # Build timeline
    lines: list[str] = [
        f"# Replay: {args.cid} on {args.date}",
        "",
        f"## Raw HTML: {'available' if payload.raw_html else 'not found'}",
        f"HTML size: {len(payload.raw_html)} bytes" if payload.raw_html else "",
        "",
        f"## Events ({len(payload.events)} total)",
        "",
    ]
    for event in payload.events:
        ts = event.get("ts", "?")
        kind = event.get("kind", "?")
        lines.append(f"- `{ts}` **{kind}** {json.dumps({k: v for k, v in event.items() if k not in ('ts', 'kind', 'event_id', 'run_id', 'property_id')}, default=str)}")

    if payload.extract_result:
        lines.extend([
            "",
            "## Extract result",
            f"```json",
            json.dumps(payload.extract_result, indent=2, default=str),
            "```",
        ])

    lines.append("")
    report = "\n".join(lines)

    # Output
    out_path = args.out or Path(f"replay_{args.cid}_{args.date}.md")
    out_path.write_text(report, encoding="utf-8")
    log.info("Wrote replay to %s", out_path)
    print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
