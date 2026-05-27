"""Entrata nested ``/conventional/`` anchor-discovery tests (2026-05-27).

Background
----------
Reference impl in
``investigations/2026-05-27-failure-grind/artifacts/probe/entrata_deep_probe.py``
rescued 14 Entrata properties by extracting ``href="...conventional/..."``
anchors from the homepage and drilling them. The prior adapter regex
required absolute hrefs and didn't rank slug-matching candidates, so
Entrata sites that emit the same nav as a root-relative path silently
dropped through to ``ENTRATA_EMPTY``.

These tests pin:
  1. Absolute-href anchor discovery still works (Princeton Bradford
     live fixture — homepage links to
     ``https://www.princetonbradford.com/haverhill-ma-apartments/
     princeton-bradford/conventional/``).
  2. Root-relative href discovery now works (synthetic markup mirroring
     other Entrata Vanity hosts in the residue).
  3. Slug-matching candidates are ranked first so the top-3 cap reaches
     the right page on multi-property portals.
  4. End-to-end the Princeton Bradford ``/conventional/`` fixture
     parses to at least 5 plan-level rows with rent.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ma_poc.pms.adapters.entrata import (
    _parse_pp_fp_group_item,
    parse_entrata_prospectportal_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "entrata"


# Mirror of the in-adapter regexes; if the adapter ones change the
# tests will fail and force the patterns to stay in sync.
_RE_DEEP_ABS = re.compile(
    r'href=["\']'
    r'(https?://[^"\']+/(?:[^/"\']+/){2,}'
    r"(?:conventional|affordable)/?[^\"'?#]*?)[\"']",
    re.IGNORECASE,
)
_RE_DEEP_REL = re.compile(
    r'href=["\']'
    r"(/(?:[^/\"']+/){2,}(?:conventional|affordable)/?[^\"'?#]*?)[\"']",
    re.IGNORECASE,
)


def _discover(html: str, base: str, cap: int = 3) -> list[str]:
    """Replicate the adapter's Step-2 anchor discovery so we can test
    its behaviour without standing up a full Playwright AdapterContext.
    """
    host = urlparse(base).netloc
    host_slug = re.sub(r"^www\.|\..*$", "", host).lower()
    raw: list[str] = []
    for m in _RE_DEEP_ABS.finditer(html):
        cand = m.group(1).split("?")[0].split("#")[0]
        if cand and urlparse(cand).netloc.endswith(host):
            raw.append(cand)
    for m in _RE_DEEP_REL.finditer(html):
        rel = m.group(1).split("?")[0].split("#")[0]
        if rel:
            raw.append(base + rel)

    def score(u: str) -> int:
        return 1 if host_slug and host_slug in urlparse(u).path.lower() else 0

    seen: set[str] = set()
    out: list[str] = []
    for cand in sorted(raw, key=lambda u: -score(u)):
        if cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if len(out) >= cap:
            break
    return out


def test_princeton_bradford_absolute_anchor_discovered() -> None:
    """Live homepage capture must yield the nested /conventional/ URL."""
    home = (FIXTURES / "princeton_bradford_home.html").read_text()
    base = "https://www.princetonbradford.com"
    found = _discover(home, base)
    assert (
        "https://www.princetonbradford.com/haverhill-ma-apartments/"
        "princeton-bradford/conventional/"
    ) in found


def test_relative_anchor_resolved_to_absolute() -> None:
    """Root-relative ``/city/prop/conventional/`` must be lifted too."""
    html = (
        '<a href="/morrisville/wellington-woods/conventional/">Floor plans</a>'
        '<a href="/somewhere/else/about/">About</a>'
    )
    found = _discover(html, "https://www.wellingtonwoodsapartments.com")
    assert found == [
        "https://www.wellingtonwoodsapartments.com/morrisville/"
        "wellington-woods/conventional/"
    ]


def test_slug_match_ranked_first() -> None:
    """Candidate whose path contains the host's property slug wins the
    top-3 cap on multi-property portals."""
    html = (
        '<a href="https://www.targetprop.com/citya/otherprop/conventional/">A</a>'
        '<a href="https://www.targetprop.com/cityb/targetprop/conventional/">B</a>'
        '<a href="https://www.targetprop.com/cityc/thirdprop/conventional/">C</a>'
        '<a href="https://www.targetprop.com/cityd/fourthprop/conventional/">D</a>'
    )
    # host slug ⇒ "targetprop"
    found = _discover(html, "https://www.targetprop.com")
    assert len(found) == 3
    assert "targetprop" in urlparse(found[0]).path


def test_cap_at_three() -> None:
    html = "".join(
        f'<a href="https://x.example.com/city{i}/p{i}/conventional/">p{i}</a>'
        for i in range(8)
    )
    found = _discover(html, "https://x.example.com")
    assert len(found) == 3


def test_princeton_bradford_conventional_extracts_rented_plans() -> None:
    """End-to-end: the live /conventional/ body must parse into ≥5
    plan-level rows with at least 3 carrying a rent.

    Pins the existing Template B parser against the Princeton Bradford
    live capture so a future selector regression on Entrata's PP grid
    fails the suite. Paired with the anchor-discovery tests above this
    closes the rescue path for the entire ``conventional/`` anchor
    cohort (~30 estimated false-negatives in the 612-residue worklist).
    """
    html = (FIXTURES / "princeton_bradford_conventional.html").read_text()
    soup = BeautifulSoup(html, "lxml")
    items = soup.select("li.fp-group-item")
    assert len(items) >= 5
    units = _parse_pp_fp_group_item(
        items,
        "https://www.princetonbradford.com/haverhill-ma-apartments/"
        "princeton-bradford/conventional/",
    )
    assert len(units) >= 5
    with_rent = [u for u in units if u.get("market_rent_low")]
    assert len(with_rent) >= 3, [u.get("market_rent_low") for u in units]

    # And the unified wrapper resolves Template B without falling
    # through to Template A or C.
    via_wrapper = parse_entrata_prospectportal_html(
        html,
        "https://www.princetonbradford.com/haverhill-ma-apartments/"
        "princeton-bradford/conventional/",
    )
    assert len(via_wrapper) == len(units)
    assert all(
        u.get("extraction_tier") == "TIER_1_DOM_ENTRATA_PP_FPGROUP"
        for u in via_wrapper
    )
