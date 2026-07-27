"""Regression test — link-hop must recover via PMS fingerprint priors
when the profile is empty AND the homepage HTML yields no keyword candidates.

On 2026-05-11, 1633 properties failed in this exact shape: the LLM rescue
ran and returned "no candidates after filtering"; the runner then invoked
``_try_link_hop`` which short-circuited at ``scraper.py:1331`` because
``ranked == []`` after profile-top injection + keyword ranking + visited/
explored filtering. The 1633 properties all share three preconditions:

  1. Profile starvation: ``navigation.winning_page_url`` and
     ``availability_links`` are empty (collateral from days of pre-639ccc3
     profile-save swallowing).
  2. SPA marketing-shell homepage: the rendered HTML has no useful anchor
     tags — React/Vue routers only emit them after hydration, but the
     runner snapshots HTML at ``networkidle`` before that.
  3. PMS fingerprint match: ``extract.detector_signals.fingerprints_matched``
     contains a known PMS (RentCafe in 558 cases). The runner KNOWS the
     site is RentCafe-templated — RentCafe properties expose units at
     ``/floorplans`` — but no code path uses that prior.

This test asserts the recovery behaviour: when (1) + (2) hold and a strong
PMS fingerprint is present, ``_try_link_hop`` must produce at least one
candidate derived from the PMS template's well-known sub-paths and attempt
to fetch it. See docs/2026_05_11_regressions_fix_design.md, section
"Bug B — _try_link_hop returns None when profile + keyword ranker both
empty" for the four-source priority order (LLM hint → sitemap → PMS
prior → captured-API path inspection).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


# SPA marketing-shell homepage. Notable: every anchor here is either
# blocked by ``_LINK_SKIP_PATTERNS`` (social, contact, careers, sitemap)
# or has zero score from the keyword ranker. The "Floor Plans" link the
# router would emit after hydration is conspicuously absent — matching
# the production capture for property 10141 wymberlycrossing.com.
_SPA_SHELL_HTML = """<!DOCTYPE html>
<html lang="en"><head>
    <title>Wymberly Crossing Apartments</title>
    <script src="https://cdngeneralmvc.rentcafe.com/widget.js"></script>
</head><body>
    <header>
      <a href="/contact">Contact</a>
      <a href="/careers">Careers</a>
      <a href="https://facebook.com/wymberly">Facebook</a>
      <a href="https://instagram.com/wymberly">Instagram</a>
      <a href="/sitemap">Sitemap</a>
      <a href="/privacy">Privacy</a>
    </header>
    <main>
      <div id="root">
        <!-- React/Vue router renders here after hydration. The runner
             snapshots HTML at networkidle BEFORE this populates, so the
             /floorplans link the user would see never reaches the
             keyword ranker. -->
      </div>
    </main>
