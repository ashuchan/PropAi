"""Body-capable resolver (task #37 Track 1, 2026-07-19).

Production dispatches L3 with page=None, so the live resolver (CTA-hop / iframe /
redirect) was skipped — the root of the SightMap-iframe / portal-hop / JS-marker
misroute mass. resolve_target_from_body runs the SAME scoring over the fetched
RENDER body. These tests pin: (a) parity with the page-based resolver on the same
signals, (b) relative→absolute href resolution, (c) never-fail degradation to
fetch_only.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.detector import detect_pms
from ma_poc.pms.resolver import (
    _links_and_iframes_from_body,
    resolve_target_from_body,
)


def test_links_and_iframes_absolutise_relative() -> None:
    body = (
        "<html><body>"
        "<a href='/floor-plans'>Floor Plans</a>"
        "<a href='https://www.rentcafe.com/x/'>Apply</a>"
        "<a href='mailto:x@y.com'>email</a>"  # non-http dropped
        "<iframe src='//tour.sightmap.com/embed/Z'></iframe>"
        "<iframe src='/local-frame'></iframe>"
        "</body></html>"
    )
    links, iframes = _links_and_iframes_from_body(body, "https://vanity.example/")
    hrefs = {link["href"] for link in links}
    assert "https://vanity.example/floor-plans" in hrefs  # relative → absolute
    assert "https://www.rentcafe.com/x/" in hrefs
    assert all(h.startswith(("http://", "https://")) for h in hrefs)  # mailto dropped
    assert "https://tour.sightmap.com/embed/Z" in iframes  # scheme-relative resolved
    assert "https://vanity.example/local-frame" in iframes


def test_links_and_iframes_never_raises_on_garbage() -> None:
    assert _links_and_iframes_from_body("", "https://x/") == ([], [])
    # not valid HTML — bs4 tolerates, returns no anchors/iframes
    links, iframes = _links_and_iframes_from_body("<<<not html>>>", "https://x/")
    assert links == [] and iframes == []


# ── parity with the page-based resolver on identical signals ──


@pytest.mark.asyncio
async def test_body_resolver_cta_link_parity() -> None:
    """A RentCafe apply anchor in the body must hop exactly like the page path."""
    body = (
        "<html><body><a href='https://www.rentcafe.com/apartments/mi/ann-arbor/foo/'>"
        "Apply Now</a></body></html>"
    )
    detection = detect_pms("https://vanity.example/")
    res = await resolve_target_from_body(
        body, "https://vanity.example/", "https://vanity.example/", detection
    )
    assert res.method == "cta_link"
    assert "rentcafe.com" in res.resolved_url
    assert res.final_detection.pms == "rentcafe"


@pytest.mark.asyncio
async def test_body_resolver_sightmap_iframe() -> None:
    body = (
        "<html><body><iframe src='https://tour.sightmap.com/embed/X'></iframe>"
        "</body></html>"
    )
    detection = detect_pms("https://vanity.example/")
    res = await resolve_target_from_body(
        body, "https://vanity.example/", "https://vanity.example/", detection
    )
    assert res.method == "iframe"
    assert "sightmap.com" in res.resolved_url
    assert res.final_detection.pms == "sightmap"


@pytest.mark.asyncio
async def test_body_resolver_redirect_via_final_url() -> None:
    """final_url differing from original + landing on a PMS host → redirect hop."""
    detection = detect_pms("https://vanity.example/")
    res = await resolve_target_from_body(
        "<html><body>no links</body></html>",
        "https://vanity.example/",
        "https://8756399.onlineleasing.realpage.com/",
        detection,
    )
    assert res.method == "redirect"
    assert "onlineleasing.realpage.com" in res.resolved_url


@pytest.mark.asyncio
async def test_body_resolver_no_signal_is_failed_not_wrong_hop() -> None:
    """A plain marketing body with no portal signals must NOT invent a hop."""
    detection = detect_pms("https://vanity.example/")
    res = await resolve_target_from_body(
        "<html><body><a href='https://vanity.example/gallery'>Photos</a></body></html>",
        "https://vanity.example/",
        "https://vanity.example/",
        detection,
    )
    assert res.method == "failed"
    assert res.resolved_url == "https://vanity.example/"


@pytest.mark.asyncio
async def test_confident_known_pms_not_downgraded_by_samehost_hop() -> None:
    """Regression (2026-07-19): a confident BODY-detected RentCafe on a *vanity*
    URL must keep its adapter even when a same-host /floorplans CTA exists.

    Before the downgrade guard, the vanity URL failed Step-1's url-fingerprint
    gate, fell to Pass-3b, hopped to the same-host /floorplans anchor, re-detected
    it as generic, and demoted a RentCafe UNIT-level gold to TIER_3_DOM_GENERIC
    plan-level (8/44 test100c gold→plan demotions)."""
    # A confident known-PMS detection (as html_detection would produce from a
    # body carrying a rentcafe marker).
    rentcafe = detect_pms("https://www.rentcafe.com/apartments/mi/ann-arbor/foo/")
    assert rentcafe.pms == "rentcafe" and rentcafe.confidence >= 0.7
    body = (
        "<html><body>"
        "<a href='/floorplans'>Floor Plans</a>"  # same-host CTA-path anchor
        "<a href='/availability'>Availability</a>"
        "</body></html>"
    )
    res = await resolve_target_from_body(
        body, "https://parkplacejville.com/", "https://parkplacejville.com/", rentcafe
    )
    # Guard fires: no downgrade to generic, adapter stays rentcafe, no spurious hop.
    assert res.final_detection.pms == "rentcafe"
    assert res.method == "no_hop_known_pms"
    assert res.resolved_url == "https://parkplacejville.com/"


@pytest.mark.asyncio
async def test_cross_known_pms_hop_still_allowed() -> None:
    """The guard only blocks DOWNGRADES to generic — a confident detection that
    hops to a *different known adapter* (a real portal) must still resolve."""
    rentcafe = detect_pms("https://www.rentcafe.com/apartments/mi/ann-arbor/foo/")
    body = (
        "<html><body><iframe src='https://tour.sightmap.com/embed/X'></iframe>"
        "</body></html>"
    )
    res = await resolve_target_from_body(
        body, "https://vanity.example/", "https://vanity.example/", rentcafe
    )
    # sightmap is a known adapter (not generic) → hop preserved.
    assert res.final_detection.pms == "sightmap"
    assert res.method == "iframe"


@pytest.mark.asyncio
async def test_unknown_initial_still_hops_to_generic_subpage() -> None:
    """The guard must NOT block the legit recovery case: an UNKNOWN vanity that
    genuinely needs the same-host /floorplans hop still resolves (no known PMS
    to protect, so the hop is the best available signal)."""
    unknown = detect_pms("https://vanity.example/")
    assert unknown.pms in ("unknown", "generic_plan_text")
    body = "<html><body><a href='/floor-plans-and-pricing'>Floor Plans</a></body></html>"
    res = await resolve_target_from_body(
        body, "https://vanity.example/", "https://vanity.example/", unknown
    )
    # Not blocked by the guard (initial is generic/unknown) — hop attempted.
    assert res.method in ("cta_link", "failed")


@pytest.mark.asyncio
async def test_body_resolver_none_body_is_fetch_only() -> None:
    detection = detect_pms("https://vanity.example/")
    res = await resolve_target_from_body(
        None, "https://vanity.example/", "https://vanity.example/", detection
    )
    assert res.method == "fetch_only"
    assert res.resolved_url == "https://vanity.example/"


@pytest.mark.asyncio
async def test_body_resolver_never_raises() -> None:
    detection = detect_pms("https://vanity.example/")
    # bytes-like / odd input must degrade, not raise
    res = await resolve_target_from_body(
        "\x00\xff garbage <a href=", "https://vanity.example/", "", detection
    )
    assert res.method in ("failed", "fetch_only", "no_hop", "cta_link", "iframe", "redirect")
