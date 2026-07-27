"""LAYER 4 (CONSUME) — pin link-hop ranker + budget refresh helpers.

These pure-function helpers in ``pms/scraper.py`` are the sharp
edges where LLM signals turn into scraping behaviour. The previous
behaviour either never read profile navigation memory at all (Bug 3.1)
or refreshed only the cost cap on rich hops (Bug 5 alignment), missing
the equally-important call-counter reset for high-confidence hints.

Pinned contracts:
  * ``_augment_ranked_with_hints`` — LLM hints prepend at sentinel
    score, drop keyword duplicates so the LLM-anchored entry wins.
  * ``_refresh_monolithic_budget_for_llm_hint`` — bumps
    ``llm_monolithic`` from 0 to 1 only when 0; idempotent at ≥1.
  * ``_refresh_cost_cap_for_hop`` — only raises (never lowers); never
    exceeds the ceiling multiplier.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.scraper import (
    _LLM_HINT_ANCHOR_PREFIX,
    _LLM_HINT_SCORE,
    _augment_ranked_with_hints,
    _refresh_monolithic_budget_for_llm_hint,
)


class _FakeProbeResponse:
    """curl_cffi-response stand-in for the ``_probe`` seam.

    Two production paths in this file's call graph reach ``probe_get``:
    the link-hop cheap-GET gate (``_crawl_get_gate_should_skip``) and the
    detection rescue inside the recursive ``scrape()`` (``scraper.py`` step
    4b, which curl-refetches ``/``, ``/floorplans/``, … to re-run
    ``detect_pms``). A 200 carrying an empty document is neutral for both:
    the gate only retires ``404``/``410`` + <10 KB, and a body with no PMS
    fingerprint leaves the detection unchanged.
    """

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.text = "<html><body></body></html>"
        self.content = self.text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every ``probe_get`` in this module off the live network.

    Overrides the repo-level guard in ``ma_poc/conftest.py`` for this module
    only. These tests assert on candidate ordering / dedup / budget events,
    never on fetched page content, so one neutral response per URL suffices.
    """

    def _fake_probe_get(url: str, *args: Any, **kwargs: Any) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)


# ── _augment_ranked_with_hints ───────────────────────────────────────────────


def test_augment_prepends_llm_hint_above_keyword_candidates() -> None:
    """LLM hints must outrank every keyword-matched candidate."""
    keyword_ranked = [
        ("https://x.com/availability", 100, "Availability"),
        ("https://x.com/floor-plans", 95, "Floor Plans"),
    ]
    out = _augment_ranked_with_hints(
        keyword_ranked,
        ["/floorplans-page"],
        "https://x.com/",
    )
    # First entry is the LLM hint with sentinel score
    assert out[0][0] == "https://x.com/floorplans-page"
    assert out[0][1] == _LLM_HINT_SCORE
    assert out[0][2].startswith(_LLM_HINT_ANCHOR_PREFIX)
    # Keyword candidates remain after, in original order
    assert out[1][0] == "https://x.com/availability"
    assert out[2][0] == "https://x.com/floor-plans"


def test_augment_dedups_keyword_match_when_llm_hints_same_url() -> None:
    """When the LLM hint resolves to a URL also present in
    keyword-ranked candidates, drop the keyword duplicate so the
    LLM-anchored entry is the one that fires (its anchor is what
    triggers the budget refresh in ``_try_link_hop``)."""
    keyword_ranked = [
        ("https://x.com/floor-plans", 95, "Floor Plans link"),
        ("https://x.com/contact", 30, "Contact"),
    ]
    out = _augment_ranked_with_hints(
        keyword_ranked,
        ["/floor-plans"],  # → resolves to https://x.com/floor-plans (dup)
        "https://x.com/",
    )
    # /floor-plans appears exactly once, with the LLM-hint anchor:
    matching = [(u, s, a) for (u, s, a) in out if u == "https://x.com/floor-plans"]
    assert len(matching) == 1
    assert matching[0][2].startswith(_LLM_HINT_ANCHOR_PREFIX)
    # The /contact keyword candidate survives:
    assert any(u == "https://x.com/contact" for u, _, _ in out)


def test_augment_handles_relative_and_absolute_urls() -> None:
    out = _augment_ranked_with_hints(
        [],
        ["/relative-path", "https://other.com/absolute"],
        "https://x.com/",
    )
    urls = [u for u, _, _ in out]
    assert "https://x.com/relative-path" in urls
    assert "https://other.com/absolute" in urls


def test_augment_skips_invalid_or_empty_hints() -> None:
    """Whitespace, empty strings, non-http schemes are filtered."""
    out = _augment_ranked_with_hints(
        [],
        ["", "   ", "javascript:void(0)", "tel:+15555555", "mailto:x@x.com"],
        "https://x.com/",
    )
    assert out == []


def test_augment_dedupes_repeated_hints() -> None:
    """When the LLM emits the same hint twice across tiers (which the
    aggregator already dedupes), augment also dedupes defensively."""
    out = _augment_ranked_with_hints(
        [],
        ["/floorplans", "/floorplans", "/floorplans"],
        "https://x.com/",
    )
    assert len(out) == 1
    assert out[0][0] == "https://x.com/floorplans"


def test_augment_with_no_hints_returns_input_untouched() -> None:
    """No-hint case is the back-compat path — link-hop falls back to
    the keyword-ranked list."""
    keyword_ranked = [("https://x.com/availability", 100, "Avail")]
    out = _augment_ranked_with_hints(keyword_ranked, [], "https://x.com/")
    assert out == keyword_ranked


def test_augment_hint_score_outranks_max_keyword_score() -> None:
    """The sentinel ``_LLM_HINT_SCORE`` must be strictly greater than
    any keyword/host weight defined in the ranker. Otherwise a
    well-named subdomain could compete with the LLM's diagnostic."""
    # _LINK_HOST_KEYWORDS tops out around 120 in the current ranker —
    # any future increase that approaches 10000 should fail this test.
    assert _LLM_HINT_SCORE >= 1000


# ── _refresh_monolithic_budget_for_llm_hint ─────────────────────────────────


def test_monolithic_refresh_bumps_zero_to_one() -> None:
    """The headline use case — entry page burned monolithic to 0,
    LLM-hinted hop deserves a fresh shot."""
    budget: dict[str, Any] = {"llm_monolithic": 0}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_no_op_when_already_at_one() -> None:
    """Idempotent — already-available budget isn't double-stamped."""
    budget: dict[str, Any] = {"llm_monolithic": 1}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is False
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_no_op_when_above_one() -> None:
    """If a future budget config grants >1 monolithic calls per
    property, the refresh is still a no-op — we never inflate."""
    budget: dict[str, Any] = {"llm_monolithic": 3}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is False
    assert budget["llm_monolithic"] == 3


def test_monolithic_refresh_handles_missing_key() -> None:
    """Missing key is treated as 0 — the helper doesn't crash on a
    sparse budget dict."""
    budget: dict[str, Any] = {}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_handles_non_int_value() -> None:
    """Defensive — a corrupt budget value (None / string) is treated
    as 0 rather than crashing the helper."""
    budget: dict[str, Any] = {"llm_monolithic": None}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1

    budget = {"llm_monolithic": "not-an-int"}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    assert budget["llm_monolithic"] == 1


def test_monolithic_refresh_emits_telemetry_when_property_id_present(monkeypatch) -> None:
    """When the helper raises the counter, it emits a
    LINK_HOP_BUDGET_REFRESH event tagged with
    ``refresh_kind="llm_monolithic_counter"`` so analysers can
    distinguish counter-refresh from cost-cap-refresh.

    Regression note: the helper used to pass ``kind=`` as a kwarg,
    colliding with ``emit(kind: EventKind, ...)``'s positional
    parameter — TypeError was silently swallowed by the surrounding
    try/except, so this telemetry never actually fired in production.
    The kwarg was renamed to ``refresh_kind`` and this test pins the
    correct emission so the bug can't regress."""
    captured: list[dict[str, Any]] = []

    def _capture_emit(kind, prop_id, **kwargs):
        captured.append({"event_kind": str(kind), "property_id": prop_id, **kwargs})

    monkeypatch.setattr("ma_poc.observability.events.emit", _capture_emit)

    budget: dict[str, Any] = {"llm_monolithic": 0}
    _refresh_monolithic_budget_for_llm_hint(
        budget,
        property_id="P1",
        sub_url="https://x.com/floorplans",
        hop_index=1,
    )

    assert len(captured) == 1, f"expected 1 emission, got {captured}"
    e = captured[0]
    # EventKind values are lowercase string slugs, not enum names —
    # ``EventKind.LINK_HOP_BUDGET_REFRESH = "planner.link_hop_budget_refresh"``.
    assert "link_hop_budget_refresh" in e["event_kind"].lower()
    assert e["property_id"] == "P1"
    assert e["sub_url"] == "https://x.com/floorplans"
    assert e["hop_index"] == 1
    # The discriminator kwarg distinguishes counter-refresh from cost-cap:
    assert e["refresh_kind"] == "llm_monolithic_counter"
    assert e["old_value"] == 0
    assert e["new_value"] == 1


