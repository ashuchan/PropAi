"""Pure-logic coverage for platform-agnostic browser endpoint discovery."""

from __future__ import annotations

import asyncio

import pytest

import ma_poc.scripts.diagnostics.browser_endpoint_discovery as discovery
from ma_poc.scripts.diagnostics.browser_endpoint_discovery import (
    _WORKFLOW_VERSION,
    BrowserEndpointProbeResult,
    _completed_ids_from_checkpoint_payloads,
    _warm_url,
    _warm_urls,
    api_floorplan_plan_rows,
    availability_control_indexes,
    bounded_response_body,
    classify_non_strict_discovery,
    filter_uncompleted_records,
    is_availability_control,
    is_floorplan_aggregate_response,
    is_inventory_frame_url,
    is_public_detail_drill_url,
    is_public_portal_link,
    is_safe_endpoint_template,
    portal_origin,
    portal_rows_from_html,
    prospectportal_known_template,
    public_plan_pricing,
    public_portal_navigation_url,
    public_route_key,
    resman_date_scoped_url,
    sanitized_xhr_path,
    select_batch_records,
    strict_api_proof,
    unlocked_public_links,
    wait_for_inventory_settle,
    wait_for_network_settle,
)
from ma_poc.scripts.diagnostics.cohort_endpoint_route_plan import (
    DiscoveryRoute,
    RoutePlanRecord,
)


def test_safe_endpoint_rejects_sensitive_or_frozen_request_state() -> None:
    """A durable profile never receives credentials or a one-day date."""
    assert is_safe_endpoint_template("https://api.example.test/v1/units?property=9") is True
    assert is_safe_endpoint_template("https://api.example.test/v1/units?token=secret") is False
    assert is_safe_endpoint_template("https://api.example.test/v1/units?move_in_date=2026-07-27") is False


