"""Entrata Prospect Portal per-plan unit-card drill tests.

Covers ``parse_entrata_pp_unit_cards`` and ``find_entrata_pp_plan_links``
(both added 2026-05-25 to close canary 1ef1060 regression #9 — 212-
property cohort, ~1,759 units flipped from synthetic ``inferred_<sha16>``
ids to real PP ``unit_number`` values).

Fixtures are unmodified live captures (curl_cffi chrome120, 2026-05-25)
from the two user-flagged URLs:

  * https://www.risewestarlington.com/floorplans/arlington-TX/
    rise-west-arlington/a1-silver-1212885-1/   (1 unit, fpid 1212885)
  * https://foxlake.prospectportal.com/floorplans/knoxville-knoxville-TN/
    fox-lake-apartment-homes/abbington-1440-1/  (5 units, fpid 1440)

Regression guard: ``test_no_inferred_unit_id_pattern`` pins the contract
that every emitted row has a natural ``unit_number`` so the runner's
fallback hash path (``core/identity.compute_fallback_unit_id`` —
``inferred_<sha16>``) never fires for this cohort again.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ma_poc.pms.adapters import entrata as entrata_mod
from ma_poc.pms.adapters.entrata import (
    EntrataAdapter,
    find_entrata_pp_plan_links,
    parse_entrata_pp_unit_cards,
)


def _sids(u: dict[str, Any]) -> dict[str, str]:
    """make_unit_dict declares ``list[dict[str, str]]`` but ``source_ids``
    is actually a nested dict — cast so mypy --strict accepts the
    test's index access."""
    return cast(dict[str, str], u["source_ids"])

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"

RISE_URL = (
    "https://www.risewestarlington.com/floorplans/arlington-TX/"
    "rise-west-arlington/a1-silver-1212885-1/"
)
FOXLAKE_URL = (
    "https://foxlake.prospectportal.com/floorplans/knoxville-knoxville-TN/"
    "fox-lake-apartment-homes/abbington-1440-1/"
)


def _rise_html() -> str:
    return (FIXTURES / "prospectportal_per_plan_unit_cards_risewestarlington.html").read_text()


def _foxlake_html() -> str:
    return (FIXTURES / "prospectportal_per_plan_unit_cards_foxlake.html").read_text()


def _foxlake_idx_html() -> str:
    return (FIXTURES / "prospectportal_index_with_plan_links_foxlake.html").read_text()


# ── parse_entrata_pp_unit_cards — live-fixture coverage ────────────────


def test_rise_west_arlington_yields_one_unit_with_real_number() -> None:
    """User-flagged URL: 1 unit-card → 1 unit with the visible PP number
    ``"184"``. The pre-fix path produced one plan-level row whose
    ``unit_number=""`` flowed into the runner as ``inferred_<sha16>``."""
    units = parse_entrata_pp_unit_cards(_rise_html(), RISE_URL)
    assert len(units) == 1
    u = units[0]
    assert u["unit_number"] == "184"
    assert u["floor_plan_name"] == "A1 Silver"
    assert u["bedrooms"] == "1"
    assert u["bathrooms"] == "1"
    assert u["sqft"] == "480"
    # make_unit_dict's runtime stores int rents under str-typed keys —
    # int(...) casts past the declared list[dict[str, str]] signature.
    assert int(u["market_rent_low"]) == 670
    assert int(u["market_rent_high"]) == 670
    assert u["availability_date"] == "2026-07-21"
    assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"


def test_foxlake_yields_five_units_with_real_numbers() -> None:
    """User-flagged URL: 5 unit-cards → 5 unit rows. Visible numbers
    pinned to live capture so future template drift breaks the test."""
    units = parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL)
    assert len(units) == 5
    unit_numbers = [u["unit_number"] for u in units]
    assert unit_numbers == ["8700", "8940", "8882", "8924", "8989"]
    rents = [int(u["market_rent_low"]) for u in units]
    assert rents == [1595, 1575, 1585, 1585, 1575]
    for u in units:
        assert u["floor_plan_name"] == "Abbington"
        assert u["bedrooms"] == "2"
        assert u["bathrooms"] == "2"
        assert u["sqft"] == "954"
        assert u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"


