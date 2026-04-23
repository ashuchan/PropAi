"""Storage layer — append-only event log + per-property extraction outputs."""

# Relative import so the package resolves whether its parent is imported
# as `storage` (ma_poc/ on sys.path) or `ma_poc.storage` (PropAi root on
# sys.path). The absolute `from storage.event_log import ...` used to
# work only in the first case and confused mypy into thinking
# `ma_poc/storage/event_log.py` was reachable under two different module
# names.
from .event_log import EventLog, write_extraction_output

__all__ = ["EventLog", "write_extraction_output"]
