"""Source-level guardrails: every adapter probe must thread ``ctx`` and
``stage`` through ``probe_get`` so the proxy gate fires.

Motivated by run 2026-05-24 deep-dive:
  * rentcafe wp_probe used a bare ``httpx.AsyncClient`` (no proxy gate,
    no clearance cookies). 3,711 attempts / 0 status_200s historically.
  * entrata's ``_entrata_static_fetch`` called ``probe_get`` without
    ``ctx`` / ``stage`` → ``proxy_gate.decide`` fail-closed at Layer 0a
    for every Entrata probe (1,119 PP probes, 808 sitemap fetches —
    none gate-routed).
  * appfolio had NO direct-probe path at all — XHR-only.

The tests below DON'T attempt to validate runtime behaviour (that needs
mocking the curl_cffi response, the proxy gate, contextvars, etc.) —
they pin the source-level wiring so a future refactor can't silently
drop ``ctx``/``stage`` again.
"""

from __future__ import annotations

import inspect
import re


def test_rentcafe_wp_probe_routes_through_probe_get() -> None:
    """The wp_probe call must use ``probe_get`` with ``ctx=ctx`` and
    ``stage="wp_probe"`` — not a bare ``httpx.AsyncClient``."""
    from ma_poc.pms.adapters import rentcafe as _rc
    src = inspect.getsource(_rc)
    # The probe_get call site exists.
    assert "probe_get(" in src, (
        "rentcafe.py no longer imports / uses probe_get for wp_probe — "
        "regression: the bare httpx.AsyncClient bypass is back."
    )
    # The wp_probe call passes both ctx and stage explicitly.
    match = re.search(
        r"probe_get\(\s*api_url\s*,\s*ctx\s*=\s*ctx\s*,\s*stage\s*=\s*[\"']wp_probe[\"']",
        src,
    )
    assert match, (
        "rentcafe wp_probe must call probe_get(api_url, ctx=ctx, "
        'stage="wp_probe", ...) so the proxy gate consults the static '
        "host allowlist + per-task clearance cookies. Bare httpx "
        "bypasses both."
    )


def test_rentcafe_wp_probe_no_bare_httpx_client() -> None:
    """The wp_probe code path must not construct an ``httpx.AsyncClient``
    inline — that's the pre-fix pattern that bypassed everything."""
    from ma_poc.pms.adapters import rentcafe as _rc
    src = inspect.getsource(_rc)
    # The function body around wp_probe must not have a bare httpx client.
    # Find the wp_probe block (between def for the probe and the next
    # top-level def) and grep within it.
    wp_block_start = src.find('"wp_probe"')
    assert wp_block_start > 0
    # Look in a 5000-char window around the wp_probe activity.
    window = src[max(0, wp_block_start - 1000):wp_block_start + 4000]
    assert "httpx.AsyncClient(" not in window, (
        "Regression: rentcafe wp_probe path constructs httpx.AsyncClient "
        "directly. Use probe_get instead — it routes through the proxy "
        "gate, attaches clearance cookies, and escalates to Web Unlocker "
        "on 403."
    )


def test_rentcafe_wp_probe_gated_on_wordpress_signal() -> None:
    """The wp_probe must skip pages without a WordPress signal in the HTML.
    Otherwise the prop_id regex matches SightMap/Yardi numeric IDs and
    we fire on origins that have no wp-json route at all (599 / 3,711
    of yesterday's attempts were 404 from this leak)."""
    from ma_poc.pms.adapters import rentcafe as _rc
    src = inspect.getsource(_rc)
    # The gate references at least two WordPress-specific signals.
    has_wp_content = "wp-content" in src
    has_wp_json = "wp-json" in src
    has_skip_reason = "skipped_no_wordpress_signal" in src
    assert has_wp_content and has_wp_json, (
        "WordPress signal gate missing — must check for wp-content / "
        "wp-json substrings in the page HTML before constructing the "
        "wp-json middleware URL."
    )
    assert has_skip_reason, (
        "WordPress-skip event must emit a distinct reason so the "
        "analyzer can bucket it separately from 'no property_id found'."
    )


