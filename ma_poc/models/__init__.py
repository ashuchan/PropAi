"""Pydantic v2 data models. Forward-compatible with Phase B PostgreSQL schema."""

# Use fully-qualified imports — the bare ``from models.X import ...`` form
# only resolves when ``_MA_POC_ROOT`` happens to be on ``sys.path``
# (which the runners arrange but the Dockerfile's deploy-validator step
# does not). Loading this package via ``ma_poc.models`` is the canonical
# path; the bare form was a latent bug that surfaced as soon as anything
# in ``ma_poc.pms.adapters`` imported a model directly.
from ma_poc.models.extraction_result import ExtractionResult, ExtractionStatus, ExtractionTier
from ma_poc.models.scrape_event import ChangeDetectionResult, ScrapeEvent, ScrapeOutcome
from ma_poc.models.unit_record import AvailabilityStatus, DataQualityFlag, UnitRecord

__all__ = [
    "AvailabilityStatus",
    "ChangeDetectionResult",
    "DataQualityFlag",
    "ExtractionResult",
    "ExtractionStatus",
    "ExtractionTier",
    "ScrapeEvent",
    "ScrapeOutcome",
    "UnitRecord",
]
