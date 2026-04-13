"""Storage layer — append-only event log + per-property extraction outputs."""
from storage.event_log import EventLog, write_extraction_output

__all__ = ["EventLog", "write_extraction_output"]