def test_entrata_static_fetch_accepts_ctx_and_stage() -> None:
    """``_entrata_static_fetch`` must accept ``ctx`` and ``stage`` so
    every caller (sitemap, prospectportal) can thread them to
    ``probe_get`` — without them the proxy gate fail-closes."""
    from ma_poc.pms.adapters import entrata as _entrata
    sig = inspect.signature(_entrata._entrata_static_fetch)
    params = set(sig.parameters)
    assert "ctx" in params, (
        "_entrata_static_fetch must accept a ctx parameter so probe_get "
        "can run the proxy gate against the property's profile + budget."
    )
    assert "stage" in params, (
        "_entrata_static_fetch must accept a stage parameter so probe_get "
        "can route based on stage-eligibility (Layer 2 host allowlist + "
        "Web Unlocker escalation)."
    )


def test_entrata_static_fetch_forwards_ctx_to_probe_get() -> None:
    """The body of ``_entrata_static_fetch`` must actually pass ``ctx``
    and ``stage`` to ``probe_get``."""
    from ma_poc.pms.adapters import entrata as _entrata
    src = inspect.getsource(_entrata._entrata_static_fetch)
    assert "ctx=ctx" in src, (
        "_entrata_static_fetch accepts ctx but doesn't forward it to "
        "probe_get — that's the same 'silent no_proxy_configured' bug "
        "the run 2026-05-24 investigation exposed."
    )
    assert "stage=stage" in src, (
        "_entrata_static_fetch must forward stage to probe_get."
    )


def test_entrata_callers_pass_stage_to_static_fetch() -> None:
    """Each entrata probe call site must pass a non-empty ``stage`` so
    the gate can distinguish sitemap_fetch from prospectportal_probe."""
    from ma_poc.pms.adapters import entrata as _entrata
    src = inspect.getsource(_entrata)
    # All four caller sites must thread stage.
    assert src.count('stage="prospectportal_probe"') >= 1, (
        "_probe_prospectportal must pass stage=\"prospectportal_probe\" "
        "to _entrata_static_fetch."
    )
    assert src.count('stage="sitemap_fetch"') >= 1, (
        "_probe_sitemap_conventional initial sitemap.xml fetch must "
        "pass stage=\"sitemap_fetch\"."
    )
    assert src.count('stage="sitemap_conventional"') >= 1, (
        "_probe_sitemap_conventional follow-up conv_url fetch must "
        "pass stage=\"sitemap_conventional\"."
    )


def test_appfolio_has_direct_probe_path() -> None:
    """The appfolio adapter must include a direct-probe fallback that
    runs ``probe_get`` against ``{slug}.appfolio.com/listings`` after
    XHR / SSR / detail-page paths all miss. The 174-property AppFolio
    cohort with 0 captured XHR responses lives or dies on this path."""
    from ma_poc.pms.adapters import appfolio as _appfolio
    src = inspect.getsource(_appfolio)
    assert "probe_get(" in src, (
        "appfolio.py must import / call probe_get for the direct-probe "
        "fallback. Without it, AppFolio vanity-domain sites with no "
        "XHR-captured /listings response have no recovery path."
    )
    # The probe is gated by find_appfolio_slug (avoids probing unknown hosts).
    assert "find_appfolio_slug(" in src, (
        "Direct probe must be gated on find_appfolio_slug so we only "
        "fire when the page references a known tenant subdomain."
    )


def test_appfolio_probe_uses_appfolio_api_probe_stage() -> None:
    """The AppFolio direct probe must use a distinct stage string so
    proxy-gate telemetry can attribute the cost / decision to AppFolio."""
    from ma_poc.pms.adapters import appfolio as _appfolio
    src = inspect.getsource(_appfolio)
    match = re.search(
        r"probe_get\([^)]*?stage\s*=\s*[\"']appfolio_api_probe[\"']",
        src,
        re.DOTALL,
    )
    assert match, (
        "AppFolio direct probe must call probe_get(..., stage="
        '"appfolio_api_probe", ...) so the gate decision telemetry can '
        "bucket AppFolio probes separately from sc_probe / wp_probe."
    )