def test_regression_guard_no_inferred_unit_id_pattern() -> None:
    """REGRESSION GUARD (canary 1ef1060 regr#9): every row emitted by
    the per-plan drill MUST have a real ``unit_number``. If this test
    fails the runner will fall back to ``compute_fallback_unit_id`` and
    re-introduce ``inferred_<sha16>`` ids for the 212-prop cohort —
    that's exactly the regression we shipped this drill to close."""
    for html, url in (
        (_rise_html(), RISE_URL),
        (_foxlake_html(), FOXLAKE_URL),
    ):
        for u in parse_entrata_pp_unit_cards(html, url):
            num = str(u.get("unit_number") or "")
            assert num, f"empty unit_number on {url}"
            assert not num.startswith("inferred_"), (
                f"unit_number {num!r} starts with 'inferred_' — drill "
                f"is leaking the synthetic id back into the unit row"
            )
            # The "ent-<uid>" placeholder is acceptable only when the
            # PP card has no visible unit-number; both fixtures publish
            # the visible number so it must never trigger here.
            assert not num.startswith("ent-"), (
                f"unit_number {num!r} fell back to ent-<uid> — the "
                f"visible .unit-number h3 should have won"
            )


def test_emits_stable_entrata_uid_in_source_ids() -> None:
    """source_ids.entrata_uid carries the stable PP numeric id so the
    downstream merge can survive renumbering of the visible unit
    number (PP operators occasionally renumber on building renames)."""
    foxlake_units = parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL)
    # source_ids is a dict[str, Any] inside the dict[str, str] facade —
    # cast for the test access.
    foxlake_uids = [_sids(u)["entrata_uid"] for u in foxlake_units]
    # Pinned to live capture — these are the canonical PP unit ids.
    assert foxlake_uids == ["267", "338", "306", "322", "371"]

    rise_units = parse_entrata_pp_unit_cards(_rise_html(), RISE_URL)
    assert _sids(rise_units[0])["entrata_uid"] == "5256171"


def test_emits_fpid_in_source_ids_from_url_slug() -> None:
    """The ``-<fpid>-<phase>`` token in the per-plan URL is the
    authoritative floor-plan id. parse_entrata_pp_unit_cards must
    surface it via source_ids.entrata_fpid so the cross-tier merge
    can collapse a plan whose units came from this drill with a plan
    whose units came from the widget API (same fpid → same plan)."""
    for u in parse_entrata_pp_unit_cards(_rise_html(), RISE_URL):
        assert _sids(u)["entrata_fpid"] == "1212885"
    for u in parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL):
        assert _sids(u)["entrata_fpid"] == "1440"


def test_handles_available_now_phrase_as_empty_date() -> None:
    """The first foxlake card publishes "Available Now" (no date).
    availability_date must be empty so downstream date-validation
    doesn't flag a synthetic date. status stays AVAILABLE so the
    unit isn't dropped by the validity gate."""
    units = parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL)
    unit_8700 = next(u for u in units if u["unit_number"] == "8700")
    assert unit_8700["availability_date"] == ""
    assert unit_8700["availability_status"] == "AVAILABLE"


def test_does_not_pick_up_deposit_dollar_token_as_rent() -> None:
    """Foxlake cards render ``Deposit: $500`` immediately before the
    actual ``from $1,595`` rent. The pre-fix text-fallback grabbed the
    first ``$NNN`` token, yielding $500 rent — a poisoned value that
    would have passed the validity gate. The .unit-pricing selector
    now wins, so we get the real rent every time."""
    units = parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL)
    assert all(int(u["market_rent_low"]) >= 1500 for u in units), (
        "rent < $1500 means we leaked the $500 deposit token into rent"
    )


