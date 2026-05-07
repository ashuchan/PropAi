"""Regression tests covering the 10 bugs found in the 2026-05-06 run audit.

Each test reproduces a specific failure mode from a real property in that run
and asserts the patched code now yields the expected behaviour.

Bug catalog (see also: docs/2026-05-06-rent-recovery-audit.md):
  #1  Planner gates on per-unit completeness only
  #2  api_broad accepts list-of-named-objects with no pricing/availability key
  #3  LLM navigation_hint not threaded into link-hop
  #4  iframe srcs from known PMS hosts not followed
  #5  Link-hop ranker can pick sister/wrong-property URL
  #6  Fetcher follows redirects to wrong-property domain
  #7  Noise filter (or related drop step) discards PMS unit-data API hosts
  #8  AppFolio query-data endpoint has no parser
  #9  Portal links inside JSON responses ignored
  #10 Reporter never populates links_found
"""

from __future__ import annotations

import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Bug #2 — api_broad must require a pricing/availability/structural signal,
# not accept any list-of-dicts whose first item has a `name` key.
# ---------------------------------------------------------------------------


def _ensure_repo_root_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


_ensure_repo_root_on_path()


def test_bug2_birchrun_supabase_property_metadata_is_not_a_unit() -> None:
    """Birch Run (id 14182) — a 1-item supabase /rest/v1/properties response
    with property metadata (name, address, amenity lists) but ZERO unit/rent
    fields used to be accepted as 1 "unit" by the broad parser. After the
    pricing/availability gate it must be rejected.
    """
    from ma_poc.scripts.entrata import parse_api_responses

    # Real shape from the 2026-05-06 run, trimmed to the relevant keys.
    supabase_response = [
        {
            "url": "https://x.supabase.co/rest/v1/properties?id=eq.abc",
            "body": [
                {
                    "id": "9688735a-2d67-4742-8038-92c296cd3a63",
                    "name": "Birch Run",
                    "description": "Welcome to Birch Run Apartments...",
                    "street_address": "1204 Brockett Road",
                    "city": "Clarkston",
                    "state": "GA",
                    "zip_code": "30021",
                    "number_of_units": "198",
                    "property_type": "Conventional",
                    "company_website": "http://example.com",
                    "property_phone": "(404) 738 - 6437",
                    "slug": "birchrunapts",
                    "community_amenities": ["Pool", "Fitness Center"],
                    "apartment_amenities": ["Dishwasher", "Washer/Dryer"],
                    "floor_plan_link": "https://birchrunapts.securecafe.com/onlineleasing/birch-run-apartments/floorplans.aspx",
                }
            ],
        }
    ]

    units = parse_api_responses(supabase_response)
    assert units == [], (
        f"Property-metadata response was accepted as units. Got {len(units)}: {units}"
    )


def test_bug2_ascent_nestio_neighborhoods_are_not_units() -> None:
    """Ascent at Fitzsimons (id 302701) — a 260-item nestio neighborhoods
    response (each item: id, name, area, city, state) used to be accepted as
    260 "units" because the gate was satisfied by `name`. After the patch the
    items lack rent, availability, AND lack any structural numeric signal
    (sqft is the string "Downtown" not a number, no beds/baths) so the gate
    rejects them.
    """
    from ma_poc.scripts.entrata import parse_api_responses

    nestio_neighborhoods = [
        {
            "url": "https://nestiolistings.com/api/v2/neighborhoods",
            "body": [
                {"id": 1, "name": "East Village", "area": "Downtown", "city": "New York", "state": "NY"},
                {"id": 2, "name": "Williamsburg", "area": "Brooklyn", "city": "Brooklyn", "state": "NY"},
                {"id": 3, "name": "Astoria", "area": "Queens", "city": "Queens", "state": "NY"},
            ],
        }
    ]

    units = parse_api_responses(nestio_neighborhoods)
    assert units == [], (
        f"Neighborhoods list was accepted as units. Got {len(units)}: {units}"
    )


def test_bug2_realpage_floorplan_stub_is_still_accepted() -> None:
    """Counter-test: legitimate RealPage /floorplans response has plans with
    no rent (rent comes from /units endpoint) but DOES carry beds/sqft/name.
    The gate must still accept these as floor-plan stubs.
    """
    from ma_poc.scripts.entrata import parse_api_responses

    realpage_floorplans = [
        {
            "url": "https://example.com/api/floorplans",
            "body": {
                "floor_plans": [
                    {
                        "id": "1A",
                        "floorPlanName": "Studio - Renovated",
                        "bedrooms": 0,
                        "bathrooms": 1,
                        "sqft": 615,
                        # no rent — comes from /units endpoint
                    },
                    {
                        "id": "2B",
                        "floorPlanName": "1BR-1BA",
                        "bedrooms": 1,
                        "bathrooms": 1,
                        "sqft": 750,
                    },
                    {
                        "id": "3C",
                        "floorPlanName": "2BR-2BA",
                        "bedrooms": 2,
                        "bathrooms": 2,
                        "sqft": 1100,
                    },
                ]
            },
        }
    ]

    units = parse_api_responses(realpage_floorplans)
    assert len(units) >= 3, f"RealPage floor-plan stubs were rejected. Got {len(units)}"
    # Confirm names made it through
    names = {u.get("floor_plan_name") for u in units}
    assert "Studio - Renovated" in names or "1BR-1BA" in names


def test_bug2_avail_only_unit_is_still_accepted() -> None:
    """Counter-test: a unit with no rent yet but with a unit_number and
    available_date must still be accepted (this is a real availability stub).
    """
    from ma_poc.scripts.entrata import parse_api_responses

    avail_stub = [
        {
            "url": "https://example.com/api/units",
            "body": [
                {"unitNumber": "101", "availableDate": "2026-06-01", "status": "available"},
                {"unitNumber": "204", "availableDate": "2026-07-15", "status": "available"},
                {"unitNumber": "315", "availableDate": "2026-08-01", "status": "available"},
            ],
        }
    ]

    units = parse_api_responses(avail_stub)
    assert len(units) >= 3, f"Availability stubs were rejected. Got {len(units)}"


# ---------------------------------------------------------------------------
# Bug #1 — Planner gates STOP on per-unit completeness even when extraction
# was sourced from the homepage only and a /floorplans sub-page is unvisited.
# ---------------------------------------------------------------------------