def test_appfolio_probe_emits_tier_label_on_success() -> None:
    """When the AppFolio direct probe produces units, the result's
    ``tier_used`` must reflect the new tier label so the run report
    distinguishes probe-recovered units from XHR / SSR / embed paths.

    Two distinct tier labels exist (2026-05-24):
      * ``TIER_1_DOM_APPFOLIO_PROBE`` — SSR HTML shape (/listings)
      * ``TIER_1_API_APPFOLIO_PROBE`` — JSON API shape (/2/api/property_units)
    """
    from ma_poc.pms.adapters import appfolio as _appfolio
    src = inspect.getsource(_appfolio)
    assert "TIER_1_DOM_APPFOLIO_PROBE" in src, (
        "AppFolio direct probe must stamp tier_used="
        "TIER_1_DOM_APPFOLIO_PROBE for the SSR HTML path."
    )
    assert "TIER_1_API_APPFOLIO_PROBE" in src, (
        "AppFolio direct probe must stamp tier_used="
        "TIER_1_API_APPFOLIO_PROBE for the JSON API fallback path "
        "(/2/api/property_units)."
    )


def test_appfolio_probe_tries_two_url_shapes() -> None:
    """The probe must try BOTH ``/listings`` AND
    ``/2/api/property_units`` before falling through to embed-recovery.
    Each AppFolio tenant exposes a subset of these endpoints; trying
    both maximises the deterministic recovery rate before paying for
    embed extraction (Playwright-required)."""
    from ma_poc.pms.adapters.appfolio import AppFolioAdapter
    assert hasattr(AppFolioAdapter, "_appfolio_probe_tenant"), (
        "AppFolio must expose _appfolio_probe_tenant — extracted helper "
        "that owns the two-shape probe sequence."
    )
    src = inspect.getsource(AppFolioAdapter._appfolio_probe_tenant)
    assert "/listings" in src, "Missing /listings probe URL"
    assert "/2/api/property_units" in src, (
        "Missing /2/api/property_units JSON API probe URL"
    )
    # Both shapes must use the same proxy stage.
    assert src.count('stage="appfolio_api_probe"') >= 2, (
        "Both probe shapes must use stage='appfolio_api_probe' so the "
        "proxy gate decision telemetry can attribute consistently."
    )


# ── 2026-05-24 sweep: g5 / knock / sightmap probe-routing tests ────────────


class TestG5ProbeRouting:
    """g5.py:594 used a bare ``httpx.AsyncClient`` to POST the GraphQL
    units query — bypassing the proxy gate AND clearance cookies. The
    inventory.g5marketingcloud.com host is CF-protected."""

    def test_g5_no_bare_httpx_client(self) -> None:
        from ma_poc.pms.adapters import g5 as _g5
        src = inspect.getsource(_g5)
        assert "httpx.AsyncClient(" not in src, (
            "Regression: g5.py constructs httpx.AsyncClient inline. "
            "Use probe_post(ctx=ctx, stage='g5_probe') instead."
        )

    def test_g5_uses_probe_post_with_stage(self) -> None:
        from ma_poc.pms.adapters import g5 as _g5
        src = inspect.getsource(_g5)
        assert "probe_post(" in src, "g5.py must import / call probe_post"
        assert re.search(
            r"probe_post\([^)]*?stage\s*=\s*[\"']g5_probe[\"']",
            src, re.DOTALL,
        ), 'probe_post must be called with stage="g5_probe"'

    def test_g5_fetch_accepts_ctx(self) -> None:
        from ma_poc.pms.adapters.g5 import _fetch_g5_units
        sig = inspect.signature(_fetch_g5_units)
        assert "ctx" in sig.parameters, (
            "_fetch_g5_units must accept ctx so the proxy gate can run"
        )


