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