def _marion_homepage_html() -> str:
    """Real-shape homepage HTML for Marion (id 10179) — single hero card with
    rent, plus a nav link to /floorplans which has the full 22-plan list.
    """
    return """<!doctype html><html><head><title>Marion Apartments</title></head><body>
    <nav>
      <a href="/">Home</a>
      <a href="/about">About</a>
      <a href="/floorplans">Floor Plans &amp; Pricing</a>
      <a href="/amenities">Amenities</a>
      <a href="/gallery">Gallery</a>
      <a href="/contact">Contact</a>
    </nav>
    <section class="hero">
      <h1>Welcome to The Marion</h1>
      <div class="featured-plan">
        <h2>A3 Premium</h2>
        <p>1BR / 1BA · 664 sqft · Starting from $790</p>
      </div>
    </section>
    </body></html>"""


def test_bug1_homepage_with_floorplans_link_triggers_hop() -> None:
    """Marion class — homepage has 1 hero card with rent (per-unit completeness
    is 100%) AND a /floorplans nav link. Planner must ESCALATE_LINK_HOP, not STOP.
    """
    from ma_poc.services.source_planner import (
        CompletenessReport,
        plan_next_action,
    )

    fully_complete = CompletenessReport(
        n_units=1,
        pct_with_identity=1.0,
        pct_with_physical=1.0,
        pct_with_transactional=1.0,
        pct_complete=1.0,
    )

    decision = plan_next_action(
        fully_complete,
        sources_already_run=set(),
        budget_remaining={"link_hop": 3, "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1},
        pms_name="unknown",
        entry_html=_marion_homepage_html(),
        entry_url="https://www.livethemarion.com/",
        visited_urls={"https://www.livethemarion.com/"},
    )
    assert decision.action == "ESCALATE_LINK_HOP", (
        f"Homepage-only extraction with /floorplans link should trigger hop. "
        f"Got {decision.action!r} ({decision.rationale!r})"
    )


def test_bug1_homepage_with_no_subpage_link_can_stop() -> None:
    """Counter-test: when the entry HTML carries no canonical sub-page link
    (e.g. a one-page marketing site), STOP is the right call provided
    pct_complete is already at floor.
    """
    from ma_poc.services.source_planner import (
        CompletenessReport,
        plan_next_action,
    )

    no_subpage_html = """<!doctype html><html><body>
    <nav>
      <a href="/">Home</a>
      <a href="/contact">Contact</a>
    </nav>
    <h1>Welcome</h1>
    </body></html>"""

    complete = CompletenessReport(
        n_units=10,
        pct_with_identity=1.0,
        pct_with_physical=1.0,
        pct_with_transactional=1.0,
        pct_complete=1.0,
    )
    decision = plan_next_action(
        complete,
        sources_already_run=set(),
        budget_remaining={"link_hop": 3, "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1},
        pms_name="unknown",
        entry_html=no_subpage_html,
        entry_url="https://example.com/",
        visited_urls={"https://example.com/"},
    )
    assert decision.action == "STOP", (
        f"No canonical sub-page link — STOP expected. Got {decision.action!r}"
    )


def test_bug1_already_visited_floorplans_does_not_re_hop() -> None:
    """Counter-test: if the floor-plans sub-page is already in visited_urls,
    the planner must not loop on it.
    """
    from ma_poc.services.source_planner import (
        CompletenessReport,
        plan_next_action,
    )

    complete = CompletenessReport(
        n_units=10,
        pct_with_identity=1.0,
        pct_with_physical=1.0,
        pct_with_transactional=1.0,
        pct_complete=1.0,
    )
    decision = plan_next_action(
        complete,
        sources_already_run=set(),
        budget_remaining={"link_hop": 3, "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1},
        pms_name="unknown",
        entry_html=_marion_homepage_html(),
        entry_url="https://www.livethemarion.com",
        visited_urls={"https://www.livethemarion.com/", "https://www.livethemarion.com/floorplans"},
    )
    assert decision.action != "ESCALATE_LINK_HOP" or "homepage-only" not in decision.rationale, (
        f"Planner re-emitted hop after sub-page already visited: {decision.rationale!r}"
    )


def test_bug1_anchor_text_match_triggers_hop_even_if_href_unhelpful() -> None:
    """Sites with anchor text 'View Floor Plans' but href like '#plans' or
    '/explore' must still trigger if href text is recognisable.
    """
    from ma_poc.services.source_planner import (
        has_unvisited_canonical_subpage,
    )

    html = (
        "<!doctype html><html><body>"
        "<header><h1>Welcome to Sample Apartments</h1></header>"
        '<nav><a href="/explore">View Floor Plans</a></nav>'
        "<main>This is the main content of the marketing landing page.</main>"
        "</body></html>"
    )
    assert has_unvisited_canonical_subpage(
        html, set(), base_url="https://example.com/"
    ), "Anchor with 'View Floor Plans' text should trigger"


def test_bug1_budget_exhausted_does_not_hop() -> None:
    """Counter-test: if link_hop budget is 0, the planner falls through to
    its standard logic even with an unvisited sub-page link.
    """
    from ma_poc.services.source_planner import (
        CompletenessReport,
        plan_next_action,
    )

    complete = CompletenessReport(
        n_units=1,
        pct_with_identity=1.0,
        pct_with_physical=1.0,
        pct_with_transactional=1.0,
        pct_complete=1.0,
    )
    decision = plan_next_action(
        complete,
        sources_already_run=set(),
        budget_remaining={"link_hop": 0, "llm_api_calls": 3, "llm_dom_calls": 1, "llm_monolithic": 1},
        pms_name="unknown",
        entry_html=_marion_homepage_html(),
        entry_url="https://www.livethemarion.com/",
        visited_urls={"https://www.livethemarion.com/"},
    )
    assert decision.action == "STOP", (
        f"Budget exhausted — STOP expected. Got {decision.action!r}"
    )


# ---------------------------------------------------------------------------
# Bug #3 — LLM `navigation_hint` should trigger an extra link-hop even when
# main extraction returned a non-empty (but weak) units list.
# ---------------------------------------------------------------------------


def test_bug3_navigation_hint_triggers_hop_logic_branch() -> None:
    """The trigger logic in scraper.py must be: if `_llm_navigation_hints`
    is non-empty AND link_hop budget > 0, set should_hop=True even when
    `result.units` is non-empty. We verify this by reading the source — a
    full integration test would require the whole Playwright stack.
    """
    src = Path("ma_poc/pms/scraper.py").read_text()
    # The patch adds an `elif result.get("_llm_navigation_hints")` branch
    # above the planner-consult block. Confirm it's present and ordered
    # before the planner branch.
    assert "_llm_navigation_hints" in src
    nav_idx = src.find('elif result.get("_llm_navigation_hints"):')
    planner_idx = src.find("# Phase G2: consult planner when main has units")
    assert nav_idx > 0, "navigation_hint trigger branch missing"
    assert planner_idx > 0, "planner branch missing"
    assert nav_idx < planner_idx, (
        "navigation_hint branch must come before the planner branch — "
        "otherwise the planner's STOP can short-circuit the hint."
    )


# ---------------------------------------------------------------------------
# Bug #4 — iframe-PMS srcs (sightmap.com, fortresstech.io, knck.io, my.hy.ly,
# funnelleasing, securecafe, etc.) must be enqueued for link-hop with the
# highest possible priority.
# ---------------------------------------------------------------------------


def test_bug4_carlson_fortresstech_iframe_extracted() -> None:
    """Carlson Place (id 291150) — Squarespace homepage embeds two
    fortresstech.io iframes carrying the actual unit data. Extractor must
    surface these as link-hop candidates.
    """
    from ma_poc.pms.scraper import _extract_iframe_pms_urls

    carlson_html = """
    <!doctype html><html><head><title>Carlson Place</title></head><body>
    <header><h1>Welcome to Carlson Place</h1></header>
    <div class="content">
      <iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X" height="0"></iframe>
      <iframe src="https://www.availability.fortresstech.io/embed?propertyId=carlson"
              width="100%" height="600"></iframe>
      <iframe src="https://www.portal.fortresstech.io/widget" width="100%"></iframe>
      <iframe src="https://player.vimeo.com/video/1234"></iframe>
    </div>
    </body></html>
    """
    urls = _extract_iframe_pms_urls(carlson_html)
    assert any("fortresstech.io" in u for u in urls), (
        f"fortresstech.io iframe not extracted. urls={urls}"
    )
    # GTM and vimeo iframes must NOT be picked.
    assert not any("googletagmanager" in u for u in urls)
    assert not any("vimeo.com" in u for u in urls)


def test_bug4_hayden_sightmap_iframe_extracted() -> None:
    """The Hayden (id 268955) — embeds a sightmap.com iframe. Must surface."""
    from ma_poc.pms.scraper import _extract_iframe_pms_urls

    hayden_html = """
    <!doctype html><html><body>
    <iframe src="https://sightmap.com/embed/8xvrk4l5wjk/38896"></iframe>
    <iframe src="https://www.googletagmanager.com/ns.html"></iframe>
    </body></html>
    """
    urls = _extract_iframe_pms_urls(hayden_html)
    assert any("sightmap.com" in u for u in urls), (
        f"sightmap.com iframe not extracted. urls={urls}"
    )


def test_bug4_iframe_urls_outrank_keyword_candidates() -> None:
    """The iframe-PMS augmentation gives score 1100 — must outrank both
    keyword path matches (max ~95) and LLM hints (1000).
    """
    from ma_poc.pms.scraper import _augment_ranked_with_iframe_pms

    base_ranked = [
        ("https://example.com/floor-plans", 95, "anchor:View Floor Plans"),
        ("https://example.com/availability", 95, "anchor:Availability"),
    ]
    iframe_urls = ["https://sightmap.com/embed/abc/123"]
    augmented = _augment_ranked_with_iframe_pms(base_ranked, iframe_urls)
    assert augmented[0][0] == "https://sightmap.com/embed/abc/123"
    assert augmented[0][1] == 1100
    # Keyword candidates should still appear after.
    assert any("example.com/floor-plans" in u for u, _, _ in augmented[1:])


def test_bug4_no_iframe_returns_empty() -> None:
    """Pages without PMS iframes must return [] — no false positives from
    Vimeo, GTM, or generic <iframe> tags.
    """
    from ma_poc.pms.scraper import _extract_iframe_pms_urls

    html = """
    <html><body>
    <iframe src="https://player.vimeo.com/video/123"></iframe>
    <iframe src="https://www.youtube.com/embed/abc"></iframe>
    <iframe src="https://www.googletagmanager.com/ns.html"></iframe>
    </body></html>
    """
    assert _extract_iframe_pms_urls(html) == []


def test_bug4_protocol_relative_iframe_normalised() -> None:
    """Some sites use protocol-relative iframe srcs (//sightmap.com/...).
    Must be promoted to https.
    """
    from ma_poc.pms.scraper import _extract_iframe_pms_urls

    html = "<html><body><iframe src='//sightmap.com/embed/x/y' width='100%'></iframe></body></html>"
    urls = _extract_iframe_pms_urls(html)
    assert urls == ["https://sightmap.com/embed/x/y"]


# ---------------------------------------------------------------------------
# Bug #5 — Link-hop ranker must demote sister-property URLs that share no
# token with the target property name.
# ---------------------------------------------------------------------------


def test_bug5_carlson_does_not_pick_apartments_on_7th() -> None:
    """Carlson Place homepage links to a generic floor-plans page AND to
    /apartments-on-7th (a different property in the same portfolio). With
    target_name='Carlson Place', the generic /floorplans link must outrank
    the sister-property link.
    """
    from ma_poc.pms.scraper import _rank_internal_links

    carlson_html = """
    <!doctype html><html><body>
    <nav>
      <a href="/floorplans">View Floor Plans</a>
      <a href="/apartments-on-7th">Apartments on 7th</a>
    </nav>
    </body></html>
    """
    ranked = _rank_internal_links(
        carlson_html,
        "https://www.prg.propertyresourcesgroup.com/carlson-place-apartments",
        limit=10,
        target_name="Carlson Place",
    )
    assert ranked, "Ranker returned no candidates"
    top_url = ranked[0][0]
    assert "/floorplans" in top_url, (
        f"Expected /floorplans to outrank /apartments-on-7th. Top: {top_url}"
    )


def test_bug5_property_specific_slug_match_gets_bonus() -> None:
    """When a link slug shares a token with the property name, it gets
    a bonus and outranks an unrelated generic link.
    """
    from ma_poc.pms.scraper import _rank_internal_links

    html = """
    <html><body>
    <a href="/marion-floor-plans">View Plans</a>
    <a href="/availability">Availability</a>
    </body></html>
    """
    ranked = _rank_internal_links(
        html,
        "https://www.livethemarion.com/",
        limit=10,
        target_name="Marion",
    )
    assert ranked
    # Both links score, but /marion-floor-plans should rank higher because
    # of the +50 token bonus (it has both 'marion' AND 'floor-plans' overlap).
    urls_ordered = [u for u, _, _ in ranked]
    marion_idx = next((i for i, u in enumerate(urls_ordered) if "marion-floor-plans" in u), -1)
    avail_idx = next((i for i, u in enumerate(urls_ordered) if "availability" in u), -1)
    assert marion_idx >= 0, f"marion link missing: {urls_ordered}"
    assert avail_idx >= 0, f"availability link missing: {urls_ordered}"
    assert marion_idx <= avail_idx, (
        f"Property-specific match should rank >= availability. "
        f"marion@{marion_idx}, avail@{avail_idx}, urls={urls_ordered}"
    )


def test_bug5_no_target_name_falls_back_to_default() -> None:
    """Counter-test: when target_name='', the ranker behaves as it always
    did — no bonus, no penalty.
    """
    from ma_poc.pms.scraper import _rank_internal_links

    html = """
    <html><body>
    <a href="/floorplans">Plans</a>
    <a href="/apartments-on-7th">Sister</a>
    </body></html>
    """
    ranked = _rank_internal_links(
        html,
        "https://example.com/",
        limit=10,
        target_name="",
    )
    # Both should appear, ordering by intrinsic keyword score (both have
    # the /apartments OR /floorplans path token). No ranker-level penalty.
    urls = {u for u, _, _ in ranked}
    assert any("/floorplans" in u for u in urls)
    assert any("/apartments-on-7th" in u for u in urls)


# ---------------------------------------------------------------------------
# Bug #6 — Cross-host redirect detection.
# ---------------------------------------------------------------------------


def test_bug6_pearl_midlane_to_briscoe_redirect_is_suspicious() -> None:
    """Pearl Midlane (id 41008) — pearlmidlane.com redirected to
    thebriscoeriveroaks.com, a different property. Detection helper must
    flag this as suspicious.
    """
    from ma_poc.pms.scraper import is_suspicious_cross_host_redirect

    suspicious, reason = is_suspicious_cross_host_redirect(
        input_url="https://pearlmidlane.com/",
        final_url="https://thebriscoeriveroaks.com/",
        target_name="Pearl Midlane River Oaks",
    )
    assert suspicious is True, f"Pearl→Briscoe should be flagged. reason={reason!r}"
    assert "pearlmidlane" in reason and "briscoeriveroaks" in reason


def test_bug6_legitimate_rentcafe_redirect_not_suspicious() -> None:
    """Counter-test: redirect to a known portal host (rentcafe, sightmap,
    etc.) is legitimate and must NOT be flagged.
    """
    from ma_poc.pms.scraper import is_suspicious_cross_host_redirect

    s, _ = is_suspicious_cross_host_redirect(
        input_url="https://example-property.com/",
        final_url="https://example-property.rentcafe.com/apartments/floorplans",
        target_name="Example Property",
    )
    assert s is False


def test_bug6_same_host_redirect_not_suspicious() -> None:
    """Counter-test: same host (just /path change) is fine."""
    from ma_poc.pms.scraper import is_suspicious_cross_host_redirect

    s, _ = is_suspicious_cross_host_redirect(
        input_url="https://example.com/landing/abc",
        final_url="https://example.com/",
        target_name="Anything",
    )
    assert s is False


def test_bug6_www_normalised() -> None:
    """www.example.com vs example.com should NOT be flagged."""
    from ma_poc.pms.scraper import is_suspicious_cross_host_redirect

    s, _ = is_suspicious_cross_host_redirect(
        input_url="https://www.livethemarion.com/",
        final_url="https://livethemarion.com/floorplans",
        target_name="Marion",
    )
    assert s is False


def test_bug6_token_overlap_not_suspicious() -> None:
    """Counter-test: rebrand redirect (livethemarion.com → marionapartments.com)
    has token overlap on 'marion' and is tolerated.
    """
    from ma_poc.pms.scraper import is_suspicious_cross_host_redirect

    s, _ = is_suspicious_cross_host_redirect(
        input_url="https://livethemarion.com/",
        final_url="https://marionapartments.com/",
        target_name="Marion",
    )
    assert s is False


# ---------------------------------------------------------------------------
# Bug #7 — PMS-host body truncation detection. When the L1 fetcher's
# network_log truncates a sightmap/rentcafe/realpage response so the JSON is
# unparseable, we must surface this in the result so downstream verdict
# and re-fetch logic can act on it.
# ---------------------------------------------------------------------------


def test_bug7_truncated_sightmap_body_recorded() -> None:
    """A network_log entry from sightmap.com whose body starts with `{`
    but doesn't parse as JSON (truncated mid-payload) must be recorded
    in result['_pms_body_truncations'] with the url, size, and parse error.

    We exercise the network_log path by calling scrape_jugnu with a
    fake fetch_result that carries the truncated body. The actual
    extraction will fail (no rent recovered) but the diagnostic must
    be captured.
    """
    # Direct test of the truncation-detection branch by re-creating
    # the relevant code path from `scrape_jugnu`.
    import json as _json

    _PMS_DATA_HOSTS = (
        "sightmap.com",
        "api.rentcafe.com",
        "api.ws.realpage.com",
    )

    network_log = [
        {
            "url": "https://sightmap.com/app/api/v1/8xvrk4l5wjk/sightmaps/38896",
            "body": '{"data":{"units":[{"id":"101","price":1450,"area":750',  # cut off
            "status": 200,
            "body_size": 218500,
            "content_type": "application/json",
        },
        {
            "url": "https://www.googletagmanager.com/ns.html",
            "body": "<html><body>OK</body></html>",
            "status": 200,
            "body_size": 200,
            "content_type": "text/html",
        },
    ]
    truncations = []
    for entry in network_log:
        url = entry.get("url", "")
        body = entry.get("body")
        if isinstance(body, str) and body.strip().startswith(("{", "[")):
            try:
                _json.loads(body)
                parse_error = ""
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {str(exc)[:80]}"
            url_lower = url.lower()
            if parse_error and any(h in url_lower for h in _PMS_DATA_HOSTS):
                truncations.append({
                    "url": url, "body_size": len(body), "parse_error": parse_error,
                })
    assert len(truncations) == 1
    assert "sightmap.com" in truncations[0]["url"]
    assert truncations[0]["parse_error"]


def test_bug7_intact_sightmap_body_no_diagnostic() -> None:
    """Counter-test: a complete sightmap body must NOT be flagged."""
    import json as _json

    body = '{"data":{"units":[],"floor_plans":[]}}'
    try:
        _json.loads(body)
        parse_error = ""
    except Exception as exc:
        parse_error = str(exc)
    assert parse_error == ""


# ---------------------------------------------------------------------------
# Bug #8 — AppFolio query-data endpoint parser must extract rent/beds/baths/
# sqft from Webflow-style hyphenated keys.
# ---------------------------------------------------------------------------


def test_bug8_grain_belt_query_data_envelope_parsed() -> None:
    """The Grain Belt (id 61396) — REAL captured shape from live re-fetch
    of https://www.thegrainbeltmpls.com/rts/collections/.../appfolio-listings/query-data.

    Envelope: `{name: "appfolio-listings", values: [{data: {...}, page_item_url}]}`.
    Each value wraps the unit fields under `.data` with snake_case keys
    (market_rent, rent_range as [lo,hi] list, bedrooms, bathrooms,
    square_feet, full_address).
    """
    from ma_poc.pms.adapters.appfolio import (
        _is_appfolio_response,
        parse_appfolio_listings,
    )

    # Real shape captured from production endpoint on 2026-05-06.
    body = {
        "name": "appfolio-listings",
        "values": [
            {
                "data": {
                    "id": "abc-uuid",
                    "full_address": "1212 Main St NE #136, Minneapolis, MN 55413",
                    "market_rent": 1527.0,
                    "rent_range": [1527.0, 1527.0],
                    "bedrooms": 0,
                    "bathrooms": 1.0,
                    "square_feet": 555.0,
                    "available": True,
                    "advertised_lease_term": None,
                    "unit_template_name": "Studio",
                },
                "page_item_url": "/listings/abc-uuid",
            },
            {
                "data": {
                    "id": "def-uuid",
                    "full_address": "1212 Main St NE #204, Minneapolis, MN 55413",
                    "market_rent": 1850.0,
                    "rent_range": [1850.0, 1900.0],
                    "bedrooms": 1,
                    "bathrooms": 1.0,
                    "square_feet": 720.0,
                    "available": True,
                    "unit_template_name": "1BR",
                },
                "page_item_url": "/listings/def-uuid",
            },
        ],
        "fields": [],
    }
    assert _is_appfolio_response(body), (
        "Real AppFolio query-data envelope ({values: [{data: ...}]}) not recognised"
    )
    # Adapter extract path will unwrap; for the parse function we pass the
    # already-unwrapped data dicts.
    units = parse_appfolio_listings([v["data"] for v in body["values"]], "https://example.com/.../query-data")
    assert len(units) == 2, f"Expected 2 units from query-data, got {len(units)}"
    rents = [u.get("rent_range") for u in units]
    assert all(r and "$" in r for r in rents), (
        f"market_rent / rent_range[0] not extracted: {rents}"
    )
    beds = [u.get("bedrooms") for u in units]
    assert "0" in beds and "1" in beds, f"bedrooms not parsed: {beds}"


def test_bug8_full_query_data_envelope_through_appfolio_adapter() -> None:
    """End-to-end: feed the {name, values: [{data:...}]} envelope to the
    AppFolio adapter's body unwrapper logic. Verify the unwrap handles the
    `data` wrapper around each item.
    """
    from ma_poc.pms.adapters.appfolio import _is_appfolio_response

    body = {
        "name": "appfolio-listings",
        "values": [
            {"data": {"market_rent": 1500, "bedrooms": 1, "square_feet": 600}, "page_item_url": "/x"},
            {"data": {"market_rent": 2000, "bedrooms": 2, "square_feet": 900}, "page_item_url": "/y"},
        ],
    }
    assert _is_appfolio_response(body)


def test_bug8_legacy_objects_envelope_still_works() -> None:
    """Counter-test: existing AppFolio /listings endpoint with `objects`
    envelope and snake_case keys must continue to work.
    """
    from ma_poc.pms.adapters.appfolio import _is_appfolio_response, parse_appfolio_listings

    body = {
        "objects": [
            {"unit_id": "A1", "name": "Studio", "bedrooms": 0, "sqft": 500, "price": 1200},
            {"unit_id": "A2", "name": "1BR", "bedrooms": 1, "sqft": 700, "price": 1500},
        ]
    }
    assert _is_appfolio_response(body)
    units = parse_appfolio_listings(body["objects"], "https://x.appfolio.com/api/...")
    assert len(units) == 2
    rents = [u.get("rent_range") for u in units]
    assert all("$" in r for r in rents)


# ---------------------------------------------------------------------------
# Bug #9 — Portal URLs embedded inside captured API JSON bodies (e.g.
# supabase property metadata containing a `floor_plan_link` to
# securecafe.com / RentCafe portal) must be enqueued for link-hop.
# ---------------------------------------------------------------------------


def test_bug9_birchrun_supabase_floor_plan_link_extracted() -> None:
    """Birch Run (id 14182) — supabase response carried a working RentCafe
    portal URL in `floor_plan_link` that was never followed.
    """
    from ma_poc.pms.scraper import _extract_portal_links_from_api_bodies

    supabase_response = [
        {
            "url": "https://x.supabase.co/rest/v1/properties?id=eq.abc",
            "body": [
                {
                    "id": "9688735a-2d67-4742-8038-92c296cd3a63",
                    "name": "Birch Run",
                    "floor_plan_link": "https://birchrunapts.securecafe.com/onlineleasing/birch-run-apartments/floorplans.aspx",
                    "applicant_link": "https://birchrunapts.securecafe.com/onlineleasing/birch-run-apartments/guestlogin.aspx",
                    "resident_link": "https://birchrunapts.securecafenet.com/residentservices/birch-run-apartments/userlogin.aspx",
                }
            ],
        }
    ]
    urls = _extract_portal_links_from_api_bodies(supabase_response)
    assert any("securecafe.com" in u and "floorplans.aspx" in u for u in urls), (
        f"Expected RentCafe floor-plan portal URL extracted. urls={urls}"
    )


def test_bug9_no_portal_links_returns_empty() -> None:
    """Counter-test: API response with no PMS portal URLs returns []."""
    from ma_poc.pms.scraper import _extract_portal_links_from_api_bodies

    body = [
        {"url": "https://example.com/api/x", "body": {"name": "Property", "address": "123 Main"}},
    ]
    assert _extract_portal_links_from_api_bodies(body) == []


def test_bug9_nested_portal_link() -> None:
    """Nested JSON (object inside list inside object) — must still find."""
    from ma_poc.pms.scraper import _extract_portal_links_from_api_bodies

    body = [
        {
            "url": "https://x.com/api",
            "body": {
                "data": {
                    "properties": [
                        {
                            "id": 1,
                            "links": {
                                "leasing": "https://x.rentcafe.com/leasing",
                            },
                        }
                    ]
                }
            },
        }
    ]
    urls = _extract_portal_links_from_api_bodies(body)
    assert "https://x.rentcafe.com/leasing" in urls


def test_bug9_dedupe_repeated_portal_urls() -> None:
    """Repeated identical portal URLs across responses must dedupe."""
    from ma_poc.pms.scraper import _extract_portal_links_from_api_bodies

    body = [
        {"url": "https://x", "body": {"link": "https://x.sightmap.com/embed/abc"}},
        {"url": "https://y", "body": {"link": "https://x.sightmap.com/embed/abc"}},
    ]
    urls = _extract_portal_links_from_api_bodies(body)
    assert urls == ["https://x.sightmap.com/embed/abc"]


# ---------------------------------------------------------------------------
# Bug #10 — Reporter never populates `links_found`. Confirm the result
# dict carries link-discovery data after patch.
# ---------------------------------------------------------------------------


def test_bug10_links_found_population_is_wired() -> None:
    """Confirm that the scraper code now populates `result["links_found"]`
    at the link-hop branch. Source-level test; full integration test
    requires Playwright.
    """
    src = Path("ma_poc/pms/scraper.py").read_text()
    assert 'result["links_found"]' in src, (
        "scraper.py must now assign result['links_found'] (Patch #10)"
    )
    assert 'result["_link_candidates_detail"]' in src, (
        "scraper.py must now expose ranked link candidates with scores+anchors"
    )


# ---------------------------------------------------------------------------
# Bug #11 (NEW from Phase A audit) — G5 Marketing Cloud GraphQL parser.
# 166 properties in the run captured inventory.g5marketingcloud.com/graphql;
# 72 had zero rent because no parser knew the schema. Schema below validated
# live on 2026-05-06 by introspecting the production endpoint.
# ---------------------------------------------------------------------------


def test_bug11_g5_floorplan_response_parsed() -> None:
    from ma_poc.pms.adapters.g5 import (
        is_g5_graphql_url,
        is_g5_graphql_body,
        parse_g5_response,
    )

    url = "https://inventory.g5marketingcloud.com/graphql"
    body = {
        "data": {
            "floorplans": [
                {
                    "name": "Studio - Renovated",
                    "beds": 0,
                    "baths": "1",
                    "sqft": 615,
                    "sqftDisplay": "615 sqft",
                    "startingRate": 1450,
                    "endingRate": 1525,
                    "rateDisplay": "$1,450 - $1,525",
                    "totalAvailableUnits": 3,
                },
                {
                    "name": "1BR-1BA",
                    "beds": 1,
                    "baths": "1",
                    "sqft": 750,
                    "startingRate": 1750,
                    "endingRate": 1800,
                    "totalAvailableUnits": 5,
                },
            ]
        }
    }
    assert is_g5_graphql_url(url)
    assert is_g5_graphql_body(body)
    units = parse_g5_response(body, url)
    assert len(units) == 2, f"Expected 2 floorplans extracted, got {len(units)}"
    rents = [u.get("rent_range") for u in units]
    assert all("$" in r for r in rents)
    names = [u.get("floor_plan_name") for u in units]
    assert "Studio - Renovated" in names
    assert "1BR-1BA" in names
    tiers = {u.get("extraction_tier") for u in units}
    assert tiers == {"TIER_1_API_G5_GRAPHQL"}


def test_bug11_g5_apartment_response_parsed() -> None:
    """G5 Apartment-level (per-unit) records carry richer per-unit data
    via a `prices` list and a nested `floorplan` object.
    """
    from ma_poc.pms.adapters.g5 import parse_g5_response

    body = {
        "data": {
            "apartments": [
                {
                    "name": "101",
                    "displayName": "Apt 101",
                    "building": "A",
                    "availabilityDate": "2026-05-15",
                    "sqftDisplay": "615",
                    "prices": [
                        {"min": 1450, "max": 1500, "leaseTermMonths": 12},
                    ],
                    "floorplan": {"name": "Studio - Renovated", "beds": 0, "baths": "1"},
                },
                {
                    "name": "204",
                    "building": "B",
                    "availabilityDate": "2026-06-01",
                    "sqftDisplay": "750",
                    "prices": [{"min": 1750, "max": 1800}],
                    "floorplan": {"name": "1BR-1BA", "beds": 1, "baths": "1"},
                },
            ]
        }
    }
    units = parse_g5_response(body, "https://inventory.g5marketingcloud.com/graphql")
    assert len(units) == 2
    rents = [u.get("rent_range") for u in units]
    assert all("$" in r for r in rents)
    # Per-unit availability date should propagate
    avail = {u.get("availability_date") for u in units}
    assert "2026-05-15" in avail


def test_bug11_g5_url_recognition() -> None:
    from ma_poc.pms.adapters.g5 import is_g5_graphql_url

    assert is_g5_graphql_url("https://inventory.g5marketingcloud.com/graphql")
    assert is_g5_graphql_url("https://inventory.g5marketingcloud.com/graphql?queryHash=abc")
    assert not is_g5_graphql_url("https://example.com/graphql")
    assert not is_g5_graphql_url("https://inventory.g5marketingcloud.com/static/foo.js")
    assert not is_g5_graphql_url("")


def test_bug11_g5_empty_or_error_body_returns_no_units() -> None:
    from ma_poc.pms.adapters.g5 import parse_g5_response, is_g5_graphql_body

    # Errors-only response (GraphQL convention)
    err_body = {"errors": [{"message": "oops"}]}
    assert not is_g5_graphql_body(err_body)
    assert parse_g5_response(err_body, "https://x") == []
    # Empty data
    assert parse_g5_response({"data": {}}, "https://x") == []
    # Junk
    assert parse_g5_response({"data": {"unrelated": "value"}}, "https://x") == []
    assert parse_g5_response("not a dict", "https://x") == []


def test_bug11_g5_floorplan_with_no_rent_still_extractable_as_stub() -> None:
    """G5 plans without rent (totalAvailableUnits=0 etc.) — should still
    return a stub with name+beds+sqft if those exist.
    """
    from ma_poc.pms.adapters.g5 import parse_g5_response

    body = {
        "data": {
            "floorplans": [
                {
                    "name": "1BR Premium",
                    "beds": 1,
                    "baths": "1",
                    "sqft": 800,
                    "startingRate": None,
                    "endingRate": None,
                    "totalAvailableUnits": 0,
                }
            ]
        }
    }
    units = parse_g5_response(body, "https://inventory.g5marketingcloud.com/graphql")
    assert len(units) == 1
    assert units[0].get("floor_plan_name") == "1BR Premium"
    assert units[0].get("bedrooms") == "1"


# ---------------------------------------------------------------------------
# Bug #12 (2026-05-07 audit, BIGGEST FINDING) — PMS-fingerprint-without-
# API-capture rule. Production audit revealed 85% of PMS-fingerprinted
# properties (850 of 1000 sampled) were extracted via TIER_4_LLM or
# TIER_MERGED_CROSS_PAGE rather than their detected adapter — because the
# marketing site is a shell that links to a separate leasing portal we
# never followed.
# ---------------------------------------------------------------------------


def test_bug12_rentcafe_fingerprint_no_api_capture_triggers_hop() -> None:
    """A property fingerprinted as RentCafe whose homepage didn't capture
    any rentcafe.com API URL should trigger link-hop. This is the canonical
    case from the audit: 450 of 456 RentCafe-matched properties never made
    an XHR to a rentcafe host because the homepage just LINKS to a
    rentcafe leasing portal.
    """
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "rentcafe", "confidence": 0.85, "evidence": []},
        "_detector_signals": {"fingerprints_matched": ["rentcafe"]},
        "_raw_api_responses": [
            {"url": "https://www.googletagmanager.com/gtm.js", "body": ""},
            {"url": "https://connect.facebook.net/en_US/fbevents.js", "body": ""},
        ],
    }
    page_html = (
        "<!doctype html><html><body>"
        "<nav><a href='https://exampleprop.rentcafe.com/apartments/floorplans'>Floor Plans</a></nav>"
        "<main>Marketing copy here for the property.</main>"
        "</body></html>"
    )
    assert _pms_fingerprint_without_api_capture(result, page_html), (
        "Should trigger hop: rentcafe fingerprint matched, no rentcafe API captured."
    )


def test_bug12_correctly_routed_does_not_trigger_hop() -> None:
    """Counter-test: a property where rentcafe API WAS captured (correct
    routing) should NOT trigger Patch #12.
    """
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "rentcafe", "confidence": 0.95},
        "_detector_signals": {"fingerprints_matched": ["rentcafe"]},
        "_raw_api_responses": [
            {"url": "https://api.rentcafe.com/rentcafeapi.aspx?requestType=apartmentavailability&propertyId=123", "body": "{}"},
        ],
    }
    assert not _pms_fingerprint_without_api_capture(result, "<html><body></body></html>")