def test_does_not_pick_up_concession_dollar_token_as_rent() -> None:
    """The rise card has a ``Discounted Rents - $305 Off Monthly Rent``
    banner that precedes the canonical rent in the card text. Without
    the .unit-pricing selector preference the parser would emit $305
    as the rent (it's the first ``$NNN`` in the text blob)."""
    units = parse_entrata_pp_unit_cards(_rise_html(), RISE_URL)
    assert int(units[0]["market_rent_low"]) == 670


def test_returns_empty_for_html_without_unit_card_class() -> None:
    """Cheap early-exit when the page has no ``.unit-card`` markers —
    a plan-grid INDEX page falls through here without the BS4 parse
    overhead."""
    assert parse_entrata_pp_unit_cards("<html></html>", "") == []
    assert parse_entrata_pp_unit_cards("", "") == []
    # The PP-SSR index fixture has .fp-group-item but no .unit-card —
    # must early-exit before the BS4 parse.
    assert parse_entrata_pp_unit_cards(_foxlake_idx_html(), "") == []


def test_dedupes_cards_with_identical_uid() -> None:
    """PP sometimes repeats a card inside a ``compare units`` modal on
    the same page — both copies share the same ``data-unit-id``. The
    drill must emit one row per stable uid."""
    # Synthesise a 2-card page that duplicates foxlake's unit 8700
    one_card_html = (
        '<html><body><div class="unit-card unit-item-details-267"'
        ' data-unit-id="267">'
        '<div class="unit-header"><h3 class="unit-number">8700</h3></div>'
        '<div>2 Bed • 2 Bath • 954 SqFt</div>'
        '<div class="unit-pricing"><span class="price-value">from $1,595'
        ' per month</span></div>'
        '<div>Available Now</div>'
        '</div>'
    )
    html = (
        "<html><body>"
        + one_card_html.replace("<html><body>", "")
        + one_card_html.replace("<html><body>", "")
        + "</body></html>"
    )
    units = parse_entrata_pp_unit_cards(html, FOXLAKE_URL)
    assert len(units) == 1
    assert units[0]["unit_number"] == "8700"
    assert _sids(units[0])["entrata_uid"] == "267"


def test_derives_floor_plan_name_from_url_slug_when_caller_silent() -> None:
    """When caller doesn't supply the parent plan name, the URL slug
    (``a1-silver-1212885-1`` → ``"A1 Silver"``) becomes the
    floor_plan_name. Critical: every row must carry a non-empty
    floor_plan_name so the validity gate admits it AND so the merge
    can collapse plan-tier and unit-tier observations of the same
    plan together."""
    units = parse_entrata_pp_unit_cards(_rise_html(), RISE_URL)
    assert units[0]["floor_plan_name"] == "A1 Silver"


def test_caller_supplied_floor_plan_name_wins() -> None:
    """When caller passes ``floor_plan_name`` (e.g. the adapter
    already parsed the plan title from the index page) it overrides
    the URL-slug derivation. The caller's value is canonical because
    it preserves PP's display casing / spacing."""
    units = parse_entrata_pp_unit_cards(
        _rise_html(), RISE_URL, floor_plan_name="A1 - Silver Series"
    )
    assert units[0]["floor_plan_name"] == "A1 - Silver Series"


def test_extracts_building_when_present() -> None:
    """The foxlake fixture publishes ``Building 15`` / ``Building 11``
    etc.; the building number is a downstream merge anchor."""
    units = parse_entrata_pp_unit_cards(_foxlake_html(), FOXLAKE_URL)
    # Every foxlake card has a Building suffix
    buildings = [u.get("building", "") for u in units]
    assert all(b for b in buildings), f"missing building on: {buildings}"
    # Pinned to live capture
    assert buildings == ["15", "11", "13", "11", "12"]


# ── find_entrata_pp_plan_links — link-discovery coverage ───────────────


def test_find_pp_plan_links_foxlake_index() -> None:
    """Foxlake's index page (``/knoxville-knoxville/fox-lake-apartment-
    homes/conventional/``) has 7 .fp-name-link anchors → 7 plan URLs.
    The drill in EntrataAdapter.extract iterates these to feed
    parse_entrata_pp_unit_cards one plan at a time."""
    links = find_entrata_pp_plan_links(
        _foxlake_idx_html(), "https://foxlake.prospectportal.com"
    )
    assert len(links) == 7
    # The user-flagged abbington URL must appear first (PP renders
    # plans in fpid-ascending order; abbington is fpid 1440).
    assert links[0] == FOXLAKE_URL
    # Every link matches the per-plan URL pattern.
    for link in links:
        assert "/floorplans/" in link
        assert link.endswith("/")


