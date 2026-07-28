"""Generation-guarded persistence for unit-endpoint discovery evidence.

Acceptance criteria (2026-07-26 endpoint-discovery recovery):
* Persist only API-verified or strict SSR-DOM warm paths to a scrape profile.
* Save a warm page separately from a direct endpoint; DOM success must never be
  represented as an API endpoint.
* Reject templates containing cookies or static move-in dates.
* Use a GCS object-generation precondition so concurrent discovery workers
  cannot overwrite one another's profile changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse

from ma_poc.models.scrape_profile import ApiEndpoint, ScrapeProfile


class DiscoveryClassification(StrEnum):
    """Evidence-based endpoint-discovery result classes."""

    API_VERIFIED = "API_VERIFIED"
    SSR_DOM_ONLY = "SSR_DOM_ONLY"
    PUBLIC_PLAN_ONLY = "PUBLIC_PLAN_ONLY"
    PUBLIC_ZERO = "PUBLIC_ZERO"
    API_NOT_FOUND_YET = "API_NOT_FOUND_YET"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"


@dataclass(frozen=True, slots=True)
class DiscoveryEvidence:
    """One completed discovery observation safe to checkpoint or persist."""

    canonical_id: str
    classification: DiscoveryClassification
    warm_page_url: str | None
    strict_row_count: int = 0
    endpoint_template: str | None = None
    endpoint_provider: str | None = None
    unit_page_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileWriteResult:
    """Result of a best-effort generation-guarded profile update."""

    outcome: str
    generation: int | None = None
    error: str | None = None


class _GenerationGuardedBlob(Protocol):
    """The GCS Blob surface used by the persistence helper."""

    generation: int | str | None

    def reload(self) -> None:
        """Refresh generation metadata before a conditional write."""

    def download_as_bytes(self) -> bytes:
        """Return the current JSON profile document."""

    def upload_from_string(
        self,
        data: str,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        """Upload only if the object still has the loaded generation."""


_STATIC_DATE_RE = re.compile(
    r"(?:19|20)\d{2}(?:[-/]\d{1,2}){2}|\d{1,2}(?:[-/]\d{1,2}){2}(?:[-/](?:19|20)\d{2})?"
)
_COOKIE_MARKERS = ("cookie", "cf_clearance", "sessionid", "session_id")


def _valid_public_url(url: str | None) -> bool:
    """Return whether *url* is a complete HTTP(S) public URL."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def durable_endpoint_template(template: str | None) -> bool:
    """Return whether an endpoint pattern contains no transient state.

    Direct endpoints without date parameters are valid. When a move-in date is
    present it must remain the runtime ``{move_in_date}`` placeholder.
    """
    if template is None or not _valid_public_url(template):
        return False
    lowered = template.lower()
    if any(marker in lowered for marker in _COOKIE_MARKERS):
        return False
    if _STATIC_DATE_RE.search(template):
        return False
    if "move_in_date=" in lowered and "move_in_date={move_in_date}" not in lowered:
        return False
    return True


def profile_is_persistable(evidence: DiscoveryEvidence) -> bool:
    """Return whether evidence is strong enough to alter a durable profile."""
    if not _valid_public_url(evidence.warm_page_url) or evidence.strict_row_count < 1:
        return False
    if evidence.classification == DiscoveryClassification.API_VERIFIED:
        return durable_endpoint_template(evidence.endpoint_template)
    return evidence.classification == DiscoveryClassification.SSR_DOM_ONLY


def _append_unique(values: list[str], value: str) -> None:
    """Append *value* once while preserving observed navigation order."""
    if value not in values:
        values.append(value)