def test_explicit_direct_device_mode_does_not_construct_a_bright_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-requested local validation stays on the device outbound IP."""
    record = RoutePlanRecord(
        "direct-ip",
        "https://example.test/floorplans/",
        ("https://example.test/floorplans/",),
        "unknown",
        0,
        DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
    )

    class _UnexpectedBrightProvider:
        def __init__(self) -> None:
            raise AssertionError("direct local validation must not create BrightDataProvider")

    monkeypatch.setattr(discovery, "BrightDataProvider", _UnexpectedBrightProvider)
    proxy = discovery.discovery_proxy_config(record, direct_device_ip=True)
    assert proxy.is_direct is True
    assert proxy.tier.value == "direct"


def test_strict_api_proof_requires_unit_and_numeric_rent_in_same_response_row() -> None:
    """Floorplan-only JSON must not become a browser endpoint success."""
    endpoint, rows = strict_api_proof(
        [
            {
                "url": "https://api.example.test/floorplans",
                "body": {"units": [{"floorPlanName": "A1", "price": 1200, "bedrooms": 1}]},
            },
            {
                "url": "https://api.example.test/units",
                "body": {"units": [{"unitNumber": "101", "price": 1234, "bedrooms": 1, "sqft": 700}]},
            },
        ],
        "43",
    )
    assert endpoint == "https://api.example.test/units"
    assert len(rows) == 1
    assert rows[0]["unit_number"] == "101"


def test_floorplan_aggregate_id_cannot_be_promoted_to_a_unit_api_success() -> None:
    """A marketing ``/floorplans/all`` id remains plan evidence, not a unit."""
    url = "https://www.thegrandatwestchase.com/api/v3/floorplans/all/"
    body = [{"id": "plan-a1", "name": "A1", "bedrooms": 1, "price": 1200}]
    assert is_floorplan_aggregate_response(url, body) is True
    assert strict_api_proof([{"url": url, "body": body}], "12989") == (None, ())
    plan_rows = api_floorplan_plan_rows([{"url": url, "body": body}], "12989")
    assert plan_rows[0]["unit_number"] == ""
    assert plan_rows[0]["source_ids"] == {"api_floorplan_id": "plan-a1"}
    assert public_plan_pricing(plan_rows) == (1, 1, 1200, 1200)


def test_floorplans_endpoint_with_explicit_unit_number_remains_api_eligible() -> None:
    """An endpoint name alone cannot suppress concrete unit inventory."""
    url = "https://example.test/api/floorplans/current"
    body = {"units": [{"unitNumber": "101", "price": 1234, "bedrooms": 1}]}
    assert is_floorplan_aggregate_response(url, body) is False
    _, rows = strict_api_proof([{"url": url, "body": body}], "43")
    assert rows[0]["unit_number"] == "101"


def test_plan_pricing_keeps_inferred_plan_rows_out_of_strict_unit_success() -> None:
    """A visible plan price is recorded even though it is not a unit row."""
    assert public_plan_pricing(
        [
            {"unit_number": "inferred_a1", "floor_plan_name": "A1", "market_rent_low": 1200},
            {"unit_number": "", "floor_plan_name": "B1", "market_rent_low": 1350},
            {"unit_number": "101", "floor_plan_name": "A1", "market_rent_low": 1240},
        ]
    ) == (2, 2, 1200, 1350)


def test_non_strict_classification_does_not_hide_a_blocked_unit_portal_as_plan_only() -> None:
    """Marketing plan evidence remains secondary when an Applicant route is blocked."""
    assert (
        classify_non_strict_discovery(
            plan_rows_observed=4,
            blocked_public_paths=(
                "https://bromleyhouse.securecafe.com/onlineleasing/bromley-house/oleapplication.aspx",
                "https://bromleyhouse.securecafeapplicant.com/onlineleasing/content3/access/bromley-house/floorplans/2039041",
            ),
            unlocker_success_paths=(
                "https://bromleyhouse.securecafe.com/onlineleasing/bromley-house/oleapplication.aspx",
            ),
        )
        == discovery.DiscoveryClassification.ACCESS_BLOCKED
    )
    assert (
        classify_non_strict_discovery(
            plan_rows_observed=4,
            blocked_public_paths=("https://example.securecafe.com/floorplans",),
            unlocker_success_paths=("https://example.securecafe.com/floorplans",),
        )
        == discovery.DiscoveryClassification.PUBLIC_PLAN_ONLY
    )
    assert (
        classify_non_strict_discovery(
            plan_rows_observed=4,
            blocked_public_paths=("https://example.securecafe.com/floorplans",),
            unlocker_success_paths=("https://example.securecafe.com/floorplans",),
            unlocker_budget_exhausted=True,
        )
        == discovery.DiscoveryClassification.API_NOT_FOUND_YET
    )


def test_availability_control_matcher_avoids_apply_actions() -> None:
    """Browser automation opens availability UI but never starts an application."""
    assert is_availability_control("Check Availability") is True
    assert is_availability_control("View Available Units") is True
    assert is_availability_control("Apply Now") is False


def test_availability_control_indexes_use_the_preloaded_dom_labels() -> None:
    """A bounded bulk DOM read preserves only safe inventory actions."""
    assert availability_control_indexes(
        ["Apply now", "Floor Plans", "", "Check Availability", "View Available Units"]
    ) == [1, 3, 4]


def test_inventory_frame_filter_skips_non_inventory_widgets_but_keeps_public_pms() -> None:
    """Frame traversal avoids chat/blank documents without losing unit widgets."""
    page_url = "https://www.emersonaustin.com/availability"
    assert is_inventory_frame_url(page_url, page_url) is False
    assert is_inventory_frame_url("about:blank", page_url) is False
    assert is_inventory_frame_url("https://chat.example.test/widget", page_url) is False
    assert is_inventory_frame_url("https://gordon.appfolio.com/listings?property=emerson", page_url) is True
    assert is_inventory_frame_url("https://www.emersonaustin.com/floorplans/a1", page_url) is True


def test_detail_drill_accepts_public_floorplan_but_not_application_navigation() -> None:
    """The generic deep drill stays within public inventory navigation."""
    warm = "https://example.test/floorplans/"
    assert is_public_detail_drill_url("https://example.test/floorplans/a1", warm) is True
    assert is_public_detail_drill_url("https://example.test/apply", warm) is False
    assert is_public_detail_drill_url("https://portal.example.test/floorplans/a1", warm) is False
    assert is_public_detail_drill_url("https://example.test/floorplans/a1#pricing", warm) is False
    assert is_public_detail_drill_url("https://example.test/assets/floorplan.jpg", warm) is False
    assert is_public_detail_drill_url("https://example.test/models", warm) is True


def test_portal_evidence_keeps_only_observed_public_inventory_links() -> None:
    """An audit row can name a public portal but never an application link."""
    warm = "https://www.liveatthemirage.com/floorplans/"
    assert (
        is_public_portal_link("https://themirage.prospectportal.com/arlington/the-mirage/conventional/", warm)
        is True
    )
    assert (
        is_public_portal_link(
            "https://themirage.prospectportal.com/arlington/the-mirage/guest-card/contact-us/1/", warm
        )
        is True
    )
    assert (
        is_public_portal_link(
            "https://themirage.prospectportal.com/Apartments/module/application_authentication/", warm
        )
        is False
    )
    assert (
        portal_origin("https://themirage.prospectportal.com/arlington/the-mirage/guest-card/contact-us/1/")
        == "https://themirage.prospectportal.com/"
    )
    assert public_portal_navigation_url("https://9007790.onlineleasing.realpage.com/#k=71668") == (
        "https://9007790.onlineleasing.realpage.com/#k=71668"
    )
    securecafe_floorplan = (
        "https://bromleyhouse.securecafe.com/onlineleasing/bromley-house/"
        "oleapplication.aspx?stepname=floorplan&myOlePropertyId=477465"
    )
    assert is_public_portal_link(securecafe_floorplan, "https://www.lindyproperty.com/property/bromley-house/")
    assert public_portal_navigation_url(securecafe_floorplan) == securecafe_floorplan
    applicant_floorplan = "https://bromleyhouse.securecafeapplicant.com/onlineleasing/content3/access/bromley-house/floorplans"
    assert is_public_portal_link(applicant_floorplan, "https://www.lindyproperty.com/property/bromley-house/")
    assert public_portal_navigation_url(applicant_floorplan) == applicant_floorplan
    resman_portal = (
        "https://westshore.myresman.com/Portal/Applicants/Availability"
        "?a=1421&p=ac8adee4-fcc4-4257-a488-b53d681fc471"
    )
    assert public_portal_navigation_url(resman_portal) == resman_portal
    assert (
        sanitized_xhr_path("https://api.example.test/units?move_in_date=2026-07-27&token=x")
        == "https://api.example.test/units"
    )


def test_resman_date_scope_is_transient_and_keeps_the_observed_property_url() -> None:
    """A validation run chooses its roster date without persisting that date."""
    portal = (
        "https://westshore.myresman.com/Portal/Applicants/Availability"
        "?a=1421&p=ac8adee4-fcc4-4257-a488-b53d681fc471"
    )
    scoped = resman_date_scoped_url(portal, discovery.date(2026, 7, 27))
    assert "moveInDate=7%2F27%2F2026" in scoped
    assert "refreshPricing=true" in scoped
    assert discovery.without_resman_date_scope(scoped) == portal


def test_unlocked_html_follows_only_observed_public_inventory_routes() -> None:
    """Unlocker escalation follows public plan/portal evidence, never an application link."""
    html = """
    <a href="/floorplans/a1">A1</a>
    <a href="https://themirage.prospectportal.com/arlington/the-mirage/conventional/">Tour</a>
    <a href="/apply">Apply</a>
    <a href="https://themirage.prospectportal.com/Apartments/module/application_authentication/">Lease</a>
    """
    assert unlocked_public_links(html, "https://www.liveatthemirage.com/floorplans/") == [
        "https://www.liveatthemirage.com/floorplans/a1",
        "https://themirage.prospectportal.com/arlington/the-mirage/conventional/",
    ]
    assert public_route_key("https://example.test/floorplans/a1?utm_source=x#pricing") == (
        "https://example.test/floorplans/a1"
    )


def test_stratified_batch_covers_distinct_platform_lanes_before_repeats() -> None:
    """The first browser canary is not accidentally six copies of one PMS."""
    records = [
        RoutePlanRecord(
            "1",
            "https://a.test",
            ("https://a.test",),
            "unknown",
            0,
            DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
        ),
        RoutePlanRecord(
            "2",
            "https://b.test",
            ("https://b.test",),
            "entrata",
            0,
            DiscoveryRoute.ENTRATA_BROWSER_XHR_DISCOVERY,
        ),
        RoutePlanRecord(
            "3",
            "https://c.test",
            ("https://c.test",),
            "unknown",
            0,
            DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
        ),
        RoutePlanRecord(
            "4", "https://d.test", ("https://d.test",), "resman", 0, DiscoveryRoute.RESMAN_PORTAL_DISCOVERY
        ),
    ]
    selected = select_batch_records(records, 3, stratified=True)
    assert [record.canonical_id for record in selected] == ["1", "2", "4"]


def test_warm_url_ignores_saved_pdf_and_expired_application_state() -> None:
    """A previous profile's attachment or stale auth link cannot win routing."""
    known = RoutePlanRecord(
        "11727",
        "https://www.risebedfordlake.com/",
        (
            "https://media.example.test/floorplans/a1.pdf",
            "https://www.risebedfordlake.com/bedford/rise-bedford-lake/conventional/",
            "https://www.risebedfordlake.com/",
        ),
        "entrata",
        1,
        DiscoveryRoute.KNOWN_ENDPOINT_REVALIDATE,
    )
    entrata = RoutePlanRecord(
        "13942",
        "https://ironhorseflats.com/",
        (
            "https://ironhorseflats.prospectportal.com/Apartments/module/application_authentication/lease_start_date=undefined",
            "https://ironhorseflats.com/",
        ),
        "entrata",
        0,
        DiscoveryRoute.ENTRATA_BROWSER_XHR_DISCOVERY,
    )
    assert _warm_url(known) == "https://www.risebedfordlake.com/bedford/rise-bedford-lake/conventional/"
    assert _warm_url(entrata) == "https://ironhorseflats.com/"


