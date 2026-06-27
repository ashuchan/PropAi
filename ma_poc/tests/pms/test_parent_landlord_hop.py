"""Parent-landlord external-property link-hop allowlist (2026-06-27).

Streetlights.com publishes per-property landing pages
(streetlights.com/properties/<slug>/) that have no rent / floor-plan
data of their own — they just link out to the property's own marketing
site. Before this chip the link-hop tier filtered out external links,
so both The Beverly and The Asher returned 0 units in the 35-property
test run despite The Beverly's real site (thebeverlyonescottsdale.com)
having a full SightMap inventory.

These tests pin the allowlist behavior:
  - external .com candidates DO survive when entry host is streetlights.com
  - junk hosts (social, analytics, investor portals) NEVER survive
  - non-parent-landlord entry hosts keep their old strict same-site/portal gate
"""
from __future__ import annotations

import pytest

from ma_poc.pms.scraper import (
    _is_parent_landlord_entry,
    _is_parent_landlord_external_candidate,
    _rank_internal_links,
)


# ─── Host classifier ─────────────────────────────────────────


@pytest.mark.parametrize("host,expected", [
    ("streetlights.com", True),
    ("www.streetlights.com", True),
    ("gables.com", False),
    ("", False),
])
def test_is_parent_landlord_entry(host, expected) -> None:
    assert _is_parent_landlord_entry(host) is expected


@pytest.mark.parametrize("host,expected", [
    # Real property sites — keep
    ("thebeverlyonescottsdale.com", True),
    ("www.theoliverdallas.com", True),
    ("livenashvilleyards.com", True),
    # Junk — drop
    ("facebook.com", False),
    ("www.facebook.com", False),
    ("googletagmanager.com", False),
    ("monsterinsights.com", False),
    ("smartbidnet.com", False),
    ("securecc.smartbidnet.com", False),
    ("investors.streetlightsres.com", False),
    # Garbage
    ("", False),
    ("nodot", False),
])
def test_is_parent_landlord_external_candidate(host, expected) -> None:
    assert _is_parent_landlord_external_candidate(host) is expected


# ─── Ranker integration ──────────────────────────────────────


def test_streetlights_entry_surfaces_external_property_link() -> None:
    """The Beverly recovery path: streetlights.com page links to
    thebeverlyonescottsdale.com — that link must survive the ranker."""
    html = """
    <html><body>
      <a href="https://thebeverlyonescottsdale.com/floorplans/">View Floor Plans</a>
      <a href="https://facebook.com/streetlights">Facebook</a>
      <a href="https://investors.streetlightsres.com/">Investors</a>
      <a href="/contact">Contact</a>
    </body></html>
    """
    ranked = _rank_internal_links(
        html, "https://streetlights.com/properties/the-beverly/", limit=5
    )
    urls = [u for u, _, _ in ranked]
    assert any("thebeverlyonescottsdale.com" in u for u in urls), \
        f"property site filtered out; got {urls}"
    # Junk MUST stay filtered even on a parent-landlord entry
    assert not any("facebook.com" in u for u in urls)
    assert not any("streetlightsres.com" in u for u in urls)


def test_non_parent_landlord_entry_still_blocks_external_links() -> None:
    """The allowlist must NOT regress the global same-site/portal gate
    for ordinary direct-site entries — only streetlights.com gets the
    external pass."""
    html = """
    <html><body>
      <a href="https://other-property.com/floorplans/">Other property</a>
      <a href="/floorplans">Floor plans</a>
    </body></html>
    """
    ranked = _rank_internal_links(
        html, "https://www.somepms.com/property/", limit=5
    )
    urls = [u for u, _, _ in ranked]
    # External link must NOT survive when entry is a normal property site
    assert not any("other-property.com" in u for u in urls), \
        f"external link leaked through non-parent-landlord entry: {urls}"