</body></html>
"""


class _FakeProbeResponse:
    """curl_cffi-response stand-in for the ``_probe`` seam.

    ``_try_link_hop`` gates each prior-derived candidate with a cheap
    ``probe_get`` (``scraper._crawl_get_gate_should_skip``) before the RENDER
    sub-fetch, and the recursive ``scrape()`` curl-refetches sub-paths for its
    detection rescue. A 200 with an empty document is neutral for both: the
    gate only retires ``404``/``410`` + <10 KB bodies, so the RentCafe priors
    still reach ``_fake_fetch`` and land in ``fetch_calls`` — a 404-shaped
    stub would gate them away and defeat what this test pins.
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
    only.
    """

    def _fake_probe_get(url: str, *args: Any, **kwargs: Any) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)


class _StarvedNavigation:
    """A profile.navigation block in the state Bug-1's collateral left it:
    blocked_endpoints may exist from prior runs, but winning_page_url and
    availability_links were never persisted because the saving call
    crashed on the json_paths validator."""

    winning_page_url = None
    availability_links: list[str] = []
    explored_links: list[str] = []


class _StarvedProfile:
    navigation = _StarvedNavigation()
    api_hints = None  # not consulted by _try_link_hop


@pytest.mark.asyncio
async def test_link_hop_recovers_via_pms_prior_when_profile_starved() -> None:
    """When profile is empty and HTML yields no keyword candidates,
    ``_try_link_hop`` must still produce at least one candidate from the
    detected PMS's well-known sub-paths (rentcafe → /floorplans, etc.).

    Failing today: _try_link_hop returns None at scraper.py:1331 without
    emitting LINK_HOP_STARTED. The fetcher is never invoked. The 1633
    production failures on 2026-05-11 all match this shape — same shard,
    same trace, same outcome.

    The mocked ``jugnu_fetch`` below would record any sub-URL the runner
    attempts to fetch. The assertion is on ``fetch_calls`` rather than on
    the return value because what matters in production is "did we try
    SOMETHING, anything, given a strong PMS prior?" — even if the
    specific URL guess is wrong, an attempt creates the opportunity for
    the cascade (or its own carryforward) to make progress, whereas a
    silent None means the property is dead for this run.
    """
    from ma_poc.pms.detector import DetectedPMS, _STRATEGY_BY_PMS
    from ma_poc.pms.scraper import _try_link_hop

    detected = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=[
            "fingerprint_matched:rentcafe",
            "script_src:cdngeneralmvc.rentcafe.com",
        ],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    fetch_calls: list[str] = []

    async def _fake_fetch(task: Any) -> Any:
        """Stand-in for ``ma_poc.fetch.fetch``. Records every URL the
        runner attempted to fetch and returns a benign empty body so
        the recursive ``scrape()`` call inside link-hop is short-lived.
        """
        fetch_calls.append(getattr(task, "url", str(task)))

        class _Outcome:
            value = "OK"

        class _Result:
            outcome = _Outcome()
            status = 200
            body = b"<html><body></body></html>"
            final_url = getattr(task, "url", "")
            elapsed_ms = 0
            content_type = "text/html"
            captcha_detected = False
            error_signature = None
            identity_ua_hash = "test"
            render_mode = type("M", (), {"value": "RENDER"})()
            headers: dict = {}

            def to_dict(self) -> dict:
                return {"outcome": "OK", "status": 200}

        return _Result()

    # Patch the lazy import inside ``_try_link_hop`` so no real fetch
    # happens. The patch target matches the module-level alias the
    # function imports at line 1334.
    with patch("ma_poc.fetch.fetch", _fake_fetch, create=True):
        result = await _try_link_hop(
            entry_url="https://www.wymberlycrossing.com/",
            entry_page_html=_SPA_SHELL_HTML,
            detected=detected,
            profile=_StarvedProfile(),
            expected_total_units=None,
            property_id="TEST-WYMBERLY",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=None,  # LLM did not emit one (10141 case)
            visited_urls=None,
            shared_budget=None,
        )

    assert fetch_calls, (
        "_try_link_hop returned without attempting any sub-page fetch.\n\n"
        "Preconditions in this test mirror production for property 10141 "
        "(and 1632 sibling failures on 2026-05-11):\n"
        "  - profile.navigation.winning_page_url = None\n"
        "  - profile.navigation.availability_links = []\n"
        "  - homepage HTML is a SPA marketing shell with no useful anchors\n"
        "  - detector.fingerprints_matched = ['rentcafe'] (conf 0.9)\n\n"
        "Required behaviour: the runner must use the matched PMS fingerprint "
        "as a prior — RentCafe properties expose units at /floorplans by "
        "template — to inject at least one candidate into the link-hop "
        "ranker. See docs/run_2026_05_11_manual_analysis.md for the "
        "layered fallback (LLM nav hint → sitemap → PMS prior → captured-API "
        "path inspection).\n\n"
        f"Observed: _try_link_hop returned {result!r}, no fetches attempted."
    )

    # Bonus assertion (will only fire if the test above passes): if a
    # candidate WAS produced, at least one should point at a well-known
    # RentCafe sub-path. Documents the *kind* of fallback that closes
    # the gap, without dictating the exact URL list.
    rentcafe_priors = ("/floorplan", "/availab", "/apartments")
    assert any(any(p in url.lower() for p in rentcafe_priors) for url in fetch_calls), (
        f"Link-hop produced candidates but none matched the RentCafe "
        f"template's known availability paths {rentcafe_priors}. "
        f"Attempted URLs: {fetch_calls}"
    )
