"""PR 1 — sentinel round-trip probe.

The probe is the deploy-time guardrail that catches the next instance of the
2026-05-10 regression: a writer that ships but never writes. It runs at
runner startup, fails loud, and gates the runner exit so the shard's PG
sync (gated on runner exit code in shard_entry.py) won't poison the DB.

These tests cover:
- Happy path: round-trip succeeds against the FS ProfileStore.
- Disabled by env: skips silently when ENABLE_PERSISTENCE_PROBE=false.
- Sentinel cleanup: removes the __persistence_probe__ row on success.
- Detection of every channel break: each writeable channel break MUST
  raise PersistenceProbeError with the channel named in the message.
"""

from __future__ import annotations

import pytest

from models.scrape_profile import ScrapeProfile
from services.profile_persistence_probe import (
    SENTINEL_CANONICAL_ID,
    PersistenceProbeError,
    run_sentinel_probe,
)
from services.profile_store import ProfileStore


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profiles")


def test_happy_path_round_trip_succeeds(store: ProfileStore) -> None:
    """Round-trip through the FS store completes without raising."""
    run_sentinel_probe(store)


def test_disabled_by_env_skips_silently(store: ProfileStore, monkeypatch) -> None:
    """ENABLE_PERSISTENCE_PROBE=false → no-op, no exception."""
    monkeypatch.setenv("ENABLE_PERSISTENCE_PROBE", "false")
    # Even with a clearly-broken store, disabled probe must not raise.

    class BrokenStore:
        def put(self, p): raise RuntimeError("would fail in real run")
        def get(self, cid): raise RuntimeError("would fail in real run")
        def delete(self, cid): return False

    run_sentinel_probe(BrokenStore())


def test_sentinel_cleanup_after_success_when_store_supports_delete() -> None:
    """When the store exposes ``delete()``, the sentinel row is removed.

    Cleanup is best-effort by design (the FS ``ProfileStore`` predates the
    IProfileStore contract and has no ``delete``), so the assertion only
    holds for stores that do implement it. In production
    (DATA_PROVIDER=postgres) the SqlProfileStore does, so analytics
    queries don't see a stray ``__persistence_probe__`` row.
    """

    class TrackingStore:
        def __init__(self):
            self._stash: dict[str, ScrapeProfile] = {}
            self.delete_calls: list[str] = []

        def put(self, p):
            self._stash[p.canonical_id] = p

        def get(self, cid):
            return self._stash.get(cid)

        def delete(self, cid):
            self.delete_calls.append(cid)
            return self._stash.pop(cid, None) is not None

    s = TrackingStore()
    run_sentinel_probe(s)
    assert SENTINEL_CANONICAL_ID in s.delete_calls
    assert s.get(SENTINEL_CANONICAL_ID) is None


def test_sentinel_cleanup_best_effort_when_store_lacks_delete(store: ProfileStore) -> None:
    """FS ProfileStore lacks ``delete`` — probe must NOT raise. Documented
    behaviour: sentinel may persist on disk; next run overwrites it.
    """
    run_sentinel_probe(store)  # no exception
    # The sentinel may still be on disk — that's the documented best-effort
    # contract for stores without delete().


def test_put_failure_raises_with_phase_marker(store: ProfileStore) -> None:
    """Store that raises on put → PersistenceProbeError with the put phase."""

    class PutBroken:
        def put(self, p): raise RuntimeError("connection refused")
        def get(self, cid): return None
        def delete(self, cid): return False

    with pytest.raises(PersistenceProbeError) as excinfo:
        run_sentinel_probe(PutBroken())
    assert "put failed" in str(excinfo.value)


def test_get_returns_none_raises(store: ProfileStore) -> None:
    """Put succeeds but get returns None → unmistakable signal that the
    sync layer isn't durable."""

    class WriteOnly:
        def put(self, p): pass
        def get(self, cid): return None
        def delete(self, cid): return False

    with pytest.raises(PersistenceProbeError) as excinfo:
        run_sentinel_probe(WriteOnly())
    assert "get returned None" in str(excinfo.value)


def test_mapping_channel_drop_detected(store: ProfileStore) -> None:
    """Store that strips llm_field_mappings on round-trip → caught."""

    class StripsMappings:
        def __init__(self):
            self._stash: dict[str, ScrapeProfile] = {}
        def put(self, p: ScrapeProfile) -> None:
            stripped = p.model_copy(deep=True)
            stripped.api_hints.llm_field_mappings = []  # the regression shape
            self._stash[p.canonical_id] = stripped
        def get(self, cid: str) -> ScrapeProfile | None:
            return self._stash.get(cid)
        def delete(self, cid: str) -> bool:
            return self._stash.pop(cid, None) is not None

    with pytest.raises(PersistenceProbeError) as excinfo:
        run_sentinel_probe(StripsMappings())
    assert "channel=llm_field_mappings" in str(excinfo.value)


def test_dom_selector_channel_drop_detected(store: ProfileStore) -> None:
    """Store that strips dom_hints.field_selectors → caught."""

    class StripsDomSelectors:
        def __init__(self):
            self._stash: dict[str, ScrapeProfile] = {}
        def put(self, p):
            stripped = p.model_copy(deep=True)
            stripped.dom_hints.field_selectors.container = None
            self._stash[p.canonical_id] = stripped
        def get(self, cid):
            return self._stash.get(cid)
        def delete(self, cid):
            return self._stash.pop(cid, None) is not None

    with pytest.raises(PersistenceProbeError) as excinfo:
        run_sentinel_probe(StripsDomSelectors())
    assert "channel=dom_hints.field_selectors" in str(excinfo.value)


def test_navigation_winning_url_drop_detected(store: ProfileStore) -> None:
    """Store that strips navigation.winning_page_url → caught."""

    class StripsNavWinningUrl:
        def __init__(self):
            self._stash: dict[str, ScrapeProfile] = {}
        def put(self, p):
            stripped = p.model_copy(deep=True)
            stripped.navigation.winning_page_url = None
            self._stash[p.canonical_id] = stripped
        def get(self, cid):
            return self._stash.get(cid)
        def delete(self, cid):
            return self._stash.pop(cid, None) is not None

    with pytest.raises(PersistenceProbeError) as excinfo:
        run_sentinel_probe(StripsNavWinningUrl())
    assert "channel=navigation.winning_page_url" in str(excinfo.value)


def test_supports_legacy_save_load_contract(store: ProfileStore) -> None:
    """Probe accepts FS-style save/load contract via the store adapter."""

    class LegacyFs:
        def __init__(self):
            self._stash: dict[str, ScrapeProfile] = {}
        def save(self, p): self._stash[p.canonical_id] = p
        def load(self, cid): return self._stash.get(cid)
        def delete(self, cid): return self._stash.pop(cid, None) is not None

    run_sentinel_probe(LegacyFs())  # must not raise
