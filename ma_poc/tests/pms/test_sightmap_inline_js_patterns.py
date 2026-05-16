"""Tests for SightMap inline-JS init pattern recognition.

Added 2026-05-16 as Bug #4 follow-up. The original `sightmap_id` /
`data-sightmap-id` patterns missed three real-world SDK invocation
shapes observed during the Playwright forensic on 16139 chaseknollsapts:

1. `embed_id` / `embedId` (engrain.com SDK newer versions)
2. `mapId` / `map_id` (some white-label embeds)
3. `<sightmap-app embed="...">` / `<sightmap-embed id="...">`
   custom elements (SDK v3+)

These tests guard each new alternative against accidental removal.
"""
from __future__ import annotations

from ma_poc.pms.scraper import _scan_inline_js_pms_init

# _scan_inline_js_pms_init returns [] for inputs < 100 chars (cheap-out
# guard). Pad short fixtures with neutral marketing text so the function
# actually runs the regex passes.
_PAD = (
    "<!doctype html><html><head><title>Test</title></head><body>"
    "<header>Marketing copy goes here, plus some boilerplate text "
    "for the cheap-out length guard at the top of the function.</header>"
)
_END = "</body></html>"


def test_canonical_sightmap_id_still_matches() -> None:
    """The original `sightmap_id: "..."` pattern must continue to work
    after the 2026-05-16 additions."""
    html = _PAD + '<script>SightMap.init({sightmap_id: "n9w6mmzrv71"});</script>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert hits, "canonical sightmap_id pattern stopped matching"
    assert any(
        "n9w6mmzrv71" in url and pms == "sightmap"
        for url, pms in hits
    )


def test_data_sightmap_id_still_matches() -> None:
    """Data-attribute placeholder must still resolve to the canonical URL."""
    html = _PAD + '<div data-sightmap-id="abc123xyz">map</div>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert any("abc123xyz" in url for url, _ in hits)


def test_embed_id_alternative_pattern_matches() -> None:
    """New 2026-05-16 pattern: `embed_id: "..."`."""
    html = _PAD + '<script>window.SM = {embed_id: "longidentifier"};</script>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert hits, "embed_id alternative pattern not matched"
    assert any("longidentifier" in url for url, pms in hits if pms == "sightmap")


def test_embedId_camelcase_matches() -> None:
    """camelCase `embedId` variant must also match."""
    html = _PAD + '<script>SightMap.init({embedId: "qwer1234"});</script>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert any("qwer1234" in url for url, pms in hits if pms == "sightmap")


def test_map_id_alternative_pattern_matches() -> None:
    """New 2026-05-16 pattern: `map_id: "..."` / `mapId: "..."`."""
    html = _PAD + '<script>SightMap.create({mapId: "asdf5678"});</script>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert any("asdf5678" in url for url, pms in hits if pms == "sightmap")


def test_sightmap_custom_element_matches() -> None:
    """SDK v3 uses custom elements like `<sightmap-app embed="...">`."""
    html = _PAD + '<sightmap-app embed="zxcvbnm123">stub</sightmap-app>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert any("zxcvbnm123" in url for url, pms in hits if pms == "sightmap")


def test_sightmap_embed_element_with_id_matches() -> None:
    """SDK v3 alternative: `<sightmap-embed id="...">`."""
    html = _PAD + '<sightmap-embed id="my9charkey">stub</sightmap-embed>' + _END
    hits = _scan_inline_js_pms_init(html)
    assert any("my9charkey" in url for url, pms in hits if pms == "sightmap")


def test_min_length_guard_drops_short_ids() -> None:
    """Patterns require 6+ characters to avoid false positives on `abc`
    or other CSS-class-like values that happen to match `embed_id`."""
    html = _PAD + '<script>SightMap.init({sightmap_id: "abc"});</script>' + _END
    hits = _scan_inline_js_pms_init(html)
    # `abc` is 3 chars — below the 6-char minimum.
    assert not any("abc" in url for url, pms in hits if pms == "sightmap")


def test_empty_html_returns_empty() -> None:
    assert _scan_inline_js_pms_init("") == []
    assert _scan_inline_js_pms_init("<html></html>") == []


def test_no_sightmap_signal_returns_empty() -> None:
    html = "<html><body><h1>Hello world</h1></body></html>"
    hits = _scan_inline_js_pms_init(html)
    # No SightMap-related output for non-SightMap HTML.
    assert not any(pms == "sightmap" for _, pms in hits)