def test_monolithic_refresh_silent_when_no_property_id() -> None:
    """When called without a property_id (e.g. unit-test scenarios),
    the helper still works but doesn't emit telemetry."""
    budget: dict[str, Any] = {"llm_monolithic": 0}
    raised = _refresh_monolithic_budget_for_llm_hint(budget)
    assert raised is True
    # No exception, returns the right value.


# ── Bug B (P4) — PMS fingerprint priors ─────────────────────────────────────


@pytest.mark.parametrize(
    "pms_name,expected_first_path",
    [
        ("rentcafe", "/floorplans"),
        ("entrata", "/floorplans"),
        ("appfolio", "/listings"),
        ("onesite", "/floorplans"),
        ("realpage_oll", "/floorplans"),
        ("sightmap", "/floorplans"),
        ("avalonbay", "/floor-plans-pricing"),
        ("amli", "/floor-plans"),
        ("funnel", "/floorplans"),
    ],
)
def test_pms_priors_for_each_supported_pms_injects_template_paths(
    pms_name: str, expected_first_path: str
) -> None:
    """Every PMS with a body-shape checker (i.e., every PMS that rides
    the Bug-C P3 preservation path) must produce at least one priors
    candidate when detected. The first prior path matches the dominant
    convention for that platform. Bug B contract."""
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _PMS_PRIOR_SCORE, _pms_priors_for

    detected = DetectedPMS(
        pms=pms_name,
        confidence=0.9,
        evidence=[f"fp:{pms_name}"],
        recommended_strategy=_STRATEGY_BY_PMS.get(pms_name, "cascade"),
    )
    priors = _pms_priors_for(detected, "https://example.com/")
    assert priors, f"expected at least one prior for {pms_name!r}, got none"
    # First entry is the canonical sub-path for this PMS.
    first_url, first_score, first_anchor = priors[0]
    assert first_url == f"https://example.com{expected_first_path}"
    assert first_score == _PMS_PRIOR_SCORE
    assert first_anchor == f"pms_prior:{pms_name}"


@pytest.mark.parametrize(
    "pms_name",
    ["realpage_oll", "realpage_cws"],
)
def test_realpage_priors_include_both_floorplan_path_variants(pms_name: str) -> None:
    """2026-05-27 612-failure-grind chip: RealPage OLL/CWS sites are
    split between ``/floorplans`` (unhyphenated) and ``/floor-plans``
    (hyphenated). Tremont Burlington exposes rents on the unhyphenated
    path; 81arch / domainontheparkway / many residue siblings expose
    them on the hyphenated path. Both variants must appear in the prior
    list (memory: feedback_no_shallow_probing — never just one)."""
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _pms_priors_for

    detected = DetectedPMS(
        pms=pms_name,
        confidence=0.9,
        evidence=[f"fp:{pms_name}"],
        recommended_strategy=_STRATEGY_BY_PMS.get(pms_name, "cascade"),
    )
    priors = _pms_priors_for(detected, "https://example.com/")
    urls = {url for url, _score, _anchor in priors}
    assert "https://example.com/floorplans" in urls, (
        f"{pms_name} priors must include unhyphenated /floorplans; got {urls}"
    )
    assert "https://example.com/floor-plans" in urls, (
        f"{pms_name} priors must include hyphenated /floor-plans; got {urls}"
    )


def test_pms_priors_falls_back_to_universal_when_pms_is_unknown() -> None:
    """When detection produces ``unknown``, the universal multifamily
    sub-path priors fire as a fallback. This decouples link-hop recovery
    from PMS-fingerprint recognition — sites on unrecognised CMSes still
    get a fair shot at the canonical sub-paths. Pre-2026-05-12: returned
    ``[]`` (the bug); fix lets recovery work for any unrecognised template
    (Jonah Digital, custom stacks, future CMSes).
    """
    from ma_poc.pms.detector import DetectedPMS
    from ma_poc.pms.scraper import _PMS_PRIOR_SCORE, _UNIVERSAL_SUB_PATH_PRIORS, _pms_priors_for

    detected = DetectedPMS(
        pms="unknown",
        confidence=0.0,
        evidence=["no signal"],
        recommended_strategy="cascade",
    )
    priors = _pms_priors_for(detected, "https://example.com/")
    assert len(priors) == len(_UNIVERSAL_SUB_PATH_PRIORS)
    for url, score, anchor in priors:
        assert score == _PMS_PRIOR_SCORE
        assert anchor == "pms_prior:universal"
    # First entry should be /floorplans — the most common multifamily
    # availability-page convention across CMSes.
    assert priors[0][0] == "https://example.com/floorplans"


def test_pms_priors_falls_back_to_universal_when_detection_is_none() -> None:
    """Defensive: when ``detected`` is None (caller didn't run detection),
    still emit the universal priors. Treating absent detection the same as
    ``unknown`` keeps the recovery path open."""
    from ma_poc.pms.scraper import _UNIVERSAL_SUB_PATH_PRIORS, _pms_priors_for

    priors = _pms_priors_for(None, "https://example.com/")
    assert len(priors) == len(_UNIVERSAL_SUB_PATH_PRIORS)
    assert all(anchor == "pms_prior:universal" for _, _, anchor in priors)


def test_pms_priors_falls_back_to_universal_when_pms_lacks_template_entry() -> None:
    """A PMS that's known to the detector but absent from
    ``_PMS_SUB_PATH_PRIORS`` (e.g., wix_nopms / squarespace_nopms which
    are syndication-only with no inventory data path) still gets the
    universal fallback. If those sites happen to expose a `/floorplans`
    page, link-hop will try it; if not, the fetch fails cheap (one 404)
    and the generic adapter falls through to its other tiers. Cost is
    bounded by max_hops; no correctness risk."""
    from ma_poc.pms.detector import DetectedPMS
    from ma_poc.pms.scraper import _UNIVERSAL_SUB_PATH_PRIORS, _pms_priors_for

    detected = DetectedPMS(
        pms="wix_nopms",
        confidence=0.8,
        evidence=["fp:wix"],
        recommended_strategy="cascade",
    )
    priors = _pms_priors_for(detected, "https://example.com/")
    assert len(priors) == len(_UNIVERSAL_SUB_PATH_PRIORS)
    assert all(anchor == "pms_prior:universal" for _, _, anchor in priors)


def test_pms_priors_universal_unblocks_skyline_at_kessler_regression() -> None:
    """Regression for 2026-05-12 canary. Skyline at Kessler (11611) is
    a Jonah Digital marketing-CMS site — not a PMS we fingerprint.
    Detection returns ``unknown``. Pre-fix the prior ladder returned
    ``[]`` and link-hop's keyword ranker found nothing (SPA marketing
    shell with no statically-discoverable anchors), so the runner
    never visited ``/floorplans/`` and missed the units API.

    Post-fix: the universal fallback emits ``/floorplans`` as a
    candidate regardless of whether the template is recognised. Once
    link-hop fetches it, the generic adapter's DOM scan / LLM DOM
    tier processes the rendered floor-plan cards.

    This test pins the behaviour for the SPECIFIC unrecognised-template
    + zero-link-hop-candidate shape that the canary surfaced.
    """
    from ma_poc.pms.detector import DetectedPMS
    from ma_poc.pms.scraper import _pms_priors_for

    skyline_detection = DetectedPMS(
        pms="unknown",  # Jonah Digital is not in our PMS fingerprint set
        confidence=0.0,
        evidence=[],
        recommended_strategy="cascade",
    )
    priors = _pms_priors_for(
        skyline_detection,
        "https://www.skylineatkessler.com/",
    )
    urls = [u for u, _, _ in priors]
    assert "https://www.skylineatkessler.com/floorplans" in urls, (
        "Universal fallback must inject /floorplans for unknown-PMS sites "
        "so link-hop fetches the page that triggers Jonah's renderer XHR."
    )