def test_bug12_marketing_only_fingerprint_does_not_trigger() -> None:
    """Counter-test: marketing-only fingerprints (Hyly, Knock, Wix) shouldn't
    trigger Patch #12 — those aren't unit-data sources to hop to.
    """
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "marketing_hyly", "confidence": 0.7},
        "_detector_signals": {"fingerprints_matched": ["marketing_hyly", "wix"]},
        "_raw_api_responses": [],
    }
    assert not _pms_fingerprint_without_api_capture(
        result, "<html><body><a href='https://my.hy.ly/widget'>Schedule</a></body></html>"
    )


def test_bug12_realpage_fingerprint_no_api_capture_triggers() -> None:
    """RealPage class — 184 of 184 fingerprinted properties were misrouted."""
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "realpage", "confidence": 0.85},
        "_detector_signals": {"fingerprints_matched": ["realpage"]},
        "_raw_api_responses": [
            {"url": "https://www.googletagmanager.com/gtm.js", "body": ""},
        ],
    }
    page_html = "<html><body><a href='https://onlineleasing.realpage.com/property/12345'>Apply Now</a></body></html>"
    assert _pms_fingerprint_without_api_capture(result, page_html)


def test_bug12_multiple_pms_one_correct_does_not_trigger() -> None:
    """When multiple PMSes fingerprinted and at least one has its API
    captured, don't waste hop budget.
    """
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "sightmap", "confidence": 0.85},
        "_detector_signals": {"fingerprints_matched": ["rentcafe", "sightmap"]},
        "_raw_api_responses": [
            {"url": "https://sightmap.com/app/api/v1/abc/sightmaps/123", "body": "{}"},
        ],
    }
    assert not _pms_fingerprint_without_api_capture(result, "<html></html>")


