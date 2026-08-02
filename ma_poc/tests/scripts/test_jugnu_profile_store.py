"""Tests for the Jugnu runner's profile-store wiring.

Background — these tests pin the fix for the bug where every Cloud Run
task started with an empty FS profile dir (``.dockerignore`` excludes
``config/profiles/``), so saved ``llm_field_mappings`` and DOM selectors
never replayed. ``_SimpleProfileStore`` is now backing-aware: in prod it
delegates to ``DataProvider.profiles`` (a ``SqlProfileStore`` that
survives container teardown); in local-dev it keeps the FS path.

What's pinned:
  - With no backing supplied the store keeps the legacy FS read/write
    path so existing dev workflows are untouched.
  - When an IProfileStore-shaped backing is wired in, ``get_profile``
    delegates to ``.get`` (not ``.load``), ``save`` delegates to
    ``.put`` (not ``.save``), and ``backing`` returns ``self`` so
    ``profile_updater`` keeps writing through this same dispatch.
  - On a transient backing failure the FS store catches the write so
    the update isn't silently lost; the next sync reconciles.
  - ``_build_profile_store`` reads ``DATA_PROVIDER`` and only routes to
    the data-provider when the env points at a real backend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# The runner module lives under ma_poc/scripts/runners/jugnu.py. Path
# wiring so the import below resolves.
_MA_POC = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC) not in sys.path:
    sys.path.insert(0, str(_MA_POC))
if str(_MA_POC.parent) not in sys.path:
    sys.path.insert(0, str(_MA_POC.parent))


# Importing the runner triggers ``dotenv.load_dotenv()``, which seeds
# ``CLOUD_SQL_IP_TYPE`` etc. into ``os.environ`` from ``ma_poc/.env``.
# Without this guard those values leak into sibling tests that depend
# on a clean env (notably ``test_cloud_sql_engine.py::test_connector_path_*``,
# which expects ``CLOUD_SQL_IP_TYPE`` unset to default to PRIVATE). We
# snapshot and restore the affected keys around the import so this
# test module's import is hermetic regardless of pytest collection order.
_DOTENV_LEAK_KEYS = (
    "CLOUD_SQL_IP_TYPE",
    "CLOUD_SQL_AUTH_TYPE",
    "CLOUD_SQL_INSTANCE",
    "CLOUD_SQL_PROXY_BIN",
    "DATA_PROVIDER",
    "DATABASE_URL",
)
_PRE_IMPORT_ENV: dict[str, str | None] = {
    k: os.environ.get(k) for k in _DOTENV_LEAK_KEYS
}

from ma_poc.scripts.runners.jugnu import (  # noqa: E402  — must follow snapshot
    _SimpleProfileStore,
    _build_profile_store,
)

# Restore immediately. Tests below set their own env vars via
# ``monkeypatch`` so they get a fresh slate per test; this restore
# undoes only the cross-module pollution from ``load_dotenv()``.
for _k, _v in _PRE_IMPORT_ENV.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
del _k, _v


class _FakeProfile:
    """Minimal stand-in — the store doesn't introspect the profile."""

    def __init__(self, canonical_id: str) -> None:
        self.canonical_id = canonical_id
        self.version = 1


class _FakeIProfileStore:
    """Tracks calls so tests can assert on dispatch."""

    def __init__(self, *, fail_on_put: bool = False, fail_on_get: bool = False) -> None:
        self._store: dict[str, Any] = {}
        self.put_calls: list[Any] = []
        self.get_calls: list[str] = []
        self.fail_on_put = fail_on_put
        self.fail_on_get = fail_on_get

    def get(self, canonical_id: str) -> Any:
        self.get_calls.append(canonical_id)
        if self.fail_on_get:
            raise RuntimeError("simulated DB read failure")
        return self._store.get(canonical_id)

    def put(self, profile: Any) -> None:
        self.put_calls.append(profile)
        if self.fail_on_put:
            raise RuntimeError("simulated DB write failure")
        self._store[profile.canonical_id] = profile

    def list_ids(self) -> list[str]:
        return list(self._store.keys())

    def delete(self, canonical_id: str) -> bool:
        return self._store.pop(canonical_id, None) is not None


# ── Backing-selection contract ─────────────────────────────────────────────


def test_store_with_no_backing_uses_fs_load(tmp_path: Path) -> None:
    """Default behaviour (no backing supplied) reads/writes the FS store —
    matches the legacy ``services.profile_store.ProfileStore`` semantics."""
    store = _SimpleProfileStore(tmp_path / "profiles")
    # FS-backed read on missing id returns None, not an error.
    assert store.get_profile("missing") is None
    # No data-provider was wired; backing should expose the FS save() the
    # legacy profile_updater path expects.
    assert store.backing is not store
    assert hasattr(store.backing, "save")


def test_store_with_backing_delegates_get_to_iprofilestore(tmp_path: Path) -> None:
    """``IProfileStore`` exposes ``.get``, not ``.load``. The store must
    dispatch on the wired contract — calling ``.load`` here would raise
    AttributeError, so this also catches accidental delegation regressions."""
    fake = _FakeIProfileStore()
    fake.put(_FakeProfile("P-1"))

    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)
    got = store.get_profile("P-1")
    assert got is not None
    assert got.canonical_id == "P-1"
    assert fake.get_calls == ["P-1"]


