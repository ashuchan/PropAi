"""Cluster #4 soft-404 recovery — pin the scraper hook + marker list.

2026-05-20 cluster #4 sub-pattern (the shea / olympus / liveatcrossroadsranch
flavor): some marketing sites return HTTP 404 for valid property URLs
while serving full apartment content + nav links. The fetcher classifies
these as DEAD_URL and ``scrape_jugnu`` used to short-circuit to
``generic:no_body_short_circuit`` — dropping the property entirely.

Main extracts via TIER_1_API_SIGHTMAP after link-hopping from the
soft-404 page to /apartments/.../floor-plans (where the SightMap embed
lives). The fix: when outcome=DEAD_URL but body has >=10 KB of content
AND >=1 known inventory nav marker, skip the short-circuit and let the
rest of the pipeline (link-hop, generic adapter, F2 LLM rescue) try.

The full scrape_jugnu path is too involved to integration-test in
isolation; instead we pin:
  1. The decision logic via a re-implemented helper (mirror of the
     production hook, kept in sync via source-grep below).
  2. The scraper hook still references the expected symbols.
"""

from __future__ import annotations

import re
from pathlib import Path

# Mirror of the production marker list. Kept in sync via the source-grep
# contract test below.
_SOFT_404_MARKERS: tuple[str, ...] = (
    "/floor-plans",
    "/floorplans",
    "/availability",
    "/available-units",
    "/availableunits",
    "/apartments/",
    "sightmap.com/embed/",
    "rentcafe.com",
    "knockdoorway",
)


def _decide_soft_404_recovery(outcome: str, body: bytes | str, size: int) -> bool:
    """Mirror of the scraper decision: DEAD_URL + >=10 KB body + >=1
    marker → recover. Otherwise short-circuit."""
    if outcome != "DEAD_URL":
        return False
    if size < 10_000:
        return False
    text = (
        body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    ).lower()
    return any(m in text for m in _SOFT_404_MARKERS)


# ─────────────────────────────────────────────────────────────────────
# Decision logic
# ─────────────────────────────────────────────────────────────────────


def test_soft_404_recovers_on_floor_plans_nav_link() -> None:
    """liveatcrossroadsranch pattern — 404 status + 59 KB body + nav
    links to /apartments/.../floor-plans → recovery."""
    body = (
        '<html><body><nav>'
        '<a href="/apartments/tx/houston/floor-plans">Floor Plans</a>'
        '<a href="/apartments/tx/houston/amenities">Amenities</a>'
        '</nav></body></html>'
        + ("x" * 60_000)  # bulk to exceed the 10 KB threshold
    )
    assert _decide_soft_404_recovery("DEAD_URL", body.encode(), len(body)) is True


def test_soft_404_recovers_on_sightmap_embed_url() -> None:
    """Body has a sightmap.com/embed/ reference — recover."""
    body = (
        '<html><body>'
        '<iframe src="https://sightmap.com/embed/abc123"></iframe>'
        '</body></html>'
        + ("x" * 12_000)
    )
    assert _decide_soft_404_recovery("DEAD_URL", body.encode(), len(body)) is True


def test_soft_404_recovers_on_rentcafe_marker() -> None:
    body = '<html><body>resource.rentcafe.com</body></html>' + ("x" * 12_000)
    assert _decide_soft_404_recovery("DEAD_URL", body.encode(), len(body)) is True


def test_soft_404_does_not_recover_on_genuine_404() -> None:
    """Tiny ``404 Not Found`` body — short-circuit, don't waste extraction."""
    body = b"<html><body><h1>404 Not Found</h1></body></html>"
    assert _decide_soft_404_recovery("DEAD_URL", body, len(body)) is False


def test_soft_404_does_not_recover_when_no_markers_present() -> None:
    """Body is large but has no inventory markers — likely a soft-404
    landing page for an unrelated content type. Don't waste extraction."""
    body = "<html><body>" + ("lorem ipsum dolor sit amet " * 2000) + "</body></html>"
    assert _decide_soft_404_recovery("DEAD_URL", body.encode(), len(body)) is False


def test_soft_404_does_not_recover_on_non_dead_url_outcome() -> None:
    """The recovery is scoped to DEAD_URL. Other not-OK outcomes
    (BOT_BLOCKED, HARD_FAIL, etc.) keep their existing handling."""
    body = '<html><body><a href="/floor-plans">x</a></body></html>' + ("x" * 12_000)
    for outcome in ("BOT_BLOCKED", "HARD_FAIL", "TRANSIENT", "PROXY_ERROR"):
        assert (
            _decide_soft_404_recovery(outcome, body.encode(), len(body)) is False
        ), f"recovery must be scoped to DEAD_URL; fired for {outcome!r}"


def test_soft_404_recovers_on_real_liveatcrossroadsranch_shape() -> None:
    """Synthesized from the actual 2026-05-20 live-probe bytes —
    /home returns 404 + 59 KB with floor-plans + amenities + apply
    nav links to /apartments/tx/houston/*."""
    body = (
        '<!doctype html ><html lang="en"><head></head><body>'
        '<nav>'
        '<a href="/apartments/tx/houston/floor-plans">Floor Plans</a>'
        '<a href="/apartments/tx/houston/amenities">Amenities</a>'
        '<a href="/apartments/tx/houston/apply">Apply</a>'
        '<a href="/apartments/tx/houston/contact-us">Contact</a>'
        '<a href="/apartments/tx/houston/gallery">Gallery</a>'
        '</nav>'
        # Real page content padding to reach ~59 KB
        + ("<div class='content'>" + ("x " * 4_000) + "</div>") * 4
        + '</body></html>'
    )
    assert _decide_soft_404_recovery("DEAD_URL", body.encode(), len(body)) is True


# ─────────────────────────────────────────────────────────────────────
# Contract — the production scraper hook still references the expected
# symbols. If a future refactor renames or drops these, this test fails
# and forces re-syncing with the helper above.
# ─────────────────────────────────────────────────────────────────────


_SCRAPER_PATH = (
    Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
)


def test_scraper_soft_404_hook_kept_in_sync() -> None:
    """Greps scraper.py for the soft-404 recovery markers. Catches drift
    between the inline production decision and this test helper."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    for symbol in (
        "_soft_404_recovery",
        "_SOFT_404_MARKERS",
        "_soft_404_status",
        '"DEAD_URL"',
    ):
        assert symbol in src, (
            f"scraper.py no longer references {symbol!r} — soft-404 "
            f"recovery hook is out of sync with this test helper. "
            f"Update one or the other."
        )


def test_scraper_marker_list_matches_test_helper() -> None:
    """Greps the actual marker list in scraper.py and asserts it
    contains every entry in this test's _SOFT_404_MARKERS. If they
    drift apart, this catches it."""
    src = _SCRAPER_PATH.read_text(encoding="utf-8")
    # Find the _SOFT_404_MARKERS tuple definition
    match = re.search(
        r"_SOFT_404_MARKERS\s*=\s*\((.*?)\)",
        src,
        re.DOTALL,
    )
    assert match is not None, (
        "Could not find _SOFT_404_MARKERS tuple definition in scraper.py"
    )
    marker_block = match.group(1)
    for m in _SOFT_404_MARKERS:
        assert m in marker_block, (
            f"marker {m!r} present in test helper but missing from "
            f"production _SOFT_404_MARKERS — out of sync"
        )
