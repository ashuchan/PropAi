"""SightMap direct API probe tests (2026-05-24).

Pins the HAR-driven fix that lifts TIER_1_API_SIGHTMAP_SHAPE_REJECTED
(7 props in focused-3886351 canary; HAR-validated 100% lift potential).

Pre-fix: SightMapAdapter relied entirely on XHRs captured during the
canary's page-load window. When the SightMap iframe lazy-loaded or
the operator hid the map behind a user-interaction event, the
adapter saw 0 SightMap responses and bucketed as SHAPE_REJECTED or
NO_RESPONSE.

Post-fix: when the captured-response path fails, parse the embed code
from the marketing page body (regex against ``sightmap.com/embed/
{TOKEN}``), fetch the embed redirect to learn the {TOKEN}/{ID}
pairing, then fetch
``sightmap.com/app/api/v1/{TOKEN}/sightmaps/{NUMERIC_ID}`` directly
via curl_cffi and join via the existing ``parse_sightmap_response``.

Tier label: ``TIER_1_API_SIGHTMAP_DIRECT`` (parallel to the existing
captured-XHR tier).
"""
from __future__ import annotations

from ma_poc.pms.adapters.sightmap import _extract_sightmap_embed_codes


def test_extract_embed_codes_from_iframe_src() -> None:
    body = (
        '<iframe src="https://sightmap.com/embed/8xvrmoo6pjk?enable_api=1" '
        'width="100%" height="600"></iframe>'
    )
    codes = _extract_sightmap_embed_codes(body)
    assert codes == ["8xvrmoo6pjk"]


def test_extract_embed_codes_from_protocol_relative_src() -> None:
    """Some pages use ``//sightmap.com/embed/...`` (no protocol)."""
    body = '<iframe src="//sightmap.com/embed/abc123def"></iframe>'
    codes = _extract_sightmap_embed_codes(body)
    assert codes == ["abc123def"]


def test_extract_embed_codes_from_app_embed_path() -> None:
    """Newer embed URL pattern: /app/embed/{code}."""
    body = '<iframe src="https://sightmap.com/app/embed/xyz789"></iframe>'
    codes = _extract_sightmap_embed_codes(body)
    assert codes == ["xyz789"]


def test_extract_embed_codes_deduplicates() -> None:
    body = (
        '<iframe src="https://sightmap.com/embed/code1"></iframe>'
        '<script>var c = "https://sightmap.com/embed/code1";</script>'
    )
    codes = _extract_sightmap_embed_codes(body)
    assert codes == ["code1"]


def test_extract_embed_codes_skips_invalid_data_attribute_format() -> None:
    """We don't match raw HTML data-attribute syntax (``data-sightmap-id=
    "..."``) because the canonical embed pattern (``sightmap.com/embed/
    {CODE}``) is universal across observed properties. Defensive: ensure
    we don't accidentally match arbitrary data-* attributes."""
    body = '<div data-sightmap-embed="qwerty1234">Loading…</div>'
    # Not matched by current implementation — and that's fine, the
    # iframe-src pattern catches all observed in-the-wild cases.
    codes = _extract_sightmap_embed_codes(body)
    # Should NOT match an arbitrary data-* attribute (could be noise)
    assert "qwerty1234" not in codes


def test_extract_embed_codes_returns_empty_for_no_marker() -> None:
    body = '<html><body>No sightmap here.</body></html>'
    assert _extract_sightmap_embed_codes(body) == []


def test_extract_embed_codes_handles_empty_body() -> None:
    assert _extract_sightmap_embed_codes("") == []
    assert _extract_sightmap_embed_codes(None) == []  # type: ignore[arg-type]


def test_extract_embed_codes_multiple_distinct_codes() -> None:
    """Some operators have two SightMap maps on the same property
    (e.g. main map + availability map) — keep both in document order."""
    body = (
        '<iframe src="https://sightmap.com/embed/main111"></iframe>'
        '<iframe src="https://sightmap.com/embed/avail222"></iframe>'
    )
    codes = _extract_sightmap_embed_codes(body)
    assert codes == ["main111", "avail222"]


def test_real_canary_embed_codes_recognized() -> None:
    """Verbatim embed codes from the 5 HAR-matched
    SIGHTMAP_SHAPE_REJECTED properties (2026-05-24)."""
    canary_codes = [
        "8xvrmoo6pjk",   # livahwatukee
        "40vl5go2wle",   # traditionapthomes
        "yjp248rxvxl",   # residencesatfalconnorth
        "gow3zg5zp2m",   # creekwoodapartmenthomes
        "rxwjqnozv1e",   # livegreenview
    ]
    for code in canary_codes:
        body = f'<iframe src="https://sightmap.com/embed/{code}"></iframe>'
        codes = _extract_sightmap_embed_codes(body)
        assert codes == [code], f"failed for {code}: got {codes}"
