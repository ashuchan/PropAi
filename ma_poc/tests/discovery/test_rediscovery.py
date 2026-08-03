"""Tests for the DEAD_URL re-discovery consumer (ma_poc.discovery.rediscovery).

Fixtures use real slugs from the 07-12 DEAD_URL cohort's management sites
(pedcorliving.com, dermotcompany.com) so the precision thresholds are exercised
against the same data they were calibrated on.
"""

from __future__ import annotations

import json
from pathlib import Path

from ma_poc.discovery.rediscovery import (
    FetchedPage,
    RediscoveryEngine,
    RediscoveryEntry,
    RediscoveryMethod,
    RediscoveryStatus,
    SearchHit,
    base_property_url,
    build_search_query,
    extract_anchor_hrefs,
    match_score,
    normalize_name,
    parse_sitemap,
    rank_search_hits,
    slug_to_text,
)
from ma_poc.scripts.rediscover_dead_urls import (
    load_csv_index,
    load_entries_from_queue,
    load_entries_from_run,
    summarize,
    write_results,
)

# ── Realistic fixtures (trimmed from live probes) ─────────────────────────────
PEDCOR_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml</loc></sitemap>
</sitemapindex>"""

PEDCOR_CHILD = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.pedcorliving.com/apartments/aaron-lake</loc></url>
  <url><loc>https://www.pedcorliving.com/apartments/aaron-lake/floorplans</loc></url>
  <url><loc>https://www.pedcorliving.com/apartments/elevate-at-604-west</loc></url>
  <url><loc>https://www.pedcorliving.com/apartments/weatherly-ridge-apartments</loc></url>
  <url><loc>https://www.pedcorliving.com/apartments/hickory-knoll-apartments</loc></url>
  <url><loc>https://www.pedcorliving.com/apartments/franklin-cove</loc></url>
</urlset>"""

# Ambiguous portfolio: "Park Place" cannot be told from north vs south.
AMBIG_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://mgmt.example.com/apartments/park-place-north</loc></url>
  <url><loc>https://mgmt.example.com/apartments/park-place-south</loc></url>
  <url><loc>https://mgmt.example.com/apartments/willow-creek</loc></url>
</urlset>"""

GSC_HARBOUR_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://gscapts.com/apartments/florida/harbour-pointe/</loc></url>
  <url><loc>https://gscapts.com/apartments/florida/harbour-pointe-apartment-homes/</loc></url>
  <url><loc>https://gscapts.com/apartments/florida/bradenton-reserve/</loc></url>
</urlset>"""

# A rebranded single-property site that embeds a RentCafe leasing widget.
REBRAND_HTML = (
    b"<html><head><title>The Huntington Apartments</title></head><body>"
    b'<nav><a href="/floorplans">Floor Plans</a><a href="/amenities">Amenities</a>'
    b'<a href="/contact">Contact</a></nav>'
    b'<iframe src="https://widget.rentcafe.com/apartments/the-huntington"></iframe>'
    + b"<p>Welcome home to The Huntington.</p>" * 80
    + b"</body></html>"
)