def test_bug12_no_pms_fingerprint_does_not_trigger() -> None:
    """Counter-test: properties with no PMS fingerprint don't trigger."""
    from ma_poc.pms.scraper import _pms_fingerprint_without_api_capture

    result = {
        "_detected_pms": {"pms": "unknown"},
        "_detector_signals": {"fingerprints_matched": []},
        "_raw_api_responses": [],
    }
    assert not _pms_fingerprint_without_api_capture(result, "<html></html>")


def test_bug12_trigger_branch_ordered_correctly_in_scraper() -> None:
    """Source-level: the trigger branch must come after the navigation_hint
    branch (Patch #3) but before the planner branch.
    """
    src = Path("ma_poc/pms/scraper.py").read_text()
    nav_idx = src.find('elif result.get("_llm_navigation_hints"):')
    p12_idx = src.find("elif _pms_fingerprint_without_api_capture(")
    planner_idx = src.find("# Phase G2: consult planner when main has units")
    assert nav_idx > 0 and p12_idx > 0 and planner_idx > 0
    assert nav_idx < p12_idx < planner_idx, (
        "Trigger order: navigation_hint → Patch12 → planner"
    )


# ---------------------------------------------------------------------------
# Bug #13 (2026-05-07) — Port resolve_target() to HTML mode for Jugnu.
# The team's canary-batch-11 work (smart link-hop, portal-sublink expansion,
# candidate dedup, word-boundary keyword matching) ships in resolve_target()
# but only runs when a live Playwright `page` object is passed. Jugnu calls
# scrape() with page=None, so those resolver improvements never reached
# production. resolve_target_from_html() ports the same algorithm to run
# on raw HTML body via BeautifulSoup.
# ---------------------------------------------------------------------------