def apply_discovery_evidence(profile: ScrapeProfile, evidence: DiscoveryEvidence) -> bool:
    """Apply verified warm-path and endpoint knowledge to one profile.

    Args:
        profile: Current validated scrape profile.
        evidence: A fresh discovery observation for the same canonical id.

    Returns:
        ``True`` when the profile changed. Raises ``ValueError`` for mismatched
        ids or evidence that is not safe to persist.
    """
    if profile.canonical_id != evidence.canonical_id:
        raise ValueError("canonical_id_mismatch")
    if not profile_is_persistable(evidence):
        raise ValueError("evidence_not_persistable")
    assert evidence.warm_page_url is not None  # proven by profile_is_persistable

    changed = False
    navigation = profile.navigation
    for proven_path in (evidence.warm_page_url, evidence.unit_page_url):
        if not _valid_public_url(proven_path):
            continue
        assert proven_path is not None
        had_link = proven_path in navigation.availability_links
        _append_unique(navigation.availability_links, proven_path)
        if not had_link:
            changed = True
    # Fresh strict unit evidence outranks a prior plan-only winner, but never
    # dislodges a separately proven unit-level warm path.
    existing_winner = navigation.winning_page_url
    proof_page_url = (
        evidence.unit_page_url
        if evidence.classification == DiscoveryClassification.SSR_DOM_ONLY
        and _valid_public_url(evidence.unit_page_url)
        else evidence.warm_page_url
    )
    prior_unit_level = profile.quality.last_quality_flag == "UNIT_LEVEL"
    if not existing_winner or not prior_unit_level:
        if existing_winner != proof_page_url:
            navigation.winning_page_url = proof_page_url
            navigation.availability_page_path = urlparse(proof_page_url).path or "/"
            changed = True
    if proof_page_url != existing_winner:
        changed = True

    if evidence.classification == DiscoveryClassification.API_VERIFIED:
        assert evidence.endpoint_template is not None
        if not any(
            endpoint.url_pattern == evidence.endpoint_template
            for endpoint in profile.api_hints.known_endpoints
        ):
            profile.api_hints.known_endpoints.append(
                ApiEndpoint(
                    url_pattern=evidence.endpoint_template,
                    provider=evidence.endpoint_provider or "unknown",
                )
            )
            changed = True
        if profile.api_hints.api_provider in {None, "unknown"} and evidence.endpoint_provider:
            profile.api_hints.api_provider = evidence.endpoint_provider
            changed = True

    if changed:
        profile.version += 1
        profile.updated_at = datetime.utcnow()
        profile.updated_by = "ENDPOINT_DISCOVERY"
    return changed


def persist_generation_guarded(
    blob: _GenerationGuardedBlob, evidence: DiscoveryEvidence
) -> ProfileWriteResult:
    """Load, patch, and conditionally save one GCS-backed profile.

    Args:
        blob: A GCS Blob or compatible test double for one profile JSON object.
        evidence: Fresh strict discovery evidence.

    Returns:
        A structured result. Generation conflicts and malformed stored profiles
        are reported without retrying, so the caller can re-read and decide.
    """
    if not profile_is_persistable(evidence):
        return ProfileWriteResult("not_persistable")
    try:
        blob.reload()
        generation = int(blob.generation or 0)
        profile = ScrapeProfile.model_validate_json(blob.download_as_bytes())
    except Exception as exc:
        return ProfileWriteResult("load_failed", error=type(exc).__name__)

    try:
        changed = apply_discovery_evidence(profile, evidence)
    except ValueError as exc:
        return ProfileWriteResult("not_persistable", generation=generation, error=str(exc))
    if not changed:
        return ProfileWriteResult("unchanged", generation=generation)

    payload = json.dumps(profile.model_dump(mode="json"), separators=(",", ":"))
    try:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=generation,
        )
    except Exception as exc:
        # google.api_core.exceptions.PreconditionFailed is deliberately not
        # imported: keeping this dependency-free makes the conflict visible to
        # every caller and lets the batch checkpoint retry after a fresh read.
        return ProfileWriteResult("write_conflict_or_failed", generation=generation, error=type(exc).__name__)
    return ProfileWriteResult("persisted", generation=generation)
