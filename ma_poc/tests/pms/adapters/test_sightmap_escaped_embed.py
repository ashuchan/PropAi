"""SightMap escaped-embed routing + extraction (prod 2026-07-12).

The IMT WordPress theme ships the embed URL JSON-escaped in a config blob:
``"sightmap_embed_url":"https:\\/\\/sightmap.com\\/embed\\/{code}"``. The
literal-slash detector fingerprint and embed regex missed it, so IMT sites
routed to imt_spaces (plan-level) instead of SightMap (unit-level). Fixed by
slash-normalization + a JSON-key-aware skip.

CRITICAL GUARD (proven required by the catalog): the Entrata cookie-consent
text (``"token":"engrain_sightmap"``) and empty Engrain configs
(``engrainedUrl: ''``) mention SightMap but carry NO embed code — they must
NOT route to the (dead) SightMap adapter, or 11 working Entrata/RealPage/
OneSite props regress.
"""

from __future__ import annotations

from ma_poc.pms.adapters.sightmap import (
    _normalize_sightmap_slashes,
    find_sightmap_embed_codes,
)
from ma_poc.pms.detector import _detect_sightmap_embed

# Real IMT shape (escaped slashes, JSON-value position).
_IMT = (
    '<script>window.__cfg = {"sightmap_embed_url":'
    '"https:\\/\\/sightmap.com\\/embed\\/4d7p16exvkx","x":1};</script>'
)
_IMT_U002F = (
    '{"sightmap_link":"https:\\u002f\\u002fsightmap.com\\u002fembed\\u002fdgow3rn8w2m"}'
)
_LITERAL = '<iframe src="https://sightmap.com/embed/abc123xyz"></iframe>'

# False-positive forms that MUST NOT route to SightMap.
_COOKIE = '{"token":"engrain_sightmap","desc":"This cookie enables Engrain\'s SightMap"}'
_EMPTY_CFG = "var t = {engrainSitemapId: '', engrainedText: 'Interactive Map', engrainedUrl: ''};"


# ── extraction (adapter) ────────────────────────────────────────────────────

def test_extracts_escaped_embed_code() -> None:
    assert find_sightmap_embed_codes(_IMT) == ["4d7p16exvkx"]


def test_extracts_u002f_escaped_code() -> None:
    assert find_sightmap_embed_codes(_IMT_U002F) == ["dgow3rn8w2m"]


def test_literal_embed_still_extracted() -> None:
    assert find_sightmap_embed_codes(_LITERAL) == ["abc123xyz"]


def test_cookie_and_empty_config_yield_no_codes() -> None:
    assert find_sightmap_embed_codes(_COOKIE) == []
    assert find_sightmap_embed_codes(_EMPTY_CFG) == []


def test_normalize_helper() -> None:
    assert _normalize_sightmap_slashes("a\\/b\\u002fc") == "a/b/c"
    assert _normalize_sightmap_slashes("") == ""


# ── routing guard (detector) ────────────────────────────────────────────────

def test_detector_routes_real_escaped_embed() -> None:
    assert _detect_sightmap_embed(_IMT) is True
    assert _detect_sightmap_embed(_IMT_U002F) is True
    assert _detect_sightmap_embed(_LITERAL) is True


def test_detector_guard_rejects_false_positives() -> None:
    # cookie-consent text + empty config carry 'sightmap' but no code
    assert _detect_sightmap_embed(_COOKIE) is False
    assert _detect_sightmap_embed(_EMPTY_CFG) is False
    assert _detect_sightmap_embed("no sightmap here at all") is False