def test_find_pp_plan_links_dedupes() -> None:
    """When the same plan URL appears under multiple selectors
    (.fp-name-link AND .fp-cta-link), it must appear once in the
    output. Document order is preserved."""
    html = (
        '<html><body>'
        '<li class="fp-group-item">'
        '<a class="fp-name-link" href="/floorplans/x/y/plan-a-100-1/">A</a>'
        '<a class="fp-cta-link" href="/floorplans/x/y/plan-a-100-1/">apply</a>'
        '</li>'
        '<li class="fp-group-item">'
        '<a class="fp-name-link" href="/floorplans/x/y/plan-b-101-1/">B</a>'
        '</li>'
        '</body></html>'
    )
    links = find_entrata_pp_plan_links(html, "https://example.prospectportal.com")
    assert links == [
        "https://example.prospectportal.com/floorplans/x/y/plan-a-100-1/",
        "https://example.prospectportal.com/floorplans/x/y/plan-b-101-1/",
    ]


def test_find_pp_plan_links_ignores_non_plan_hrefs() -> None:
    """Non-per-plan hrefs (apply link, residents portal, amenities)
    must NOT show up — only hrefs matching the slug-fpid-phase
    pattern qualify."""
    html = (
        '<html><body>'
        '<a class="fp-name-link" href="/floorplans/x/y/real-100-1/">real</a>'
        '<a class="fp-name-link" href="/floorplans/">grid</a>'
        '<a class="fp-name-link" href="/apply/">apply</a>'
        '<a class="fp-name-link" href="https://example.residentportal.com/">res</a>'
        '</body></html>'
    )
    links = find_entrata_pp_plan_links(html, "https://example.prospectportal.com")
    assert links == [
        "https://example.prospectportal.com/floorplans/x/y/real-100-1/"
    ]


@pytest.mark.asyncio
async def test_rendered_landing_plan_links_drilled(monkeypatch: Any) -> None:
    """#91 Lever A (Step-3b): the per-plan /floorplans/{plan}/ links live ONLY
    in the RENDERED DOM — plain ``<a href="/floorplans/...">`` anchors, 2-digit
    fpids, no fp-card/fp-name-link class (brownstonetx.com shape). The static
    body is a SPA shell, so Steps 1-3 add nothing to pp_ssr_index_bodies. The
    Step-3b hook harvests ``page.content()`` so the drill discovers + crawls the
    plan links — flipping the property plan-level -> unit-level."""
    unit_card_body = _foxlake_html()  # real per-plan unit-cards

    rendered = (
        "<html><body>"
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/1-bedroom-1-bath-a1-53-1/">A1</a>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/2-bedroom-2-bath-b1-52-1/">B1</a>'
        "</body></html>"
    )

    async def _fake_fetch(url: str, *, unlocker: bool = True) -> str:
        return unit_card_body if "/floorplans/" in url else ""

    async def _no_probe(self: Any, page: Any, ctx: Any) -> list[Any]:
        return []

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _fake_fetch)
    monkeypatch.setattr(EntrataAdapter, "_probe_known_endpoints", _no_probe)

    class _Page:
        async def content(self) -> str:
            return rendered

    ctx = SimpleNamespace(
        _api_responses=[],
        base_url="https://www.brownstonetx.com/",
        property_id="218853",
        address="",
        zip_code="",
        fetch_result=SimpleNamespace(
            final_url="https://www.brownstonetx.com/",
            body="<html><body><div id='root'></div></body></html>",  # SPA shell
        ),
    )
    result = await EntrataAdapter().extract(cast(Any, _Page()), cast(Any, ctx))
    assert result.units, f"expected drilled units, got errors={result.errors}"
    assert result.tier_used == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"


