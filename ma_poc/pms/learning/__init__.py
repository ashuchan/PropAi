"""API-supervised fallback-parser learning (POC).

The extraction cascade already has a REPLAY half — ``LlmFieldMapping`` (API
JSON-path replay) and ``dom_hints.field_selectors`` (DOM replay). Those
mappings are produced by an LLM call. This package is the LEARNING half: it
DERIVES the same replayable mappings deterministically (no LLM, no cost)
from a run's own API "gold" data — the unit rows a Tier-1/2 extractor
already returned, whose ``unit_number`` is the human-canonical value listed
on the marketing page.

Loop:  gold_units (API)  +  rendered body  ->  induce a replayable parser
       ->  self-validate it reproduces the roster WITH the marketing
           unit numbers  ->  persist for the day the API breaks.

The self-validation gate is the point: an induced parser is accepted ONLY
if replaying it reproduces the gold roster keyed on the real marketing
``unit_number`` (never a synthetic ``inferred_``/``unkeyable_`` id). That is
what makes this safe to run unsupervised across every API-class adapter.
"""

from ma_poc.pms.learning.induce_parser import (
    InducedParser,
    InductionReport,
    induce_dom_selectors,
    induce_fallback_parser,
    induce_json_field_mapping,
    parser_from_dict,
    parser_to_dict,
    replay,
    replay_induced_dom_to_units,
    validate_induction,
)

__all__ = [
    "InducedParser",
    "InductionReport",
    "parser_from_dict",
    "parser_to_dict",
    "replay_induced_dom_to_units",
    "induce_fallback_parser",
    "induce_dom_selectors",
    "induce_json_field_mapping",
    "replay",
    "validate_induction",
]
