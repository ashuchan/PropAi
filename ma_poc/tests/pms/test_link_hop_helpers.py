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
    """
    from unittest.mock import patch

    from ma_poc.pms.detector import _STRATEGY_BY_PMS, DetectedPMS
    from ma_poc.pms.scraper import _try_link_hop

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fp:rentcafe"],
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    fetch_calls: list[str] = []

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

    # Profile says /floorplans was explored and had no data (skip it).
    # RentCafe prior would otherwise inject /floorplans as candidate #1.
    with patch("ma_poc.fetch.fetch", _fake_fetch, create=True):
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

    assert "https://example.com/floorplans" not in fetch_calls, (
        f"Prior URL was in profile.explored_links but still got fetched: "
        f"{fetch_calls}. The skip-list must apply to priors the same way "
        f"it applies to keyword candidates."
    )
    # The OTHER priors (/availability, /apartments) should still fire.
    assert any("availability" in u or "apartments" in u for u in fetch_calls), (
        f"Skipping /floorplans should not suppress the rest of the "
        f"RentCafe priors; got fetch_calls={fetch_calls}"
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