@pytest.mark.asyncio
async def test_render_lever_body_plan_links_drilled(monkeypatch: Any) -> None:
    """#91-A1b: the #42/#45 render lever (jugnu.py) re-runs extraction with the
    RENDERED DOM in fetch_result.body and page=None. Step-3b must harvest the
    plan links from fr_body_check (page.content() is None here), else the drill
    never fires. This is the production path that recovers Brownstone/Drexel —
    their STATIC body has 0 /floorplans/ links; they appear only post-render."""
    unit_card_body = _foxlake_html()

    rendered_body = (
        "<html><body>"
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/1-bedroom-1-bath-a1-53-1/">A1</a>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/2-bedroom-2-bath-b1-52-1/">B1</a>'
        "</body></html>"
    )

    async def _fake_fetch(url: str, *, unlocker: bool = True) -> str:
        return unit_card_body if "/floorplans/" in url else ""

    async def _no_probe(self: Any, page: Any, ctx: Any) -> list[Any]:
        return []

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _fake_fetch)
    monkeypatch.setattr(EntrataAdapter, "_probe_known_endpoints", _no_probe)

    ctx = SimpleNamespace(
        _api_responses=[], base_url="https://www.brownstonetx.com/",
        property_id="218853", address="", zip_code="",
        fetch_result=SimpleNamespace(
            final_url="https://www.brownstonetx.com/", body=rendered_body,
        ),
    )
    # page=None — exactly the #42/#45 render-lever re-run shape.
    result = await EntrataAdapter().extract(None, cast(Any, ctx))
    assert result.units, f"expected drilled units, got errors={result.errors}"
    assert result.tier_used == "TIER_1_DOM_ENTRATA_PP_UNIT_LEVEL"


@pytest.mark.asyncio
async def test_rendered_harvest_skipped_when_static_body_has_links(monkeypatch: Any) -> None:
    """The Step-3b render harvest must NOT fire when a static index body already
    exposes plan links — page.content() should not even be consulted (avoids a
    redundant render read and keeps existing PP-SSR behaviour unchanged)."""
    idx = _foxlake_idx_html()  # static index WITH fp-name-link plan links
    consulted = {"content": False}

    async def _fake_fetch(url: str, *, unlocker: bool = True) -> str:
        return _foxlake_html() if "/floorplans/" in url else ""

    async def _no_probe(self: Any, page: Any, ctx: Any) -> list[Any]:
        return []

    monkeypatch.setattr(entrata_mod, "_entrata_static_fetch", _fake_fetch)
    monkeypatch.setattr(EntrataAdapter, "_probe_known_endpoints", _no_probe)

    class _Page:
        async def content(self) -> str:
            consulted["content"] = True
            return "<html></html>"

    ctx = SimpleNamespace(
        _api_responses=[], base_url="https://foxlake.prospectportal.com/",
        property_id="1", address="", zip_code="",
        fetch_result=SimpleNamespace(
            final_url="https://foxlake.prospectportal.com/", body=idx,
        ),
    )
    result = await EntrataAdapter().extract(cast(Any, _Page()), cast(Any, ctx))
    assert result.units
    assert consulted["content"] is False, "rendered content read despite static links"


def test_find_pp_plan_links_two_digit_fpid_brownstone() -> None:
    """2026-07-30: Brownstone (brownstonetx.com) numbers all its plans with
    2-digit fpids (a1-53-1, b1-52-1, a2-54-1, c1-55-1). The old \\d{3,9} floor
    rejected the WHOLE property -> SUCCESS_PLAN_LEVEL despite real /floorplans/
    unit-cards one hop deeper (unit 423, 1/1, 610sf). \\d{2,9} recovers them.

    The four real detail URLs, verbatim from the rendered brownstonetx.com
    landing DOM (the discovery source; the static /floorplans/ index is a SPA)."""
    html = (
        '<html><body>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/1-bedroom-1-bath-a1-53-1/">A1</a>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/2-bedroom-2-bath-b1-52-1/">B1</a>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/1-bedroom-1-bath-a2-54-1/">A2</a>'
        '<a href="/floorplans/uvalde-TX/brownstone-apartments/3-bedroom-2-bath-c1-55-1/">C1</a>'
        # still-rejected non-plan hrefs — the relax must not widen the net.
        '<a href="/floorplans/">grid</a>'
        '<a href="/apply/">apply</a>'
        '</body></html>'
    )
    links = find_entrata_pp_plan_links(html, "https://www.brownstonetx.com")
    assert len(links) == 4, links
    assert all("/floorplans/uvalde-TX/brownstone-apartments/" in u for u in links)
    assert not any(u.endswith("/floorplans/") or "/apply/" in u for u in links)


