"""Propagate layer: noise/403 API endpoints are recorded as blocked.

`update_profile_blocklist()` adds an entry to `profile.api_hints.blocked_endpoints`.
Re-blocking an existing URL increments `attempts` rather than adding a duplicate.
The list is capped at 50 entries.
"""

from __future__ import annotations

from models.scrape_profile import ScrapeProfile
from services.profile_updater import update_profile_blocklist


# ---------------------------------------------------------------------------
# Basic block recording
# ---------------------------------------------------------------------------


def test_blocked_endpoint_recorded_on_profile() -> None:
    """A noise API URL is added to profile.api_hints.blocked_endpoints."""
    profile = ScrapeProfile(canonical_id="prop-be-001")

    update_profile_blocklist(profile, "https://example.com/api/chatbot", reason="chatbot_config")

    assert len(profile.api_hints.blocked_endpoints) == 1
    ep = profile.api_hints.blocked_endpoints[0]
    assert ep.url_pattern == "https://example.com/api/chatbot"
    assert ep.reason == "chatbot_config"
    assert ep.attempts == 1  # default is 1 on first record


def test_blocking_same_url_increments_attempts() -> None:
    """Re-blocking an existing URL increments attempts rather than adding a duplicate."""
    profile = ScrapeProfile(canonical_id="prop-be-002")
    url = "https://example.com/api/noise"

    update_profile_blocklist(profile, url, reason="no_unit_data")  # initial: attempts=1
    update_profile_blocklist(profile, url, reason="no_unit_data")  # +1 → 2
    update_profile_blocklist(profile, url, reason="no_unit_data")  # +1 → 3

    assert len(profile.api_hints.blocked_endpoints) == 1
    assert profile.api_hints.blocked_endpoints[0].attempts == 3


def test_default_reason_is_no_unit_data() -> None:
    """When no reason is provided, the default is 'no_unit_data'."""
    profile = ScrapeProfile(canonical_id="prop-be-003")

    update_profile_blocklist(profile, "https://example.com/api/whatever")

    assert profile.api_hints.blocked_endpoints[0].reason == "no_unit_data"


def test_multiple_distinct_urls_each_get_own_entry() -> None:
    """Each distinct URL gets its own blocked_endpoints entry."""
    profile = ScrapeProfile(canonical_id="prop-be-004")

    for i in range(5):
        update_profile_blocklist(profile, f"https://example.com/api/noise/{i}")

    assert len(profile.api_hints.blocked_endpoints) == 5


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


def test_blocked_endpoints_capped_at_50() -> None:
    """The blocked_endpoints list is capped at 50; oldest entries are evicted."""
    profile = ScrapeProfile(canonical_id="prop-be-005")

    for i in range(60):
        update_profile_blocklist(profile, f"https://example.com/api/ep{i:03d}")

    assert len(profile.api_hints.blocked_endpoints) <= 50


def test_cap_evicts_oldest_entries() -> None:
    """When the cap fires, the most recently added entries are retained."""
    profile = ScrapeProfile(canonical_id="prop-be-006")

    for i in range(55):
        update_profile_blocklist(profile, f"https://example.com/api/ep{i:03d}")

    kept_patterns = {ep.url_pattern for ep in profile.api_hints.blocked_endpoints}
    # The last-added URL must survive; the first-added ones may have been evicted
    assert "https://example.com/api/ep054" in kept_patterns
    assert "https://example.com/api/ep000" not in kept_patterns