class FakeFetcher:
    """Route-table PageFetcher: exact-URL match, else a 404 FetchedPage."""

    def __init__(self, routes: dict[str, FetchedPage], default_status: int = 404) -> None:
        self.routes = routes
        self.default_status = default_status
        self.calls: list[str] = []

    async def __call__(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        return FetchedPage(url=url, status=self.default_status, final_url=url, body=b"")


def _page(status: int, final_url: str, body: bytes = b"", error: str | None = None) -> FetchedPage:
    return FetchedPage(url=final_url, status=status, final_url=final_url, body=body, error=error)


# ── Pure helpers ──────────────────────────────────────────────────────────────
def test_normalize_name_strips_punctuation_and_case() -> None:
    assert normalize_name("The Quaye at Wellington!") == "the quaye at wellington"
    assert normalize_name("  Aaron   Lake  ") == "aaron lake"


def test_slug_to_text_extracts_property_segment() -> None:
    assert slug_to_text("https://x.com/apartments/aaron-lake") == "aaron lake"
    # trailing sub-page segment is stripped
    assert slug_to_text("https://x.com/apartments/aaron-lake/floorplans") == "aaron lake"


def test_slug_to_text_rejects_placeholder_numeric_and_empty() -> None:
    assert slug_to_text("https://x.com/apartments/%apartment_location%/") == ""
    assert slug_to_text("https://x.com/") == ""
    assert slug_to_text("https://x.com/building/12345") == ""


def test_base_property_url_strips_subpages_only() -> None:
    assert (
        base_property_url("https://x.com/apartments/aaron-lake/floorplans")
        == "https://x.com/apartments/aaron-lake"
    )
    assert (
        base_property_url("https://x.com/apartments/aaron-lake")
        == "https://x.com/apartments/aaron-lake"
    )


def test_match_score_positive_and_noise_floor() -> None:
    # true match scores at the ceiling
    assert match_score("Aaron Lake Apartments", "aaron lake") >= 95.0
    # calibrated noise floor: a shared trailing token must NOT clear threshold
    assert match_score("The Quaye at Wellington", "ryder place at wellington") < 90.0
    assert match_score("", "aaron lake") == 0.0


def test_parse_sitemap_urlset_and_index() -> None:
    pages, children = parse_sitemap(PEDCOR_CHILD)
    assert "https://www.pedcorliving.com/apartments/aaron-lake" in pages
    assert children == []
    idx_pages, idx_children = parse_sitemap(PEDCOR_INDEX)
    assert idx_pages == []
    assert idx_children == ["https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml"]


def test_parse_sitemap_non_namespaced_and_malformed() -> None:
    bare = b"<urlset><url><loc>https://x.com/apartments/foo</loc></url></urlset>"
    pages, _ = parse_sitemap(bare)
    assert pages == ["https://x.com/apartments/foo"]
    assert parse_sitemap(b"<<not xml") == ([], [])


def test_extract_anchor_hrefs_absolute_http_only() -> None:
    html = (
        b'<a href="/floorplans">fp</a>'
        b'<a href="https://other.com/x">abs</a>'
        b'<a href="mailto:a@b.com">mail</a>'
        b'<a href="#top">frag</a>'
    )
    hrefs = extract_anchor_hrefs(html, "https://mgmt.example.com/")
    assert "https://mgmt.example.com/floorplans" in hrefs
    assert "https://other.com/x" in hrefs
    assert all(not h.startswith("mailto") and "#top" not in h for h in hrefs)


def test_build_search_query_joins_present_fields() -> None:
    e = RediscoveryEntry("1", "Sagamore", "https://x.com/", city="Fort Worth", state="TX")
    assert build_search_query(e) == "Sagamore Fort Worth TX apartments"
    e2 = RediscoveryEntry("2", "Sagamore", "https://x.com/")
    assert build_search_query(e2) == "Sagamore apartments"


def test_rank_search_hits_excludes_aggregators_and_dead_host() -> None:
    e = RediscoveryEntry("1", "Sagamore", "https://sagamoreaptsliving.com/", city="Fort Worth")
    hits = [
        SearchHit("https://www.apartments.com/sagamore", "Sagamore Apartments"),  # aggregator
        SearchHit("https://sagamoreaptsliving.com/x", "Sagamore"),  # the known-dead host
        SearchHit("https://livesagamore.com/", "Sagamore Apartments | Fort Worth, TX"),
    ]
    ranked = rank_search_hits(e, hits)
    hosts = [r.url for r in ranked]
    assert hosts == ["https://livesagamore.com/"]
    assert ranked[0].score >= 90.0


# ── Engine: approach (a) ──────────────────────────────────────────────────────
async def test_engine_resolves_via_mgmt_sitemap_index() -> None:
    fetcher = FakeFetcher(
        {
            "https://pedcorhomes.com/": _page(200, "https://www.pedcorliving.com/", b"<html>portfolio</html>"),
            "https://pedcorliving.com/sitemap.xml": _page(200, "https://pedcorliving.com/sitemap.xml", PEDCOR_INDEX),
            "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml": _page(
                200, "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml", PEDCOR_CHILD
            ),
        }
    )
    engine = RediscoveryEngine(fetcher)
    entry = RediscoveryEntry("45986", "Aaron Lake Apartments", "https://pedcorhomes.com/")
    res = await engine.rediscover(entry)
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.method is RediscoveryMethod.MGMT_SITEMAP
    assert res.rediscovered_url == "https://www.pedcorliving.com/apartments/aaron-lake"
    assert res.matched_text == "aaron lake"
    assert res.confidence >= 0.9


async def test_engine_resolves_second_property_on_same_portfolio() -> None:
    fetcher = FakeFetcher(
        {
            "https://pedcorhomes.com/": _page(200, "https://www.pedcorliving.com/", b"<html>x</html>"),
            "https://pedcorliving.com/sitemap.xml": _page(200, "https://pedcorliving.com/sitemap.xml", PEDCOR_INDEX),
            "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml": _page(
                200, "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml", PEDCOR_CHILD
            ),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("261342", "Elevate at 604 West", "https://pedcorhomes.com/"))
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.rediscovered_url.endswith("/apartments/elevate-at-604-west")


async def test_engine_ambiguous_withheld_for_precision() -> None:
    fetcher = FakeFetcher(
        {
            "https://parkplace-old.com/": _page(200, "https://mgmt.example.com/", b"<html>x</html>"),
            "https://mgmt.example.com/sitemap.xml": _page(200, "https://mgmt.example.com/sitemap.xml", AMBIG_SITEMAP),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("1", "Park Place", "https://parkplace-old.com/"))
    assert res.status is RediscoveryStatus.AMBIGUOUS
    assert res.rediscovered_url is None
    assert res.runner_up_score >= 90.0


async def test_engine_lexical_tie_resolves_only_when_redirect_disproves_rival() -> None:
    """Real GSC shape: stale short slug redirects to a generic state index."""
    old = "http://www.gscapts.com/apartments/Bradenton_FL/zip_34210/gsc/15388"
    short = "https://gscapts.com/apartments/florida/harbour-pointe/"
    canonical = (
        "https://gscapts.com/apartments/florida/"
        "harbour-pointe-apartment-homes/"
    )
    fetcher = FakeFetcher(
        {
            old: _page(200, "https://gscapts.com/", b"<html>portfolio</html>"),
            "https://gscapts.com/sitemap.xml": _page(
                200, "https://gscapts.com/sitemap.xml", GSC_HARBOUR_SITEMAP
            ),
            short: _page(200, "https://gscapts.com/apartments/florida/", b"index"),
            canonical: _page(200, canonical, b"property"),
        }
    )
    result = await RediscoveryEngine(fetcher).rediscover(
        RediscoveryEntry("6477", "Harbour Pointe", old)
    )
    assert result.status is RediscoveryStatus.RESOLVED
    assert result.rediscovered_url == canonical
    assert result.method is RediscoveryMethod.MGMT_SITEMAP


async def test_engine_same_name_tie_uses_explicit_city_and_state() -> None:
    """Live GSC collision: FL Harbour Pointe vs GA Harbor Pointe."""
    old = "http://www.gscapts.com/apartments/Bradenton_FL/zip_34210/gsc/15388"
    florida = (
        "https://gscapts.com/apartments/florida/"
        "harbour-pointe-apartment-homes/"
    )
    georgia = "https://gscapts.com/apartments/georgia/harbor-pointe/"
    florida_body = (
        b'<script type="application/ld+json">{"addressLocality":"Bradenton",'
        b'"addressRegion":"FL","postalCode":"34210"}</script>'
        + b" Harbour Pointe apartments Bradenton " * 80
    )
    georgia_body = (
        b'<script type="application/ld+json">{"addressLocality":"Moultrie",'
        b'"addressRegion":"GA","postalCode":"31768"}</script>'
        + b" Harbor Pointe apartments Moultrie " * 80
    )
    live_tie_sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{florida}</loc></url><url><loc>{georgia}</loc></url>"
        "</urlset>"
    ).encode()
    fetcher = FakeFetcher(
        {
            old: _page(200, "https://gscapts.com/", b"<html>portfolio</html>"),
            "https://gscapts.com/sitemap.xml": _page(
                200, "https://gscapts.com/sitemap.xml", live_tie_sitemap
            ),
            florida: _page(200, florida, florida_body),
            georgia: _page(200, georgia, georgia_body),
        }
    )
    result = await RediscoveryEngine(fetcher).rediscover(
        RediscoveryEntry(
            "6477",
            "Harbour Pointe",
            old,
            city="Bradenton",
            state="FL",
        )
    )
    assert result.status is RediscoveryStatus.RESOLVED
    assert result.rediscovered_url == florida


async def test_engine_no_match_when_property_absent_from_portfolio() -> None:
    # "Avenir on Fifteenth" is not in this portfolio -> web-search fallback,
    # which is disabled -> NEEDS_WEB_SEARCH (never a forced bad match).
    fetcher = FakeFetcher(
        {
            "https://avenirphilly.com/": _page(200, "https://mgmt.example.com/", b"<html>x</html>"),
            "https://mgmt.example.com/sitemap.xml": _page(200, "https://mgmt.example.com/sitemap.xml", AMBIG_SITEMAP),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("63191", "Avenir on Fifteenth", "https://avenirphilly.com/"))
    assert res.status is RediscoveryStatus.NEEDS_WEB_SEARCH
    assert res.rediscovered_url is None


async def test_engine_resolves_rebrand_via_redirect_to_single_property_site() -> None:
    fetcher = FakeFetcher(
        {
            "https://thehuntingtonapartments.com/": _page(200, "https://enjoyhuntington.com/", REBRAND_HTML),
            # no sitemap on the rebranded host -> homepage anchors (all sub-pages)
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("46342", "The Huntington", "https://thehuntingtonapartments.com/"))
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.method is RediscoveryMethod.REDIRECT_REBRAND
    assert res.rediscovered_url == "https://enjoyhuntington.com/"
    assert res.detected_pms == "rentcafe"


async def test_engine_dead_dns_routes_to_needs_web_search() -> None:
    fetcher = FakeFetcher(
        {"https://sagamoreaptsliving.com/": _page(None, "https://sagamoreaptsliving.com/", error="ConnectError: dns")}
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("8797", "Sagamore", "https://sagamoreaptsliving.com/"))
    assert res.status is RediscoveryStatus.NEEDS_WEB_SEARCH
    assert "DEAD_DNS" in res.notes


async def test_engine_dead_end_host_routes_to_needs_web_search() -> None:
    fetcher = FakeFetcher(
        {"https://www.riverfallsdallas.com/": _page(200, "https://notfound.apts247.info/", b"<html>not found</html>")}
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("8654", "Riverfalls at Bellmar", "https://www.riverfallsdallas.com/"))
    assert res.status is RediscoveryStatus.NEEDS_WEB_SEARCH
    assert "DEAD_END_HOST" in res.notes


# Portfolio sitemap polluted with a bare index page + a blog headline that
# token-subset-matches a property name — both must be filtered out.
POLLUTED_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://mgmt.example.com/apartments</loc></url>
  <url><loc>https://mgmt.example.com/scully-news/leasing-has-started-at-avenir-on-fifteenth</loc></url>
  <url><loc>https://mgmt.example.com/apartments/aaron-lake</loc></url>
</urlset>"""

SPLASH_HTML = (
    b"<html><body><h1>Pedcor</h1>"
    b'<a href="https://www.pedcorliving.com/apartments">View Our Apartments</a>'
    b"</body></html>"
)


def test_is_nonproperty_path_flags_news_and_blog() -> None:
    from ma_poc.discovery.rediscovery import is_nonproperty_path

    assert is_nonproperty_path("https://x.com/scully-news/leasing-has-started")
    assert is_nonproperty_path("https://x.com/blog/2026/hello")
    assert not is_nonproperty_path("https://x.com/apartments/aaron-lake")


def test_host_matches_name_distinctive_token_only() -> None:
    from ma_poc.discovery.rediscovery import host_matches_name

    assert host_matches_name("enjoyhuntington.com", "The Huntington")
    assert host_matches_name("livesagamore.com", "Sagamore")
    # a shared-portfolio host that carries no property token must NOT match
    assert not host_matches_name("pedcorliving.com", "Aaron Lake")
    assert not host_matches_name("lawsoncompanies.com", "Edgefield")


async def test_engine_filters_generic_index_no_spurious_ambiguous() -> None:
    # "/apartments" index slug == "apartments" token-matches "Aaron Lake
    # Apartments" at 100; without the generic-slug filter it ties the real
    # property and yields a spurious AMBIGUOUS.
    fetcher = FakeFetcher(
        {
            "https://dead.com/": _page(200, "https://mgmt.example.com/", b"<html>x</html>"),
            "https://mgmt.example.com/sitemap.xml": _page(
                200, "https://mgmt.example.com/sitemap.xml", POLLUTED_SITEMAP
            ),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("1", "Aaron Lake Apartments", "https://dead.com/"))
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.rediscovered_url == "https://mgmt.example.com/apartments/aaron-lake"


async def test_engine_never_matches_a_blog_headline() -> None:
    # The only page mentioning "Avenir on Fifteenth" is a blog post — it must
    # NOT be returned as the property URL (the cardinal false-positive).
    fetcher = FakeFetcher(
        {
            "https://avenirphilly.com/": _page(200, "https://mgmt.example.com/", b"<html>x</html>"),
            "https://mgmt.example.com/sitemap.xml": _page(
                200, "https://mgmt.example.com/sitemap.xml", POLLUTED_SITEMAP
            ),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("1", "Avenir on Fifteenth", "https://avenirphilly.com/"))
    assert res.status is RediscoveryStatus.NEEDS_WEB_SEARCH
    assert res.rediscovered_url is None


async def test_engine_resolves_via_secondary_portfolio_link() -> None:
    # pedcorhomes.com serves a 200 splash page whose only useful content is a
    # single link to pedcorliving.com/apartments (no HTTP redirect).
    fetcher = FakeFetcher(
        {
            "https://pedcorhomes.com/": _page(200, "https://pedcorhomes.com/", SPLASH_HTML),
            "https://pedcorliving.com/sitemap.xml": _page(200, "https://pedcorliving.com/sitemap.xml", PEDCOR_INDEX),
            "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml": _page(
                200, "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml", PEDCOR_CHILD
            ),
        }
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("45986", "Aaron Lake Apartments", "https://pedcorhomes.com/"))
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.rediscovered_url == "https://www.pedcorliving.com/apartments/aaron-lake"


async def test_engine_rebrand_rejected_when_host_does_not_match_name() -> None:
    # Cross-host redirect to a real PMS site whose host carries NO property
    # token ("ulcoleman" vs "The Willows at Wright") is NOT a rebrand — withhold.
    fetcher = FakeFetcher(
        {"https://thewillowsulc.com/": _page(200, "https://ulcoleman.com/", REBRAND_HTML)}
    )
    engine = RediscoveryEngine(fetcher)
    res = await engine.rediscover(RediscoveryEntry("240534", "The Willows at Wright", "https://thewillowsulc.com/"))
    assert res.status is RediscoveryStatus.NEEDS_WEB_SEARCH
    assert res.method is None


# ── Engine: approach (b), gated ───────────────────────────────────────────────
async def test_engine_web_search_resolves_when_enabled() -> None:
    async def fake_search(query: str) -> list[SearchHit]:
        assert "Sagamore" in query
        return [
            SearchHit("https://www.apartments.com/sagamore", "Sagamore Apartments"),
            SearchHit("https://livesagamore.com/", "Sagamore Apartments | Fort Worth TX"),
        ]

    fetcher = FakeFetcher(
        {"https://sagamoreaptsliving.com/": _page(None, "https://sagamoreaptsliving.com/", error="ConnectError")}
    )
    engine = RediscoveryEngine(fetcher, enable_web_search=True, search_fn=fake_search)
    res = await engine.rediscover(
        RediscoveryEntry("8797", "Sagamore", "https://sagamoreaptsliving.com/", city="Fort Worth", state="TX")
    )
    assert res.status is RediscoveryStatus.RESOLVED
    assert res.method is RediscoveryMethod.WEB_SEARCH
    assert res.rediscovered_url == "https://livesagamore.com/"


async def test_engine_web_search_backend_error_is_swallowed() -> None:
    async def boom(query: str) -> list[SearchHit]:
        raise RuntimeError("search backend down")

    fetcher = FakeFetcher(
        {"https://x.com/": _page(None, "https://x.com/", error="ConnectError")}
    )
    engine = RediscoveryEngine(fetcher, enable_web_search=True, search_fn=boom)
    res = await engine.rediscover(RediscoveryEntry("1", "X", "https://x.com/"))
    assert res.status is RediscoveryStatus.NO_MATCH


async def test_rediscover_many_returns_one_result_per_entry() -> None:
    fetcher = FakeFetcher(
        {
            "https://pedcorhomes.com/": _page(200, "https://www.pedcorliving.com/", b"<html>x</html>"),
            "https://pedcorliving.com/sitemap.xml": _page(200, "https://pedcorliving.com/sitemap.xml", PEDCOR_INDEX),
            "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml": _page(
                200, "https://www.pedcorliving.com/sitemaps/apartments_sitemap.xml", PEDCOR_CHILD
            ),
            "https://dead.com/": _page(None, "https://dead.com/", error="ConnectError"),
        }
    )
    engine = RediscoveryEngine(fetcher)
    entries = [
        RediscoveryEntry("1", "Aaron Lake Apartments", "https://pedcorhomes.com/"),
        RediscoveryEntry("2", "Nowhere", "https://dead.com/"),
    ]
    results = await engine.rediscover_many(entries, concurrency=2)
    assert len(results) == 2
    by_pid = {r.property_id: r.status for r in results}
    assert by_pid["1"] is RediscoveryStatus.RESOLVED
    assert by_pid["2"] is RediscoveryStatus.NEEDS_WEB_SEARCH


# ── CLI loaders ───────────────────────────────────────────────────────────────
def _write_csv(tmp_path: Path) -> Path:
    # utf-8-sig prepends a BOM, matching the real properties.csv export.
    csv_path = tmp_path / "properties.csv"
    csv_path.write_text(
        "apartmentid,name,address,city,state,zip,website\n"
        "45986,Aaron Lake,1 Main St,Carmel,IN,46032,https://pedcorhomes.com/\n"
        "8797,Sagamore,2 Oak Rd,Fort Worth,TX,76107,https://sagamoreaptsliving.com/\n",
        encoding="utf-8-sig",
    )
    return csv_path


def test_load_csv_index_handles_bom(tmp_path: Path) -> None:
    idx = load_csv_index(_write_csv(tmp_path))
    assert idx["45986"]["name"] == "Aaron Lake"
    assert idx["45986"]["website"] == "https://pedcorhomes.com/"
    assert idx["8797"]["city"] == "Fort Worth"


def test_load_entries_from_run_filters_dead_url(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"kind": "output.property_emitted", "property_id": "45986", "verdict": "DEAD_URL"},
                {"kind": "output.property_emitted", "property_id": "8797", "verdict": "SUCCESS"},
                {"kind": "some.other.event", "property_id": "999", "verdict": "DEAD_URL"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    idx = load_csv_index(_write_csv(tmp_path))
    entries = load_entries_from_run(run_dir, idx)
    assert [e.property_id for e in entries] == ["45986"]
    assert entries[0].name == "Aaron Lake"
    assert entries[0].original_url == "https://pedcorhomes.com/"


def test_load_entries_from_queue_fills_from_csv(tmp_path: Path) -> None:
    idx = load_csv_index(_write_csv(tmp_path))
    q = tmp_path / "queue.jsonl"
    q.write_text(
        json.dumps({"property_id": "45986"}) + "\n"
        + json.dumps({"pid": "8797", "name": "Sagamore Overridden", "url": "https://custom.com/"}) + "\n"
        + "not-json\n",
        encoding="utf-8",
    )
    entries = load_entries_from_queue(q, idx)
    assert len(entries) == 2
    assert entries[0].name == "Aaron Lake"  # filled from CSV
    assert entries[0].original_url == "https://pedcorhomes.com/"
    assert entries[1].name == "Sagamore Overridden"  # explicit wins
    assert entries[1].original_url == "https://custom.com/"


def test_write_results_and_summarize_roundtrip(tmp_path: Path) -> None:
    from ma_poc.discovery.rediscovery import RediscoveryResult

    results = [
        RediscoveryResult("1", "https://a/", RediscoveryStatus.RESOLVED, rediscovered_url="https://x/", confidence=0.99),
        RediscoveryResult("2", "https://b/", RediscoveryStatus.NEEDS_WEB_SEARCH),
    ]
    out = tmp_path / "out.jsonl"
    write_results(results, out)
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["status"] == "RESOLVED"
    assert lines[0]["rediscovered_url"] == "https://x/"
    assert summarize(results) == {"RESOLVED": 1, "NEEDS_WEB_SEARCH": 1}