def test_pms_priors_filter_entry_url_collisions() -> None:
    """When a template path resolves to the entry URL itself (e.g.,
    entry_url already includes the path), filter that prior out — we
    only want strictly-distinct sub-pages because re-fetching the same
    URL is wasted budget."""
    from ma_poc.pms.detector import DetectedPMS
    from ma_poc.pms.scraper import _pms_priors_for

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy="jsonld_first",
    )
    # entry_url IS /floorplans. urljoin("...../floorplans", "/floorplans")
    # returns the same URL — filter it.
    priors = _pms_priors_for(detected, "https://x.com/floorplans")
    urls = [u for u, _, _ in priors]
    assert "https://x.com/floorplans" not in urls
    # But the other priors for rentcafe (/availability, /apartments) still
    # produce — the entry-URL filter is per-path, not all-or-nothing.
    assert "https://x.com/availability" in urls
    assert "https://x.com/apartments" in urls


def test_pms_priors_dict_covers_all_pms_with_body_checker() -> None:
    """Invariant: every PMS that implements ``matches_response_body``
    (i.e., every PMS that rides the Bug-C P3 preservation path) must
    also have a ``_PMS_SUB_PATH_PRIORS`` entry.

    Rationale: those are the PMSes the detector confidently identifies
    AND for which the scraper has a known template. If the body-shape
    check is good enough to preserve a detection through ``confirm_detection``,
    it should be good enough to seed a link-hop prior. Catches drift if
    a new PMS adapter is added with `matches_response_body` but
    without a prior list — silent under today's code, loud under this
    test.
    """
    from ma_poc.pms.adapters.registry import all_adapters
    from ma_poc.pms.scraper import _PMS_SUB_PATH_PRIORS

    pms_with_checker = {
        getattr(a, "pms_name", "")
        for a in all_adapters()
        if callable(getattr(a, "matches_response_body", None))
    }
    pms_with_checker.discard("")  # paranoid: skip any nameless adapter
    missing = pms_with_checker - set(_PMS_SUB_PATH_PRIORS)
    assert not missing, (
        f"PMSes that have ``matches_response_body`` but no ``_PMS_SUB_PATH_PRIORS`` "
        f"entry: {sorted(missing)}. Add their canonical sub-paths to "
        f"``_PMS_SUB_PATH_PRIORS`` so link-hop has a template prior to "
        f"fall back on. See Bug B / P4 in "
        f"docs/2026_05_11_regressions_fix_design.md."
    )


# ── Bug B (P4) — merge behaviour inside _try_link_hop ───────────────────────
#
# These tests exercise the merge + dedup loop in ``_try_link_hop`` that
# combines profile-top, PMS priors, and keyword candidates into a single
# ranked list. Unlike the pure-helper tests above, these mock the L1
# fetcher and re-run ``_try_link_hop`` end-to-end.


def _starved_profile() -> Any:
    """Profile with no learned navigation memory (the Bug B starvation
    shape — Bug-1 collateral over many days)."""

    class _Nav:
        winning_page_url = None
        availability_links: list[str] = []
        explored_links: list[str] = []

    class _Profile:
        navigation = _Nav()
        api_hints = None

    return _Profile()


def _profile_with_winning_url(url: str) -> Any:
    """Profile that learned ``url`` as the winning page on a prior run."""

    class _Nav:
        winning_page_url = url
        availability_links: list[str] = []
        explored_links: list[str] = []

    class _Profile:
        navigation = _Nav()
        api_hints = None

    return _Profile()


def _profile_with_explored_dead_end(url: str) -> Any:
    """Profile that recorded ``url`` as explored-and-empty (skip-list)."""

    class _Nav:
        winning_page_url = None
        availability_links: list[str] = []
        explored_links: list[str] = [url]

    class _Profile:
        navigation = _Nav()
        api_hints = None

    return _Profile()


@pytest.mark.asyncio
async def test_link_hop_dedups_profile_against_pms_prior_for_same_url() -> None:
    """When profile.winning_page_url is /floorplans AND the RentCafe
    prior also wants /floorplans, the merged candidate list contains
    exactly one entry for that URL. Profile wins on score (10001 vs
    5000) so its anchor (``profile:winning_page_url``) is the one
    recorded.

    Documents the priority ordering: profile > PMS prior > keyword.
    """
    from unittest.mock import patch

    from ma_poc.pms import scraper as pms_scraper
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _try_link_hop

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    fetch_calls: list[str] = []
    candidate_records: list[list[dict]] = []

    async def _fake_fetch(task: Any) -> Any:
        fetch_calls.append(getattr(task, "url", ""))

        class _O:
            value = "OK"

        class _R:
            outcome = _O()
            status = 200
            body = b"<html></html>"
            final_url = task.url
            elapsed_ms = 0
            content_type = "text/html"
            captcha_detected = False
            error_signature = None
            identity_ua_hash = "test"
            render_mode = type("M", (), {"value": "RENDER"})()
            headers: dict = {}

            def to_dict(self) -> dict:
                return {"outcome": "OK"}

        return _R()

    # Capture the LINK_HOP_STARTED event so we can read the candidate list.
    from ma_poc.observability import events as obs_events

    original_emit = obs_events.emit

    def _spy_emit(kind: Any, property_id: str, **payload: Any) -> Any:
        kind_value = getattr(kind, "value", str(kind))
        if "link_hop_started" in kind_value.lower():
            candidate_records.append(payload.get("candidates", []))
        return original_emit(kind, property_id, **payload)

    with (
        patch("ma_poc.fetch.fetch", _fake_fetch, create=True),
        patch.object(obs_events, "emit", _spy_emit),
        # Cheap-GET gate off — it probe_get()s the live network per hop.
        patch.object(pms_scraper, "_crawl_get_gate_should_skip", lambda url: False),
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html="<html></html>",  # no useful anchors
            detected=detected,
            profile=_profile_with_winning_url("https://example.com/floorplans"),
            expected_total_units=None,
            property_id="DEDUP-TEST",
            csv_row=None,
            max_hops=3,
        )

    assert candidate_records, "LINK_HOP_STARTED event not emitted"
    candidates = candidate_records[0]
    matching = [c for c in candidates if c["url"] == "https://example.com/floorplans"]
    assert len(matching) == 1, (
        f"Profile + prior should dedup to one entry for /floorplans; got "
        f"{len(matching)}: {[c for c in matching]}"
    )
    # Profile wins on score (its anchor is recorded, not the prior's).
    assert matching[0]["anchor"].startswith("profile:"), (
        f"On collision profile must win over prior; got anchor "
        f"{matching[0]['anchor']!r}"
    )


@pytest.mark.asyncio
async def test_link_hop_priors_respect_explored_skip_list() -> None:
    """A PMS prior URL that the profile has already marked as
    ``explored_links`` (i.e., known-empty from prior runs) must be
    filtered out of the candidate list before the cap. We don't re-pay
    for known dead ends just because the template says so.

    Documents the dead-end skip-list contract for the new prior layer.

    Asserts on the emitted ``LINK_HOP_FETCHED`` events — the honest
    per-hop signal — rather than on the fetch mock alone: the cheap-GET
    gate (``ENABLE_CRAWL_GET_GATE``) can retire a hop as
    ``DEAD_URL_GATED`` before the sub-fetch is reached, so an empty
    ``fetch_calls`` does not by itself mean a candidate was skipped.
    The gate is stubbed off here so the hop is exercised end-to-end
    (and so the test does not depend on live network responses for
    ``example.com`` subpaths).
    """
    from unittest.mock import patch

    from ma_poc.pms import scraper as pms_scraper
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _try_link_hop

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    fetch_calls: list[str] = []
    hop_fetched: list[str] = []

    async def _fake_fetch(task: Any) -> Any:
        fetch_calls.append(getattr(task, "url", ""))

        class _O:
            value = "OK"

        class _R:
            outcome = _O()
            status = 200
            body = b"<html></html>"
            final_url = task.url
            elapsed_ms = 0
            content_type = "text/html"
            captcha_detected = False
            error_signature = None
            identity_ua_hash = "test"
            render_mode = type("M", (), {"value": "RENDER"})()
            headers: dict = {}

            def to_dict(self) -> dict:
                return {"outcome": "OK"}

        return _R()

    # Every attempted hop emits LINK_HOP_FETCHED (success, error, or
    # gated) — that is the signal the skip-list contract lives on.
    from ma_poc.observability import events as obs_events

    original_emit = obs_events.emit

    def _spy_emit(kind: Any, property_id: str, **payload: Any) -> Any:
        kind_value = getattr(kind, "value", str(kind))
        if "link_hop_fetched" in kind_value.lower():
            hop_fetched.append(str(payload.get("url", "")))
        return original_emit(kind, property_id, **payload)

    # Profile says /floorplans was explored and had no data (skip it).
    # RentCafe prior would otherwise inject /floorplans as candidate #1.
    with (
        patch("ma_poc.fetch.fetch", _fake_fetch, create=True),
        patch.object(obs_events, "emit", _spy_emit),
        # Cheap-GET gate off: it would otherwise probe_get() the live
        # network and retire every prior as DEAD_URL_GATED before the
        # sub-fetch, masking whichever candidates were really attempted.
        patch.object(pms_scraper, "_crawl_get_gate_should_skip", lambda url: False),
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html="<html></html>",
            detected=detected,
            profile=_profile_with_explored_dead_end("https://example.com/floorplans"),
            expected_total_units=None,
            property_id="SKIP-TEST",
            csv_row=None,
            max_hops=3,
        )

    # Guard the mock itself: an empty fetch_calls means the patch target
    # stopped intercepting the hop sub-fetch, not that priors were skipped.
    assert fetch_calls, (
        "No sub-fetch was intercepted — the ma_poc.fetch.fetch patch target "
        "no longer covers _try_link_hop's sub-fetch, so the assertions below "
        "would pass vacuously."
    )
    assert hop_fetched, "No LINK_HOP_FETCHED event emitted"

    assert "https://example.com/floorplans" not in hop_fetched, (
        f"Prior URL was in profile.explored_links but still got fetched: "
        f"{hop_fetched}. The skip-list must apply to priors the same way "
        f"it applies to keyword candidates."
    )
    assert "https://example.com/floorplans" not in fetch_calls, (
        f"Skip-listed prior reached the sub-fetch: {fetch_calls}"
    )
    # The OTHER priors (/availability, /apartments) should still fire.
    assert any("availability" in u or "apartments" in u for u in hop_fetched), (
        f"Skipping /floorplans should not suppress the rest of the "
        f"RentCafe priors; got hop_fetched={hop_fetched}"
    )


