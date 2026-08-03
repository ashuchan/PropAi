"""Entrata plan index lives at /{city}/{slug}/conventional/ (2026-07-26).

ProspectPortal mounts its plan grid at ``/{city-slug}/{property-slug}/
conventional/``, NOT at ``/floorplans`` — which on many of these hosts 302s
to the homepage. The adapter's candidate-path probes therefore found no
index, the per-plan unit drill had nothing to walk, and the property shipped
plan-level.

MEASURED on the 2026-07-26-plancohort canary: TIER_1_DOM_ENTRATA_PP_SSR fired
for 40 properties and only 1 reached unit level (2%). Of the 53 unconverted
Entrata-surface properties, 20 (38%) expose this href on their own landing
page. Live-verified on gardengateokc.com: the conventional index returns 19
plan links whose detail pages carry real apartments (4032 Renovated $1,863,
4028 Renovated $1,976).

The slug shape is deliberately HARVESTED, not constructed — city and property
are each sometimes doubled and there is no rule that covers all three live
forms:
    /rockville/fenestra-at-the-square/
    /oklahoma-city-oklahoma-city/garden-gate/
    /corvallis-corvallis/grand-oaks-grand-oaks/
"""

from __future__ import annotations

from ma_poc.pms.adapters.entrata import _find_pp_conventional_index

_BASE = "https://www.gardengateokc.com/"


def test_absolute_href_is_found() -> None:
    html = (
        '<a href="https://www.gardengateokc.com/oklahoma-city-oklahoma-city/'
        'garden-gate/conventional/">Floor Plans</a>'
    )
    assert _find_pp_conventional_index(html, _BASE) == [
        "https://www.gardengateokc.com/oklahoma-city-oklahoma-city/garden-gate/conventional/"
    ]


def test_root_relative_href_is_resolved() -> None:
    html = '<a href="/rockville/fenestra-at-the-square/conventional/">Plans</a>'
    assert _find_pp_conventional_index(html, _BASE) == [
        "https://www.gardengateokc.com/rockville/fenestra-at-the-square/conventional/"
    ]


def test_absolute_form_action_is_found() -> None:
    """Some live homepages publish the grid only as a CTA form target."""
    html = (
        '<form action="https://www.gardengateokc.com/oklahoma-city-oklahoma-city/'
        'garden-gate/conventional/" method="post"></form>'
    )
    assert _find_pp_conventional_index(html, _BASE) == [
        "https://www.gardengateokc.com/oklahoma-city-oklahoma-city/garden-gate/conventional/"
    ]


def test_root_relative_form_action_is_resolved() -> None:
    html = '<form action="/pittsburgh/bryn-mawr/conventional/"></form>'
    assert _find_pp_conventional_index(html, _BASE) == [
        "https://www.gardengateokc.com/pittsburgh/bryn-mawr/conventional/"
    ]


def test_foreign_form_action_is_rejected() -> None:
    html = '<form action="https://sibling.example/x/y/conventional/"></form>'
    assert _find_pp_conventional_index(html, _BASE) == []


def test_trailing_slash_is_optional() -> None:
    """Observed both ways in the wild."""
    html = '<a href="/corvallis-corvallis/grand-oaks-grand-oaks/conventional">Plans</a>'
    assert len(_find_pp_conventional_index(html, _BASE)) == 1


def test_prospectportal_twin_host_is_allowed() -> None:
    """Vanity sites routinely hand off to their *.prospectportal.com twin —
    that is the same property, so it must not be filtered as foreign."""
    html = (
        '<a href="https://flatson12.prospectportal.com/college-station/flats-on-12/conventional/">Plans</a>'
    )
    assert _find_pp_conventional_index(html, _BASE) == [
        "https://flatson12.prospectportal.com/college-station/flats-on-12/conventional/"
    ]


def test_foreign_host_is_rejected() -> None:
    """A PP vanity site can link a SIBLING property. Drilling that roster
    would attribute another property's apartments to this one — a silent
    data-integrity bug far worse than the miss it would fix."""
    html = '<a href="https://some-other-property.com/x/y/conventional/">Plans</a>'
    assert _find_pp_conventional_index(html, _BASE) == []


def test_unrelated_links_ignored() -> None:
    assert _find_pp_conventional_index('<a href="/about/">About</a>', _BASE) == []
    assert _find_pp_conventional_index("", _BASE) == []


def test_duplicates_collapse() -> None:
    """PP repeats the nav link in header and footer; drilling it twice is
    wasted request volume across the cohort."""
    one = '<a href="/city/prop/conventional/">Plans</a>'
    assert len(_find_pp_conventional_index(one * 3, _BASE)) == 1


def test_adapter_uses_the_helper_only_when_no_index_was_found() -> None:
    """Cost guard: properties whose known paths already yielded an index must
    not pay for an extra fetch."""
    import inspect

    from ma_poc.pms.adapters import entrata

    src = inspect.getsource(entrata)
    assert "if not pp_ssr_index_bodies:" in src
    assert "_find_pp_conventional_index(" in src