def test_store_quarantines_confirmed_bad_route_on_read(tmp_path: Path) -> None:
    from models.scrape_profile import ProfileMaturity, ScrapeProfile

    bad = "https://sightmap.com/app/api/v1/yjp2415rvxl/sightmaps/104541"
    profile = ScrapeProfile(canonical_id="264077")
    profile.navigation.winning_page_url = bad
    profile.confidence.maturity = ProfileMaturity.HOT
    fake = _FakeIProfileStore()
    fake.put(profile)

    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)
    loaded = store.get_profile("264077")

    assert loaded.navigation.winning_page_url is None
    assert loaded.confidence.maturity == ProfileMaturity.COLD


def test_store_with_backing_delegates_save_to_put(tmp_path: Path) -> None:
    """``profile_updater`` calls ``store.save(profile)``. With a
    data-provider backing we must translate that to ``.put`` so writes
    actually land in Postgres."""
    fake = _FakeIProfileStore()
    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)

    p = _FakeProfile("P-NEW")
    store.save(p)

    assert len(fake.put_calls) == 1
    assert fake.put_calls[0] is p


def test_store_with_backing_exposes_self_as_backing(tmp_path: Path) -> None:
    """``profile_updater(... store=profile_store.backing ...)`` calls
    ``backing.save(profile)``. With a data-provider wired in, ``backing``
    must be the wrapper itself so the FS-vs-DB dispatch in ``save()``
    fires. Returning the raw IProfileStore would bypass it (and fail
    because IProfileStore has no ``save`` method)."""
    fake = _FakeIProfileStore()
    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)
    assert store.backing is store

    # And calling save() through that backing reaches the IProfileStore.
    p = _FakeProfile("P-VIA-BACKING")
    store.backing.save(p)
    assert fake.put_calls and fake.put_calls[-1] is p


def test_put_alias_calls_save(tmp_path: Path) -> None:
    """For external callers that prefer the IProfileStore contract API."""
    fake = _FakeIProfileStore()
    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)
    p = _FakeProfile("P-PUT")
    store.put(p)
    assert fake.put_calls and fake.put_calls[-1] is p


# ── Resilience ─────────────────────────────────────────────────────────────


def test_get_falls_back_to_fs_on_backing_error(tmp_path: Path) -> None:
    """A transient DB read failure must not block the run. We fall back
    to the FS store (which returns None when the id isn't there). Without
    this the runner would refuse to scrape a property whenever the DB
    blipped."""
    fake = _FakeIProfileStore(fail_on_get=True)
    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)
    # FS dir is empty so the fallback returns None — point is no exception.
    assert store.get_profile("anything") is None


def test_save_falls_back_to_fs_on_backing_error(tmp_path: Path) -> None:
    """A DB write failure mid-run shouldn't drop the learned mapping;
    fall back to FS so the end-of-shard sync (which copies FS → PG) still
    captures it. The next successful run reconciles."""
    fake = _FakeIProfileStore(fail_on_put=True)
    store = _SimpleProfileStore(tmp_path / "profiles", backing=fake)

    from models.scrape_profile import ScrapeProfile  # noqa: WPS433 — runtime path

    p = ScrapeProfile(canonical_id="P-FALLBACK")
    store.save(p)

    # The FS store must now have the profile so end-of-shard sync_run_to_pg
    # picks it up. Read it back via the FS layer.
    from services.profile_store import ProfileStore  # noqa: WPS433 — runtime path

    fs = ProfileStore(tmp_path / "profiles")
    loaded = fs.load("P-FALLBACK")
    assert loaded is not None
    assert loaded.canonical_id == "P-FALLBACK"


# ── _build_profile_store wiring ────────────────────────────────────────────


def test_build_profile_store_filesystem_returns_fs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``DATA_PROVIDER=filesystem`` (or unset) we must NOT wire the
    data-provider — local-dev / pytest workflows shouldn't suddenly need
    a Postgres connection just to import the runner."""
    monkeypatch.setenv("DATA_PROVIDER", "filesystem")
    store = _build_profile_store(tmp_path / "profiles")
    # FS-backed when no DataProvider is wired.
    assert store.backing is not store


def test_build_profile_store_postgres_routes_to_data_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``DATA_PROVIDER=postgres`` the build helper must route the
    profile store through the data-provider's ``profiles`` attribute.
    We monkeypatch ``get_data_provider`` so the test doesn't need a live
    Postgres — the only thing we're pinning here is the wiring."""
    fake_backing = _FakeIProfileStore()

    class _FakeProvider:
        name = "postgres"
        profiles = fake_backing

    import data_provider.factory as factory_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("DATA_PROVIDER", "postgres")
    monkeypatch.setattr(factory_mod, "get_data_provider", lambda: _FakeProvider())

    store = _build_profile_store(tmp_path / "profiles")
    # When the DP is wired, ``backing`` returns self so save dispatch runs.
    assert store.backing is store

    # And reads go through the fake provider's get(), not the FS store.
    fake_backing.put(_FakeProfile("P-DB"))
    assert store.get_profile("P-DB") is not None
    assert fake_backing.get_calls == ["P-DB"]


def test_build_profile_store_data_provider_failure_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If get_data_provider() throws (e.g. broken DATABASE_URL), the build
    must NOT crash the runner — fall back to the FS store and log loudly.
    The end-of-shard sync provides the durability backstop so we still
    capture mutations made this run."""
    import data_provider.factory as factory_mod  # type: ignore[import-not-found]

    def _boom() -> Any:
        raise RuntimeError("simulated provider init failure")

    monkeypatch.setenv("DATA_PROVIDER", "postgres")
    monkeypatch.setattr(factory_mod, "get_data_provider", _boom)

    store = _build_profile_store(tmp_path / "profiles")
    # FS fallback — backing isn't self.
    assert store.backing is not store