# ── Entrata /conventional/ prior ────────────────────────────────────────────


def test_entrata_priors_include_conventional_path() -> None:
    """Entrata custom-domain sites use /{city}/{property}/conventional/ as
    the floor-plan page — the standard Entrata sub-paths (/floorplans,
    /availability, /leasing) 404 on those sites.  The prior tuple must
    include /conventional/ so link-hop tries it without needing the LLM
    to discover the URL.  Regression for alistermontclair.com (228073).
    """
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _pms_priors_for

    detected = DetectedPMS(
        pms="entrata",
        confidence=0.9,
        evidence=["fp:entrata"],
        recommended_strategy=_STRATEGY_BY_PMS.get("entrata", "cascade"),
    )
    priors = _pms_priors_for(detected, "https://www.alistermontclair.com/")
    paths = [url for url, _, _ in priors]
    assert any("conventional" in p for p in paths), (
        f"expected /conventional/ in Entrata priors; got {paths}"
    )
    assert any("apartments" in p for p in paths), (
        f"expected /apartments/ in Entrata priors; got {paths}"
    )
    # Standard paths must still be present so non-custom-domain Entrata
    # sites keep working.
    assert any("floorplans" in p for p in paths), (
        f"expected /floorplans in Entrata priors; got {paths}"
    )


# ── Navigation-menu anchor keyword scoring ───────────────────────────────────


def test_find_your_home_anchor_scores_above_apply() -> None:
    """'Find Your Home' is a floor-plan CTA that should rank above generic
    CTAs like 'Apply'. Before this fix the keyword list had no entry for
    it, so a nav link 'Find Your Home' → /conventional/ would score 0.
    """
    from ma_poc.pms.scraper import _LINK_ANCHOR_KEYWORDS

    kw_map = {kw.lower(): score for kw, score in _LINK_ANCHOR_KEYWORDS}
    assert "find your home" in kw_map, "'find your home' must be in anchor keywords"
    assert "view availability" in kw_map, "'view availability' must be in anchor keywords"
    assert kw_map["find your home"] > kw_map.get("apply", 0), (
        "'find your home' must score higher than 'apply'"
    )


def test_conventional_path_keyword_scores() -> None:
    """/conventional/ appearing in a URL path must yield a non-zero score
    from the path keyword list so it ranks above unscored links."""
    from ma_poc.pms.scraper import _LINK_PATH_KEYWORDS

    path_map = {kw.lower(): score for kw, score in _LINK_PATH_KEYWORDS}
    assert "/conventional/" in path_map, (
        "'/conventional/' must be in path keywords for Entrata custom-domain detection"
    )
    assert path_map["/conventional/"] > 50, (
        "score for /conventional/ must be above 50 to outrank noise links"
    )


def test_rank_internal_links_surfaces_entrata_deep_url() -> None:
    """Integration: given an alistermontclair-style HTML with a navigation
    menu containing 'Find Your Home' → /montclair/alister-montclair/conventional/,
    _rank_internal_links must return that URL in the top candidates.
    """
    from ma_poc.pms.scraper import _rank_internal_links

    html = """<html><head></head><body>
    <nav>
      <ul>
        <li><a href="/about">About Us</a></li>
        <li>
          <a href="#">Floor Plans</a>
          <ul>
            <li><a href="/montclair/alister-montclair/conventional/">Find Your Home</a></li>
            <li><a href="/apply">Apply Now</a></li>
          </ul>
        </li>
        <li><a href="/contact">Contact</a></li>
      </ul>
    </nav>
    </body></html>"""

    ranked = _rank_internal_links(html, "https://www.alistermontclair.com/", limit=5)
    urls = [u for u, _, _ in ranked]
    assert any("conventional" in u for u in urls), (
        f"expected /conventional/ URL in ranked candidates; got {urls}"
    )
    # Multi-signal boost: anchor "find your home" (score 88) + path
    # "/conventional/" (score 88) should both fire, triggering the
    # boost to _PMS_PRIOR_SCORE - 1000 = 4000.  This ensures the link
    # competes with PMS priors (5000) and isn't crowded out.
    conventional_entry = next(
        ((u, s, a) for u, s, a in ranked if "conventional" in u), None
    )
    assert conventional_entry is not None
    _, boosted_score, _ = conventional_entry
    from ma_poc.pms.scraper import _PMS_PRIOR_SCORE
    assert boosted_score >= _PMS_PRIOR_SCORE - 1_000, (
        f"expected multi-signal boost to >= {_PMS_PRIOR_SCORE - 1000}; "
        f"got score={boosted_score}"
    )


def test_rank_internal_links_discovers_form_action_url() -> None:
    """The alistermontclair.com shape: 'Pick Your Home' is an <a> pointing
    at prospectportal.com (different host), but the same URL also appears
    in a <form action>.  _rank_internal_links must parse form actions so
    the same-site URL is discovered even when the <a> is cross-domain.
    Regression for 228073.
    """
    from ma_poc.pms.scraper import _rank_internal_links

    html = """<html><head></head><body>
    <nav>
      <!-- <a> goes to prospectportal.com — cross-site, filtered -->
      <a href="https://alistermontclair.prospectportal.com/montclair/alister-montclair/conventional/">
        Pick Your Home
      </a>
    </nav>
    <!-- <form action> carries the same-site URL -->
    <form method="post"
          action="https://www.alistermontclair.com/montclair/alister-montclair/conventional/">
      <input type="hidden" name="occupancy_type" value="conventional">
      <button type="submit">Search</button>
    </form>
    </body></html>"""

    ranked = _rank_internal_links(
        html, "https://www.alistermontclair.com/", limit=10
    )
    urls = [u for u, _, _ in ranked]

    # Same-site URL from form action must appear
    assert any("montclair/alister-montclair/conventional" in u for u in urls), (
        f"form-action URL not in ranked candidates; got {urls}"
    )
    # Cross-site prospectportal.com link should also be discovered (portal host)
    assert any("prospectportal.com" in u for u in urls), (
        f"prospectportal.com URL not in candidates (should score as portal host); got {urls}"
    )


# ── link-hop wall-clock budget (600s-timeout guard) ─────────────────────────


def _fake_ok_fetch_recorder(fetch_calls: list[str]) -> Any:
    """Build an async fetch stub that records every URL and returns an OK
    HTML body (empty → no units, so the hop loop keeps trying candidates)."""

    async def _fake_fetch(task: Any) -> Any:
        fetch_calls.append(getattr(task, "url", ""))

        class _O:
            value = "OK"

        class _R:
            outcome = _O()
            status = 200
            body = b"<html></html>"
            final_url = task.url
            elapsed_ms = 0
            content_type = "text/html"
            captcha_detected = False
            error_signature = None
            identity_ua_hash = "test"
            render_mode = type("M", (), {"value": "RENDER"})()
            headers: dict = {}

            def to_dict(self) -> dict:
                return {"outcome": "OK"}

        return _R()

    return _fake_fetch