def test_find_pp_plan_links_strips_query_and_fragment() -> None:
    """Plan URLs occasionally come with ``?move_in=...`` or ``#section``
    suffixes that PP's deep-link JS adds. Canonicalise so the drill
    fetches one URL per plan, not one per click-track variant."""
    html = (
        '<html><body>'
        '<a class="fp-name-link" '
        'href="/floorplans/x/y/plan-100-1/?ref=widget#units">A</a>'
        '</body></html>'
    )
    links = find_entrata_pp_plan_links(html, "https://example.prospectportal.com")
    assert links == [
        "https://example.prospectportal.com/floorplans/x/y/plan-100-1/"
    ]


def test_find_pp_plan_links_empty_for_non_pp_html() -> None:
    """Non-PP pages (RentCafe, AppFolio, plain WordPress) must return
    empty so the adapter doesn't waste fetches on hrefs that don't
    match the PP schema."""
    assert find_entrata_pp_plan_links("", "https://example.com") == []
    assert find_entrata_pp_plan_links(
        "<html><body><a href='/about'>About</a></body></html>",
        "https://example.com",
    ) == []


def test_find_pp_plan_links_beans_floorplans_map_theme() -> None:
    """2026-05-25 (wave-2 cluster #3 CF+Entrata+SightMap, pid 258254 /
    14fiftyapartments.com): the newer PP ``beans-floorplans-map-tab``
    theme replaces the legacy ``li.fp-group-item`` / ``.fp-card``
    wrappers with a tabbed layout that anchors plan links via
    ``.fp-name-link`` ONLY. The drill's caller used to gate body
    inclusion on ``fp-card`` / ``fp-group-item`` presence and silently
    drop these bodies. find_entrata_pp_plan_links must still emit the
    plan URLs from this body shape — that's the regression guard so
    the broadened predicate (entrata.py step-1 / step-3 gates) can
    rely on it."""
    html = (
        '<html><body>'
        '<div class="beans-floorplans-map-tabs-wrapper">'
        '<div class="beans-floorplans-map-tab-content active">'
        '<a class="fp-name-link" '
        'href="/floorplans/kissimmee-FL/14fifty-neocity/'
        'a1-29819-1/">A1</a>'
        '<a class="fp-name-link" '
        'href="/floorplans/kissimmee-FL/14fifty-neocity/'
        'a2-29821-1/">A2</a>'
        '</div>'
        '<div class="beans-floorplans-map-tab-content">'
        '<a class="fp-name-link" '
        'href="/floorplans/kissimmee-FL/14fifty-neocity/'
        'b1-29823-1/">B1</a>'
        '</div>'
        '</div>'
        '</body></html>'
    )
    # No fp-card / fp-group-item / unit-item markers exist in this body.
    assert "fp-card" not in html
    assert "fp-group-item" not in html
    assert "unit-item" not in html
    links = find_entrata_pp_plan_links(
        html, "https://www.14fiftyapartments.com"
    )
    assert links == [
        "https://www.14fiftyapartments.com/floorplans/kissimmee-FL/"
        "14fifty-neocity/a1-29819-1/",
        "https://www.14fiftyapartments.com/floorplans/kissimmee-FL/"
        "14fifty-neocity/a2-29821-1/",
        "https://www.14fiftyapartments.com/floorplans/kissimmee-FL/"
        "14fifty-neocity/b1-29823-1/",
    ]