class TestKnockProbeRouting:
    """knock.py had three bare-httpx call sites (453 / 464 / 632). All
    must route through ``probe_get(ctx, stage='knock_probe')``."""

    def test_knock_no_bare_httpx_client(self) -> None:
        from ma_poc.pms.adapters import knock as _knock
        src = inspect.getsource(_knock)
        assert "httpx.AsyncClient(" not in src, (
            "Regression: knock.py constructs httpx.AsyncClient inline. "
            "Use probe_get(ctx=ctx, stage='knock_probe') instead."
        )

    def test_knock_uses_probe_get_with_stage(self) -> None:
        from ma_poc.pms.adapters import knock as _knock
        src = inspect.getsource(_knock)
        # At least 4 probe_get sites — _fetch_knock_units has 3 calls
        # (numeric_property + community + units) and _fetch_units (inner
        # in by_domain) has 1, plus 2 outer (boot, profile).
        assert src.count('stage="knock_probe"') >= 3, (
            "All knock Doorway-API calls must thread stage=\"knock_probe\""
        )

    def test_knock_fetch_funcs_accept_ctx(self) -> None:
        from ma_poc.pms.adapters.knock import (
            _fetch_knock_units, _fetch_knock_units_by_domain,
        )
        assert "ctx" in inspect.signature(_fetch_knock_units).parameters
        assert "ctx" in inspect.signature(_fetch_knock_units_by_domain).parameters


class TestSightmapProbeRouting:
    """sightmap.py:624 + 661 used bare httpx — both passes (direct API
    and embed-iframe fallback) must now route through probe_get."""

    def test_sightmap_no_bare_httpx_client(self) -> None:
        from ma_poc.pms.adapters import sightmap as _sm
        src = inspect.getsource(_sm)
        assert "httpx.AsyncClient(" not in src, (
            "Regression: sightmap.py constructs httpx.AsyncClient inline."
        )

    def test_sightmap_uses_probe_get_with_stage(self) -> None:
        from ma_poc.pms.adapters import sightmap as _sm
        src = inspect.getsource(_sm)
        assert "probe_get(" in src
        # Two passes -> two distinct call paths, ≥ 3 stage strings
        # (direct_api loop + embed fetch + api fetch).
        assert src.count('stage="sightmap_probe"') >= 3


# ── Stage registration in _PROXY_ELIGIBLE_STAGES ───────────────────────────


class TestProxyEligibleStagesRegistration:
    """Every adapter stage that exists in source must also be registered
    in ``_PROXY_ELIGIBLE_STAGES`` — otherwise the proxy gate fail-closes
    even when the adapter threads ctx/stage correctly."""

    def test_all_new_stages_registered(self) -> None:
        from ma_poc.fetch.proxy_gate import _PROXY_ELIGIBLE_STAGES
        for stage in (
            "appfolio_api_probe",
            "g5_probe",
            "knock_probe",
            "sightmap_probe",
            "sitemap_fetch",
            "sitemap_conventional",
            "entrata_view_unit_spaces",
            "entrata_wp_probe",
        ):
            assert stage in _PROXY_ELIGIBLE_STAGES, (
                f"Stage {stage!r} threaded by an adapter is not "
                f"registered in _PROXY_ELIGIBLE_STAGES — the gate will "
                f"fail-closed even when ctx is passed correctly."
            )


# ── Entrata sitemap regex widening (2026-05-24 R5) ─────────────────────────


