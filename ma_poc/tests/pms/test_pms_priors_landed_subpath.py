"""Tests for Bug #2 (2026-05-18) — PMS-prior synthesis must use the landed
path prefix when the entry redirected to a deep path on the landed host.

Pre-fix behaviour: the synthesiser emitted BOTH a host-root variant
(``https://krcapartments.com/floor-plans``) and a path-prefix variant
(``https://krcapartments.com/property-detail/wedgewood-place/floor-plans``).
The host-root variant 404'd on management-company hosts and burned hop
budget. The path-prefix variant had only a +10 score boost — frequently
ran second.

Stronger fix: when the entry has a non-trivial path AND the keyword is
absolute, emit ONLY the path-prefix variant. The host-root variant is
suppressed because empirical evidence (13+ confirmed PIDs on 2026-05-18)
shows it's a 404 in every case on multi-property hosts.
"""

from __future__ import annotations

from ma_poc.pms.scraper import _pms_priors_for


# ── Multi-property host (deep landed path) ───────────────────────────────────


def test_deep_landed_path_emits_only_subpath_variant() -> None:
    """The krcapartments / venterraliving / scullycompany pattern."""
    urls = _pms_priors_for(
        None,  # detected — falls through to universal priors
        "https://krcapartments.com/property-detail/wedgewood-place/",
    )
    out_urls = {u for u, _, _ in urls}
    # Path-prefix variants present:
    assert "https://krcapartments.com/property-detail/wedgewood-place/floor-plans" in out_urls
    assert "https://krcapartments.com/property-detail/wedgewood-place/availability" in out_urls
    # Host-root variants suppressed:
    assert "https://krcapartments.com/floor-plans" not in out_urls
    assert "https://krcapartments.com/availability" not in out_urls


def test_subpath_variant_carries_correct_anchor_tag() -> None:
    """Anchor must include the ``:prop_subpath`` suffix for grep-ability."""
    urls = _pms_priors_for(
        None, "https://venterraliving.com/apartments/vinings/",
    )
    anchors = [a for _, _, a in urls]
    assert any(":prop_subpath" in a for a in anchors)


def test_deep_landed_path_with_google_sites_layout() -> None:
    """Google Sites hosts EVERYTHING at /view/<slug>/ — the host root must
    NOT be queried (it returns the sites.google.com landing page)."""
    urls = _pms_priors_for(
        None, "https://sites.google.com/view/bellevueheights/home",
    )
    out_urls = {u for u, _, _ in urls}
    assert "https://sites.google.com/view/bellevueheights/home/floor-plans" in out_urls
    assert "https://sites.google.com/floor-plans" not in out_urls


# ── Plain host root (CSV URL, no redirect) ───────────────────────────────────


def test_host_root_entry_still_emits_host_root_priors() -> None:
    """The pre-fix common case: CSV URL is ``https://example.com/`` with no
    redirect. The synthesiser should still emit host-root priors — no
    behaviour change for this case."""
    urls = _pms_priors_for(None, "https://example.com/")
    out_urls = {u for u, _, _ in urls}
    assert "https://example.com/floor-plans" in out_urls
    assert "https://example.com/availability" in out_urls


def test_host_root_with_empty_path_unchanged() -> None:
    urls = _pms_priors_for(None, "https://example.com")
    out_urls = {u for u, _, _ in urls}
    assert any(u.startswith("https://example.com/") for u in out_urls)


def test_subpath_with_only_trailing_slash_treated_as_root() -> None:
    """``https://example.com/`` after rstrip("/") is empty — same as no path."""
    urls = _pms_priors_for(None, "https://example.com/")
    out_urls = {u for u, _, _ in urls}
    # Should emit host-root variants (no subpath to use).
    assert "https://example.com/floor-plans" in out_urls


# ── Anchor labeling preserved ────────────────────────────────────────────────


def test_unknown_pms_uses_universal_anchor() -> None:
    """When PMS isn't detected, fall through to universal priors with the
    ``pms_prior:universal`` anchor label."""
    urls = _pms_priors_for(None, "https://example.com/")
    anchors = [a for _, _, a in urls]
    assert any("pms_prior:universal" in a for a in anchors)