@pytest.mark.asyncio
async def test_link_hop_stops_at_wall_clock_budget(monkeypatch: Any) -> None:
    """Budget spent → no NEW hop starts.

    link-hop was bounded only by ``max_hops`` (a page COUNT), never elapsed
    time — so when a host tarpitted, up to ~14 sequential RENDER sub-fetches
    blew the 600s per-property timeout. With LINK_HOP_BUDGET_S=0 the deadline
    is already past on entry, so the loop must break on the first iteration
    (zero fetches) and emit HOP_BUDGET_EXCEEDED — never firing the full
    max_hops fan-out.
    """
    from unittest.mock import patch

    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _try_link_hop

    # from-import inside _try_link_hop re-reads the module attr at call time,
    # so patching the module attribute controls the budget without a reload.
    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_BUDGET_S", 0)

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )
    fetch_calls: list[str] = []
    budget_hits: list[str] = []

    from ma_poc.observability import events as obs_events

    original_emit = obs_events.emit

    def _spy_emit(kind: Any, property_id: str, **payload: Any) -> Any:
        if payload.get("outcome") == "HOP_BUDGET_EXCEEDED":
            budget_hits.append(payload.get("url", ""))
        return original_emit(kind, property_id, **payload)

    with (
        patch("ma_poc.fetch.fetch", _fake_ok_fetch_recorder(fetch_calls), create=True),
        patch.object(obs_events, "emit", _spy_emit),
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html="<html></html>",
            detected=detected,
            profile=_profile_with_winning_url("https://example.com/floorplans"),
            expected_total_units=None,
            property_id="BUDGET-ZERO",
            csv_row=None,
            max_hops=7,
        )

    assert fetch_calls == [], f"budget=0 must fire ZERO hops; got {fetch_calls}"
    assert budget_hits, "HOP_BUDGET_EXCEEDED must be emitted when the budget is spent"


@pytest.mark.asyncio
async def test_link_hop_generous_budget_no_premature_cutoff(monkeypatch: Any) -> None:
    """Contrast case: with a generous budget the guard must NOT fire — no
    HOP_BUDGET_EXCEEDED is emitted. Proves the cutoff in the budget=0 test is
    the deadline itself, not the guard tripping on every run.

    (Asserted on the emitted event rather than fetch_calls: the legacy
    ``ma_poc.fetch.fetch`` patch target no longer intercepts the hop fetch, so
    fetch-call counting is unreliable here — the event is the honest signal.)
    """
    from unittest.mock import patch

    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _try_link_hop

    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_BUDGET_S", 100_000)

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )
    fetch_calls: list[str] = []
    budget_hits: list[str] = []

    from ma_poc.observability import events as obs_events

    original_emit = obs_events.emit

    def _spy_emit(kind: Any, property_id: str, **payload: Any) -> Any:
        if payload.get("outcome") == "HOP_BUDGET_EXCEEDED":
            budget_hits.append(payload.get("url", ""))
        return original_emit(kind, property_id, **payload)

    with (
        patch("ma_poc.fetch.fetch", _fake_ok_fetch_recorder(fetch_calls), create=True),
        patch.object(obs_events, "emit", _spy_emit),
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html="<html></html>",
            detected=detected,
            profile=_profile_with_winning_url("https://example.com/floorplans"),
            expected_total_units=None,
            property_id="BUDGET-BIG",
            csv_row=None,
            max_hops=7,
        )

    assert budget_hits == [], (
        f"generous budget must NOT trip the hop-budget guard; got {budget_hits}"
    )


# ── per-hop fetch cap (LINK_HOP_PER_FETCH_S, 2026-07-27) ────────────────────
#
# The wall-clock budget above stops new hops from STARTING. It did NOT stop
# hop #1 from consuming the whole budget: the in-flight allowance was
# ``max(_MIN_HOP_FETCH_S, deadline - now)`` — i.e. ALL remaining budget — so a
# single tarpitting fetch starved every later candidate. Measured 2026-07-27
# (run …-sample100-7fc8b4c, 100 properties): 8 properties hit
# HOP_FETCH_BUDGET_EXCEEDED, 6 of them on hop_index=1; 5 of the 7 affected
# properties ended FAILED_NO_DATA with every remaining candidate unfetched.
# 1 of 30 successful hop fetches exceeded 90s (max 94.5s) and 0 of the 20
# unit-recovering hops did, which is where the 90s default comes from. See the
# benefit/limits note on LINK_HOP_PER_FETCH_S in config/feature_flags.py — the
# rescue rate is 0 measured, 2 plausible.
#
# These tests drive the REAL ``_try_link_hop`` and assert on EMITTED EVENTS.
# They deliberately do not re-implement the allowance expression: the two
# pre-existing guards in test_timeout_salvage.py did exactly that and stayed
# green through the whole starvation regime.

_HOP_TEST_HTML = (
    "<html><body>"
    '<a href="/floorplans">Floor Plans</a>'
    '<a href="/availability">Availability</a>'
    '<a href="/apartments">Apartments</a>'
    '<a href="/pricing">Pricing</a>'
    '<a href="/units">Units</a>'
    '<a href="/rates">Rates</a>'
    '<a href="/rentals">Rentals</a>'
    "</body></html>"
)


def _hop_cap_env(
    monkeypatch: Any,
    *,
    budget_s: float,
    per_fetch_s: float,
    floor_s: float = 0.05,
) -> None:
    """Scale the hop clock into milliseconds so the suite stays fast.

    All three are read as MODULE ATTRIBUTES at call time (the ``from ... import``
    inside ``_try_link_hop`` re-resolves per hop), so setattr controls them
    without a reload. ``ENABLE_CRAWL_GET_GATE`` defaults TRUE and would fire a
    live ``probe_get`` per candidate — ``conftest`` turns that into an
    ``UnstubbedNetworkCall``, so it must be off.
    """
    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_BUDGET_S", budget_s)
    monkeypatch.setattr("ma_poc.config.feature_flags.LINK_HOP_PER_FETCH_S", per_fetch_s)
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", False)
    monkeypatch.setattr("ma_poc.pms.scraper._MIN_HOP_FETCH_S", floor_s)


def _detected_rentcafe() -> Any:
    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS

    return DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )


def _tarpit_fetch(
    calls: list[tuple[str, float]],
    *,
    fast_after: int | None = None,
) -> Any:
    """Fetch stub that hangs forever, recording (url, monotonic-at-entry).

    When *fast_after* is set, calls with index >= it return an OK body
    immediately — the "the hop that actually holds the roster" case.
    """

    async def _fetch(task: Any) -> Any:
        import asyncio as _aio
        import time as _t

        n = len(calls)
        calls.append((getattr(task, "url", ""), _t.monotonic()))
        if fast_after is not None and n >= fast_after:

            class _O:
                value = "OK"

            class _R:
                outcome = _O()
                status = 200
                body = b"<html></html>"
                final_url = task.url
                elapsed_ms = 1
                content_type = "text/html"
                captcha_detected = False
                error_signature = None
                identity_ua_hash = "test"
                render_mode = type("M", (), {"value": "RENDER"})()
                headers: dict = {}

                def to_dict(self) -> dict:
                    return {"outcome": "OK"}

            return _R()
        await _aio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    return _fetch


async def _run_hop_with_spy(
    fetch_stub: Any,
    events: list[dict[str, Any]],
    *,
    property_id: str,
    max_hops: int = 7,
    entry_html: str = _HOP_TEST_HTML,
) -> float:
    """Run the real ``_try_link_hop``, capturing every emitted payload.

    Returns the elapsed wall clock so the total-bound test can assert on it.
    """
    import time as _t
    from unittest.mock import patch

    from ma_poc.observability import events as obs_events
    from ma_poc.pms.scraper import _try_link_hop

    original_emit = obs_events.emit

    def _spy_emit(kind: Any, pid: str, **payload: Any) -> Any:
        events.append(dict(payload))
        return original_emit(kind, pid, **payload)

    t0 = _t.monotonic()
    with (
        patch("ma_poc.fetch.fetch", fetch_stub, create=True),
        patch.object(obs_events, "emit", _spy_emit),
    ):
        await _try_link_hop(
            entry_url="https://example.com/",
            entry_page_html=entry_html,
            detected=_detected_rentcafe(),
            profile=_profile_with_winning_url("https://example.com/floorplans"),
            expected_total_units=None,
            property_id=property_id,
            csv_row=None,
            max_hops=max_hops,
        )
    return _t.monotonic() - t0