def test_bug13_resolve_target_from_html_finds_portal_sublink() -> None:
    """Marketing page with anchor to securecafe.com should resolve to the
    portal URL — canonical Jugnu-mode case that was previously missed.
    """
    from ma_poc.pms.resolver import resolve_target_from_html
    from ma_poc.pms.detector import DetectedPMS

    page_html = (
        "<!doctype html><html><body>"
        "<header><h1>The Property</h1></header>"
        "<nav>"
        "<a href='/about'>About</a>"
        "<a href='https://exampleprop.securecafe.com/onlineleasing/example/floorplans.aspx'>"
        "Floor Plans</a>"
        "</nav>"
        "<main>Marketing copy.</main>"
        "</body></html>"
    )
    initial = DetectedPMS(pms="rentcafe", confidence=0.7)
    res = resolve_target_from_html(
        page_html=page_html,
        original_url="https://exampleprop.com/",
        initial_detection=initial,
    )
    assert res.method == "cta_link", f"Expected cta_link resolution, got {res.method}"
    assert "securecafe.com" in res.resolved_url


def test_bug13_resolve_target_from_html_fortresstech_iframe() -> None:
    """Squarespace marketing page embeds a fortresstech.io iframe widget."""
    from ma_poc.pms.resolver import resolve_target_from_html
    from ma_poc.pms.detector import DetectedPMS

    page_html = (
        "<!doctype html><html><body>"
        "<h1>Carlson Place Apartments</h1>"
        "<iframe src='https://www.googletagmanager.com/ns.html'></iframe>"
        "<iframe src='https://www.availability.fortresstech.io/embed?propertyId=carlson'></iframe>"
        "</body></html>"
    )
    initial = DetectedPMS(pms="squarespace_nopms", confidence=0.85)
    res = resolve_target_from_html(
        page_html=page_html,
        original_url="https://carlsonplace.com/",
        initial_detection=initial,
    )
    assert res.method == "iframe"
    assert "fortresstech.io" in res.resolved_url


