"""Tests for leasing-portal detection from embedded JSON.

Some property sites (Jonah Digital, headless WordPress marketing
shells, custom React shells) don't host their own unit data — they
inline a configuration blob that points at a third-party leasing
portal (SightMap, RealPage OLL, RentCafe, FunnelLeasing, AppFolio)
where the actual units live. ``detect_embedded_portal_urls`` walks
parsed embedded-JSON blobs and surfaces those URLs as fetch
candidates so link-hop can dispatch them to the matching PMS adapter.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._html_extract import detect_embedded_portal_urls


# ── Unit tests ──────────────────────────────────────────────────────────


def test_detects_sightmap_embed_url() -> None:
    """The Skyline-at-Kessler shape: a Jonah Digital widget config
    block carries the SightMap embed URL and ``integration: engrain``."""
    blobs = [
        {
            "url": "embedded:json-block:sightmap-config",
            "body": {
                "id": "3",
                "embed_url": "https://sightmap.com/embed/8epm6rn0v6d?enable_api=1",
                "integration": "engrain",
                "engrain": {"asset_id": "31355"},
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert len(hints) == 1
    url, portal = hints[0]
    assert "sightmap.com/embed/" in url
    assert portal == "sightmap"


def test_detects_realpage_online_leasing() -> None:
    """RealPage Online Leasing URLs inside an SSR blob."""
    blobs = [
        {
            "url": "embedded:json-block:cfg",
            "body": {
                "leasing": {
                    "iframe_src": "https://onlineleasing.realpage.com/lease/12345"
                }
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [
        ("https://onlineleasing.realpage.com/lease/12345", "realpage_oll")
    ]


def test_detects_rentcafe_apartments_link() -> None:
    """RentCafe ``/apartments/<id>`` pattern inside a config blob."""
    blobs = [
        {
            "url": "embedded:json-block:portal",
            "body": {
                "settings": {
                    "lease_url": "https://www.rentcafe.com/apartments/tx/austin/property-x/?utm=site"
                }
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert len(hints) == 1
    assert "rentcafe.com/apartments/" in hints[0][0]
    assert hints[0][1] == "rentcafe"


def test_detects_funnel_leasing_embed() -> None:
    blobs = [
        {
            "url": "embedded:json-block:funnel",
            "body": {
                "widget": {
                    "embed": "https://funnelleasing.com/embed/abc123",
                }
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://funnelleasing.com/embed/abc123", "funnel")]


def test_detects_appfolio_widget() -> None:
    blobs = [
        {
            "url": "embedded:json-block:cfg",
            "body": {
                "iframe": "https://apartments.appfolio.com/listings"
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://apartments.appfolio.com/listings", "appfolio")]


def test_deep_nested_lookup() -> None:
    """Portal URL buried inside several layers of nested config."""
    blobs = [
        {
            "url": "embedded:json-block:big-cfg",
            "body": {
                "config": {
                    "css": {"font": "Roboto"},
                    "integrations": {
                        "providers": [
                            {"name": "engrain", "url": "https://sightmap.com/embed/abc"},
                            {"name": "google_tag_manager", "url": "https://www.googletagmanager.com/..."}
                        ]
                    }
                }
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert ("https://sightmap.com/embed/abc", "sightmap") in hints


def test_normalises_protocol_relative_urls() -> None:
    """``//sightmap.com/embed/...`` is promoted to ``https://``."""
    blobs = [
        {
            "url": "embedded:json-block:protocol-rel",
            "body": {"embed": "//sightmap.com/embed/xyz"},
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://sightmap.com/embed/xyz", "sightmap")]


def test_unescapes_json_slashes() -> None:
    """JSON-escaped slashes (``\\/``) are normalised before matching.

    json.loads already unescapes those, but the helper is defensive in
    case a caller hands in raw text accidentally.
    """
    blobs = [
        {
            "url": "embedded:json-block:escaped",
            "body": {"u": "https:\\/\\/sightmap.com\\/embed\\/abc"},
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://sightmap.com/embed/abc", "sightmap")]


def test_dedupes_repeated_urls() -> None:
    """Same URL appearing in multiple places yields one hint."""
    blobs = [
        {
            "url": "embedded:json-block:a",
            "body": {
                "primary": "https://sightmap.com/embed/abc",
                "fallback": "https://sightmap.com/embed/abc",
            },
        },
        {
            "url": "embedded:json-block:b",
            "body": {"copy": "https://sightmap.com/embed/abc"},
        },
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://sightmap.com/embed/abc", "sightmap")]


def test_skips_strings_without_url_shape() -> None:
    """Strings that aren't URLs (CSS values, labels) are ignored."""
    blobs = [
        {
            "url": "embedded:json-block:cfg",
            "body": {
                "color": "#cc6600",
                "label": "Apartments",
                "comment": "engrain provides sightmap.com but this isn't a URL",
            },
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == []


def test_no_portal_signal_returns_empty() -> None:
    """A config blob with no portal URLs returns empty."""
    blobs = [
        {
            "url": "embedded:json-block:cfg",
            "body": {"theme": {"primary": "#ffffff"}, "lang": "en"},
        }
    ]
    assert detect_embedded_portal_urls(blobs) == []


def test_empty_blobs_returns_empty() -> None:
    assert detect_embedded_portal_urls([]) == []


def test_handles_blobs_with_no_body() -> None:
    """Malformed blob entries (no ``body``) don't crash."""
    blobs = [{"url": "embedded:misc"}, {"body": None}]
    assert detect_embedded_portal_urls(blobs) == []


def test_handles_list_body() -> None:
    """Some inline blobs are top-level lists, not dicts."""
    blobs = [
        {
            "url": "embedded:json-block:list",
            "body": [
                {"kind": "tracker", "url": "https://google.com"},
                {"kind": "portal", "url": "https://sightmap.com/embed/lst123"},
            ],
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://sightmap.com/embed/lst123", "sightmap")]


def test_engrain_host_recognised() -> None:
    """``embed.engrain.com`` is the SightMap-parent host — also mapped to sightmap."""
    blobs = [
        {
            "url": "embedded:json-block:engrain",
            "body": {"src": "https://embed.engrain.com/foo/bar"},
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints == [("https://embed.engrain.com/foo/bar", "sightmap")]


def test_pattern_specificity_realpage_oll_outranks_generic() -> None:
    """``onlineleasing.realpage.com`` matches realpage_oll, not a generic
    realpage host. The patterns list is checked in priority order, so
    the specific OLL host wins."""
    blobs = [
        {
            "url": "embedded:json-block:realpage",
            "body": {"lease": "https://onlineleasing.realpage.com/path"},
        }
    ]
    hints = detect_embedded_portal_urls(blobs)
    assert hints[0][1] == "realpage_oll"


# ── Integration: generic adapter surfaces portal hints on its result ────


@pytest.mark.asyncio
async def test_portal_hint_survives_full_scrape_chain(monkeypatch) -> None:
    """Verify the attribute propagates: generic adapter SET → AdapterResult →
    scrape() surfacing → result dict key. This is the exact path that was
    breaking in the live run despite the unit test passing.

    2026-05-24: suppresses the SightMap direct probe (shipped same
    day; default-on for canary) so the test fixture's mocked LLM
    path runs as designed. Without the suppress, the probe would
    try to fetch the synthetic SightMap embed URL — likely returning
    real data — and preempt the cascade before generic + LLM run.
    """
    monkeypatch.setenv("DISABLE_SIGHTMAP_DIRECT_PROBE", "1")
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.generic import GenericAdapter
    from ma_poc.pms.scraper import scrape
    from ma_poc.pms.detector import detect_pms

    # Blobs must be ≥ 200 chars to pass extract_embedded_blobs_from_html's
    # size gate. Use realistic shapes padded to meet the threshold.
    two_blob_html = (
        b'<html><head></head><body>\n'
        b'<script type="application/json">\n'
        # 300+ chars to pass the 200-char minimum
        + (
            b'{"version":"1.8.1","base_uri":"/floorplans/","renderable_endpoint":"_fp-renderable",'
            b'"pms_property_id":null,"pricing_as_range":true,"pricing_preserve_decimals":false,'
            b'"adaptive_pricing":false,"adaptive_pricing_persistence":0,"adaptive_pricing_unit_propagate":0}'
        )
        + b'\n</script>\n'
        b'<script type="application/json">\n'
        # Real SightMap embed config shape — also ≥ 200 chars
        + (
            b'{"id":"3","name":"","embed_url":"https://sightmap.com/embed/8epm6rn0v6d?enable_api=1'
            b'&origin=https://skylineatkessler.com","integration":"engrain",'
            b'"engrain":{"asset_id":"31355","disabled_ui":["filters"]},'
            b'"adaptive_width":true,"adaptive_height":false}'
        )
        + b'\n</script>\n'
        b'</body></html>'
    )

    class _FetchResult:
        body = two_blob_html
        network_log: list = []
        outcome = type("O", (), {"value": "OK"})()
        status_code = 200
        elapsed_ms = 100
        headers: dict = {}
        error_signature = None
        final_url = "https://www.skylineatkessler.com/floorplans/"
        render_mode = "RENDER"
        identity_key = "test"
        proxy_label = None

    class _Task:
        url = "https://www.skylineatkessler.com/floorplans/"
        property_id = "11611"

    sub_result = await scrape(
        base_url="https://www.skylineatkessler.com/floorplans/",
        profile=None,
        expected_total_units=None,
        page=None,
        fetch_result=_FetchResult(),
        csv_row=None,
        property_id="11611",
    )

    hints = sub_result.get("_embedded_portal_hints")
    assert hints is not None, (
        "portal hints not surfaced onto scrape() result dict — "
        f"result keys: {[k for k in sub_result.keys() if not k.startswith('__')]}"
    )
    assert len(hints) >= 1
    url, portal = hints[0]
    assert "sightmap.com/embed/" in url
    assert portal == "sightmap"


@pytest.mark.asyncio
async def test_try_link_hop_queues_portal_hint_from_sub_fetch() -> None:
    """End-to-end of the dynamic-discovery path: when a sub-fetch's scrape
    result carries ``_embedded_portal_hints``, ``_try_link_hop`` must
    append that URL onto the queue and fetch it in the same hop pass.

    This is the contract that closes the Skyline-at-Kessler case: hop 1
    visits /floorplans/, finds a SightMap embed URL in the page's
    ``<script type="application/json">`` config, surfaces it on the
    sub_result, and the link-hop loop then fetches sightmap.com on
    hop 2 — where the URL host triggers the SightMap fingerprint and
    its adapter takes over.
    """
    from unittest.mock import patch, MagicMock
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.pms.detector import detect_pms
    from ma_poc import fetch as fetch_mod

    entry_url = "https://www.skylineatkessler.com/"
    floorplans_url = "https://www.skylineatkessler.com/floorplans/"
    sightmap_url = "https://sightmap.com/embed/abc123"

    fetched_urls: list[str] = []

    async def _fake_fetch(task):
        fetched_urls.append(task.url)
        fake_result = MagicMock()
        fake_result.outcome = MagicMock(value="OK")
        fake_result.elapsed_ms = 100
        fake_result.body = b"<html><body>x</body></html>"
        fake_result.network_log = []
        return fake_result

    scrape_results: dict[str, dict] = {
        floorplans_url: {
            "units": [],
            "_embedded_portal_hints": [(sightmap_url, "sightmap")],
        },
        sightmap_url: {
            "units": [{"unit_number": "101", "rent": 1500}],
            "extraction_tier_used": "TIER_1_API_SIGHTMAP",
        },
    }

    async def _fake_scrape(*, base_url, **kwargs):
        return dict(scrape_results.get(base_url, {"units": []}))

    # Patch at the source module so the dynamic import inside
    # ``_try_link_hop`` ("from ma_poc.fetch import fetch as jugnu_fetch")
    # resolves to our stub.
    #
    # Cheap-GET gate off: it probe_get()s the LIVE network per hop, and
    # ``abc123`` is a synthetic embed code that real sightmap.com 404s
    # with a <10KB body — exactly the DEAD_URL_GATED shape — so the hop
    # was retired before ``_fake_fetch`` ever saw it. The gate is
    # behaving correctly there; what this test asserts is the portal-hint
    # queueing contract, so stub the gate out and keep it hermetic.
    with patch.object(fetch_mod, "fetch", new=_fake_fetch), \
         patch.object(scraper_mod, "scrape", new=_fake_scrape), \
         patch.object(
             scraper_mod, "_crawl_get_gate_should_skip", lambda url: False
         ):
        hop_result = await scraper_mod._try_link_hop(
            entry_url=entry_url,
            entry_page_html="<html><a href='/floorplans/'>Floor Plans</a></html>",
            detected=detect_pms(entry_url),
            profile=None,
            expected_total_units=None,
            property_id="11611",
            csv_row=None,
            max_hops=3,
            llm_navigation_hints=["/floorplans/"],
            embedded_portal_hints=None,
            visited_urls={entry_url},
            shared_budget=None,
        )

    assert sightmap_url in fetched_urls, (
        f"expected sightmap URL to be fetched after portal-hint discovery; "
        f"got fetched_urls={fetched_urls}"
    )
    assert hop_result is not None
    assert hop_result.get("units"), (
        f"expected sightmap fetch to recover units; got {hop_result}"
    )


@pytest.mark.asyncio
async def test_generic_adapter_surfaces_portal_hints_from_embedded_json() -> None:
    """End-to-end: generic adapter parses HTML containing a SightMap
    embed config in ``<script type="application/json">``, detects the
    portal URL, and stashes it on ``result._embedded_portal_hints``
    where scrape() can promote it onto the result dict for link-hop.
    """
    from ma_poc.pms.adapters.base import AdapterContext
    from ma_poc.pms.adapters.generic import GenericAdapter
    from ma_poc.pms.detector import detect_pms

    # Realistic shape: HTML with the Jonah-style SightMap embed config.
    # The blob's keys (embed_url, integration, engrain.asset_id) match
    # nothing in _UNIT_SIGNAL_KEYS, so the embedded_json tier correctly
    # returns 0 units. But the portal URL must still be surfaced for
    # link-hop to follow.
    html = """<html><head></head><body>
    <script type="application/json">
    {
      "id": "3",
      "embed_url": "https://sightmap.com/embed/8epm6rn0v6d?enable_api=1",
      "integration": "engrain",
      "engrain": {"asset_id": "31355"},
      "adaptive_width": true
    }
    </script>
    </body></html>
    """

    class _FetchResult:
        body = html.encode("utf-8")

    ctx = AdapterContext(
        base_url="https://example.com/floorplans/",
        detected=detect_pms("https://example.com/floorplans/"),
        profile=None,
        expected_total_units=None,
        property_id="TEST-PORTAL",
        fetch_result=_FetchResult(),
    )
    ctx._api_responses = []  # type: ignore[attr-defined]

    adapter = GenericAdapter()
    result = await adapter.extract(None, ctx)  # type: ignore[arg-type]

    hints = getattr(result, "_embedded_portal_hints", None)
    assert hints is not None, "expected _embedded_portal_hints on adapter result"
    assert len(hints) >= 1
    url, portal = hints[0]
    assert "sightmap.com/embed/" in url
    assert portal == "sightmap"

    # The embedded_json tier should also have logged the detection
    # in tier_attempts so the per-property report shows what fired.
    attempts = getattr(result, "_tier_attempts", []) or []
    detected = [a for a in attempts if a.get("tier_key") == "generic:embedded_portal_detected"]
    assert detected, (
        f"expected 'generic:embedded_portal_detected' attempt; got: "
        f"{[a.get('tier_key') for a in attempts]}"
    )
    assert detected[0]["outcome"] == "ran_hint"
    assert "sightmap" in detected[0]["reason"]


# ── Floor-plan sub-page detection ────────────────────────────────────────────


def test_detect_floorplan_subpage_urls_jonah_index() -> None:
    """detect_floorplan_subpage_urls finds all /floorplans/{slug}/ links
    from a Jonah Digital floor-plan index page.  Requires both the Jonah
    config blob (renderable_endpoint + base_uri) AND the <a href> anchors
    to be present.
    """
    from ma_poc.pms.adapters._html_extract import detect_floorplan_subpage_urls

    blobs = [
        {
            "url": "embedded:json-block:jonah-config",
            "body": {
                "version": "1.8.1",
                "base_uri": "/floorplans/",
                "renderable_endpoint": "_fp-renderable",
                "pms_property_id": None,
            },
        }
    ]
    html = """<html><body>
    <a href="/floorplans/the-kidd-springs/">The Kidd Springs</a>
    <a href="/floorplans/the-edgefield/">The Edgefield</a>
    <a href="/floorplans/the-colorado/">The Colorado</a>
    <a href="/about/">About</a>
    <a href="/contact/">Contact</a>
    </body></html>"""

    hints = detect_floorplan_subpage_urls(
        blobs, html, "https://www.skylineatkessler.com/floorplans/"
    )
    urls = [u for u, _ in hints]
    kinds = {k for _, k in hints}

    assert len(urls) == 3, f"expected 3 sub-pages; got {urls}"
    assert all("floorplans" in u for u in urls)
    assert kinds == {"floorplan_subpage"}
    # Non-floorplan links must be excluded
    assert not any("about" in u or "contact" in u for u in urls)


def test_detect_floorplan_subpage_urls_no_config_returns_empty() -> None:
    """If no Jonah config blob is present, returns empty — avoids false
    positives on pages that happen to have sub-paths."""
    from ma_poc.pms.adapters._html_extract import detect_floorplan_subpage_urls

    blobs: list = []  # no blobs → no detection
    html = '<a href="/floorplans/plan-a/">Plan A</a>'
    result = detect_floorplan_subpage_urls(
        blobs, html, "https://example.com/floorplans/"
    )
    assert result == []


def test_detect_floorplan_subpage_urls_skips_deep_paths() -> None:
    """Links more than one level deeper than base_uri are not sub-pages."""
    from ma_poc.pms.adapters._html_extract import detect_floorplan_subpage_urls

    blobs = [
        {
            "url": "embedded:json-block:cfg",
            "body": {"base_uri": "/floorplans/", "renderable_endpoint": "_fp-renderable"},
        }
    ]
    html = """<html><body>
    <a href="/floorplans/the-edgefield/">Direct sub-page</a>
    <a href="/floorplans/the-edgefield/unit-101/">Deep unit page</a>
    </body></html>"""

    hints = detect_floorplan_subpage_urls(
        blobs, html, "https://example.com/floorplans/"
    )
    urls = [u for u, _ in hints]
    assert len(urls) == 1
    assert "the-edgefield" in urls[0]
    assert "unit-101" not in urls[0]