@pytest.mark.asyncio
async def test_hop_fetch_cap_does_not_consume_whole_budget(monkeypatch: Any) -> None:
    """T1 — a single hop must not be allowed to spend the entire crawl budget.

    Regression target: ``_hop_remaining = max(_MIN_HOP_FETCH_S,
    _hop_deadline - time.monotonic())``. With a 5s budget and a 0.1s per-hop
    cap, a tarpitting hop #1 must be cancelled ~0.1s in, not ~5s in. Asserted
    on the emitted event (fetch entry → timeout emit), NOT on a re-derived
    expression, so reverting the cap fails this test: the elapsed becomes the
    full budget.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []
    import time as _t

    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-CAP-T1")

    assert calls, "hop #1 never fetched"
    caps = [e for e in events if e.get("outcome") == "HOP_FETCH_CAP_EXCEEDED"]
    assert caps, (
        "no HOP_FETCH_CAP_EXCEEDED emitted — the per-hop cap did not bind. "
        f"outcomes seen: {[e.get('outcome') for e in events]}"
    )
    first_cap = caps[0]
    # The allowance the code actually used, straight off the event.
    assert first_cap["allowance_s"] <= 0.2, (
        f"hop #1 allowance {first_cap['allowance_s']}s — the cap regressed to "
        "'all remaining budget'"
    )
    # And budget genuinely survived: that is the whole point of the cap.
    assert first_cap["budget_remaining_s"] > 5.0 / 2, (
        f"only {first_cap['budget_remaining_s']}s of the 5s budget left after "
        "hop #1 — the hop ate the crawl"
    )
    # Wall-clock cross-check: hop #1 was cancelled early in the budget.
    hop1_start = calls[0][1]
    assert _t.monotonic() - hop1_start < 5.0 / 2, "hop #1 ran for half the budget"


@pytest.mark.asyncio
async def test_hop_cap_releases_loop_to_next_candidate(monkeypatch: Any) -> None:
    """T2 — the cap must not be inert: a capped-out hop CONTINUES the loop.

    This is the load-bearing half of the change. Capping the allowance while
    the timeout handler still ``break``s frees budget that nothing can spend —
    measurably so: on all 8 HOP_FETCH_BUDGET_EXCEEDED events in the
    2026-07-27 run, ZERO further hops were fetched despite 3-7 unvisited
    candidates in the matching LINK_HOP_STARTED payload.

    Structurally impossible before the ``break`` → ``continue`` edit.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=1), events, property_id="HOP-CAP-T2"
    )

    outcomes = [(e.get("hop_index"), e.get("outcome")) for e in events if e.get("outcome")]
    cap_positions = [i for i, (_, o) in enumerate(outcomes) if o == "HOP_FETCH_CAP_EXCEEDED"]
    assert len(cap_positions) == 1, f"expected exactly one capped hop; got {outcomes}"
    later_ok = [
        (h, o)
        for (h, o) in outcomes[cap_positions[0] + 1 :]
        if o == "OK"
    ]
    assert later_ok, (
        "a capped-out hop must release the loop to the next candidate; the "
        f"loop stopped instead. outcomes: {outcomes}"
    )
    assert later_ok[0][0] > outcomes[cap_positions[0]][0], (
        "the recovered hop must be a LATER hop_index than the capped one"
    )
    assert len(calls) >= 2, f"only {len(calls)} fetch(es) fired — `continue` did not land"


@pytest.mark.asyncio
async def test_hop_cap_preserves_total_wall_clock_bound(monkeypatch: Any) -> None:
    """T3 — `continue` must not re-open the hang the deadline was added to close.

    Every candidate tarpits. The bound is unchanged in form:
    ``LINK_HOP_BUDGET_S + _MIN_HOP_FETCH_S + one extraction`` — the loop-top
    deadline check still refuses to ADMIT a hop past the deadline, and the
    capped allowance is pointwise <= the old one. Both halves are asserted:
    the bound alone would pass with the old ``break``.
    """
    budget_s, floor_s = 2.0, 0.05
    _hop_cap_env(monkeypatch, budget_s=budget_s, per_fetch_s=0.3, floor_s=floor_s)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []
    max_hops = 7

    elapsed = await _run_hop_with_spy(
        _tarpit_fetch(calls), events, property_id="HOP-CAP-T3", max_hops=max_hops
    )

    # Generous slack: this asserts "bounded", not "precisely 2.05s".
    assert elapsed <= budget_s + floor_s + 2.0, (
        f"hop loop ran {elapsed:.2f}s against a {budget_s}s budget — `continue` "
        "made the wall clock unbounded"
    )
    assert len(calls) >= 2, (
        f"only {len(calls)} fetch(es) — `continue` did not run, so this test is "
        "not actually exercising the bound it claims to"
    )
    # max_hops + max_dynamic_appends (== max_hops) is the structural ceiling.
    assert len(calls) <= max_hops * 2, (
        f"{len(calls)} fetches exceeds the max_hops+dynamic-appends ceiling"
    )