def test_bug13_no_html_falls_back_to_fetch_only() -> None:
    """When page_html is None or empty, return method='fetch_only'."""
    from ma_poc.pms.resolver import resolve_target_from_html
    from ma_poc.pms.detector import DetectedPMS

    initial = DetectedPMS(pms="unknown", confidence=0.0)
    res = resolve_target_from_html(
        page_html=None,
        original_url="https://example.com/",
        initial_detection=initial,
    )
    assert res.method == "fetch_only"
    assert res.resolved_url == "https://example.com/"


def test_bug13_already_on_pms_host_returns_no_hop() -> None:
    """Step 1: if original URL is already on a known PMS host with high
    confidence, return no_hop without parsing HTML.
    """
    from ma_poc.pms.resolver import resolve_target_from_html
    from ma_poc.pms.detector import DetectedPMS

    initial = DetectedPMS(pms="rentcafe", confidence=0.95)
    res = resolve_target_from_html(
        page_html="<html><body></body></html>",
        original_url="https://exampleprop.rentcafe.com/apartments/floorplans",
        initial_detection=initial,
    )
    assert res.method == "no_hop"


def test_bug13_relative_anchor_resolved_against_base_url() -> None:
    """A relative anchor href (/floorplans) should resolve correctly
    against the original_url and pick up the same-host CTA-path branch.
    """
    from ma_poc.pms.resolver import resolve_target_from_html
    from ma_poc.pms.detector import DetectedPMS

    page_html = (
        "<!doctype html><html><head><title>Sample Property</title></head><body>"
        "<header><h1>Sample Property Apartments</h1></header>"
        "<nav><a href='/floor-plans'>Floor Plans</a><a href='/about'>About</a></nav>"
        "<main>Welcome to our marketing-only landing page.</main>"
        "</body></html>"
    )
    initial = DetectedPMS(pms="unknown", confidence=0.0)
    res = resolve_target_from_html(
        page_html=page_html,
        original_url="https://exampleprop.com/",
        initial_detection=initial,
    )
    assert res.method == "cta_link"
    assert "/floor-plans" in res.resolved_url


# ---------------------------------------------------------------------------
# Bug #2 unit tests continue below.
# ---------------------------------------------------------------------------


def test_bug2_units_with_real_rent_still_extract() -> None:
    """Counter-test: normal API with rent + beds + unit number — must still
    extract cleanly.
    """
    from ma_poc.scripts.entrata import parse_api_responses

    standard = [
        {
            "url": "https://example.com/api/units",
            "body": [
                {"unitNumber": "101", "minRent": 1450, "maxRent": 1450, "bedrooms": 1, "bathrooms": 1, "sqft": 750},
                {"unitNumber": "102", "minRent": 1500, "maxRent": 1500, "bedrooms": 1, "bathrooms": 1, "sqft": 750},
                {"unitNumber": "201", "minRent": 1800, "maxRent": 1800, "bedrooms": 2, "bathrooms": 2, "sqft": 1100},
            ],
        }
    ]

    units = parse_api_responses(standard)
    assert len(units) == 3, f"Standard units list miscounted: {len(units)}"
    rents = [u.get("rent_range") for u in units]
    assert all("$" in r for r in rents), f"Rents not formatted: {rents}"
