"""
Field-provenance primitive — Phase 2 of CLAUDE_XSOURCE_AND_LEARNING.

Introduces the SourceId enum and FieldValue / ExtractedSource / ProvenancedUnit
types that adapters and the merger consume.

These types are additive — they do not change any adapter's legacy output.
The closed SourceId enum is the single registry of extraction sources;
no string literals for source IDs are permitted outside this module
and services/source_planner.py.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceId(StrEnum):
    """Closed enum of every extraction source. Adding a new source is a
    deliberate change to this list; do NOT use string literals elsewhere."""

    # Deterministic API sources (per PMS detection)
    API_RENTCAFE_FLOORPLANS = "api_rentcafe_floorplans"
    API_RENTCAFE_UNITS = "api_rentcafe_units"
    API_ENTRATA_WIDGET = "api_entrata_widget"
    API_APPFOLIO_LISTINGS = "api_appfolio_listings"
    API_SIGHTMAP = "api_sightmap"
    API_ONESITE = "api_onesite"
    API_AVALONBAY = "api_avalonbay"
    API_GENERIC_NARROW = "api_generic_narrow"
    API_GENERIC_BROAD = "api_generic_broad"

    # Deterministic page-based sources
    JSON_LD = "json_ld"
    EMBEDDED_JSON = "embedded_json"
    DOM_CASCADE = "dom_cascade"
    DOM_PROFILE_HINTS = "dom_profile_hints"

    # Self-learning replay sources (zero LLM cost)
    MAPPING_REPLAY = "mapping_replay"
    FIELD_PATCH = "field_patch"
    CLUSTER_MAPPING_REPLAY = "cluster_mapping_replay"

    # Computed identity (not a real source — deterministic hash of physical attrs)
    IDENTITY_FALLBACK = "identity_fallback"

    # LLM-tier sources (cost-bearing)
    LLM_API_TARGETED = "llm_api_targeted"
    LLM_DOM_TARGETED = "llm_dom_targeted"
    LLM_MONOLITHIC = "llm_monolithic"
    LLM_FIELD_RECOVERY = "llm_field_recovery"

    # Last-resort defaults
    DEFAULT_AVAILABILITY = "default_availability"


class FieldValue(BaseModel):
    """A single field's value with provenance."""

    model_config = ConfigDict(extra="forbid")

    value: str | int | float | None
    source: SourceId
    confidence: float = Field(ge=0.0, le=1.0)
    source_url: str = ""
    envelope_hash: str = ""


class ExtractedSource(BaseModel):
    """One source's contribution to the merge — provenance-stamped unit dicts.

    `units` is a list of dict[field_name, FieldValue]. ProvenancedUnit is a
    dict subclass and serializes identically; we use the plain dict shape
    here so Pydantic validation flows naturally.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    source_id: SourceId
    source_url: str
    envelope_hash: str
    units: list[dict[str, FieldValue]] = Field(default_factory=list)
    has_unit_ids: bool = False
    is_floor_plan_level: bool = False


class ProvenancedUnit(dict):
    """dict[field_name, FieldValue] with helpers.

    Subclasses dict so existing code that treats unit-records as dicts
    keeps working. Iteration / item access behave identically.
    """

    def get_value(self, field_name: str) -> Any:
        """Return raw value (not the FieldValue wrapper) or None."""
        fv = self.get(field_name)
        if isinstance(fv, FieldValue):
            return fv.value
        return None


# Fields whose make_unit_dict() defaults indicate "absent" — always skip these.
_ABSENT_VALUES: frozenset = frozenset({"", None, -1, "-1"})

# Zero and string-zero are absent UNLESS the field is in _ZERO_IS_VALID_FIELDS.
# Split so empty-string/None are unconditionally rejected while the zero
# carve-out applies only to actual numeric zeros.
_ABSENT_UNLESS_FIELD_PERMITS_ZERO: frozenset = frozenset({0, "0"})

# Fields where 0 / "0" are valid values (e.g. baths=0 = no separate bathroom,
# beds=0 = studio unit).
_ZERO_IS_VALID_FIELDS: frozenset[str] = frozenset({
    "baths", "bathrooms", "floor",
    "beds", "bedrooms",  # studio = 0 beds, valid data
})


def envelope_hash_of(body: Any) -> str:
    """Stable sha256 (16-char prefix) of an API response body for drift detection.

    For dicts: sorted-keys JSON serialization.
    For lists: serialized as-is (order-significant).
    For strings/bytes: direct utf-8 hash.
    """
    if isinstance(body, (dict, list)):
        try:
            s = json.dumps(body, sort_keys=True, default=str)
        except (TypeError, ValueError):
            s = str(body)
    elif isinstance(body, bytes):
        s = body.decode("utf-8", errors="replace")
    else:
        s = str(body)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def from_legacy_unit(
    unit: dict[str, Any],
    source: SourceId,
    source_url: str,
    envelope_hash: str,
    confidence: float,
) -> ProvenancedUnit:
    """Wrap a legacy unit dict (from make_unit_dict) into a ProvenancedUnit.

    Skips fields with default/absent markers — empty string, None, 0/-1
    placeholders. For fields with valid values, sets a FieldValue.
    """
    out = ProvenancedUnit()
    for field_name, value in unit.items():
        if field_name.startswith("_"):
            # Skip private/transport fields like _provenance, _confidence
            continue
        if value in _ABSENT_VALUES:
            continue
        if value in _ABSENT_UNLESS_FIELD_PERMITS_ZERO and field_name not in _ZERO_IS_VALID_FIELDS:
            continue
        # Reject types we cannot represent in FieldValue
        if not isinstance(value, (str, int, float)) and value is not None:
            continue
        out[field_name] = FieldValue(
            value=value,
            source=source,
            confidence=confidence,
            source_url=source_url,
            envelope_hash=envelope_hash,
        )
    return out


def to_legacy_unit(unit: ProvenancedUnit) -> dict[str, Any]:
    """Serialize a ProvenancedUnit back to make_unit_dict() shape.

    Provenance lives in `_provenance` (single underscore — gets stripped
    at the v1/v2 serialization boundary).
    """
    legacy: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for field_name, fv in unit.items():
        if not isinstance(fv, FieldValue):
            continue
        legacy[field_name] = fv.value
        provenance[field_name] = {
            "source": fv.source.value,
            "confidence": fv.confidence,
            "source_url": fv.source_url,
            "envelope_hash": fv.envelope_hash,
        }
    legacy["_provenance"] = provenance
    return legacy