@pytest.mark.asyncio
async def test_deadline_bound_timeout_still_breaks(monkeypatch: Any) -> None:
    """T4a — when the DEADLINE (not the cap) bound the fetch, fail fast.

    Budget 0.4s, cap 10s → the deadline is the binding constraint, so the
    allowance is not capped, ``_cap_bound`` is False, and the 2026-07-25
    fail-fast ``break`` must be preserved verbatim: exactly one fetch, the
    original HOP_FETCH_BUDGET_EXCEEDED outcome, no continuation.
    """
    _hop_cap_env(monkeypatch, budget_s=0.4, per_fetch_s=10.0, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-CAP-T4A")

    outcomes = [e.get("outcome") for e in events if e.get("outcome")]
    assert "HOP_FETCH_BUDGET_EXCEEDED" in outcomes, (
        f"deadline-bound timeout must keep the original outcome; got {outcomes}"
    )
    assert "HOP_FETCH_CAP_EXCEEDED" not in outcomes, (
        "the cap must not claim a timeout the deadline caused"
    )
    assert len(calls) == 1, (
        f"deadline-bound timeout must fail fast; {len(calls)} fetches fired"
    )


@pytest.mark.asyncio
async def test_inert_cap_leaves_deadline_guard_intact(monkeypatch: Any) -> None:
    """T4b — a cap larger than the budget changes nothing.

    With budget 0 the loop-top guard fires before any fetch, exactly as it did
    before the cap existed: HOP_BUDGET_EXCEEDED, zero fetches, and no
    HOP_FETCH_CAP_EXCEEDED anywhere.
    """
    _hop_cap_env(monkeypatch, budget_s=0, per_fetch_s=100_000, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-CAP-T4B")

    outcomes = [e.get("outcome") for e in events if e.get("outcome")]
    assert calls == [], f"budget=0 must fire ZERO hops; got {calls}"
    assert "HOP_BUDGET_EXCEEDED" in outcomes, (
        f"the loop-top deadline guard must still fire; got {outcomes}"
    )
    assert "HOP_FETCH_CAP_EXCEEDED" not in outcomes


@pytest.mark.asyncio
async def test_cap_disabled_restores_pre_change_behaviour(monkeypatch: Any) -> None:
    """T4c — LINK_HOP_PER_FETCH_S=0 is a total kill switch, not a partial one.

    The env var is the revert path (no redeploy). With the cap off, a
    tarpitting hop #1 consumes the whole budget and the loop breaks — the
    pre-2026-07-27 behaviour this change replaces. Also proves T1/T2 are
    detecting the cap rather than some unrelated fixture effect.
    """
    _hop_cap_env(monkeypatch, budget_s=1.0, per_fetch_s=0, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=1), events, property_id="HOP-CAP-T4C"
    )

    outcomes = [e.get("outcome") for e in events if e.get("outcome")]
    assert "HOP_FETCH_CAP_EXCEEDED" not in outcomes, "cap=0 must disable the cap"
    assert "HOP_FETCH_BUDGET_EXCEEDED" in outcomes
    assert len(calls) == 1, (
        f"cap=0 must reproduce the old starvation exactly (one hop, then break); "
        f"got {len(calls)} fetches"
    )


def test_hop_fetch_allowance_table() -> None:
    """T5 — the allowance function itself, including the flag edge cases.

    Called against the real ``_hop_fetch_allowance``; there is no mirrored
    expression here. The ``cap=0 → 140.0`` row is the trap worth naming: if
    "disabled" fell through the clamp it would resolve to the 20s floor, making
    the natural "off" value the most aggressive setting in the whole range.
    """
    from ma_poc.pms.scraper import _MIN_HOP_FETCH_S, _hop_fetch_allowance

    assert _MIN_HOP_FETCH_S == 20.0, "table below is written against a 20s floor"

    # (remaining, cap) -> expected
    assert _hop_fetch_allowance(-100.0, 90.0) == 20.0  # deadline blown → floor
    assert _hop_fetch_allowance(140.0, 90.0) == 90.0  # cap binds
    assert _hop_fetch_allowance(40.0, 90.0) == 40.0  # deadline binds
    assert _hop_fetch_allowance(140.0, 0.0) == 140.0  # DISABLED != 20s
    assert _hop_fetch_allowance(140.0, -5.0) == 140.0  # negative == disabled
    assert _hop_fetch_allowance(140.0, 200.0) == 140.0  # cap above budget → inert
    assert _hop_fetch_allowance(10.0, 90.0) == 20.0  # floor still wins


def test_hop_fetch_allowance_warns_instead_of_swallowing_a_sub_floor_cap() -> None:
    """T5b — a cap below the floor is clamped UP and SAID SO.

    Without the warning, ``LINK_HOP_PER_FETCH_S=10`` and ``=0`` both resolve to
    20.0 while meaning opposite things, and a mis-set flag reads exactly like a
    working one.
    """
    from ma_poc.pms.scraper import _hop_fetch_allowance

    with pytest.warns(UserWarning, match="below the _MIN_HOP_FETCH_S floor"):
        assert _hop_fetch_allowance(140.0, 10.0) == 20.0


def test_shipped_cap_default_is_not_a_no_op() -> None:
    """T5c — the shipped defaults must actually be able to bind.

    A cap >= the whole budget is inert by construction; a cap below the floor
    is clamped. Either would ship a change that measures as a no-op.
    """
    from ma_poc.config.feature_flags import LINK_HOP_BUDGET_S, LINK_HOP_PER_FETCH_S
    from ma_poc.pms.scraper import _MIN_HOP_FETCH_S

    assert LINK_HOP_PER_FETCH_S > 0, "shipped default must have the cap ENABLED"
    assert LINK_HOP_PER_FETCH_S < LINK_HOP_BUDGET_S, (
        f"LINK_HOP_PER_FETCH_S={LINK_HOP_PER_FETCH_S} >= "
        f"LINK_HOP_BUDGET_S={LINK_HOP_BUDGET_S} — the cap can never bind"
    )
    assert LINK_HOP_PER_FETCH_S >= _MIN_HOP_FETCH_S, (
        "shipped default would be clamped up, i.e. it is not the value in force"
    )
    # A cap must leave room for at least one more real attempt after it binds.
    assert LINK_HOP_BUDGET_S - LINK_HOP_PER_FETCH_S >= _MIN_HOP_FETCH_S, (
        "capping hop #1 must leave at least one floor's worth of budget for the "
        "hop that holds the roster — otherwise `continue` has nothing to spend"
    )


@pytest.mark.asyncio
async def test_hop_telemetry_makes_the_cap_adjudicable(monkeypatch: Any) -> None:
    """T7 — the canary must be able to tell a working cap from an inert one.

    Before 2026-07-27 the only signal was ``outcome=HOP_FETCH_BUDGET_EXCEEDED``
    with no allowance, no remaining budget, no queue wait and no session index —
    every one of the eight starved allowances in the sample100 run had to be
    reconstructed from ``fetch.started`` deltas. These four fields are what make
    "the cap freed budget and a later hop spent it" directly observable.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=1), events, property_id="HOP-CAP-T7"
    )

    started = [e for e in events if "candidates" in e]
    assert started, "LINK_HOP_STARTED not emitted"
    assert started[0]["deadline_source"] == "fresh"
    assert started[0]["session_index"] == 1

    capped = [e for e in events if e.get("outcome") == "HOP_FETCH_CAP_EXCEEDED"]
    assert capped, "cap never bound — telemetry assertions below are vacuous"
    for key in ("allowance_s", "budget_remaining_s", "queue_wait_ms", "session_index"):
        assert key in capped[0], f"{key} missing from the capped-hop event"
    assert capped[0]["queue_wait_ms"] >= 0

    ok = [e for e in events if e.get("outcome") == "OK"]
    assert ok, "the continuation hop never produced an OK event"
    # Post-cap continuation count — THE metric. Structurally zero before the
    # break -> continue edit; a canary reading zero here means it did not land.
    cap_at = events.index(capped[0])
    assert any(e.get("outcome") == "OK" for e in events[cap_at + 1 :])
    assert "queue_wait_ms" in ok[0] and "allowance_s" in ok[0]


@pytest.mark.asyncio
async def test_session_index_exposes_the_reentry_hole(monkeypatch: Any) -> None:
    """T8 — a second scrape_jugnu call is visible as session_index=2.

    ``shared_budget`` is re-created per scrape_jugnu call, so the deadline does
    NOT survive (a documented, deliberately-unfixed hole — see
    test_timeout_salvage.py). The counter lives in the caller-owned
    ``_external_partial_ref``, which DOES survive, so the extra admission window
    shows up in the event stream instead of only in artifact archaeology.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    external: dict[str, Any] = {}
    seen: list[int] = []

    for pid in ("SESSION-1", "SESSION-2"):
        events: list[dict[str, Any]] = []
        calls: list[tuple[str, float]] = []
        from unittest.mock import patch

        from ma_poc.observability import events as obs_events
        from ma_poc.pms.scraper import _try_link_hop

        original_emit = obs_events.emit

        def _spy(kind: Any, p: str, **payload: Any) -> Any:
            events.append(dict(payload))
            return original_emit(kind, p, **payload)

        with (
            patch("ma_poc.fetch.fetch", _tarpit_fetch(calls, fast_after=0), create=True),
            patch.object(obs_events, "emit", _spy),
        ):
            await _try_link_hop(
                entry_url="https://example.com/",
                entry_page_html=_HOP_TEST_HTML,
                detected=_detected_rentcafe(),
                profile=_profile_with_winning_url("https://example.com/floorplans"),
                expected_total_units=None,
                property_id=pid,
                csv_row=None,
                max_hops=3,
                # A FRESH budget dict per call — exactly what scrape_jugnu builds.
                shared_budget={"_external_partial_ref": external},
            )
        started = [e for e in events if "candidates" in e]
        assert started, f"{pid}: LINK_HOP_STARTED not emitted"
        seen.append(started[0]["session_index"])

    assert seen == [1, 2], (
        f"session_index must count admission windows across scrape_jugnu calls; "
        f"got {seen}"
    )


# ── the crawl gate is charged against the hop deadline (2026-07-27 review) ──
#
# ``_crawl_get_gate_should_skip`` runs BETWEEN the loop-top deadline check and
# the fetch admission, so every second it spends is charged against
# ``_hop_deadline`` — but until this change nothing bounded it. ``probe_get``'s
# own ``timeout=10`` covers the HTTP call, not the ``asyncio.to_thread``
# queueing in front of it on a shared executor.
#
# Measured 2026-07-27 (run …-sample100-7fc8b4c): property 27577 spent 256.6s
# between LINK_HOP_STARTED and its first ``fetch.started`` with no intervening
# event, was admitted 106.6s PAST its 150s deadline on the _MIN_HOP_FETCH_S
# floor, and LINK_HOP_PER_FETCH_S was therefore INERT on it. A per-fetch cap
# cannot hold a budget spent before the fetch begins.
#
# These tests patch the gate helper itself (never ``probe_get``) so no network
# stub can fail open, and they run with ENABLE_CRAWL_GET_GATE **on** — every
# other hop test in this file disables it, which is exactly why the hole
# survived.


def _slow_gate(seconds: float, *, skip: bool = False) -> Any:
    """Sync gate stub that burns *seconds*, like a queued ``probe_get``."""

    def _gate(url: str) -> bool:
        import time as _t

        _t.sleep(seconds)
        return skip

    return _gate


@pytest.mark.asyncio
async def test_crawl_gate_cannot_outlive_the_hop_deadline(monkeypatch: Any) -> None:
    """G1 — a tarpitting gate must not blow the crawl budget before the fetch.

    Budget 2.0s, gate 5.0s per call. Unbounded, the loop admitted its first
    fetch at t+5.0s — 3s past a deadline it was supposed to respect — and the
    allowance had already collapsed to the floor. Asserted on the fetch's
    ADMISSION TIME, not on a re-derived expression.
    """
    _hop_cap_env(monkeypatch, budget_s=2.0, per_fetch_s=1.0, floor_s=0.5)
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", True)
    monkeypatch.setattr("ma_poc.pms.scraper._CRAWL_GET_GATE_BUDGET_S", 0.25)
    monkeypatch.setattr("ma_poc.pms.scraper._crawl_get_gate_should_skip", _slow_gate(5.0))

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []
    import time as _t

    t0 = _t.monotonic()
    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-GATE-G1")

    assert calls, "no fetch was ever admitted"
    admitted_at = calls[0][1] - t0
    assert admitted_at < 2.0, (
        f"first fetch admitted at t+{admitted_at:.2f}s against a 2.0s deadline — "
        "the cheap-GET gate is spending budget nothing bounds"
    )


@pytest.mark.asyncio
async def test_crawl_gate_timeout_fails_open(monkeypatch: Any) -> None:
    """G2 — bounding the gate must not turn a slow probe into a skipped page.

    The helper documents fail-OPEN on every error; a timeout is one more error.
    A gate that timed out CLOSED would silently drop candidates whenever the
    executor is busy — a far worse failure than paying for the render.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", True)
    monkeypatch.setattr("ma_poc.pms.scraper._CRAWL_GET_GATE_BUDGET_S", 0.05)
    # skip=True: were the timeout not fail-open, this candidate WOULD be gated.
    monkeypatch.setattr(
        "ma_poc.pms.scraper._crawl_get_gate_should_skip", _slow_gate(1.0, skip=True)
    )

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=0), events, property_id="HOP-GATE-G2"
    )

    outcomes = [e.get("outcome") for e in events if e.get("outcome")]
    assert "DEAD_URL_GATED" not in outcomes, (
        f"a timed-out gate skipped the candidate instead of failing open: {outcomes}"
    )
    assert calls, "fail-open must still fetch the candidate"


@pytest.mark.asyncio
async def test_gate_elapsed_is_reported_on_a_gated_candidate(monkeypatch: Any) -> None:
    """G3 — DEAD_URL_GATED carries the gate's own cost.

    Without it a saturated executor is visible only as a GAP between events,
    which is how 27577's 256.6s went unnoticed for a full run.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=1.0, floor_s=0.05)
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", True)
    monkeypatch.setattr(
        "ma_poc.pms.scraper._crawl_get_gate_should_skip", _slow_gate(0.05, skip=True)
    )

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-GATE-G3")

    gated = [e for e in events if e.get("outcome") == "DEAD_URL_GATED"]
    assert gated, "the gate never fired"
    assert "gate_elapsed_ms" in gated[0], "gate cost is still invisible"
    assert gated[0]["gate_elapsed_ms"] >= 40, gated[0]["gate_elapsed_ms"]


# ── headroom on the post-cap `continue` (2026-07-27 review) ─────────────────


@pytest.mark.asyncio
async def test_continue_requires_headroom_for_gate_plus_floor(monkeypatch: Any) -> None:
    """G4 — `continue` with a sliver of budget re-creates the RCA's own bug.

    Admitting the next hop costs a bounded gate before the fetch even starts,
    so continuing with less than gate+floor left buys a fetch admitted PAST the
    deadline on the _MIN_HOP_FETCH_S floor — the exact degenerate admission the
    2026-07-25 RCA was written about. Measured 2026-07-27, three of the five
    hops the 90s cap binds (97935 2.6s, 278371 26.1s, 30747 30.8s of freed
    budget) fall below floor+gate=32s and would have bought exactly that.
    """
    # 1.0s budget, 0.6s cap, 0.3 floor + 0.3 gate = 0.6s headroom. Hop #1 is
    # cap-bound and leaves ~0.4s — real budget, but less than an admission costs.
    _hop_cap_env(monkeypatch, budget_s=1.0, per_fetch_s=0.6, floor_s=0.3)
    monkeypatch.setattr("ma_poc.config.feature_flags.ENABLE_CRAWL_GET_GATE", True)
    monkeypatch.setattr("ma_poc.pms.scraper._CRAWL_GET_GATE_BUDGET_S", 0.3)
    monkeypatch.setattr("ma_poc.pms.scraper._crawl_get_gate_should_skip", _slow_gate(0.0))

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=1), events, property_id="HOP-CAP-G4"
    )

    capped = [e for e in events if e.get("outcome") == "HOP_FETCH_CAP_EXCEEDED"]
    assert capped, "the cap did not bind — this test is vacuous"
    assert capped[0]["budget_remaining_s"] > 0, (
        "premise: budget genuinely survived the cap"
    )
    assert len(calls) == 1, (
        f"{len(calls)} fetches — the loop continued on budget too thin to fund "
        "an admission, so the next hop is admitted past the deadline on the floor"
    )