def test_portal_warm_url_prefers_data_bearing_resman_and_rentcafe_pages() -> None:
    """Portal lanes select their unit-bearing pages before marketing homepages."""
    resman = RoutePlanRecord(
        "19357",
        "https://newportvillageaptslv.com/",
        (
            "https://newportvillageaptslv.com/floorplans/",
            "https://newearthres.myresman.com/Portal/Applicants/Availability?a=1506&p=a33c14d8-a587-4273-afa9-65cd7919c5d9",
        ),
        "resman",
        0,
        DiscoveryRoute.RESMAN_PORTAL_DISCOVERY,
    )
    rentcafe = RoutePlanRecord(
        "218786",
        "https://example.test/floorplans/",
        ("https://example.securecafe.com/onlineleasing/example/floorplans.aspx",),
        "rentcafe",
        0,
        DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY,
    )
    assert _warm_url(resman) == resman.public_url_candidates[1]
    assert _warm_url(rentcafe) == "https://example.securecafe.com/onlineleasing/example/availableunits.aspx"


def test_known_prospectportal_template_is_kept_for_fresh_dynamic_replay() -> None:
    """A durable profile template is not lost between route planning and browser work."""
    record = RoutePlanRecord(
        "11727",
        "https://www.risebedfordlake.com/",
        ("https://www.risebedfordlake.com/bedford/rise-bedford-lake/conventional/",),
        "entrata",
        1,
        DiscoveryRoute.KNOWN_ENDPOINT_REVALIDATE,
        (
            "https://www.risebedfordlake.com/?module=check_availability&action=view_unit_spaces"
            "&property_floorplan[id]={floorplan_id}&move_in_date={move_in_date}",
        ),
    )
    assert prospectportal_known_template(record) == record.known_endpoint_templates[0]


