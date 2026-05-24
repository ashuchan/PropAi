"""SecureCafe multi-base discovery tests (2026-05-23).

Background — the Majestic-style portfolio-sibling fix:
  11 of 54 RentCafe SHAPE_REJECTED properties have ≥2 distinct
  SecureCafe slugs on the homepage. The old ``_find_securecafe_base``
  used ``regex.search()`` and returned the FIRST match — which is
  often the wrong sibling on portfolio pages.

  Example: chicagorentals.com (the Majestic Vernon Hills brand site)
  has the homepage link to ``/onlineleasing/forest-cove-apartments/``
  BEFORE the link to ``/onlineleasing/the-majestic-luxury-apartments/``.
  The adapter probed Forest Cove (different property, 0 inventory) →
  SHAPE_REJECTED → property dropped.

  Fix: ``_find_all_securecafe_bases`` returns all distinct bases in
  source order; adapter probes each (capped at 3) and accepts the
  first with AvailUnitRow rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ma_poc.pms.adapters.rentcafe import (
    _find_all_securecafe_bases,
    _find_securecafe_base,
)


@dataclass
class _FetchResult:
    body: bytes | str = ""
    final_url: str = ""


@dataclass
class _Ctx:
    base_url: str = ""
    fetch_result: _FetchResult = field(default_factory=_FetchResult)
    _api_responses: list = field(default_factory=list)


# ─── multi-base discovery ────────────────────────────────────────────


def test_find_all_bases_single_base() -> None:
    html = (
        '<a href="https://acme.securecafe.com/onlineleasing/'
        'acme-tower/availableunits.aspx">Apply</a>'
    )
    ctx = _Ctx()
    bases = _find_all_securecafe_bases(html, ctx)
    assert bases == ["https://acme.securecafe.com/onlineleasing/acme-tower"]


def test_find_all_bases_multiple_siblings_returns_all_in_order() -> None:
    """The Majestic case: portfolio site links to multiple SC slugs.
    Return all in source order so the caller can try each."""
    html = (
        '<a href="https://chicagorentals.securecafe.com/onlineleasing/'
        'forest-cove-apartments/scheduletour.aspx">Forest Cove</a>'
        '<a href="https://chicagorentals.securecafe.com/onlineleasing/'
        'the-majestic-luxury-apartments/guestlogin.aspx">Majestic</a>'
    )
    ctx = _Ctx()
    bases = _find_all_securecafe_bases(html, ctx)
    assert bases == [
        "https://chicagorentals.securecafe.com/onlineleasing/forest-cove-apartments",
        "https://chicagorentals.securecafe.com/onlineleasing/the-majestic-luxury-apartments",
    ]


def test_find_all_bases_dedups_same_base_seen_multiple_times() -> None:
    """A page may reference the same securecafe slug via multiple
    different .aspx endpoints (scheduletour, contactus, guestlogin)
    — return each base only once."""
    html = (
        '<a href="https://x.securecafe.com/onlineleasing/foo/scheduletour.aspx">a</a>'
        '<a href="https://x.securecafe.com/onlineleasing/foo/contactus.aspx">b</a>'
        '<a href="https://x.securecafe.com/onlineleasing/foo/guestlogin.aspx">c</a>'
    )
    ctx = _Ctx()
    bases = _find_all_securecafe_bases(html, ctx)
    assert bases == ["https://x.securecafe.com/onlineleasing/foo"]


def test_find_all_bases_includes_api_response_urls() -> None:
    """When the HTML doesn't carry the link (patchright DOM mismatch),
    fall back to captured network responses — those almost always
    have a guestlogin.aspx / userlogin.aspx URL."""
    ctx = _Ctx(
        _api_responses=[
            {"url": "https://example.securecafe.com/onlineleasing/example/guestlogin.aspx?x=1"},
        ],
    )
    bases = _find_all_securecafe_bases("", ctx)
    assert bases == ["https://example.securecafe.com/onlineleasing/example"]


def test_find_all_bases_html_takes_priority_over_api_responses() -> None:
    """HTML matches come first in the returned list — that's the
    source-of-truth priority order."""
    html = (
        '<a href="https://html-site.securecafe.com/onlineleasing/'
        'html-slug/scheduletour.aspx">html</a>'
    )
    ctx = _Ctx(
        _api_responses=[
            {"url": "https://api-site.securecafe.com/onlineleasing/api-slug/guestlogin.aspx"},
        ],
    )
    bases = _find_all_securecafe_bases(html, ctx)
    assert bases == [
        "https://html-site.securecafe.com/onlineleasing/html-slug",
        "https://api-site.securecafe.com/onlineleasing/api-slug",
    ]


def test_find_all_bases_empty_when_no_securecafe_present() -> None:
    ctx = _Ctx()
    assert _find_all_securecafe_bases("", ctx) == []
    assert _find_all_securecafe_bases("<html>plain page</html>", ctx) == []


def test_find_all_bases_handles_securecafenet_residentservices() -> None:
    """The .securecafenet.com / residentservices path was captured in
    the wild on cluster-#5 sites — should be rewritten to the
    .securecafe.com / onlineleasing equivalent so the SC drill can run."""
    html = (
        '<a href="https://x.securecafenet.com/residentservices/'
        'some-slug/userlogin.aspx">login</a>'
    )
    ctx = _Ctx()
    bases = _find_all_securecafe_bases(html, ctx)
    # The base helper rewrites .securecafenet → .securecafe + onlineleasing.
    assert bases == ["https://x.securecafe.com/onlineleasing/some-slug"]


# ─── back-compat shim ────────────────────────────────────────────────


def test_find_securecafe_base_shim_returns_first_of_many() -> None:
    """The single-base helper is now a thin wrapper. Confirm it still
    returns the FIRST base in source order — preserves any existing
    caller's contract."""
    html = (
        '<a href="https://x.securecafe.com/onlineleasing/first/x.aspx">a</a>'
        '<a href="https://x.securecafe.com/onlineleasing/second/y.aspx">b</a>'
    )
    ctx = _Ctx()
    assert (
        _find_securecafe_base(html, ctx)
        == "https://x.securecafe.com/onlineleasing/first"
    )


def test_find_securecafe_base_shim_none_when_no_match() -> None:
    ctx = _Ctx()
    assert _find_securecafe_base("plain html", ctx) is None


# ─── boundary on adapter integration (no live network) ────────────────


def test_multi_base_cap_at_three() -> None:
    """Ensure ≥3 bases all show up in the helper output — adapter
    enforces the cap at the call site. The helper itself returns all
    (let the caller decide)."""
    html = "".join(
        f'<a href="https://x.securecafe.com/onlineleasing/site-{i}/x.aspx">x</a>'
        for i in range(5)
    )
    ctx = _Ctx()
    bases = _find_all_securecafe_bases(html, ctx)
    assert len(bases) == 5
    assert bases[0].endswith("site-0")
    assert bases[4].endswith("site-4")
