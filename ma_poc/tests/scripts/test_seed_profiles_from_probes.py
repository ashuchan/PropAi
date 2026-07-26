"""Seeding warm profiles from agent-discovered navigation steps.

The pipeline injects ``profile.navigation.winning_page_url`` as the
HIGHEST-priority hop candidate (pms/scraper.py: "Highest possible score so it
always lands first"). That makes seeding powerful and makes a wrong value
expensive — it costs a wasted fetch on every future run until the
invalidation path notices.

Measured on the first 78 agent-discovered URLs: only 41 (53%) re-fetch to
something carrying a roster. The rest were 400/401/403 (session- or
auth-bound API calls), 429, or 200-with-no-roster (SPA shells, or the agent
overstating what it reached). Hence the verification gate these tests pin.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.scripts.seed_profiles_from_probes import (
    is_seedable_candidate,
    run,
    seed_profile,
    url_serves_a_roster,
)


# ── Roster evidence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body,expected",
    [
        ('<tr class="unit-container">#311</tr>', True),          # RentCafe
        ("<tr class='AvailUnitRow'>", True),                      # SecureCafe
        ('<div class="fp-units-table">', True),                   # Entrata
        ('<div class="jd-fp-unit-card">', True),                  # Jonah Digital
        ('{"unitNumber": "204", "rent": 1500}', True),            # JSON API
        ('{"unit_id": 88, "price": 2000}', True),                 # JSON API
        ("<h1>Floor Plans</h1><p>Starting at $1,295</p>", False),  # plan marketing
        ("", False),
    ],
)
def test_roster_evidence_detection(body: str, expected: bool) -> None:
    assert url_serves_a_roster(body) is expected


# ── Candidate filtering ─────────────────────────────────────────────────────


def test_ceiling_verdicts_are_never_seeded() -> None:
    """A probe that concluded there is nothing to reach must not seed a URL."""
    for verdict in ("TRUE_CEILING_PLAN_ONLY", "NO_UNIT_SURFACE", "BLOCKED_OR_DEAD", "UNCLEAR"):
        rec = {"classification": verdict, "deepest_url": "https://x.test/floorplans"}
        assert is_seedable_candidate(rec) is None


def test_prose_navigation_steps_are_rejected() -> None:
    """Agents often answer with instructions, not a URL. Persisting prose
    into the top hop slot would waste a fetch every run."""
    assert is_seedable_candidate({"deepest_url": "STEP 1 — GET https://x.test/units"}) is None
    assert is_seedable_candidate({"deepest_url": "NONE EXISTS"}) is None
    assert is_seedable_candidate({"deepest_url": "/floorplans"}) is None


def test_clean_url_is_a_candidate() -> None:
    rec = {"classification": "UNIT_SURFACE_ONE_HOP_DEEPER", "deepest_url": "https://x.test/availableunits"}
    assert is_seedable_candidate(rec) == "https://x.test/availableunits"


# ── Persistence rules ───────────────────────────────────────────────────────


class _Nav:
    def __init__(self, wpu: str | None = None, links: list[str] | None = None) -> None:
        self.winning_page_url = wpu
        self.availability_links = links or []


class _Profile:
    def __init__(self, nav: _Nav) -> None:
        self.navigation = nav
        self.updated_by = "BOOTSTRAP"


class _Store:
    def __init__(self, profiles: dict[str, _Profile]) -> None:
        self._p = profiles
        self.saved: list[str] = []

    def load(self, cid: str) -> Any:
        return self._p.get(cid)

    def save(self, profile: Any) -> None:
        self.saved.append(getattr(profile, "updated_by", ""))


def test_seeds_an_empty_profile() -> None:
    store = _Store({"P1": _Profile(_Nav())})
    assert seed_profile(store, "P1", "https://x.test/units", commit=True) == "seeded"
    assert store._p["P1"].navigation.winning_page_url == "https://x.test/units"
    assert store._p["P1"].updated_by == "PROBE_SEED"


def test_does_not_overwrite_a_different_learned_winner() -> None:
    """A URL the pipeline EARNED from a real extraction outranks one we
    discovered by probing. Overwriting it would erase learning; the probe URL
    is demoted to availability_links so the hop layer still tries it."""
    store = _Store({"P1": _Profile(_Nav(wpu="https://x.test/learned"))})
    out = seed_profile(store, "P1", "https://x.test/probed", commit=True)
    assert out == "added_as_availability_link"
    nav = store._p["P1"].navigation
    assert nav.winning_page_url == "https://x.test/learned"
    assert "https://x.test/probed" in nav.availability_links


def test_seeding_is_idempotent() -> None:
    store = _Store({"P1": _Profile(_Nav(wpu="https://x.test/units"))})
    assert seed_profile(store, "P1", "https://x.test/units", commit=True) == "unchanged"
    assert store.saved == [], "an unchanged profile must not be re-saved"

    store2 = _Store({"P2": _Profile(_Nav(wpu="https://a", links=["https://b"]))})
    assert seed_profile(store2, "P2", "https://b", commit=True) == "already_known"
    assert store2.saved == []


def test_missing_profile_is_reported_not_created() -> None:
    """Seeding must not invent profiles for properties the run never saw."""
    assert seed_profile(_Store({}), "GHOST", "https://x.test/u", commit=True) == "no_profile"


# ── Dry run is the default ──────────────────────────────────────────────────


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the gate: a dry run verifies and reports without
    touching a single profile."""
    monkeypatch.setattr(
        "ma_poc.scripts.seed_profiles_from_probes.verify", lambda url, **_k: (True, "ok")
    )
    store = _Store({"P1": _Profile(_Nav())})
    probes = [{"canonical_id": "P1", "deepest_url": "https://x.test/units",
               "classification": "UNIT_SURFACE_ONE_HOP_DEEPER"}]

    summary = run(probes, store, commit=False)

    assert summary["outcomes"].get("seeded") == 1
    assert store._p["P1"].navigation.winning_page_url is None, "dry run must not persist"
    assert store.saved == []


def test_unverified_urls_are_rejected_not_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """47% of real candidates failed verification. None may reach a profile."""
    monkeypatch.setattr(
        "ma_poc.scripts.seed_profiles_from_probes.verify",
        lambda url, **_k: (False, "http_400"),
    )
    store = _Store({"P1": _Profile(_Nav())})
    probes = [{"canonical_id": "P1", "deepest_url": "https://x.test/api",
               "classification": "UNIT_SURFACE_BEHIND_XHR"}]

    summary = run(probes, store, commit=True)

    assert summary["outcomes"].get("rejected:http_400") == 1
    assert store._p["P1"].navigation.winning_page_url is None
    assert summary["rejected"][0]["reason"] == "http_400"