def test_warm_url_ranking_retains_a_public_fallback_after_a_blocked_portal() -> None:
    """A 403 on a saved portal still permits the same-session public fallback."""
    record = RoutePlanRecord(
        "220593",
        "https://www.eaglecreekcourtapts.com/",
        (
            "https://eaglecreekcourtapts.securecafe.com/floorplans/",
            "https://www.eaglecreekcourtapts.com/",
        ),
        "rentcafe",
        0,
        DiscoveryRoute.RENTCAFE_PORTAL_DISCOVERY,
    )
    assert _warm_urls(record) == [
        "https://eaglecreekcourtapts.securecafe.com/floorplans/",
        "https://www.eaglecreekcourtapts.com/",
    ]


def test_resman_portal_parser_returns_concrete_unit_with_numeric_rent() -> None:
    """A ResMan script payload is strict SSR unit evidence, not plan evidence."""
    record = RoutePlanRecord(
        "19357",
        "https://newportvillageaptslv.com/",
        (),
        "resman",
        0,
        DiscoveryRoute.RESMAN_PORTAL_DISCOVERY,
    )
    html = (
        '<script>var unitTypes = [{"Bedrooms":1,"Bathrooms":1,'
        '"Units":[{"Number":"4305","UnitType":"A1",'
        '"Pricing":[{"Rent":1405}]}]}];</script>'
    )
    rows = portal_rows_from_html(
        record, html, "https://newearthres.myresman.com/Portal/Applicants/Availability?a=1&p=x"
    )
    assert rows[0]["unit_number"] == "4305"
    assert rows[0]["market_rent_low"] == 1405