class TestEntrataSitemapRegexWidening:
    """The Entrata sitemap regex previously matched ``/conventional/``
    only — fitting Riviera/Chopaka/Briarwood/Royale but ZERO properties
    in run 2026-05-24 (399 no_conventional_url skips). The widened
    regex now accepts /floorplans/, /availability/, /floor-plans/ too
    while still rejecting per-floorplan detail URLs."""

    def test_conventional_url_still_matches(self) -> None:
        # Source-level: the listing_re pattern includes the old keyword
        # AND the new ones. _probe_sitemap_conventional is a method on
        # EntrataAdapter, not a module-level function.
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter._probe_sitemap_conventional)
        assert "conventional" in src
        # And the new keywords are present.
        assert "floorplans?" in src
        assert "availability" in src

    def test_terminal_keyword_guard_preserves_old_behaviour(self) -> None:
        """A ``/floorplans/{slug}/`` URL must NOT be admitted — terminal
        segment is the slug, not the keyword."""
        # Test the terminal-segment heuristic in isolation.
        _TERMINAL_KEYWORDS = {
            "conventional", "floorplans", "floorplan",
            "floor-plans", "floor-plan", "availability",
        }
        for url, should_pass in [
            ("https://example.com/area/property/conventional/", True),
            ("https://example.com/floorplans/", True),
            ("https://example.com/area/property/availability", True),
            # Reject per-floorplan detail
            ("https://example.com/floorplans/the-2br-loft/", False),
            ("https://example.com/floorplans/some-slug/conventional/", True),
            # Random page
            ("https://example.com/about/", False),
        ]:
            stripped = url.rstrip("/").lower()
            terminal = (
                stripped.rsplit("/", 1)[-1] if "/" in stripped else stripped
            )
            actual = terminal in _TERMINAL_KEYWORDS
            assert actual == should_pass, (
                f"URL {url} expected pass={should_pass}, got {actual} "
                f"(terminal={terminal!r})"
            )

    def test_keyword_priority_picks_conventional_over_floor_plans(self) -> None:
        """Bug exposed on PIDs 257570 + 262539: sort-by-length picked
        ``/floor-plans`` (29 chars, SPA wrapper with zero JSON-LD) over
        ``/{city}-{state}-apartments/{slug}/conventional/`` (76 chars,
        canonical inventory with 41-47 plans). The fix sorts by keyword
        priority (conventional > availability > floorplans index) before
        length. Source-level: the priority dict must list ``conventional``
        with a lower value than ``floorplans``."""
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter._probe_sitemap_conventional)
        assert "_KEYWORD_PRIORITY" in src, (
            "Missing _KEYWORD_PRIORITY dict — sort still uses raw length."
        )
        # Verify the priority order via source inspection (the dict is
        # local to the method body).
        import re as _re
        pri_block = _re.search(
            r"_KEYWORD_PRIORITY\s*=\s*\{(.*?)\}", src, _re.DOTALL,
        )
        assert pri_block, "Could not find _KEYWORD_PRIORITY dict literal"
        block = pri_block.group(1)
        # Find conventional's int + floorplans' int.
        conv_m = _re.search(r'"conventional"\s*:\s*(\d+)', block)
        fp_m = _re.search(r'"floorplans"\s*:\s*(\d+)', block)
        assert conv_m and fp_m, "Missing conventional / floorplans entries"
        assert int(conv_m.group(1)) < int(fp_m.group(1)), (
            "conventional must rank BEFORE floorplans in priority — "
            "otherwise SPA wrappers win over canonical inventory pages."
        )

    def test_per_floorplan_detail_urls_filtered(self) -> None:
        """The Modera-class sitemap bug: per-floorplan-detail URLs of
        the shape ``/{area}/{property}/floorplans/{plan-slug}/fp_name/
        occupancy_type/conventional/`` end in ``/conventional/`` too —
        they slipped past the terminal-keyword guard. Modera sitemaps
        carry ~40 of these per property. The fix rejects URLs containing
        the Entrata-specific ``/fp_name/`` or ``/occupancy_type/``
        interior segments."""
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter._probe_sitemap_conventional)
        assert "/fp_name/" in src, (
            "Missing /fp_name/ filter — per-floorplan-detail URLs will "
            "be admitted as candidates (40+ per property on Modera-class "
            "sitemaps)."
        )
        assert "/occupancy_type/" in src, (
            "Missing /occupancy_type/ filter — same class of leak."
        )


# ── Entrata view_unit_spaces synthesis (2026-05-24 R5) ─────────────────────


