"""
ScrapeEvent — append-only audit record for every scrape attempt.

Acceptance criteria (CLAUDE.md PR-01):
- One ScrapeEvent per scrape, written to data/scrape_events.jsonl via event_log
- Serialised with model_dump(mode="json") — never .dict()
- Includes proxy/vision/banner/sample flags + page_load_ms + change-detection
  result + scrape_outcome + tier + confidence
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ChangeDetectionResult(str, Enum):
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ScrapeOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class ScrapeEvent(BaseModel):
    event_id: str  # UUID4
    property_id: str
    scrape_timestamp: datetime
    extraction_tier: Optional[int] = None
    change_detection_result: Optional[ChangeDetectionResult] = None
    scrape_outcome: ScrapeOutcome
    failure_reason: Optional[str] = None
    page_load_ms: Optional[int] = None
    proxy_used: bool = False
    proxy_provider: Optional[str] = None
    vision_fallback_used: bool = False
    banner_capture_attempted: bool = False
    banner_concession_found: bool = False
    accuracy_sample_selected: bool = False
    raw_html_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    confidence_score: Optional[float] = None
