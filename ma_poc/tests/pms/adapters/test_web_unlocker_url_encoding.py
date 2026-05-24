"""URL encoding for BrightData Web Unlocker (2026-05-24).

Pins the fix for the 78 ``web_unlocker.error: HTTP Error 400`` events
in canary jugnu-unlocker-test-3886351-fl9gv. Every 400 was on an
Entrata ProspectPortal ``view_unit_spaces`` URL with unencoded
square brackets (``property[id]=1125701``); BD's gateway rejects
these as malformed and bills nothing, but each failed call burned
``_probe_prospectportal`` retry budget and produced no extraction.
"""
from __future__ import annotations

from ma_poc.pms.adapters._probe import _wu_safe_url


def test_brackets_in_query_get_percent_encoded() -> None:
    """The actual canary-burning URL pattern: Entrata ProspectPortal
    ``view_unit_spaces`` with bracketed property/floorplan ids."""
    raw = (
        "https://rwoodapts.prospectportal.com/?module=check_availability"
        "&is_secure=1&property[id]=1125701&action=view_unit_spaces"
        "&property_floorplan[id]=1125372&move_in_date=2026-05-24"
        "&occupancy_type=conventional"
    )
    out = _wu_safe_url(raw)
    assert "%5B" in out, "[ must be percent-encoded to %5B"
    assert "%5D" in out, "] must be percent-encoded to %5D"
    assert "[" not in out
    assert "]" not in out
    # Standard query separators must stay literal
    assert "?" in out and "&" in out and "=" in out
    # The values themselves are untouched
    assert "property%5Bid%5D=1125701" in out
    assert "property_floorplan%5Bid%5D=1125372" in out


def test_already_encoded_url_is_idempotent() -> None:
    """A URL that's already correctly encoded should pass through
    unchanged — quote() with safe='%' preserves existing escapes."""
    raw = "https://x.com/?a=%5Bb%5D&c=%5Bd%5D"
    out = _wu_safe_url(raw)
    assert out == raw, f"idempotency failed: {out!r} != {raw!r}"


def test_path_slashes_preserved() -> None:
    raw = "https://x.com/foo/bar/baz?q=1"
    out = _wu_safe_url(raw)
    assert out == raw


def test_unicode_in_path_gets_encoded() -> None:
    """Non-ASCII chars must be encoded — BD Web Unlocker only accepts
    ASCII URLs."""
    raw = "https://x.com/café/?q=1"
    out = _wu_safe_url(raw)
    assert "%C3%A9" in out  # é → utf-8 → percent
    assert "café" not in out


def test_empty_url_returns_empty() -> None:
    assert _wu_safe_url("") == ""


def test_scheme_and_host_unchanged() -> None:
    raw = "https://sub.example.com:8443/path?x=1"
    out = _wu_safe_url(raw)
    assert out.startswith("https://sub.example.com:8443/")


def test_real_canary_400_urls_become_safe() -> None:
    """Verbatim sample of the 5 distinct hosts whose 400 errors we saw
    in the canary. After encoding, no host should still carry raw
    brackets."""
    canary_400_urls = [
        "https://rwoodapts.prospectportal.com/?module=check_availability&is_secure=1&property[id]=1125701&action=view_unit_spaces&property_floorplan[id]=1125372&move_in_date=2026-05-24&occupancy_type=conventional",
        "https://riverrockapt.prospectportal.com/?module=check_availability&is_secure=1&property[id]=100082318&action=view_unit_spaces&property_floorplan[id]=1150468&move_in_date=2026-05-24&occupancy_type=conventional",
        "https://moderawoodbridge.prospectportal.com/?module=check_availability&is_secure=1&property[id]=265113&action=view_unit_spaces&property_floorplan[id]=1015554&move_in_date=2026-05-24&occupancy_type=conventional",
        "https://emeraldpointeapartments.prospectportal.com/?module=check_availability&is_secure=1&property[id]=100125319&action=view_unit_spaces&property_floorplan[id]=1167385&move_in_date=2026-05-24&occupancy_type=conventional",
        "https://industrytallahassee.prospectportal.com/?module=check_availability&is_secure=1&property[id]=100012110&action=view_unit_spaces&property_floorplan[id]=724331&move_in_date=2026-05-24&occupancy_type=conventional",
    ]
    for raw in canary_400_urls:
        safe = _wu_safe_url(raw)
        assert "[" not in safe, f"bracket leaked through: {safe}"
        assert "]" not in safe, f"bracket leaked through: {safe}"
        # All the meaningful pieces survive
        assert "module=check_availability" in safe
        assert "action=view_unit_spaces" in safe


def test_web_unlocker_get_passes_encoded_url_to_brightdata(monkeypatch) -> None:
    """End-to-end: web_unlocker_get must POST the encoded URL — not the
    raw bracketed one — to BD's API endpoint. Verifies the wiring."""
    import json as _json

    from ma_poc.pms.adapters import _probe

    monkeypatch.setattr(_probe, "web_unlocker_key", lambda: "test-key")

    captured_body: list[bytes] = []

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"<html>ok</html>"

    def fake_urlopen(req, timeout=120):
        captured_body.append(req.data)
        return _FakeResp()

    monkeypatch.setattr(
        _probe.urllib.request, "urlopen", fake_urlopen
    )

    bracketed = "https://x.prospectportal.com/?property[id]=42&action=foo"
    resp = _probe.web_unlocker_get(bracketed, timeout=10)

    assert resp.status_code == 200
    assert captured_body, "BD API was not called"
    sent = _json.loads(captured_body[0])
    assert "[" not in sent["url"], (
        f"raw [ leaked through to BD URL: {sent['url']}"
    )
    assert "%5B" in sent["url"]
    assert "%5D" in sent["url"]
    assert sent["format"] == "raw"
