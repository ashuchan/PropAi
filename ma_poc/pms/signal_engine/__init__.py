"""Unified Signal Engine — public API.

Three collaborating components answer "is this signal worth pursuing?" in one place:

    SourceSignal ──► SourceQualifier ──► SourceRanker ──► ActionDecider
    (any source)     (is it valid?)      (how important?)  (budget dispatch)

Import from this module, not from sub-modules, for forward-compatibility.
"""

from __future__ import annotations

from ma_poc.pms.signal_engine.models import (
    SourceKind,
    SourceSignal,
)
from ma_poc.pms.signal_engine.qualifier import (
    FieldCombination,
    MediaTypeFilter,
    QualificationResult,
    SourceQualifier,
)
from ma_poc.pms.signal_engine.ranker import (
    RankedSignal,
    ScoringTables,
    SourceRanker,
)
from ma_poc.pms.signal_engine.decider import (
    ActionDecider,
    ActionType,
    DecisionContext,
    DOMAnalysisResult,
    ExtractionAction,
)
from ma_poc.pms.signal_engine.defaults import (
    DEFAULT_MEDIA_FILTER,
    create_default_qualifier,
    create_default_ranker,
    create_rentcafe_qualifier,
)

__all__ = [
    "ActionDecider",
    "ActionType",
    "DecisionContext",
    "DEFAULT_MEDIA_FILTER",
    "DOMAnalysisResult",
    "ExtractionAction",
    "FieldCombination",
    "MediaTypeFilter",
    "QualificationResult",
    "RankedSignal",
    "ScoringTables",
    "SourceKind",
    "SourceRanker",
    "SourceSignal",
    "SourceQualifier",
    "create_default_qualifier",
    "create_default_ranker",
    "create_rentcafe_qualifier",
]