@pytest.mark.asyncio
async def test_capped_timeout_reports_actual_fetch_elapsed(monkeypatch: Any) -> None:
    """G5 — the event must show what the fetch ACTUALLY cost, not its allowance.

    ``asyncio.wait_for`` is not a wall-clock bound here: a cancelled RENDER
    unwinds through Playwright IPC, and the measured overshoot past the
    allowance in the 2026-07-27 run was 0.9-144.4s (median 13.1s), charged
    against the very budget the cap frees. ``allowance_s`` alone cannot show
    that the bound did not hold.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(_tarpit_fetch(calls), events, property_id="HOP-CAP-G5")

    capped = [e for e in events if e.get("outcome") == "HOP_FETCH_CAP_EXCEEDED"]
    assert capped, "the cap did not bind"
    assert "fetch_elapsed_s" in capped[0], (
        "no fetch_elapsed_s — allowance_s alone cannot distinguish a cap that "
        "held from one that overshot by 144s"
    )
    assert capped[0]["fetch_elapsed_s"] >= capped[0]["allowance_s"] - 0.05


# ── freed budget must not be spent re-fetching what just tarpitted ──────────


def test_hop_url_key_collapses_scheme_www_and_trailing_slash() -> None:
    """G6 — the normalisation table, against the real ``_hop_url_key``.

    Drawn from the two properties the cap is meant to rescue: 48389's queue was
    ``https://villagegatenc.com/floor-plans/`` then
    ``http://www.villagegatenc.com/floor-plans/``; 256603's was
    ``…/floorplans/`` then ``…/floorplans``. ``visited`` is an exact-string set,
    so neither pair deduped.
    """
    from ma_poc.pms.scraper import _hop_url_key as k

    assert k("https://villagegatenc.com/floor-plans/") == k(
        "http://www.villagegatenc.com/floor-plans/"
    )
    assert k("https://wyldewoodgosling.com/floorplans/") == k(
        "https://wyldewoodgosling.com/floorplans"
    )
    assert k("https://example.com:443/a") == k("http://example.com:80/a")
    # Query is identity: per-unit application shells are genuinely different.
    assert k("https://x.com/a?UnitId=3") != k("https://x.com/a?UnitId=4")
    # Different paths stay different; a hostless string degrades, never raises.
    assert k("https://x.com/a") != k("https://x.com/b")
    assert k("not a url") == "not a url"


@pytest.mark.asyncio
async def test_capped_hop_does_not_refetch_a_variant_of_itself(monkeypatch: Any) -> None:
    """G7 — budget freed by the cap must buy a DIFFERENT page.

    Both properties the cap could plausibly rescue queue a near-duplicate of
    the URL that just tarpitted directly behind it, on the same origin that
    just demonstrated >147s latency. Spending the freed budget there is the
    rescue quietly failing.
    """
    _hop_cap_env(monkeypatch, budget_s=5.0, per_fetch_s=0.1, floor_s=0.05)

    calls: list[tuple[str, float]] = []
    events: list[dict[str, Any]] = []

    await _run_hop_with_spy(
        _tarpit_fetch(calls, fast_after=1),
        events,
        property_id="HOP-CAP-G7",
        entry_html=(
            "<html><body>"
            '<a href="https://example.com/floorplans/">Floor Plans</a>'
            '<a href="http://www.example.com/floorplans">Floor Plans</a>'
            '<a href="https://example.com/availability">Availability</a>'
            "</body></html>"
        ),
    )

    fetched = [u for u, _ in calls]
    assert len(fetched) >= 2, f"the loop never continued: {fetched}"
    from ma_poc.pms.scraper import _hop_url_key

    keys = [_hop_url_key(u) for u in fetched]
    assert len(keys) == len(set(keys)), (
        f"the freed budget was spent re-fetching a variant of the tarpit: {fetched}"
    )
    skipped = [e for e in events if e.get("outcome") == "HOP_SKIPPED_TARPIT_VARIANT"]
    assert skipped, "the variant skip is not observable in the event stream"