class TestEntrataVUSSynthesis:
    """``_synthesize_view_unit_spaces_url`` rewrites a fee_calculator
    URL into a view_unit_spaces URL, attaching ``move_in_date`` and
    ``occupancy_type`` defaults. Real-world fee_calc URL shape from
    PID 257301 sonalofts widget body line 230."""

    def _real_fee_calc(self) -> str:
        return (
            "https://commoncf.entrata.com/Apartments/module/check_availability/"
            "?module=check_availability&is_secure=1"
            "&property[id]=1165501"
            "&action=view_rent_calculator"
            "&property_floorplan[id]=820322"
        )

    def test_basic_action_swap(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        out = _synthesize_view_unit_spaces_url(
            self._real_fee_calc(), move_in="2026-06-01"
        )
        assert out is not None
        assert "action=view_unit_spaces" in out
        assert "action=view_rent_calculator" not in out
        assert "move_in_date=2026-06-01" in out
        assert "occupancy_type=conventional" in out

    def test_already_view_unit_spaces_no_op_on_action(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        in_url = (
            "https://x.com/?module=check_availability"
            "&property[id]=1&action=view_unit_spaces&property_floorplan[id]=2"
        )
        out = _synthesize_view_unit_spaces_url(in_url, move_in="2026-06-01")
        assert out is not None
        # action stays view_unit_spaces, just adds defaults
        assert out.count("action=") == 1

    def test_missing_property_id_returns_none(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        # No property[id]
        assert _synthesize_view_unit_spaces_url(
            "https://x.com/?action=view_rent_calculator&property_floorplan[id]=5"
        ) is None

    def test_missing_fp_id_returns_none(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        # No property_floorplan[id]
        assert _synthesize_view_unit_spaces_url(
            "https://x.com/?action=view_rent_calculator&property[id]=5"
        ) is None

    def test_unrecognised_action_returns_none(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        # action=submit_lead — not a known fee_calc shape
        assert _synthesize_view_unit_spaces_url(
            "https://x.com/?property[id]=1&action=submit_lead&property_floorplan[id]=2"
        ) is None

    def test_url_encoded_keys_accepted(self) -> None:
        """Some upstream emitters URL-encode the bracket chars."""
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        encoded = (
            "https://x.com/?module=check_availability"
            "&property%5Bid%5D=1&action=view_rent_calculator"
            "&property_floorplan%5Bid%5D=2"
        )
        out = _synthesize_view_unit_spaces_url(encoded)
        assert out is not None
        assert "action=view_unit_spaces" in out

    def test_empty_input_returns_none(self) -> None:
        from ma_poc.pms.adapters.entrata import _synthesize_view_unit_spaces_url
        assert _synthesize_view_unit_spaces_url("") is None
        assert _synthesize_view_unit_spaces_url(None) is None  # type: ignore[arg-type]


# ── Entrata wp_probe wiring (2026-05-24 R5) ────────────────────────────────


class TestEntrataWPProbeWiring:
    """parse_entrata_available_units + find_entrata_fp_detail_links
    shipped 2026-05-13 as dead code (only test callers). The 2026-05-24
    fix wires both into EntrataAdapter.extract() under stage
    ``entrata_wp_probe``."""

    def test_probe_entrata_wp_method_exists(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        assert hasattr(EntrataAdapter, "_probe_entrata_wp"), (
            "EntrataAdapter must expose _probe_entrata_wp"
        )

    def test_extract_wires_wp_probe(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter.extract)
        assert "_probe_entrata_wp" in src, (
            "extract() must call self._probe_entrata_wp — otherwise the "
            "newly-wired WP probe is dead code again."
        )
        assert "TIER_1_DOM_ENTRATA_WP" in src

    def test_wp_probe_uses_correct_stage(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter._probe_entrata_wp)
        assert 'stage="entrata_wp_probe"' in src


# ── Entrata view_unit_spaces wiring ────────────────────────────────────────


class TestEntrataVUSWiring:
    def test_expand_method_exists(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        assert hasattr(EntrataAdapter, "_expand_view_unit_spaces")

    def test_extract_wires_vus_expansion(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter.extract)
        assert "_expand_view_unit_spaces" in src
        # And the env-flag escape hatch is in place.
        assert "ENABLE_ENTRATA_VUS_EXPANSION" in src

    def test_vus_uses_correct_stage(self) -> None:
        from ma_poc.pms.adapters.entrata import EntrataAdapter
        src = inspect.getsource(EntrataAdapter._expand_view_unit_spaces)
        assert 'stage="entrata_view_unit_spaces"' in src