def test_generic_discovery_parses_an_observed_resman_portal() -> None:
    """A portal linked by a generic marketing site retains its concrete units."""
    record = RoutePlanRecord(
        "19357", "https://riverwalkapartments.com/", (), "unknown", 0,
        DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
    )
    html = (
        '<script>var unitTypes = [{"Bedrooms":1,"Bathrooms":1,'
        '"Units":[{"Number":"216-306","UnitType":"A1",'
        '"Pricing":[{"Rent":1399}]}]}];</script>'
    )
    rows = portal_rows_from_html(
        record, html, "https://westshore.myresman.com/Portal/Applicants/Availability?a=1&p=x"
    )
    assert rows[0]["unit_number"] == "216-306"


def test_apollo_unit_detail_parser_is_available_to_generic_browser_discovery() -> None:
    """A public Apollo roster is strict only when the card identifies an apartment."""
    record = RoutePlanRecord(
        "16377",
        "https://www.westshoretampabay.com/",
        ("https://www.westshoretampabay.com/",),
        "unknown",
        0,
        DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
    )
    html = """
    <div class="unit-details" data-unit-id="deaf17c5-5634-4166-8004-04a738b82216"
         data-unit-code="104" data-availabledate="1786752000000"
         data-rent-min="1450.00" data-rent-max="1450.00">
      <div class="unit-header"><h3 class="standard">Apartment 104</h3></div>
      <ul class="list-divider"><li>1 Bed</li><li>1 Bath</li><li>465 SqFt</li></ul>
    </div>
    <div class="unit-details" data-rent-min="1300.00">
      <div class="unit-header"><h3 class="standard">Westshore Studio with Nook C511</h3></div>
    </div>
    """
    rows = portal_rows_from_html(
        record,
        html,
        "https://www.westshoretampabay.com/Marketing/FloorPlans/Units/c6c56a28-412c-4bad-aa2a-37c684c8084a",
    )
    assert [(row["unit_number"], row["market_rent_low"]) for row in rows] == [("104", 1450)]
    assert rows[0]["source_ids"] == {"rs365_unit_guid": "deaf17c5-5634-4166-8004-04a738b82216"}


def test_resume_uses_only_current_workflow_checkpoint_rows() -> None:
    """A corrected workflow retries v1 evidence but never repeats its own v2 rows."""
    completed = _completed_ids_from_checkpoint_payloads(
        [
            '{"workflow_version":"browser-endpoint-discovery-v2","canonical_id":"old"}\n'
            f'{{"workflow_version":"{_WORKFLOW_VERSION}","canonical_id":"done"}}\n'
            "not-json\n"
        ]
    )
    records = [
        RoutePlanRecord(
            "done", "https://done.test", (), "unknown", 0, DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY
        ),
        RoutePlanRecord(
            "next", "https://next.test", (), "unknown", 0, DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY
        ),
    ]
    assert completed == {"done"}
    assert [record.canonical_id for record in filter_uncompleted_records(records, completed)] == ["next"]


