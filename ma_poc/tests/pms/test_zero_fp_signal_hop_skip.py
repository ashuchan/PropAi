"""Source-level guard for the zero-floor-plan-signal hop-page skip.

When a link-hop body has no inventory signals AND no portal-discovery
markers, the extraction cascade is skipped — saves ~5-10s/property on
guaranteed-empty pages. The guard MUST be conservative: it must keep
the cascade alive whenever an alternate recovery path could surface
data, namely:

  - any live Playwright session (XHR capture might still fire);
  - any ``<iframe>`` in the body (cross-origin widget might host data);
  - any ``<script type="application/json">`` block (SSR portal config
    blob carrying a SightMap / RentCafe / RealPage embed URL — the
    Skyline-at-Kessler canonical case).

End-to-end behaviour is exercised by canary runs; these tests pin the
guard at source level so a future refactor can't silently regress it.
"""

from __future__ import annotations

import inspect

from ma_poc.pms import scraper as _scraper_mod


_SRC = inspect.getsource(_scraper_mod)


def test_guard_only_fires_in_http_only_hops() -> None:
    """The guard checks ``browser_page is None`` — a live Playwright
    session may still capture XHRs that the static body doesn't reflect."""
    assert "browser_page is None" in _SRC


def test_guard_consults_floor_plan_signal_engine() -> None:
    """Skip condition #1: the body must have zero structural floor-plan
    signals (uses ``has_floor_plan_signals`` from the signal engine)."""
    assert "has_floor_plan_signals" in _SRC
    assert "SIGNAL_THRESHOLD_ANY" in _SRC


def test_guard_keeps_cascade_alive_when_iframes_present() -> None:
    """Skip condition #2: the body must contain NO ``<iframe>`` tags.
    Cross-origin widgets (Wix Visual Data, Cross Street, FortressTech)
    embed real inventory inside iframes; the static body has no rent
    text but the iframe URL is the recovery path."""
    assert "<iframe" in _SRC, (
        "zero-signal hop guard regression: removed the iframe check, "
        "would break Wix / Cross Street / FortressTech portal discovery."
    )


def test_guard_keeps_cascade_alive_when_json_script_blocks_present() -> None:
    """Skip condition #3: the body must contain NO
    ``<script type="application/json">`` blocks. SSR config blobs
    (SightMap embed config, RentCafe init data, Squarespace product
    JSON-LD) live there and are the recovery path for the canonical
    Skyline-at-Kessler dynamic-discovery case."""
    assert 'application/json' in _SRC


def test_guard_continues_loop_on_skip() -> None:
    """When the gate fires it must mark ``explored[sub_url] = False``
    (so the URL is recorded as visited-but-empty for next-run learning)
    AND ``continue`` (skip the recursive scrape)."""
    idx = _SRC.find("zero-signal hop guard")
    assert idx > -1, "guard comment missing — source-grep test won't anchor"
    block = _SRC[idx : idx + 1500]
    assert "explored[sub_url] = False" in block
    assert "continue" in block
