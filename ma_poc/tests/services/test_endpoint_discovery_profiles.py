"""Tests for durable endpoint-discovery profile persistence."""

from __future__ import annotations

import json

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.services.endpoint_discovery_profiles import (
    DiscoveryClassification,
    DiscoveryEvidence,
    apply_discovery_evidence,
    durable_endpoint_template,
    persist_generation_guarded,
)

_WARM = "https://centennial.example/austin/centennial/conventional/"
_TEMPLATE = (
    "https://centennial.example/?module=check_availability&property[id]=1108495"
    "&action=view_unit_spaces&property_floorplan[id]={floorplan_id}"
    "&move_in_date={move_in_date}"
)


def _profile() -> ScrapeProfile:
    """Return a plan-level profile eligible for a fresh warm-path upgrade."""
    profile = ScrapeProfile(canonical_id="43")
    profile.quality.last_quality_flag = "PLAN_LEVEL"
    return profile


class _Blob:
    """Small generation-aware Blob test double."""

    def __init__(self, profile: ScrapeProfile, generation: int = 7) -> None:
        self.generation: int | str | None = generation
        self._body = json.dumps(profile.model_dump(mode="json")).encode()
        self.last_generation_match: int | None = None

    def reload(self) -> None:
        """Expose the stored generation."""

    def download_as_bytes(self) -> bytes:
        """Return the current profile JSON."""
        return self._body

    def upload_from_string(
        self,
        data: str,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        """Capture the conditional-write request."""
        assert content_type == "application/json"
        self.last_generation_match = if_generation_match
        self._body = data.encode()


def test_ssr_dom_records_warm_path_without_minting_api() -> None:
    """Strict DOM evidence retains navigation knowledge but no API claim."""
    profile = _profile()
    changed = apply_discovery_evidence(
        profile,
        DiscoveryEvidence(
            canonical_id="43",
            classification=DiscoveryClassification.SSR_DOM_ONLY,
            warm_page_url=_WARM,
            strict_row_count=3,
        ),
    )
    assert changed is True
    assert profile.navigation.winning_page_url == _WARM
    assert _WARM in profile.navigation.availability_links
    assert profile.api_hints.known_endpoints == []


def test_ssr_dom_keeps_grid_warm_path_and_exact_unit_proof_path() -> None:
    """Future navigation retains both the warm grid and its strict DOM page."""
    profile = _profile()
    unit_page = "https://centennial.example/floorplans/a1-1071065-1/"
    apply_discovery_evidence(
        profile,
        DiscoveryEvidence(
            canonical_id="43",
            classification=DiscoveryClassification.SSR_DOM_ONLY,
            warm_page_url=_WARM,
            unit_page_url=unit_page,
            strict_row_count=1,
        ),
    )
    assert profile.navigation.winning_page_url == unit_page
    assert profile.navigation.availability_links == [_WARM, unit_page]
    assert profile.api_hints.known_endpoints == []


def test_api_verified_adds_only_dynamic_template() -> None:
    """An API profile needs strict rows and a non-transient endpoint pattern."""
    profile = _profile()
    apply_discovery_evidence(
        profile,
        DiscoveryEvidence(
            canonical_id="43",
            classification=DiscoveryClassification.API_VERIFIED,
            warm_page_url=_WARM,
            strict_row_count=1,
            endpoint_template=_TEMPLATE,
            endpoint_provider="entrata",
        ),
    )
    assert profile.api_hints.known_endpoints[0].url_pattern == _TEMPLATE
    assert profile.api_hints.api_provider == "entrata"
    assert durable_endpoint_template(_TEMPLATE + "&move_in_date=2026-07-26") is False
    assert durable_endpoint_template(_TEMPLATE + "&cookie=secret") is False


def test_plan_only_evidence_is_checkpoint_only_not_profile_persistable() -> None:
    """A warm plan page without unit proof cannot poison the hot profile."""
    profile = _profile()
    result = persist_generation_guarded(
        _Blob(profile),
        DiscoveryEvidence(
            canonical_id="43",
            classification=DiscoveryClassification.PUBLIC_PLAN_ONLY,
            warm_page_url=_WARM,
        ),
    )
    assert result.outcome == "not_persistable"


def test_generation_guarded_write_uses_loaded_generation() -> None:
    """The GCS precondition prevents concurrent profile clobbers."""
    blob = _Blob(_profile(), generation=17)
    result = persist_generation_guarded(
        blob,
        DiscoveryEvidence(
            canonical_id="43",
            classification=DiscoveryClassification.SSR_DOM_ONLY,
            warm_page_url=_WARM,
            strict_row_count=1,
        ),
    )
    assert result.outcome == "persisted"
    assert blob.last_generation_match == 17