def test_resume_retries_timeout_checkpoint_evidence() -> None:
    """A browser timeout is audit evidence, not a completed negative result."""
    completed = _completed_ids_from_checkpoint_payloads(
        [
            f'{{"workflow_version":"{_WORKFLOW_VERSION}","canonical_id":"timed","error":"property-timeout"}}\n'
            f'{{"workflow_version":"{_WORKFLOW_VERSION}","canonical_id":"done","error":null}}\n'
        ]
    )
    assert completed == {"done"}


@pytest.mark.asyncio
async def test_network_settle_is_best_effort_after_a_navigation() -> None:
    """A persistent analytics request cannot fail a public discovery attempt."""

    class _Page:
        calls: list[tuple[str, int]] = []

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            self.calls.append((state, timeout))

    page = _Page()
    assert await wait_for_network_settle(page) is True
    assert page.calls == [("networkidle", 8_000)]


@pytest.mark.asyncio
async def test_inventory_settle_waits_for_one_bounded_iframe_render_window() -> None:
    """A populated iframe can arrive immediately after the document becomes idle."""

    class _Page:
        calls: list[tuple[str, int]] = []

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            self.calls.append((state, timeout))

        async def wait_for_timeout(self, timeout: int) -> None:
            self.calls.append(("render", timeout))

    page = _Page()
    assert await wait_for_inventory_settle(page) is True
    assert page.calls == [("networkidle", 8_000), ("render", 1_500)]


@pytest.mark.asyncio
async def test_bounded_response_body_drops_only_an_unfinished_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived widget response cannot hold the property gather open."""

    class _FastResponse:
        async def body(self) -> bytes:
            return b'{"units": []}'

    class _SlowResponse:
        async def body(self) -> bytes:
            await asyncio.sleep(1)
            return b"never reached"

    monkeypatch.setattr(discovery, "_CAPTURE_BODY_TIMEOUT_SECONDS", 0.001)
    assert await bounded_response_body(_FastResponse()) == b'{"units": []}'
    assert await bounded_response_body(_SlowResponse()) is None


@pytest.mark.asyncio
async def test_checkpoint_records_negative_path_browser_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-proof outcome says whether the browser actually provoked XHR."""

    async def _fake_capture(**_kwargs: object) -> BrowserEndpointProbeResult:
        return BrowserEndpointProbeResult(
            warm_status=200,
            classification=discovery.DiscoveryClassification.API_NOT_FOUND_YET,
            warm_page_url="https://example.test/floorplans/",
            controls_clicked=2,
            controls_matched=4,
            frames_seen=2,
            max_dom_rows_seen=3,
            networkidle_reached=True,
            xhr_total_seen=7,
            api_responses_considered=3,
            capture_truncated=False,
            bodies_dropped_oversize=1,
            forms_present=True,
            navigation_levels_reached=2,
        )

    monkeypatch.setattr(discovery, "_capture_browser_property", _fake_capture)

    class _ProxyProvider:
        def get_config(self, **_kwargs: object) -> object:
            return object()

    monkeypatch.setattr(discovery, "BrightDataProvider", _ProxyProvider)
    record = RoutePlanRecord(
        "telemetry",
        "https://example.test/floorplans/",
        ("https://example.test/floorplans/",),
        "unknown",
        0,
        DiscoveryRoute.GENERIC_BROWSER_XHR_DISCOVERY,
    )

    class _Identities:
        def pick_chrome_only(self, *, sticky_key: str) -> object:
            assert sticky_key == "telemetry"
            return object()

    class _Pool:
        async def acquire(self, _identity: object, *, proxy: object) -> object:
            assert proxy is not None
            return object()

    payload = await discovery._probe_record(
        client=object(),  # type: ignore[arg-type]
        profile_prefix="gs://unused/profiles",
        pool=_Pool(),  # type: ignore[arg-type]
        identities=_Identities(),  # type: ignore[arg-type]
        record=record,
        commit_profiles=False,
    )
    assert payload["controls_matched"] == 4
    assert payload["frames_seen"] == 2
    assert payload["max_dom_rows_seen"] == 3
    assert payload["networkidle_reached"] is True
    assert payload["xhr_total_seen"] == 7
    assert payload["bodies_dropped_oversize"] == 1
    assert payload["forms_present"] is True
    assert payload["navigation_levels_reached"] == 2
